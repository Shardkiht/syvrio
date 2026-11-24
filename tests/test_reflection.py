from __future__ import annotations

import json
import time
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from syvrio.application.reflection import ReflectionUseCases
from syvrio.background_memory import BackgroundMemory, dream, model
from syvrio.background_memory.dream import Dream
from syvrio.infra.config import QIANWEN_BASE_URL, LLMConfig
from syvrio.infra.jobs import encode_job, read_job
from syvrio.infra.tokens import estimate_tokens
from syvrio.memory.models import MemoryContext, MemoryDraft, MemoryUpdate, ReflectionResponse

pytestmark = pytest.mark.memory


def add(service, text, **context):
    return service.add(MemoryDraft(topic="gardening", text=text, context=context or None))


def test_full_stage_fits_one_request_above_old_row_and_chat_limits(service, monkeypatch):
    service.config.chat.context_window = 50000
    records = [add(service, f"Fact {n}" + " sky" * 2700) for n in range(20)]
    monkeypatch.setattr(model, "model_metadata", lambda _: {})
    monkeypatch.setattr(dream, "effective_context_window", lambda _, configured: configured)
    monkeypatch.setattr(model, "effective_context_window", lambda _, configured: configured)
    sends = []

    def complete(_config, messages, **kwargs):
        prompt, content = [message["content"] for message in messages]
        assert estimate_tokens(prompt + content) > service.config.chat.context_window
        data = json.loads(content)
        rows = data["evidence"] if prompt == dream.PROFILE else data["facts"]
        assert {row["id"] for row in rows} == {record.id for record in records}
        sends.append(prompt)
        outcome = '{"summary":"Complete"}' if prompt == dream.PROFILE else '{"proposals":[]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])

    monkeypatch.setattr(model, "complete", complete)
    engine = Dream(service)
    engine.request()
    engine.step(Event())
    engine.step(Event())
    assert sends == [dream.PROFILE, dream.INFER]
    assert engine.status()["state"] == "completed"


def test_batch_cursor_splits_an_oversized_record_without_losing_text():
    evidence = [{"id": "long", "text": "春 sky 夜⚓" * 210}, {"id": "end", "text": "end"}]
    cursor = [0, 0]
    result = []
    while cursor[0] < len(evidence):
        draft = "已读" * len(result)

        def payload(rows, draft=draft):
            return {"draft": draft, "evidence": rows}

        batch, after = dream._next_batch(evidence, cursor, dream.PROFILE, payload, 580)
        assert estimate_tokens(dream.PROFILE + model.encode_payload(payload(batch))) <= 580
        assert after > cursor
        result.extend(batch)
        cursor = after
    assert len(result) > len(evidence)
    assert "".join(row["text"] for row in result) == "".join(row["text"] for row in evidence)


def test_profile_is_a_complete_atomic_first_stage_then_new_hypotheses(service, monkeypatch):
    first = add(service, "我喜欢番茄")
    second = add(service, "我种植辣椒")
    old = service.propose_inference("可能擅长栽培", [second.id], "技能")
    service.review_inference(old.id, "confirm")
    pending = service.propose_inference("未证实的推断", [first.id], "偏好")
    deprecated = add(service, "弃用的事实")
    service.apply(updates=[MemoryUpdate(id=deprecated.id, status="deprecated")])
    service.profile.path.write_text("人工画像", encoding="utf-8")
    calls = []

    def generate(_, prompt, data, schema):
        calls.append(prompt)
        if prompt == dream.PROFILE:
            assert {r["id"] for r in data["evidence"]} == {first.id, second.id, old.id}
            assert data["draft"] == ""
            assert service.profile.read_text() == "人工画像"
            return schema(summary="# 画像\n用户喜欢种植。")
        assert service.profile.read_text() == "# 画像\n用户喜欢种植。\n"
        assert set(r["id"] for r in data["facts"]) == {first.id, second.id}
        return schema(
            proposals=[
                {"kind": "兴趣", "text": "可能喜欢园艺", "source_ids": [first.id, second.id]}
            ]
        )

    monkeypatch.setattr(dream, "ask", generate)
    engine = Dream(service)
    engine.request()
    engine.step(Event())
    assert {i.id for i in service.list_inferences()} == {old.id, pending.id}
    Dream(service).step(Event())
    assert calls == [dream.PROFILE, dream.INFER]
    items = {i.text: i for i in service.list_inferences()}
    assert (items["可能喜欢园艺"].kind, items["可能喜欢园艺"].status) == ("兴趣", "hypothesis")
    assert items[old.text].status == "confirmed"
    assert items[pending.text].status == "hypothesis"
    assert engine.status()["state"] == "completed"
    assert service.profile.profile_summary().fact_count == 2


def test_all_sources_contribute_across_profile_batches_before_publication(service, monkeypatch):
    service.config.dream.context_window = 3000
    facts = [add(service, f"合成事实 {n}" + " sample" * 140) for n in range(22)]
    seen = set()
    published = []

    def generate(_, prompt, data, schema):
        if prompt == dream.PROFILE:
            seen.update(r["id"] for r in data["evidence"])
            assert service.profile.read_text() == ""
            if data["draft"]:
                assert data["draft"].startswith("已读")
            return schema(summary=f"已读 {len(seen)}")
        published.append({r["id"] for r in data["facts"]})
        assert service.profile.read_text().strip() == "已读 22"
        return schema(proposals=[])

    monkeypatch.setattr(dream, "ask", generate)
    engine = Dream(service)
    service.config.chat.default_budget = 1
    job = engine.request()
    engine.step(Event())
    assert engine.status()["state"] == "paused"
    assert not service.profile.path.exists()
    restarted = Dream(service)
    # 在批次间更改聊天预算不会使 Dream 游标失效。
    service.config.chat.context_window *= 2
    service.config.chat.default_budget = 20
    assert restarted.request() == job
    while restarted.due():
        restarted.step(Event())
    assert seen == {f.id for f in facts} == set.union(*published)
    assert len(published) >= 2


@pytest.mark.parametrize("phase", ["profile", "hypotheses"])
def test_legacy_batch_number_replays_only_the_unfinished_stage(service, monkeypatch, phase):
    fact = add(service, "A saved fact")
    engine = Dream(service)
    engine.request()
    old = read_job(engine.path)
    old.update(
        phase=phase,
        batch=33,
        partial="Incomplete legacy draft",
        model=service.config.llm.litellm_model(),
        version=dream._state(service)[-1],
        window=50000,
    )
    engine.path.write_bytes(encode_job(old))
    service.profile.path.write_text("Published profile", encoding="utf-8")

    def generate(_, prompt, data, schema):
        assert service.profile.read_text() == "Published profile"
        if phase == "profile":
            assert prompt == dream.PROFILE and data["draft"] == ""
            assert [row["id"] for row in data["evidence"]] == [fact.id]
            return schema(summary="Complete profile")
        assert prompt == dream.INFER
        assert [row["id"] for row in data["facts"]] == [fact.id]
        return schema(proposals=[])

    monkeypatch.setattr(dream, "ask", generate)
    engine.step(Event())
    assert engine.status()["phase"] == "hypotheses"
    if phase == "profile":
        assert service.profile.read_text().strip() == "Complete profile"
    else:
        assert service.profile.read_text() == "Published profile"
        assert engine.status()["state"] == "completed"


def test_profile_failure_preserves_previous_profile_and_never_runs_hypotheses(service, monkeypatch):
    add(service, "合成事实")
    service.profile.path.write_text("先前的画像", encoding="utf-8")
    owner = BackgroundMemory(service)
    job = owner.request_dream()

    def fail(_, prompt, *args):
        assert prompt == dream.PROFILE
        raise TimeoutError("synthetic provider error")

    monkeypatch.setattr(dream, "ask", fail)
    for _ in range(3):
        with pytest.raises(TimeoutError) as err:
            owner.dream.step(Event())
        owner.dream.failed(err.value, job, owner.dream_status()["generation"])
    state = owner.dream_status()
    assert state["state"] == "failed" and state["error"] == "TimeoutError"
    assert "synthetic provider" not in str(state)
    assert service.profile.read_text() == "先前的画像"
    assert not service.list_inferences()


@pytest.mark.parametrize("provider_fails", [False, True])
def test_live_dream_status_only_marks_the_current_request_as_running(
    service, monkeypatch, provider_fails
):
    add(service, "Synthetic evidence")
    service.config.chat.auto_reflect = False
    service.config.chat.default_budget = 1
    owner = BackgroundMemory(service)
    commands = ReflectionUseCases(owner)
    entered, release = Event(), Event()

    def generate(_service, _prompt, _payload, schema):
        entered.set()
        assert release.wait(5)
        if provider_fails:
            raise TimeoutError()
        return schema(summary="Complete")

    monkeypatch.setattr(dream, "ask", generate)
    assert commands.command("run").data["state"] == "queued"
    owner.start()
    try:
        assert entered.wait(5)
        checkpoint = owner.dream.path.read_bytes()
        assert read_job(owner.dream.path)["state"] == "queued"
        assert Dream(service).status(live=True)["state"] == "queued"
        status = commands.command("status")
        assert status.data["state"] == "running" and "Phase: profile" in status.message
        repeated = commands.command("run")
        assert (
            repeated.data["state"] == "running" and repeated.message == "Dream is already running."
        )
        assert owner.dream.path.read_bytes() == checkpoint
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = commands.command("status")
            if status.data["failures"] or status.data["state"] == "paused":
                break
            time.sleep(0.01)
        assert status.data["state"] == ("queued" if provider_fails else "paused")
        assert status.data["error_type"] == ("TimeoutError" if provider_fails else None)
        assert owner.dream_status()["requests"] == 1
    finally:
        release.set()
        owner.stop()


def test_late_provider_response_after_worker_stop_or_evidence_change_is_discarded(
    service, monkeypatch
):
    fact = add(service, "测试事实")
    engine = Dream(service)
    engine.request()

    def change(*args):
        service.apply(updates=[MemoryUpdate(id=fact.id, context=MemoryContext(location="杭州"))])
        return args[-1](summary="过期画像")

    monkeypatch.setattr(dream, "ask", change)
    engine.step(Event())
    assert service.profile.read_text() == ""

    stop = Event()

    def shut_down(*args):
        stop.set()
        return args[-1](summary="Stopped before publication")

    monkeypatch.setattr(dream, "ask", shut_down)
    engine.step(stop)
    assert engine.status()["state"] == "queued" and not service.profile.path.exists()


def test_inference_failure_preserves_profile_and_resumes_same_batch(service, monkeypatch):
    source = add(service, "测试事实")
    engine = Dream(service)
    job = engine.request()
    monkeypatch.setattr(dream, "ask", lambda _, prompt, payload, schema: schema(summary="当期画像"))
    engine.step(Event())
    monkeypatch.setattr(
        dream,
        "ask",
        lambda *args: args[-1](
            proposals=[{"kind": "兴趣", "text": "异批提议", "source_ids": ["not-present"]}]
        ),
    )
    with pytest.raises(ValueError):
        engine.step(Event())
    assert service.profile.read_text().strip() == "当期画像"
    assert not service.list_inferences()
    assert engine.status()["phase"] == "hypotheses" and engine.status()["batch"] == 0

    def succeed(_, prompt, payload, schema):
        assert prompt == dream.INFER
        p = {"kind": "兴趣", "text": "提议", "source_ids": [source.id]}
        return schema(proposals=[p, p])

    monkeypatch.setattr(dream, "ask", succeed)
    assert Dream(service).request() == job
    Dream(service).step(Event())
    assert len(service.list_inferences()) == 1
    assert engine.status()["proposed"] == 1


def test_model_switch_after_failed_hypotheses_keeps_published_profile(service, monkeypatch):
    add(service, "Synthetic evidence for a profile")
    engine = Dream(service)
    job_id = engine.request()
    calls = []

    def generate(_, prompt, payload, schema):
        calls.append(prompt)
        if len(calls) == 1:
            return schema(summary="Published by the first model")
        if len(calls) == 2:
            raise TimeoutError()
        assert prompt == dream.INFER
        return schema(proposals=[])

    monkeypatch.setattr(dream, "ask", generate)
    engine.step(Event())
    metadata = engine.metadata.read_bytes()
    with pytest.raises(TimeoutError) as exc:
        engine.step(Event())
    engine.failed(exc.value, job_id, engine.status()["generation"])
    service.config.llm = replace(service.config.llm, model="replacement")
    assert engine.request(retry_now=True) == job_id
    engine.step(Event())
    assert calls == [dream.PROFILE, dream.INFER, dream.INFER]
    assert engine.metadata.read_bytes() == metadata
    assert engine.status()["state"] == "completed"


def test_model_switch_while_preparing_request_takes_effect_on_next_batch(service, monkeypatch):
    service.config.llm = LLMConfig(provider="local", model="original")
    for n in range(4):
        add(service, f"Fact {n}. " + " red" * 600)
    calls = []
    counted_ids = []

    def window(llm, _configured):
        return 2000 if llm.model == "original" else 6000

    def switch(_config):
        service.config.llm = replace(service.config.llm, model="replacement")
        return 500

    def complete(llm, messages, **_):
        prompt, content = (msg["content"] for msg in messages)
        data = json.loads(content)
        calls.append((llm.model, prompt))
        if prompt == dream.PROFILE:
            assert data["draft"] == ("" if len(calls) == 1 else "Profile in progress")
            counted_ids.extend(row["id"] for row in data["evidence"])
            result = '{"summary":"Profile in progress"}'
        else:
            result = '{"proposals":[]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=result))])

    monkeypatch.setattr(dream, "effective_context_window", window)
    monkeypatch.setattr(model, "effective_context_window", window)
    monkeypatch.setattr(dream, "dream_output_ceiling", switch)
    monkeypatch.setattr(model, "model_metadata", lambda _: {})
    monkeypatch.setattr(model, "complete", complete)
    engine = Dream(service)
    engine.request()
    engine.step(Event())
    assert 0 < read_job(engine.path)["cursor"][0] < 4
    engine.step(Event())
    engine.step(Event())
    assert calls == [
        ("original", dream.PROFILE),
        ("replacement", dream.PROFILE),
        ("replacement", dream.INFER),
    ]
    assert len(counted_ids) == len(set(counted_ids)) == 4
    assert engine.status()["state"] == "completed"


def test_evidence_version_rechecked_before_publishing_hypotheses(service, monkeypatch):
    fact = add(service, "旧事实")
    engine = Dream(service)
    engine.request()
    monkeypatch.setattr(dream, "ask", lambda *args: args[-1](summary="成功画像"))
    engine.step(Event())

    def mutate(_, prompt, payload, schema):
        service.apply(updates=[MemoryUpdate(id=fact.id, status="deprecated")])
        return schema(proposals=[{"kind": "偏好", "text": "失效的提议", "source_ids": [fact.id]}])

    monkeypatch.setattr(dream, "ask", mutate)
    engine.step(Event())
    assert not service.list_inferences()
    assert service.profile.read_text().strip() == "成功画像"


def test_profile_empty_vault_requires_no_llm_and_does_not_alter_inferences(service, monkeypatch):
    monkeypatch.setattr(dream, "ask", lambda *args: pytest.fail("empty profile needs no model"))
    service.profile.path.parent.mkdir(parents=True, exist_ok=True)
    service.profile.path.write_text("没有来源的旧画像", encoding="utf-8")
    engine = Dream(service)
    engine.request()
    engine.step(Event())
    assert engine.status()["state"] == "completed"
    assert service.profile.read_text() == ""
    assert service.profile.profile_summary() is None


def test_dream_model_disables_retries_has_bounded_output_and_preserves_emotion(
    service, monkeypatch
):
    service.config.llm = LLMConfig(provider="local", num_retries=7)
    emotion = {"event": "有点紧张", "recollection": "挺开心"}
    source = add(service, "合成展览", emotion=emotion)
    captured = {}

    def complete(config, messages, **kwargs):
        captured.update(
            retries=config.num_retries, facts=json.loads(messages[1]["content"])["facts"], **kwargs
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"proposals":[]}'))]
        )

    monkeypatch.setattr(model, "complete", complete)
    evidence = {"facts": [source.as_recall().model_dump(exclude_none=True)]}
    assert model.ask(service.config, dream.INFER, evidence, ReflectionResponse).proposals == []
    assert captured["retries"] == 0
    assert 0 < captured["max_tokens"] <= min(1800, service.config.chat.context_window)
    assert captured["facts"][0]["context"]["emotion"] == emotion


@pytest.mark.parametrize("output", [{}, {"proposals": [{"text": "x", "source_ids": ["1"]}]}])
def test_model_cannot_omit_proposals_or_kind(output):
    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate(output)


def test_truncated_dream_retries_same_batch_with_next_request_and_resets_between_phases(
    service, monkeypatch
):
    entry = add(service, "Synthetic profile evidence")
    service.profile.path.write_text("Old profile", encoding="utf-8")
    service.config.llm = LLMConfig(provider="local")
    monkeypatch.setattr(model, "model_metadata", lambda _: {"max_output_tokens": 7300})
    monkeypatch.setattr(model, "provider_reasoning_levels", lambda _: None)
    proposal = {"text": "Synthetic inference", "kind": "interest", "source_ids": [entry.id]}
    responses = iter(
        [
            ("length", '{"summary":"Incomplete"}'),
            ("stop", '{"summary":"Complete"}'),
            ("length", "{"),
            ("stop", json.dumps({"proposals": [proposal]})),
        ]
    )
    calls = []

    def complete(config, messages, **kwargs):
        calls.append((messages, kwargs["max_tokens"]))
        reason, content = next(responses)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=content))
            ]
        )

    monkeypatch.setattr(model, "complete", complete)
    engine = Dream(service)
    service.config.chat.default_budget = 1
    job = engine.request()
    with pytest.raises(model.DreamOutputTruncated) as truncated:
        engine.step(Event())
    engine.failed(truncated.value, job, engine.status()["generation"])
    assert engine.status()["state"] == "paused" and engine.status()["remaining_requests"] == 0
    engine.step(Event())
    assert len(calls) == 1 and service.profile.read_text() == "Old profile"

    restored = Dream(service)
    assert restored.request() == job
    restored.step(Event())
    assert calls[0][0] == calls[1][0]
    assert service.profile.read_text().strip() == "Complete"
    assert restored.status()["state"] == "paused" and restored.status()["phase"] == "hypotheses"

    service.config.chat.default_budget = 2
    restored.request()
    with pytest.raises(model.DreamOutputTruncated) as truncated:
        restored.step(Event())
    restored.failed(truncated.value, job, restored.status()["generation"])
    assert service.list_inferences() == [] and restored.status()["remaining_requests"] == 1
    assert restored.due() and restored.status()["retry_at"] == 0
    restored.step(Event())
    assert calls[2][0] == calls[3][0]
    assert [limit for _, limit in calls] == [4000, 7300, 1800, 7200]
    assert restored.status()["requests"] == 4 and restored.status()["state"] == "completed"
    assert [row.text for row in service.list_inferences()] == ["Synthetic inference"]


def test_dream_keeps_bounded_retry_settings_for_remaining_batches_in_same_phase(
    service, monkeypatch
):
    service.config.dream.context_window = 66000
    for index in range(17):
        add(service, f"Synthetic fact {index}" + " sky" * 3400)
    service.config.chat.default_budget = 2
    service.config.llm = LLMConfig(
        provider="qianwen", model="glm-5.3", base_url=QIANWEN_BASE_URL, reasoning_effort="max"
    )
    monkeypatch.setattr(model, "model_metadata", lambda _: {})
    monkeypatch.setattr(model, "provider_reasoning_levels", lambda _: ("low", "max"))
    calls = []

    def complete(config, messages, **kwargs):
        phase = messages[0]["content"]
        calls.append((phase, kwargs["max_tokens"], config.reasoning_effort))
        if len(calls) == 1:
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
        text = '{"summary":"Complete"}' if phase == dream.PROFILE else '{"proposals":[]}'
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text))]
        )

    monkeypatch.setattr(model, "complete", complete)
    engine = Dream(service)
    job_id = engine.request()
    with pytest.raises(model.DreamOutputTruncated) as truncated:
        engine.step(Event())
    engine.failed(truncated.value, job_id, engine.status()["generation"])
    assert engine.due() and engine.status()["remaining_requests"] == 1
    # 旧版本留下的检查点可能已超过当前上限。
    checkpoint = read_job(engine.path)
    checkpoint["retry_output_tokens"] = 47332
    engine.path.write_bytes(encode_job(checkpoint))
    engine.step(Event())
    status = engine.status()
    assert (status["state"], status["batch"], status["remaining_requests"]) == ("paused", 1, 0)
    assert status["retry_output_tokens"] == 16000
    assert not service.profile.path.exists()

    service.config.chat.default_budget = 3
    restarted = Dream(service)
    assert restarted.request() == job_id
    for _ in range(3):
        assert restarted.due()
        restarted.step(Event())
    assert calls == [
        (dream.PROFILE, 4000, "max"),
        (dream.PROFILE, 16000, "low"),
        (dream.PROFILE, 16000, "low"),
        (dream.INFER, 1800, "max"),
        (dream.INFER, 1800, "max"),
    ]
    status = restarted.status()
    assert (status["state"], status["requests"], status["remaining_requests"]) == (
        "completed",
        5,
        0,
    )
    assert "retry_output_tokens" not in status
    assert service.config.llm.reasoning_effort == "max"


def test_truncation_at_context_ceiling_pauses_without_spending_another_request(
    service, monkeypatch
):
    add(service, "Synthetic fact")
    service.config.llm = LLMConfig(provider="local")
    monkeypatch.setattr(model, "model_metadata", lambda _: {})
    monkeypatch.setattr(model, "effective_context_window", lambda *_: 3564)
    monkeypatch.setattr(model, "estimate_tokens", lambda _: 500)
    limits = []

    def complete(config, messages, **kwargs):
        limits.append(kwargs["max_tokens"])
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=""))]
        )

    monkeypatch.setattr(model, "complete", complete)
    engine = Dream(service)
    service.config.chat.default_budget = 3
    job = engine.request()
    with pytest.raises(model.DreamOutputTruncated) as truncated:
        engine.step(Event())
    engine.failed(truncated.value, job, engine.status()["generation"])
    assert not engine.due()
    assert engine.status()["state"] == "paused" and engine.status()["remaining_requests"] == 2
    assert limits == [3000] and not service.profile.path.exists()


def test_invalid_finished_dream_response_preserves_validation_error(service, monkeypatch):
    monkeypatch.setattr(
        model,
        "complete",
        lambda *args, **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))]
        ),
    )
    with pytest.raises(ValidationError) as invalid:
        model.ask(service.config, dream.INFER, {"facts": []}, ReflectionResponse)
    assert invalid.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize(("levels", "retry_effort"), [(("low", "max"), "low"), (None, "max")])
def test_larger_dream_retry_uses_only_supported_reasoning_without_changing_chat(
    service, monkeypatch, levels, retry_effort
):
    service.config.llm = LLMConfig(
        provider="qianwen", model="glm-5.3", base_url=QIANWEN_BASE_URL, reasoning_effort="max"
    )
    monkeypatch.setattr(model, "model_metadata", lambda _: {})
    monkeypatch.setattr(model, "provider_reasoning_levels", lambda _: levels)
    calls = []

    def complete(config, messages, **kwargs):
        calls.append((kwargs["max_tokens"], config.reasoning_effort))
        if len(calls) == 1:
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content='{"summary":"ok"}')
                )
            ]
        )

    monkeypatch.setattr(model, "complete", complete)
    with pytest.raises(model.DreamOutputTruncated) as truncated:
        model.ask(service.config, dream.PROFILE, {}, model.ProfileSummaryResponse)
    model.ask(
        service.config,
        dream.PROFILE,
        {},
        model.ProfileSummaryResponse,
        output_limit=truncated.value.next_limit,
    )
    assert calls == [(4000, "max"), (16000, retry_effort)]
    assert service.config.llm.reasoning_effort == "max"


def test_schedule_persists_cadence_and_only_enqueues_work(service, monkeypatch):
    add(service, "合成事实")
    monkeypatch.setattr(dream, "ask", lambda *args: pytest.fail("a scheduler must only enqueue"))
    owner = BackgroundMemory(service)
    owner.schedule.tick(100)
    next_at = owner.schedule.next_at()
    assert next_at > 100 and owner.dream_status()["state"] == "idle"
    restarted = BackgroundMemory(service)
    restarted.schedule.tick(next_at - 1)
    assert restarted.dream_status()["state"] == "idle"
    restarted.schedule.tick(next_at)
    assert restarted.dream_status()["state"] == "queued"
    assert read_job(restarted.dream.path)["requests"] == 0


def test_disabled_schedule_can_be_enabled_without_a_connected_client(service):
    owner = BackgroundMemory(service)
    service.config.chat.auto_reflect = False
    owner.schedule.tick(100)
    assert owner.schedule.next_at() is None
    assert owner.dream.status()["state"] == "idle"
    service.config.chat.auto_reflect = True
    owner.schedule.tick(200)
    assert owner.schedule.next_at() > 200
    owner.schedule.tick(owner.schedule.next_at())
    assert owner.dream.status()["state"] == "queued"


def test_due_schedule_only_rebuilds_a_published_profile_after_evidence_changes(
    service, monkeypatch
):
    add(service, "First synthetic fact")
    owner = BackgroundMemory(service)
    owner.schedule.tick(100)
    owner.schedule.tick(owner.schedule.next_at())
    calls = []

    def generate(_, prompt, data, schema):
        calls.append(prompt)
        if prompt == dream.PROFILE:
            return schema(summary="A complete profile")
        return schema(proposals=[])

    monkeypatch.setattr(dream, "ask", generate)
    owner.dream.step(Event())
    assert service.profile.read_text().strip() == "A complete profile"
    assert calls == [dream.PROFILE]
    owner.dream.step(Event())
    old_id = owner.dream.status()["id"]
    restarted = BackgroundMemory(service)
    restarted.schedule.tick(restarted.schedule.next_at())
    assert restarted.dream.status()["state"] == "completed"
    assert restarted.dream.status()["id"] == old_id
    assert calls == [dream.PROFILE, dream.INFER]
    add(service, "Second synthetic fact")
    restarted.schedule.tick(restarted.schedule.next_at())
    assert restarted.dream.status()["state"] == "queued"
    assert restarted.dream.status()["id"] != old_id


def test_automatic_dream_retries_from_checkpoint_without_consuming_extra_requests(
    service, monkeypatch
):
    import time

    add(service, "A fact to summarize")
    owner = BackgroundMemory(service)
    owner.schedule.tick(100)
    owner.schedule.tick(owner.schedule.next_at())

    def fail(*args):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(dream, "ask", fail)
    owner.start()
    try:
        deadline = time.monotonic() + 5
        while not owner.dream.status().get("failures") and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        owner.stop()
    checkpoint = owner.dream.status()
    assert checkpoint["failures"] == checkpoint["requests"] == 1
    assert not service.profile.path.exists()

    restarted = BackgroundMemory(service)
    assert restarted.dream.status() == checkpoint
    assert not restarted.dream.due()
    monkeypatch.setattr(dream, "time", SimpleNamespace(time=lambda: checkpoint["retry_at"] + 1))
    assert restarted.dream.due()
    monkeypatch.setattr(dream, "ask", lambda _, prompt, data, schema: schema(summary="Recovered"))
    restarted.dream.step(Event())
    assert service.profile.read_text().strip() == "Recovered"
    assert restarted.dream.status()["requests"] == 2
    assert restarted.dream.status()["failures"] == 0


def test_failed_model_call_and_stale_input_still_consume_budget(service, monkeypatch):
    entry = add(service, "初始事实")
    engine = Dream(service)
    service.config.chat.default_budget = 1
    engine.request()

    def model_call(_, prompt, data, schema):
        service.apply(updates=[MemoryUpdate(id=entry.id, context=MemoryContext(location="新地点"))])
        return schema(summary="已失效结果")

    monkeypatch.setattr(dream, "ask", model_call)
    engine.step(Event())
    assert engine.status()["requests"] == 1 and engine.status()["remaining_requests"] == 0
    engine.step(Event())
    assert engine.status()["state"] == "paused" and not service.profile.path.exists()


def test_run_continues_legacy_cancelled_checkpoint_and_ignores_old_generation(service):
    engine = Dream(service)
    job = engine.request()
    generation = engine.status()["generation"]
    checkpoint = read_job(engine.path)
    checkpoint.update(state="cancelled", batch=2, requests=4)
    engine.path.write_bytes(encode_job(checkpoint))
    assert engine.request() == job
    engine.failed(TimeoutError(), job, generation)
    status = engine.status()
    assert status["state"] == "queued" and status["failures"] == 0
    assert status["batch"] == 2 and status["requests"] == 4
