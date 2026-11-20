from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from syvrio.background_memory import BackgroundMemory, dream, model
from syvrio.background_memory.dream import Dream
from syvrio.infra.config import LLMConfig
from syvrio.infra.jobs import read_job
from syvrio.memory.models import MemoryContext, MemoryDraft, MemoryUpdate, ReflectionResponse

pytestmark = pytest.mark.memory


def add(service, text, **context):
    return service.add(MemoryDraft(topic="gardening", text=text, context=context or None))


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
    facts = [add(service, f"合成事实 {n}") for n in range(22)]
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
    job = engine.request(1)
    engine.step(Event())
    assert engine.status()["state"] == "paused"
    assert not service.profile.path.exists()
    restarted = Dream(service)
    assert restarted.request(20) == job
    while restarted.due():
        restarted.step(Event())
    assert seen == {f.id for f in facts} == set.union(*published)
    assert len(published) >= 2


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


def test_late_provider_response_after_cancel_or_change_is_discarded(service, monkeypatch):
    fact = add(service, "测试事实")
    engine = Dream(service)
    job = engine.request()

    def change(*args):
        service.apply(updates=[MemoryUpdate(id=fact.id, context=MemoryContext(location="杭州"))])
        return args[-1](summary="过期画像")

    monkeypatch.setattr(dream, "ask", change)
    engine.step(Event())
    assert service.profile.read_text() == ""

    def cancel(*args):
        engine.cancel()
        return args[-1](summary="取消的画像")

    monkeypatch.setattr(dream, "ask", cancel)
    engine.step(Event())
    assert engine.status()["state"] == "cancelled" and not service.profile.path.exists()
    assert engine.request(1) == job


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
    assert model.ask(service, dream.INFER, evidence, ReflectionResponse).proposals == []
    assert captured["retries"] == 0
    assert 0 < captured["max_tokens"] <= min(1800, service.config.chat.context_window)
    assert captured["facts"][0]["context"]["emotion"] == emotion


@pytest.mark.parametrize("output", [{}, {"proposals": [{"text": "x", "source_ids": ["1"]}]}])
def test_model_cannot_omit_proposals_or_kind(output):
    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate(output)


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


def test_failed_model_call_and_stale_input_still_consume_budget(service, monkeypatch):
    entry = add(service, "初始事实")
    engine = Dream(service)
    engine.request(1)

    def model_call(_, prompt, data, schema):
        service.apply(updates=[MemoryUpdate(id=entry.id, context=MemoryContext(location="新地点"))])
        return schema(summary="已失效结果")

    monkeypatch.setattr(dream, "ask", model_call)
    engine.step(Event())
    assert engine.status()["requests"] == 1 and engine.status()["remaining_requests"] == 0
    engine.step(Event())
    assert engine.status()["state"] == "paused" and not service.profile.path.exists()


def test_old_request_cannot_mark_a_resumed_dream_failed(service):
    engine = Dream(service)
    job = engine.request()
    generation = engine.status()["generation"]
    engine.cancel()
    assert engine.request() == job
    engine.failed(TimeoutError(), job, generation)
    assert engine.status()["state"] == "queued" and engine.status()["failures"] == 0
