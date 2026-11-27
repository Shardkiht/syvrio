"""按用户消息跟踪多条聊天提案，验证真实工具协议与存储结果。"""

from __future__ import annotations

import pytest

from syvrio.agent.evidence import RecallContext
from syvrio.agent.tools import build_tools
from syvrio.infra.session import SessionStore

pytestmark = pytest.mark.agent


@pytest.fixture
def chat(service):
    sessions = SessionStore(service.paths)
    session = sessions.create()
    sessions.append(
        session, {"type": "message", "role": "user", "id": "user", "content": "合成材料"}
    )
    guard = RecallContext(service)
    guard.begin_model_request()
    return guard, build_tools(service, session, config=service.config, evidence=guard)


def call(tools, name, **args):
    outcome = tools.execute(name, args)
    assert outcome.ok, outcome.content
    return outcome.data


def test_two_simultaneous_drink_proposals_independent_decisions_and_retries(service, chat):
    guard, tools = chat
    old = service.note("合成观测站备有茶具", topic="drinks")
    drafts = [
        {"topic": "drinks", "text": text}
        for text in ("合成观测站开始冲泡红茶。", "合成观测站的热水温度偶尔偏低。")
    ]
    proposed = [call(tools, "memorize", **draft) for draft in drafts]
    assert proposed[0]["proposal_id"] != proposed[1]["proposal_id"]
    assert old.id in {r["id"] for r in proposed[0]["results"]}
    decisions = [
        {"proposal_id": proposed[0]["proposal_id"], "decision": "memory"},
        {
            "proposal_id": proposed[1]["proposal_id"],
            "decision": "pending",
            "question": "哪台水壶？",
        },
    ]
    premature = call(tools, "resolve_memory_proposals", decisions=decisions)
    assert premature["status"] == "review_required"
    assert not service.pending() and len(service.store.entries()) == 1
    guard.begin_model_request()
    assert [call(tools, "memorize", **draft) for draft in drafts] == proposed
    for _ in range(2):
        assert old.id in {r["id"] for r in call(tools, "recall", query="茶具")["results"]}
    result = call(tools, "resolve_memory_proposals", decisions=decisions)["decisions"]
    assert [r["decision"] for r in result] == ["memory", "pending"]
    assert result[0]["memory"]["text"] == drafts[0]["text"]
    assert result[1]["pending"]["text"] == drafts[1]["text"]
    assert call(tools, "resolve_memory_proposals", decisions=decisions)["decisions"] == result
    assert [call(tools, "memorize", **draft) for draft in drafts] == result
    assert len(service.store.entries()) == 2
    assert len(service.pending()) == 1


def test_care_candidate_keeps_its_identity_across_rewording_and_other_tools(service, chat):
    guard, tools = chat
    base = {"text": "合成角色冬季使用护理膏", "topic": "care", "question": "使用频率？"}
    candidate = call(tools, "create_pending", **base)
    guard.begin_model_request()
    other = call(tools, "memorize", text="合成角色选用冬季护理用品", topic="habits")
    assert call(tools, "review_pending")["pending"] == []
    guard.begin_model_request()
    decisions = [
        {"proposal_id": candidate["proposal_id"], "decision": "pending"},
        {"proposal_id": other["proposal_id"], "decision": "skip"},
    ]
    result = call(tools, "resolve_memory_proposals", decisions=decisions)["decisions"]
    (saved,) = service.pending()
    assert result[0]["pending"]["id"] == saved.id
    assert (saved.text, saved.question, saved.message_id) == (
        base["text"],
        base["question"],
        "user",
    )
    assert not service.store.entries()
    guard.invalidate()
    assert call(tools, "create_pending", **base) == result[0]
    guard.invalidate(new_message=True)
    assert not tools.execute("resolve_memory_proposals", {"decisions": decisions}).ok


def test_stale_batch_refreshes_in_place_and_failed_batch_can_be_retried(service, chat):
    guard, tools = chat
    ids = [
        call(tools, "memorize", text=text, topic="synthetic")["proposal_id"]
        for text in ["合成色卡采用蓝色", "合成色卡也许有绿版"]
    ]
    choices = [
        {"proposal_id": ids[0], "decision": "memory"},
        {"proposal_id": ids[1], "decision": "pending"},
    ]
    guard.begin_model_request()
    outside = service.note("又来了一张合成色卡", topic="synthetic")
    stale = call(tools, "resolve_memory_proposals", decisions=choices)
    assert [p["proposal_id"] for p in stale["proposals"]] == ids
    assert outside.id in {r["id"] for r in stale["proposals"][0]["results"]}
    guard.begin_model_request()
    assert not tools.execute("resolve_memory_proposals", {"decisions": choices}).ok
    assert [e.id for e in service.store.entries()] == [outside.id] and not service.pending()
    choices[1]["question"] = "有没有绿版？"
    assert len(call(tools, "resolve_memory_proposals", decisions=choices)["decisions"]) == 2
    assert len(service.store.entries()) == 2 and len(service.pending()) == 1


def test_related_corrections_in_one_batch_replay_the_committed_revisions(service, chat):
    guard, tools = chat
    originals = [service.note(text, topic="cards") for text in ("合成蓝色卡片甲", "合成蓝色卡片乙")]
    drafts = [
        {"entry_id": old.id, "text": old.text + "修订", "expected_revision": old.revision}
        for old in originals
    ]
    reviews = [call(tools, "correct", **draft) for draft in drafts]
    guard.begin_model_request()
    choices = [{"proposal_id": r["proposal_id"], "decision": "memory"} for r in reviews]
    receipts = call(tools, "resolve_memory_proposals", decisions=choices)["decisions"]
    assert [call(tools, "correct", **draft) for draft in drafts] == receipts
    for old, receipt in zip(originals, receipts, strict=True):
        saved = service.store.find(old.id)
        assert saved.text == old.text + "修订"
        assert old.revision < saved.revision == receipt["memory"]["revision"]
