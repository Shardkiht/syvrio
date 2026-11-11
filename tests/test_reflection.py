from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import (
    Candidate,
    ExtractionResult,
    MemoryContext,
    ReflectionProposal,
)
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.memory


def proposal(mode, entries):
    return [ReflectionProposal(text="可能偏好安静的合成活动", source_ids=[entries[0].id])]


def test_dream_is_incremental_separate_and_restartable(service):
    source = service.note("合成阅读偏好", topic="preferences")
    first = service.reflection.run(model=proposal)
    assert first.requests == 1 and first.proposed == 1 and first.remaining == 0
    assert service.inferences.all()[0].kind == "reflection"
    assert service.store.entries() == [source]
    restarted = MemoryService(service.config)
    second = restarted.reflection.run(model=lambda *_: pytest.fail("不得重复请求"))
    assert second.requests == 0 and second.skipped == 1


def test_new_fact_does_not_restart_entire_reflection_corpus(service):
    for index in range(25):
        service.store.append(f"synthetic-{index}", f"合成事实 {index}")
    initial = service.reflection.run(max_requests=3, model=lambda *_: [])
    assert initial.remaining == 0
    added = service.note("新增合成事实")
    calls = []
    result = service.reflection.run(model=lambda mode, entries: calls.append(entries) or [])
    assert result.requests == 1 and result.remaining == 0 and result.skipped == 25
    assert len(calls[0]) <= 5
    assert added.id in {entry.id for entry in calls[0]}


def test_profile_rebuilds_from_sources_and_only_injects_confirmed_related_inferences(service):
    source = service.note("合成阅读偏好", topic="preferences")
    other = service.note("合成其他事实")
    result = service.reflection.run(
        "profile",
        model=lambda *_: [
            ReflectionProposal(text="可能偏好安静的合成活动", source_ids=[source.id])
        ],
    )
    assert result.proposed == 1
    items = service.reflection.profile()
    assert {item.kind for item in items} == {"fact", "inference"}
    assert not service.reflection.context({source.id})
    inference = service.inferences.all()[0]
    service.review_inference(inference.id, "confirm")
    assert "推断" in service.reflection.context({source.id})
    assert not service.reflection.context({other.id})
    service.forget(source.id)
    assert service.reflection.profile() == []
    assert not service.reflection.context({source.id})


def test_manual_fact_edit_invalidates_profile_evidence(service):
    source = service.note("合成早期阅读偏好", topic="preferences")
    service.reflection.run("profile", model=proposal)
    path = service.store.topic_path("preferences")
    path.write_text(
        path.read_text(encoding="utf-8").replace("合成早期阅读偏好", "合成后期阅读偏好"),
        encoding="utf-8",
    )
    assert service.store.find(source.id).status == "active"
    assert service.list_inferences()[0].status == "superseded"
    assert all(item.kind == "fact" for item in service.reflection.profile())


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_reflection_request_budget(service, budget):
    for index in range(25):
        service.store.append(f"synthetic-{index}", f"合成事实 {index}")
    calls = []
    result = service.reflection.run(
        max_requests=budget, model=lambda mode, entries: calls.append(entries) or []
    )
    assert result.requests == len(calls) == budget
    assert result.remaining == 3 - budget


@pytest.mark.parametrize("bad", ["source", "secret"])
def test_invalid_model_output_never_reaches_inference_log(service, bad):
    source = service.note("合成事实")

    def model(mode, entries):
        return [
            ReflectionProposal(
                text="password = synthetic-only" if bad == "secret" else "合成假设",
                source_ids=["missing"] if bad == "source" else [source.id],
            )
        ]

    result = service.reflection.run(model=model)
    assert result.error and result.requests == 1
    assert service.inferences.all() == []
    assert "synthetic-only" not in service.reflection.path.read_text(encoding="utf-8")


def test_cancel_keeps_chat_writes_available_and_drops_returned_candidates(service):
    service.note("合成事实")
    started, release = threading.Event(), threading.Event()

    def model(mode, entries):
        started.set()
        assert release.wait(5)
        return proposal(mode, entries)

    with ThreadPoolExecutor() as pool:
        pending = pool.submit(service.reflection.run, model=model)
        try:
            assert started.wait(5)
            # 网络等待期间不持有写锁。
            service.note("回顾期间的新事实")
            with pytest.raises(ValueError, match="已有"):
                service.reflection.run(model=proposal)
            service.reflection.cancel()
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result.cancelled and not service.inferences.all()


def test_cancel_before_worker_starts_never_calls_model(service):
    service.note("合成事实")
    token = threading.Event()
    token.set()
    result = service.reflection.run(model=lambda *_: pytest.fail("不得请求"), cancel_event=token)
    assert result.cancelled and result.requests == 0


def test_source_change_during_model_call_invalidates_result(service):
    source = service.note("合成事实")

    def model(mode, entries):
        service.forget(source.id)
        return proposal(mode, entries)

    result = service.reflection.run(model=model)
    assert result.error and service.inferences.all() == []


def test_prepared_result_resumes_without_another_model_request(service, monkeypatch):
    service.note("合成事实")
    original = service._propose_inference_locked

    def fail(*args):
        raise OSError("合成写入失败")

    monkeypatch.setattr(service, "_propose_inference_locked", fail)
    assert service.reflection.run(model=proposal).error
    monkeypatch.setattr(service, "_propose_inference_locked", original)
    result = service.reflection.run(
        max_requests=0, model=lambda *_: pytest.fail("应复用已验证结果")
    )
    assert result.requests == 0 and result.proposed == 1 and result.remaining == 0


def test_second_batch_cached_result_survives_completed_first_batch(service, monkeypatch):
    for index in range(13):
        service.store.append(f"synthetic-{index}", f"合成事实 {index}")
    original = service._propose_inference_locked
    count = 0

    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("合成写入失败")
        return original(*args)

    monkeypatch.setattr(service, "_propose_inference_locked", fail_second)
    first = service.reflection.run(max_requests=2, model=proposal)
    assert first.error and first.requests == 2
    monkeypatch.setattr(service, "_propose_inference_locked", original)
    result = service.reflection.run(
        max_requests=0, model=lambda *_: pytest.fail("不得重复发送第二批")
    )
    assert result.requests == 0 and result.proposed == 1 and result.remaining == 0


def test_job_tail_recovery_preserves_all_bytes(service):
    service.note("合成事实")
    service.reflection.path.parent.mkdir(parents=True, exist_ok=True)
    service.reflection.path.write_bytes(b'{"status":')
    before = service.reflection.path.read_bytes()
    result = service.reflection.run(model=proposal)
    assert result.proposed == 1
    assert service.reflection.path.read_bytes().startswith(before)
    assert service.reflection._events()[1] is None


def test_current_and_historical_state_views(service):
    old = service.store.append(
        "preferences", "合成偏好早期状态", context=MemoryContext(valid_from="2020-01-01")
    )
    new = service.evolve(old.id, "合成偏好后期状态", "2024-01-01")
    assert [hit.id for hit in service.recall("合成偏好")] == [new.id]
    assert [hit.id for hit in service.recall("合成偏好", as_of="2022-01-01")] == [old.id]
    assert {hit.id for hit in service.recall("合成偏好", view="history")} == {old.id, new.id}
    service.forget(new.id)
    assert not service.recall("合成偏好", view="history")


def test_correction_does_not_resurrect_known_wrong_fact_as_history(service):
    old = service.store.append(
        "preferences", "合成错误偏好", context=MemoryContext(valid_from="2020-01-01")
    )
    service.correct(old.id, "合成正确偏好")
    assert old.id not in {hit.id for hit in service.recall("合成偏好", view="history")}


def test_current_view_excludes_expired_and_future_states_but_keeps_undated_facts(service):
    old = service.store.append("goals", "合成目标旧状态", context=MemoryContext(valid_to="2000"))
    future = service.store.append(
        "goals", "合成目标未来状态", context=MemoryContext(valid_from="9999")
    )
    current = service.note("合成目标未标日期", topic="goals")
    assert {hit.id for hit in service.recall("合成目标")} == {current.id}
    assert {hit.id for hit in service.recall("合成目标", view="history")} == {
        old.id,
        future.id,
        current.id,
    }


def test_disjoint_periods_are_not_conflicts(service):
    service.store.append(
        "preferences", "合成预算为1000", context=MemoryContext(valid_from="2020", valid_to="2021")
    )
    service.extractor = StaticExtractor(
        ExtractionResult(
            memories=[
                Candidate(
                    text="合成预算为2000",
                    topic="preferences",
                    confidence=1,
                    context=MemoryContext(valid_from="2024"),
                )
            ]
        )
    )
    result = service.memorize("合成陈述")
    assert len(result.admitted) == 1 and not result.queued


def test_same_fact_can_recur_in_disjoint_periods(service):
    service.store.append(
        "preferences", "合成阅读偏好", context=MemoryContext(valid_from="2020", valid_to="2021")
    )
    service.extractor = StaticExtractor(
        ExtractionResult(
            memories=[
                Candidate(
                    text="合成阅读偏好",
                    topic="preferences",
                    kind="update",
                    confidence=1,
                    context=MemoryContext(valid_from="2024"),
                )
            ]
        )
    )
    result = service.memorize("合成陈述")
    assert len(result.admitted) == 1 and not result.duplicates and not result.queued
    assert len(service.store.entries()) == 2


def test_overlapping_periods_still_require_confirmation(service):
    service.store.append(
        "preferences", "合成预算为1000", context=MemoryContext(valid_from="2020", valid_to="2025")
    )
    service.extractor = StaticExtractor(
        ExtractionResult(
            memories=[
                Candidate(
                    text="合成预算为2000",
                    topic="preferences",
                    confidence=1,
                    context=MemoryContext(valid_from="2024"),
                )
            ]
        )
    )
    result = service.memorize("合成陈述")
    assert result.queued and not result.admitted


def test_historical_conflicts_do_not_hide_current_conflict(service):
    for index in range(3):
        service.store.append(
            "preferences", f"合成预算为{1000 + index}", context=MemoryContext(valid_to="2021")
        )
    current = service.store.append(
        "preferences", "合成预算为1500", context=MemoryContext(valid_from="2023")
    )
    service.extractor = StaticExtractor(
        ExtractionResult(
            memories=[
                Candidate(
                    text="合成预算为2000",
                    topic="preferences",
                    confidence=1,
                    context=MemoryContext(valid_from="2024"),
                )
            ]
        )
    )
    result = service.memorize("合成陈述")
    assert result.queued[0].conflict_with == current.id


def test_reflection_model_disables_retries_and_limits_output(service, monkeypatch):
    from types import SimpleNamespace

    from syvrio.infra.config import LLMConfig
    from syvrio.memory import reflection as module

    service.config.llm = LLMConfig(provider="local", num_retries=7, fallbacks=["unused"])
    source = service.note("合成事实")
    captured = {}

    def complete(config, messages, **kwargs):
        captured.update(retries=config.num_retries, fallbacks=config.fallbacks, **kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"proposals":[]}'))]
        )

    monkeypatch.setattr(module, "complete", complete)
    assert service.reflection._model("dream", [source]) == []
    assert captured["retries"] == 0 and captured["fallbacks"] == []
    assert captured["max_tokens"] <= 1800


def test_spark_profile_reserves_space_for_reasoning_and_json(service, monkeypatch):
    from types import SimpleNamespace

    from syvrio.infra.config import LLMConfig
    from syvrio.infra.tokens import estimate_tokens
    from syvrio.memory import reflection as module

    monkeypatch.setenv("SYVRIO_SYNTHETIC_SPARK_KEY", "synthetic-key")
    service.config.llm = LLMConfig(provider="spark", api_key_env="SYVRIO_SYNTHETIC_SPARK_KEY")
    service.config.chat.context_window = 8192
    entry = service.note("合成偏好")
    captured = {}

    def complete(config, messages, **kwargs):
        captured.update(
            max_tokens=kwargs["max_tokens"],
            input_tokens=sum(estimate_tokens(m["content"]) for m in messages),
        )
        content = '{"summary":"合成画像"}' if kwargs["max_tokens"] > 1024 else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(module, "complete", complete)
    draft = service.reflection._summary_draft([entry], [])
    assert service.reflection._summary_model(draft.payload) == "合成画像"
    assert captured["max_tokens"] + captured["input_tokens"] < 8192


@pytest.mark.cli
def test_profile_cli_is_read_only_and_evolve_cli_preserves_history(service, monkeypatch):
    from typer.testing import CliRunner

    from syvrio.app.cli import app

    monkeypatch.setattr("syvrio.app.cli._service", lambda *args: service)
    old = service.store.append(
        "preferences", "合成早期偏好", context=MemoryContext(valid_from="2020-01-01")
    )
    result = CliRunner().invoke(app, ["evolve", old.id, "合成后期偏好", "--from", "2024-01-01"])
    assert result.exit_code == 0, result.output
    monkeypatch.setattr(
        service.reflection, "_model", lambda *_: pytest.fail("只读画像不得调用模型")
    )
    result = CliRunner().invoke(app, ["profile"])
    assert result.exit_code == 0 and "合成后期偏好" in result.output
    result = CliRunner().invoke(app, ["recall", "合成早期偏好", "--as-of", "2022-01-01"])
    assert result.exit_code == 0 and "合成早期偏好" in result.output


@pytest.mark.mcp
async def test_mcp_profile_and_temporal_evolution(service):
    from fastmcp import Client

    from syvrio.app.server import build_server

    old = service.store.append(
        "preferences", "合成早期偏好", context=MemoryContext(valid_from="2020-01-01")
    )
    service.config.search.enabled = False
    async with Client(build_server(service=service)) as client:
        result = await client.call_tool(
            "evolve", {"entry_id": old.id, "text": "合成后期偏好", "effective_from": "2024-01-01"}
        )
        new_id = result.data["id"]
        profile = await client.call_tool("profile", {})
        assert [item["id"] for item in profile.data] == [new_id]
        history = await client.call_tool("recall", {"query": "合成偏好", "as_of": "2022-01-01"})
        assert [item["id"] for item in history.data] == [old.id]


def test_missing_proposals_is_not_a_successful_empty_batch():
    from pydantic import ValidationError

    from syvrio.memory.models import ReflectionResponse

    with pytest.raises(ValidationError):
        ReflectionResponse.model_validate({})


def test_timeout_is_identified_without_logging_exception_payload(service):
    service.note("合成事实")

    def timeout(*args):
        raise TimeoutError("private-payload-placeholder")

    result = service.reflection.run(model=timeout)
    assert "超时" in result.error
    log = service.reflection.path.read_text(encoding="utf-8")
    assert '"error_kind": "timeout"' in log
    assert "private-payload-placeholder" not in log
    assert result.remaining == 1


def test_profile_can_derive_from_custom_topics_without_listing_unrelated_facts(service):
    source = service.note("合成用户阅读时喜欢安静", topic="custom-reading")
    unrelated = service.note("合成设备有两个按钮", topic="custom-device")
    seen = []

    def model(mode, entries):
        seen.extend(e.id for e in entries)
        return [ReflectionProposal(text="可能偏好安静阅读", source_ids=[source.id])]

    result = service.reflection.run("profile", model=model)
    assert result.proposed == 1 and set(seen) == {source.id, unrelated.id}
    items = service.reflection.profile()
    assert {item.id for item in items if item.kind == "fact"} == {source.id}
    assert not service.reflection.context({source.id})
    service.review_inference(next(i.id for i in items if i.kind == "inference"), "reject")
    assert service.reflection.profile() == []


def test_profile_supports_chinese_topic_facts(service):
    source = service.note("合成偏好", topic="偏好")
    assert source.id in {item.id for item in service.reflection.profile()}


def test_profile_summary_refresh_spends_one_budget_and_stays_derived(service):
    source = service.note("合成阅读偏好：喜欢安静环境", topic="preferences")
    payloads = []

    def summary(payload):
        payloads.append(payload)
        return "整体特征\n偏好安静阅读，重视可维护性。"

    result = service.reflection.run(
        "profile",
        1,
        model=lambda *_: pytest.fail("摘要预算不应再发推断请求"),
        refresh_summary=True,
        summary_model=summary,
    )

    assert result.requests == 1
    assert result.summary_status == "updated"
    cached = service.reflection.profile_summary()
    assert cached is not None and not cached.stale
    assert "安静阅读" in cached.text and cached.fact_count == 1
    assert payloads[0]["target_chars"][0] >= 100
    assert service.store.entries() == [source]
    assert service.inferences.all() == []


def test_profile_summary_uses_remaining_budget_for_inferences(service):
    service.note("合成偏好", topic="preferences")
    calls = []

    def model(mode, entries):
        calls.append((mode, entries))
        return []

    result = service.reflection.run(
        "profile",
        2,
        model=model,
        refresh_summary=True,
        summary_model=lambda payload: "长期偏好合成的安静活动。",
    )

    assert result.requests == 2 and len(calls) == 1
    assert result.summary_status == "updated"


def test_profile_summary_is_not_regenerated_when_facts_are_unchanged(service):
    service.note("合成偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    result = service.reflection.run(
        "profile",
        1,
        model=lambda *_: [],
        refresh_summary=True,
        summary_model=lambda payload: pytest.fail("来源未变不应重新生成"),
    )
    assert result.summary_status == "fresh"
    assert result.requests == 1  # 预算转给推断批次
    assert service.reflection.profile_summary().text == "合成画像。"


def test_profile_summary_marks_stale_after_fact_edit_and_rebuilds(service):
    source = service.note("合成早期阅读偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "早期画像。"
    )
    assert not service.reflection.profile_summary().stale

    path = service.store.topic_path("preferences")
    path.write_text(
        path.read_text(encoding="utf-8").replace("合成早期阅读偏好", "合成后期阅读偏好"),
        encoding="utf-8",
    )
    assert service.reflection.profile_summary().stale

    seen = []

    def summary(payload):
        seen.extend(item["text"] for item in payload["facts"])
        return "后期画像。"

    result = service.reflection.run("profile", 1, refresh_summary=True, summary_model=summary)
    assert result.summary_status == "updated"
    assert any("合成后期阅读偏好" in text for text in seen)
    assert all("合成早期阅读偏好" not in text for text in seen)
    assert service.store.find(source.id).text == "合成后期阅读偏好"


def test_profile_summary_clears_when_nothing_remains(service):
    source = service.note("合成偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    service.forget(source.id)
    result = service.reflection.run(
        "profile", 0, refresh_summary=True, model=lambda *_: pytest.fail("无事实不得请求")
    )
    assert result.summary_status == "empty"
    assert service.reflection.profile_summary() is None


def test_profile_summary_ignores_corrupt_cache_and_can_rebuild(service):
    service.note("合成偏好", topic="preferences")
    service.reflection.summary_path.parent.mkdir(parents=True, exist_ok=True)
    service.reflection.summary_path.write_text("{not-json", encoding="utf-8")
    assert service.reflection.profile_summary() is None
    result = service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "重建画像。"
    )
    assert result.summary_status == "updated"
    assert service.reflection.profile_summary().text == "重建画像。"


def test_profile_summary_failure_keeps_previous_and_marks_stale(service):
    service.note("合成偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "旧画像。"
    )
    service.note("合成新增目标", topic="goals")

    def fail(payload):
        raise TimeoutError("synthetic-private-detail")

    result = service.reflection.run("profile", 1, refresh_summary=True, summary_model=fail)
    assert result.summary_status == "failed"
    assert result.error and "摘要" in result.error
    cached = service.reflection.profile_summary()
    assert cached.text == "旧画像。" and cached.stale
    assert "synthetic-private-detail" not in service.reflection.summary_path.read_text(
        encoding="utf-8"
    )


def test_profile_summary_cancel_is_not_published(service):
    service.note("合成偏好", topic="preferences")
    token = threading.Event()

    def summary(payload):
        token.set()
        return "不应发布的画像。"

    result = service.reflection.run(
        "profile", 1, cancel_event=token, refresh_summary=True, summary_model=summary
    )
    assert result.cancelled and result.summary_status == "cancelled"
    assert service.reflection.profile_summary() is None


def test_profile_summary_rejects_secrets(service):
    service.note("合成偏好", topic="preferences")
    result = service.reflection.run(
        "profile",
        1,
        refresh_summary=True,
        summary_model=lambda payload: "api_key = synthetic-only-placeholder",
    )
    assert result.summary_status == "failed"
    assert service.reflection.profile_summary() is None


def test_profile_summary_is_not_injected_into_chat_context(service):
    source = service.note("合成阅读偏好", topic="preferences")
    service.reflection.run(
        "profile",
        1,
        refresh_summary=True,
        summary_model=lambda payload: "不应进入聊天上下文的完整画像。",
    )
    assert service.reflection.context({source.id}) == ""


def test_profile_refresh_zero_keeps_stale_cache_and_makes_no_request(service):
    service.note("合成偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "旧画像。"
    )
    service.note("合成新增目标", topic="goals")
    result = service.reflection.run(
        "profile",
        0,
        refresh_summary=True,
        model=lambda *_: pytest.fail("预算 0 不得请求"),
        summary_model=lambda payload: pytest.fail("预算 0 不得请求"),
    )
    assert result.requests == 0 and result.summary_status == "skipped"
    cached = service.reflection.profile_summary()
    assert cached.text == "旧画像。" and cached.stale


def test_inference_review_marks_summary_outdated(service):
    source = service.note("合成偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "画像。"
    )
    assert not service.reflection.profile_summary().stale
    service.propose_inference("可能偏好安静活动", [source.id], kind="profile")
    assert service.reflection.profile_summary().stale


def test_profile_length_follows_unique_information_not_raw_count(service):
    from syvrio.memory.reflection import profile_length_range

    assert profile_length_range(10, 2) == (100, 225)
    assert profile_length_range(50, 5) == (300, 700)
    assert profile_length_range(200, 4) == (500, 1200)
    assert profile_length_range(2000, 6) == (1000, 3000)

    for _index in range(120):
        service.store.append("preferences", "合成重复偏好")
    payloads = []
    service.reflection.run(
        "profile",
        1,
        refresh_summary=True,
        summary_model=lambda payload: payloads.append(payload) or "重复画像。",
    )
    stats = payloads[0]["stats"]
    assert stats["valid_facts"] == 120 and stats["unique_facts"] == 1
    assert payloads[0]["target_chars"][1] <= 300
    assert stats["sampled_facts"] == 1


def _prime_schedule(service, now):
    state = service.reflection.schedule_state()
    entries = service.current_facts()
    previous, important = service.reflection._load_profile_fingerprints()
    changes = service.reflection._facts_changes(entries, previous, important)
    service.reflection._write_profile_fingerprints(changes.fingerprints, changes.important_ids)
    stamp = now.isoformat(timespec="seconds")
    state["profile"].update(
        {
            "last_attempt_at": stamp,
            "last_success_at": stamp,
            "next_retry_at": None,
            "fact_digest": changes.digest,
            "pending_changes": 0,
            "pending_important": False,
            "pending_inferences": False,
            "consecutive_failures": 0,
        }
    )
    state["dream"].update(
        {
            "last_attempt_at": stamp,
            "last_success_at": stamp,
            "next_retry_at": None,
            "fact_digest": changes.digest,
            "pending": False,
            "consecutive_failures": 0,
        }
    )
    state["last_seen_facts_signature"] = service.reflection._facts_signature()
    service.reflection._save_schedule_state(state)


def _set_attempts(service, stamp):
    state = service.reflection.schedule_state()
    state["profile"]["last_attempt_at"] = stamp
    state["dream"]["last_attempt_at"] = stamp
    service.reflection._save_schedule_state(state)


def test_auto_profile_waits_weekly_unless_change_is_valuable(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)

    for index in range(5):
        service.store.append("uncategorized", f"合成普通新增 {index}")
    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_plan(now + timedelta(hours=25)) == "dream"

    state = service.reflection.schedule_state()
    state["dream"]["last_attempt_at"] = (now + timedelta(hours=25)).isoformat(timespec="seconds")
    service.reflection._save_schedule_state(state)
    assert service.reflection.auto_plan(now + timedelta(days=8)) == "profile"


def test_many_pending_changes_refresh_at_cooldown_even_without_new_files(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    for index in range(25):
        service.store.append("uncategorized", f"合成大批新增 {index}")

    # 第一次扫描记录变更，但 24 小时冷却未到，不应立即刷新。
    assert service.reflection.auto_plan(now + timedelta(hours=1)) is None
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_changes"] == 25
    # 文件不再变化，仅冷却到期也必须能触发提前更新。
    assert service.reflection.auto_check_due(now + timedelta(hours=25))
    assert service.reflection.auto_plan(now + timedelta(hours=25)) == "profile"


def test_auto_plan_skips_while_another_run_is_active(service):
    from datetime import datetime

    service.note("合成普通事实", topic="uncategorized")
    service.reflection._running.acquire()
    try:
        assert service.reflection.auto_plan(datetime.now().astimezone()) is None
    finally:
        service.reflection._running.release()


def test_source_change_marks_summary_stale_without_immediate_model_call(service):
    from datetime import datetime, timedelta

    source = service.note("合成早期偏好", topic="preferences")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "旧画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    path = service.store.topic_path("preferences")
    path.write_text(
        path.read_text(encoding="utf-8").replace("合成早期偏好", "合成后期偏好"), encoding="utf-8"
    )
    assert service.reflection.profile_summary().stale
    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.store.find(source.id).text == "合成后期偏好"


def test_auto_check_does_not_scan_fact_contents(service, monkeypatch):
    from datetime import datetime

    service.note("合成普通事实", topic="uncategorized")
    monkeypatch.setattr(
        service, "current_facts", lambda: pytest.fail("轻量调度检查不得扫描完整事实")
    )
    service.reflection.auto_check_due(datetime.now().astimezone())


def test_auto_profile_skips_week_without_changes(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    # 预算 2：摘要用 1 次，剩余 1 次完成唯一的推断批次，确保没有遗留待办。
    service.reflection.run(
        "profile",
        2,
        refresh_summary=True,
        model=lambda *_: [],
        summary_model=lambda payload: "合成画像。",
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    assert not service.reflection.auto_check_due(now + timedelta(days=8))
    assert service.reflection.auto_plan(now + timedelta(days=8)) is None


def test_important_change_can_refresh_after_cooldown(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    service.store.append("goals", "长期目标发生变化：准备转向系统架构方向")

    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_plan(now + timedelta(hours=1)) is None
    assert service.reflection.auto_check_due(now + timedelta(hours=25))
    assert service.reflection.auto_plan(now + timedelta(hours=25)) == "profile"


def test_dream_auto_daily_survives_restart_and_skips_unchanged(service):
    from datetime import datetime, timedelta

    from syvrio.memory.service import MemoryService

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    service.reflection.run("dream", 1, model=lambda *_: [])
    now = datetime.now().astimezone()
    _prime_schedule(service, now)

    restarted = MemoryService(service.config)
    assert not restarted.reflection.auto_check_due(now + timedelta(hours=1))
    assert restarted.reflection.auto_plan(now + timedelta(hours=1)) is None

    restarted.store.append("uncategorized", "合成新增事实")
    assert restarted.reflection.auto_check_due(now + timedelta(hours=25))
    assert restarted.reflection.auto_plan(now + timedelta(hours=25)) == "dream"


def test_manual_failure_does_not_hide_pending_profile_work(service):
    service.note("合成普通事实", topic="uncategorized")
    result = service.reflection.run(
        "profile",
        1,
        refresh_summary=True,
        summary_model=lambda payload: (_ for _ in ()).throw(TimeoutError("synthetic")),
    )
    assert result.error
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_changes"] >= 1
    assert service.reflection.auto_check_due()


def test_auto_failure_uses_capped_backoff(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    state = service.reflection.schedule_state()
    state["dream"]["last_attempt_at"] = (now - timedelta(days=2)).isoformat(timespec="seconds")
    service.reflection._save_schedule_state(state)

    def fail(*args, **kwargs):
        raise TimeoutError("synthetic")

    for index in range(1, 7):
        result = service.reflection.run("dream", 1, model=fail, auto=True)
        assert result.error
        state = service.reflection.schedule_state()
        assert state["dream"]["consecutive_failures"] == index
        retry = service.reflection._parse_time(state["dream"]["next_retry_at"])
        hours = (retry - datetime.now().astimezone()).total_seconds() / 3600
        assert 0.9 <= hours <= 24.1

    assert not service.reflection.auto_check_due(now + timedelta(minutes=30))


def test_corrupt_schedule_state_degrades_safely(service):
    service.reflection.schedule_path.parent.mkdir(parents=True, exist_ok=True)
    service.reflection.schedule_path.write_text('{"version": 1, "profile": []}', encoding="utf-8")
    state = service.reflection.schedule_state()
    assert state["version"] == 1 and state["profile"]["pending_changes"] == 0
    assert service.reflection.auto_check_due() is True
    service.reflection.schedule_path.write_text("not-json", encoding="utf-8")
    assert service.reflection.schedule_state()["profile"]["pending_changes"] == 0
    assert service.reflection.auto_plan() is None


def test_auto_plan_never_invokes_model(service, monkeypatch):
    from datetime import datetime

    service.note("合成普通事实", topic="uncategorized")
    monkeypatch.setattr(service.reflection, "_model", lambda *_: pytest.fail("计划阶段不得请求"))
    monkeypatch.setattr(
        service.reflection, "_summary_model", lambda *_: pytest.fail("计划阶段不得请求")
    )
    assert service.reflection.auto_plan(datetime.now().astimezone()) in {None, "profile", "dream"}


def test_manual_success_resets_auto_cadence(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    result = service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    assert result.summary_status == "updated"
    state = service.reflection.schedule_state()
    assert state["profile"]["last_success_at"] and state["profile"]["consecutive_failures"] == 0
    now = datetime.now().astimezone()
    _set_attempts(service, now.isoformat(timespec="seconds"))
    assert not service.reflection.auto_check_due(now + timedelta(hours=2))


def _current_changes(service):
    previous, important = service.reflection._load_profile_fingerprints()
    return service.reflection._facts_changes(service.current_facts(), previous, important)


def test_existing_important_fact_does_not_upgrade_ordinary_changes(service):
    from datetime import datetime

    service.note("合成长期目标：提升系统架构能力", topic="goals")
    service.note("合成普通事实", topic="uncategorized")
    _prime_schedule(service, datetime.now().astimezone())
    service.store.append("uncategorized", "合成普通新增事实")
    changes = _current_changes(service)
    assert changes.count == 1
    assert changes.important is False


def test_new_important_fact_is_important(service):
    from datetime import datetime

    service.note("合成普通事实", topic="uncategorized")
    _prime_schedule(service, datetime.now().astimezone())
    service.store.append("goals", "合成新的长期目标")
    assert _current_changes(service).important is True


def test_changed_important_fact_is_important(service):
    from datetime import datetime

    service.note("合成长期目标：保持稳定", topic="goals")
    _prime_schedule(service, datetime.now().astimezone())
    path = service.store.topic_path("goals")
    path.write_text(
        path.read_text(encoding="utf-8").replace("保持稳定", "转向新的长期方向"), encoding="utf-8"
    )
    assert _current_changes(service).important is True


def test_changed_ordinary_fact_becoming_important_is_important(service):
    from datetime import datetime

    service.note("合成普通事实", topic="uncategorized")
    _prime_schedule(service, datetime.now().astimezone())
    path = service.store.topic_path("uncategorized")
    path.write_text(
        path.read_text(encoding="utf-8").replace("合成普通事实", "合成长期目标：转型方向"),
        encoding="utf-8",
    )
    assert _current_changes(service).important is True


def test_removed_important_fact_is_important(service):
    from datetime import datetime

    source = service.note("合成长期目标", topic="goals")
    _prime_schedule(service, datetime.now().astimezone())
    service.forget(source.id)
    assert _current_changes(service).important is True


def test_removed_ordinary_fact_is_not_important(service):
    from datetime import datetime

    source = service.note("合成普通事实", topic="uncategorized")
    _prime_schedule(service, datetime.now().astimezone())
    service.forget(source.id)
    changes = _current_changes(service)
    assert changes.count == 1 and changes.important is False


def test_important_change_detection_survives_restart(service):
    from datetime import datetime

    from syvrio.memory.service import MemoryService

    service.note("合成长期目标", topic="goals")
    service.note("合成普通事实", topic="uncategorized")
    _prime_schedule(service, datetime.now().astimezone())
    restarted = MemoryService(service.config)
    restarted.store.append("uncategorized", "合成普通新增事实")
    previous, important = restarted.reflection._load_profile_fingerprints()
    changes = restarted.reflection._facts_changes(restarted.current_facts(), previous, important)
    assert changes.important is False


def test_missing_summary_wakes_weekly_schedule_without_immediate_request(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    service.reflection.summary_path.unlink()
    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_check_due(now + timedelta(days=8))
    assert service.reflection.auto_plan(now + timedelta(days=8)) == "profile"


def test_corrupt_summary_wakes_weekly_schedule(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    service.reflection.summary_path.write_text("{broken", encoding="utf-8")
    assert service.reflection.auto_check_due(now + timedelta(days=8))
    assert service.reflection.auto_plan(now + timedelta(days=8)) == "profile"


def test_confirmed_inference_change_wakes_weekly_profile_refresh(service):
    from datetime import datetime, timedelta

    source = service.note("合成普通事实", topic="uncategorized")
    hypothesis = service.propose_inference("可能偏好合成活动", [source.id])
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    service.review_inference(hypothesis.id, "confirm")
    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_check_due(now + timedelta(days=8))
    assert service.reflection.auto_plan(now + timedelta(days=8)) == "profile"


def test_partial_dream_batch_keeps_pending_until_complete(service):
    from datetime import datetime, timedelta

    for index in range(30):
        service.store.append("uncategorized", f"合成批次事实 {index}")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    state = service.reflection.schedule_state()
    state["profile"]["last_attempt_at"] = now.isoformat(timespec="seconds")
    service.reflection._save_schedule_state(state)

    result = service.reflection.run("dream", 1, model=lambda *_: [], auto=True)
    assert result.requests == 1 and result.remaining == 2
    state = service.reflection.schedule_state()
    assert state["dream"]["pending"] is True
    assert state["dream"]["remaining_batches"] == 2

    # 计划阶段不能把尚未处理完的批次覆盖成已完成。
    assert service.reflection.auto_plan(now + timedelta(hours=1)) is None
    assert service.reflection.schedule_state()["dream"]["pending"] is True
    assert service.reflection.auto_check_due(now + timedelta(hours=25))
    assert service.reflection.auto_plan(now + timedelta(hours=25)) == "dream"

    second = service.reflection.run("dream", 1, model=lambda *_: [], auto=True)
    assert second.remaining > 0
    state = service.reflection.schedule_state()
    assert state["dream"]["pending"] is True
    assert state["dream"]["remaining_batches"] == second.remaining

    # 每轮只完成一批；参考批次会改变后续分组，因此以实际 remaining 收束到 0 为准。
    for _ in range(6):
        if not service.reflection.schedule_state()["dream"]["pending"]:
            break
        following = service.reflection.run("dream", 1, model=lambda *_: [], auto=True)
        assert following.requests == 1
        state = service.reflection.schedule_state()
        if following.remaining > 0:
            assert state["dream"]["pending"] is True
            assert state["dream"]["remaining_batches"] == following.remaining
        else:
            assert state["dream"]["pending"] is False
    assert service.reflection.schedule_state()["dream"]["pending"] is False


def test_retry_backoff_blocks_due_dream_until_retry_time(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    state = service.reflection.schedule_state()
    state["dream"].update(
        {
            "last_attempt_at": (now - timedelta(days=2)).isoformat(timespec="seconds"),
            "next_retry_at": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
            "pending": True,
        }
    )
    service.reflection._save_schedule_state(state)

    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_plan(now + timedelta(hours=1)) is None
    assert service.reflection.auto_check_due(now + timedelta(hours=3))
    assert service.reflection.auto_plan(now + timedelta(hours=3)) == "dream"


def test_retry_backoff_blocks_due_profile_until_retry_time(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    now = datetime.now().astimezone()
    _prime_schedule(service, now)
    state = service.reflection.schedule_state()
    state["dream"]["last_attempt_at"] = now.isoformat(timespec="seconds")
    state["profile"].update(
        {
            "last_attempt_at": (now - timedelta(days=8)).isoformat(timespec="seconds"),
            "next_retry_at": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
            "pending_important": True,
        }
    )
    service.reflection._save_schedule_state(state)

    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_plan(now + timedelta(hours=1)) is None
    assert service.reflection.auto_check_due(now + timedelta(hours=3))
    assert service.reflection.auto_plan(now + timedelta(hours=3)) == "profile"


def test_manual_run_ignores_retry_backoff(service):
    from datetime import datetime, timedelta

    service.note("合成普通事实", topic="uncategorized")
    now = datetime.now().astimezone()
    state = service.reflection.schedule_state()
    state["dream"].update(
        {
            "last_attempt_at": (now - timedelta(days=2)).isoformat(timespec="seconds"),
            "next_retry_at": (now + timedelta(hours=12)).isoformat(timespec="seconds"),
            "pending": True,
        }
    )
    service.reflection._save_schedule_state(state)
    result = service.reflection.run("dream", 1, model=lambda *_: [])
    assert result.requests == 1 and result.error is None


def test_hypothesis_only_change_does_not_keep_summary_stale(service):
    source = service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    service.propose_inference("未确认的合成假设", [source.id])
    assert service.reflection.profile_summary().stale
    service.reflection._resign_summary()
    assert not service.reflection.profile_summary().stale


def test_confirmed_inference_change_cannot_be_resigned_as_fresh(service):
    source = service.note("合成普通事实", topic="uncategorized")
    hypothesis = service.propose_inference("待确认的合成假设", [source.id])
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )
    service.review_inference(hypothesis.id, "confirm")
    assert service.reflection.profile_summary().stale
    service.reflection._resign_summary()
    assert service.reflection.profile_summary().stale


def test_summary_generation_aborts_on_mid_request_fact_change(service):
    service.note("合成普通事实", topic="uncategorized")
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "旧画像。"
    )
    service.store.append("uncategorized", "触发摘要过期的事实")

    def mutate(payload):
        service.store.append("uncategorized", "生成期间新增事实")
        return "不应发布的新画像。"

    result = service.reflection.run("profile", 1, refresh_summary=True, summary_model=mutate)
    assert result.summary_status == "stale"
    assert service.reflection.profile_summary().text == "旧画像。"


def test_summary_generation_aborts_on_mid_request_confirmation(service):
    source = service.note("合成普通事实", topic="uncategorized")
    hypothesis = service.propose_inference("待确认的合成假设", [source.id])
    service.reflection.run(
        "profile", 1, refresh_summary=True, summary_model=lambda payload: "旧画像。"
    )
    service.store.append("uncategorized", "触发摘要过期的事实")

    def mutate(payload):
        service.review_inference(hypothesis.id, "confirm")
        return "不应发布的新画像。"

    result = service.reflection.run("profile", 1, refresh_summary=True, summary_model=mutate)
    assert result.summary_status == "stale"
    assert service.reflection.profile_summary().text == "旧画像。"


def test_legacy_summary_cache_without_evidence_is_invalid(service):
    import json as json_module

    service.note("合成普通事实", topic="uncategorized")
    service.reflection.summary_path.parent.mkdir(parents=True, exist_ok=True)
    service.reflection.summary_path.write_text(
        json_module.dumps(
            {
                "version": 1,
                "text": "旧版摘要",
                "generated_at": "2026-01-01T00:00:00+08:00",
                "model": "synthetic",
                "fact_count": 1,
                "unique_count": 1,
                "topic_count": 1,
                "source_ids": [],
                "signature": "synthetic",
                "facts_signature": "synthetic",
            }
        ),
        encoding="utf-8",
    )
    assert service.reflection.profile_summary() is None


def test_summary_payload_fits_context_budget(service):
    import json as json_module

    from syvrio.infra.tokens import estimate_tokens
    from syvrio.memory.reflection import (
        PROFILE_SUMMARY_MAX_TOKENS,
        PROFILE_SUMMARY_PROMPT,
    )

    service.config.chat.context_window = 4096
    source = service.note("合成普通事实", topic="uncategorized")
    for index in range(90):
        service.store.append(f"synthetic-topic-{index % 45}", ("长事实细节" * 60) + str(index))
    for index in range(15):
        item = service.propose_inference(("可能的长推断" * 80) + str(index), [source.id])
        service.review_inference(item.id, "confirm")

    draft = service.reflection._summary_draft(service.current_facts(), service.list_inferences())
    payload_tokens = estimate_tokens(json_module.dumps(draft.payload, ensure_ascii=False))
    high = draft.high
    output_tokens = min(PROFILE_SUMMARY_MAX_TOKENS, max(256, int(high * 1.2)), max(256, 4096 // 3))
    assert estimate_tokens(PROFILE_SUMMARY_PROMPT) + payload_tokens + output_tokens + 200 <= 4096
    stats = draft.payload["stats"]
    assert len(stats["topic_counts"]) <= 40
    assert stats["other_topics"] >= 1


def test_low_context_window_summary_fails_without_model_request(service):
    service.config.chat.context_window = 1024
    service.note("合成普通事实", topic="uncategorized")
    result = service.reflection.run(
        "profile",
        1,
        refresh_summary=True,
        summary_model=lambda payload: pytest.fail("上下文不足不得请求模型"),
        model=lambda *_: pytest.fail("摘要预算不应进入推断"),
    )
    assert result.summary_status == "failed"
    assert result.requests == 0
    assert result.error and "上下文" in result.error


def test_context_uses_only_related_valid_confirmed_inferences(service, monkeypatch):
    source = service.note("合成阅读偏好", topic="preferences")
    other = service.note("合成其他事实")
    item = service.propose_inference("可能偏好安静活动", [source.id])
    service.review_inference(item.id, "confirm")
    monkeypatch.setattr(
        service.reflection, "profile", lambda: pytest.fail("聊天上下文不得构建完整画像")
    )
    assert "可能偏好安静活动" in service.reflection.context({source.id})
    assert service.reflection.context({other.id}) == ""
    service.forget(source.id)
    assert service.reflection.context({source.id}) == ""


def _completed_signatures(service):
    events = service.reflection._events()[0]
    return [
        signature
        for event in events
        if event["status"] == "completed"
        for signature in event.get("sources", [])
    ]


def _prime_profile_pending(service, now, count=30):
    """30 条事实、自动预算 1：唯一请求用于生成摘要，推断批次全部保留。"""
    for index in range(count):
        service.store.append("uncategorized", f"合成画像批次事实 {index}")
    _prime_schedule(service, now)
    return service.reflection.run(
        "profile", 1, auto=True, refresh_summary=True, summary_model=lambda payload: "合成画像。"
    )


def test_profile_summary_refresh_keeps_pending_inference_batches(service):
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    result = _prime_profile_pending(service, now)
    assert result.summary_status == "updated"
    assert result.requests == 1 and result.remaining == 3

    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True
    assert state["profile"]["pending_changes"] == 0
    # 剩余批次不得绕过周期：冷却期内既不能触发检查，也不能生成计划。
    assert not service.reflection.auto_check_due(now + timedelta(hours=1))
    assert service.reflection.auto_plan(now + timedelta(hours=1)) is None
    assert service.reflection.auto_check_due(now + timedelta(hours=169))
    assert service.reflection.auto_plan(now + timedelta(hours=169)) == "profile"


def test_profile_pending_inferences_resume_after_cycle(service):
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    assert service.reflection.auto_plan(now + timedelta(hours=169)) == "profile"
    calls = []

    def model(mode, entries):
        calls.append([entry.id for entry in entries])
        return []

    result = service.reflection.run(
        "profile",
        1,
        auto=True,
        refresh_summary=True,
        model=model,
        summary_model=lambda payload: pytest.fail("摘要有效时不得重复生成"),
    )
    assert result.summary_status == "fresh"
    assert result.requests == 1 and len(calls) == 1
    assert result.remaining == 2
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True
    resumed = datetime.now().astimezone()
    assert not service.reflection.auto_check_due(resumed + timedelta(hours=1))


def test_profile_pending_inferences_drain_without_resending_completed(service):
    from datetime import datetime

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    completed: list[str] = []
    requests = 0
    last = None
    for _ in range(8):
        if not service.reflection.schedule_state()["profile"]["pending_inferences"]:
            break
        last = service.reflection.run(
            "profile",
            1,
            auto=True,
            refresh_summary=True,
            model=lambda *_: [],
            summary_model=lambda payload: pytest.fail("摘要有效时不得重复生成"),
        )
        assert last.requests == 1
        requests += last.requests
        signatures = _completed_signatures(service)
        assert len(signatures) > len(completed)
        completed = signatures

    assert last is not None and last.remaining == 0
    assert service.reflection.schedule_state()["profile"]["pending_inferences"] is False
    # 30 条事实各完成一次，空 proposals 也标记完成；总请求数远小于逐条重试。
    assert len(completed) == len(set(completed)) == 30
    assert requests < 30


def test_profile_pending_inferences_survive_restart(service):
    from datetime import datetime, timedelta

    from syvrio.memory.service import MemoryService

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    restarted = MemoryService(service.config)
    state = restarted.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True
    later = now + timedelta(hours=169)
    assert restarted.reflection.auto_check_due(later)
    assert restarted.reflection.auto_plan(later) == "profile"


def test_manual_profile_run_updates_auto_state(service):
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    result = service.reflection.run(
        "profile",
        5,
        refresh_summary=True,
        model=lambda *_: [],
        summary_model=lambda payload: pytest.fail("摘要有效时不得重复生成"),
    )
    assert result.summary_status == "fresh"
    assert result.requests == 3 and result.remaining == 0

    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is False
    assert state["profile"]["pending_changes"] == 0
    assert state["profile"]["next_retry_at"] is None
    assert not service.reflection.auto_check_due(datetime.now().astimezone() + timedelta(hours=1))


def test_manual_prepared_batch_updates_auto_state(service):
    source = service.note("合成画像批次事实", topic="uncategorized")
    signature = service.reflection._signature(source)
    key = "manual-prepared-batch"
    service.reflection._record(key, "prepared", [], "profile", [signature], [signature])
    state = service.reflection.schedule_state()
    state["profile"]["pending_inferences"] = True
    service.reflection._save_schedule_state(state)

    result = service.reflection.run("profile", 0, refresh_summary=True)
    assert result.requests == 0 and result.remaining == 0
    assert result.completed == 1
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is False
    assert state["profile"]["last_success_at"] is not None


def test_profile_summary_failure_preserves_inference_pending(service):
    from datetime import datetime

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    state = service.reflection.schedule_state()
    state["profile"]["pending_inferences"] = False
    service.reflection._save_schedule_state(state)
    service.store.append("uncategorized", "摘要失败前的合成新增事实")

    def fail(payload):
        raise TimeoutError("synthetic")

    result = service.reflection.run(
        "profile",
        1,
        auto=True,
        refresh_summary=True,
        summary_model=fail,
        model=lambda *_: pytest.fail("摘要失败后预算已耗尽"),
    )
    assert result.summary_status == "failed"
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True
    assert state["profile"]["pending_changes"] >= 1


def test_profile_inference_failure_preserves_pending(service):
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)

    def fail(*args, **kwargs):
        raise TimeoutError("synthetic")

    result = service.reflection.run(
        "profile",
        1,
        auto=True,
        refresh_summary=True,
        model=fail,
        summary_model=lambda payload: pytest.fail("摘要有效时不得重复生成"),
    )
    assert result.error
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True
    assert state["profile"]["consecutive_failures"] == 1
    failed_at = datetime.now().astimezone()
    assert not service.reflection.auto_check_due(failed_at + timedelta(minutes=30))
    assert service.reflection.auto_check_due(failed_at + timedelta(hours=3))
    assert service.reflection.auto_plan(failed_at + timedelta(hours=3)) == "profile"


def test_profile_inference_cancel_preserves_pending(service):
    from datetime import datetime

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    token = threading.Event()

    def model(mode, entries):
        token.set()
        return []

    result = service.reflection.run(
        "profile",
        1,
        auto=True,
        refresh_summary=True,
        model=model,
        cancel_event=token,
        summary_model=lambda payload: pytest.fail("摘要有效时不得重复生成"),
    )
    assert result.cancelled
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True


def test_profile_inference_stale_source_preserves_pending(service):
    from datetime import datetime

    now = datetime.now().astimezone()
    _prime_profile_pending(service, now)
    path = service.store.topic_path("uncategorized")

    def mutate(mode, entries):
        path.write_text(
            path.read_text(encoding="utf-8").replace("合成画像批次事实", "运行中更正画像事实"),
            encoding="utf-8",
        )
        return []

    result = service.reflection.run(
        "profile",
        1,
        auto=True,
        refresh_summary=True,
        model=mutate,
        summary_model=lambda payload: pytest.fail("摘要有效时不得重复生成"),
    )
    assert result.error and "来源" in result.error
    state = service.reflection.schedule_state()
    assert state["profile"]["pending_inferences"] is True
    assert state["profile"]["consecutive_failures"] == 1
