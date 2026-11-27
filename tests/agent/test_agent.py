from __future__ import annotations

import json

import pytest

from syvrio.agent.runtime import AgentRuntime
from syvrio.infra.config import ChatConfig, Config, LLMConfig
from syvrio.infra.llm import AssistantTurn, ChatModelError, ToolCall
from syvrio.infra.session import SessionStore
from syvrio.infra.storage.database import transaction
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import Candidate, ExtractionResult
from syvrio.memory.service import MemoryService
from syvrio.memory.turn_undo import TurnUndoError
from tests.support.proposals import decision_turn
from tests.support.storage import rows

pytestmark = pytest.mark.agent


class FakeChatModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def chat(self, messages, tools, on_delta=None):
        self.calls.append((messages, tools))
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        if isinstance(turn, Exception):
            raise turn
        if on_delta is not None and turn.content:
            half = max(1, len(turn.content) // 2)
            on_delta(turn.content[:half])
            on_delta(turn.content[half:])
        return turn


class FakeSummarizer:
    def summarize(self, previous, transcript, max_chars=1200):
        return "会话摘要"


def build_runtime(tmp_path, model, *, extract_on_exit=False, max_steps=4):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(
            context_window=32000,
            max_tool_steps=max_steps,
            auto_recall=3,
            extract_on_exit=extract_on_exit,
        ),
    )
    service = MemoryService(config)
    service.initialize_files()
    runtime = AgentRuntime(
        service,
        model,
        SessionStore(config.vault),
        summarizer=FakeSummarizer(),
        config=config.chat,
    )
    runtime.start()
    return runtime, service


def test_corrupt_profile_does_not_block_chat_or_fact_recall(tmp_path):
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, service = build_runtime(tmp_path, model)
    service.note("合成偏好：喜欢安静阅读", topic="preferences")
    with transaction(service.paths) as db:
        db.execute("INSERT INTO profile VALUES(1,'saved','invalid')")
    events = []
    reply = runtime.ask("我喜欢安静阅读吗", on_event=events.append)
    assert reply.content == "收到"
    assert any("画像读取失败" in event for event in events)
    assert any("画像读取失败" in event for event in reply.events)
    assert "合成偏好" in str(model.calls[0][0])
    assert rows(service.paths, "profile")[0]["metadata"] == "invalid"


def test_start_defers_session_creation(tmp_path):
    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="收到")]))

    assert runtime.session is None
    store = SessionStore(service.config.vault)
    assert store.list() == []

    runtime.ask("你好")

    assert runtime.has_session
    assert len(store.list()) == 1


def test_natural_file_request_registers_tools_and_reads_without_memory_write(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("用户明确表示偏好合成茶饮", encoding="utf-8")
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="file1", name="read_file", arguments={"path": str(path.resolve())})
                ]
            ),
            AssistantTurn(content="这份笔记描述了茶饮偏好。"),
            AssistantTurn(content="收到"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0
    runtime.ask(f'请分析 "{path}"，不要写入记忆')
    record = next(record for record in runtime.current.records if record.get("type") == "tool")
    assert record["ok"]
    assert "合成茶饮" in record["content"]
    assert service.store.entries() == []
    runtime.ask("谢谢")
    assert "read_file" not in str(model.calls[-1][1])


def test_ask_redacts_credentials_before_model_and_session(tmp_path):
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0
    secret = "SYNTHETIC-ONLY-12345"
    runtime.ask(f"喜欢清淡。密码是 {secret}")
    assert secret not in str(model.calls)
    assert secret not in str(runtime.sessions.load(runtime.current.id).records)
    assert "喜欢清淡" in runtime.current.user_texts()[0]


@pytest.mark.parametrize("action", ["start", "resume"])
def test_session_change_clears_last_plan(tmp_path, action):
    runtime, _ = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="收到")]))
    runtime.ask("合成提问")
    session_id = runtime.current.id
    assert runtime.last_plan is not None

    if action == "start":
        runtime.start()
    else:
        runtime.resume(session_id)

    assert runtime.last_plan is None


def test_close_without_session_is_noop(tmp_path):
    runtime, _ = build_runtime(tmp_path, FakeChatModel([]))

    assert runtime.close()["admitted"] == 0


def test_failed_switch_preserves_exit_distillation(tmp_path):
    runtime, service = build_runtime(
        tmp_path, FakeChatModel([AssistantTurn(content="收到")]), extract_on_exit=True
    )
    queued = []
    runtime.on_distillation = lambda *args: queued.append(args)
    runtime.ask("喜欢合成口味甲")
    current = runtime.current
    with pytest.raises(KeyError):
        runtime.resume("s-20260101-000000-dead", close_current=True)
    assert runtime.current is current and not queued
    runtime.close()
    assert queued == [(current.id, current.last_user_message_id())]
    assert not service.current_facts()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_mutating_tool_timeout_reports_final_result(tmp_path, cancelled, fails):
    from concurrent.futures import TimeoutError

    from syvrio.agent.tools import ToolOutcome, ToolRegistry, ToolSpec

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    calls = []

    def write(arguments):
        if fails:
            raise ValueError("合成写入失败")
        service.note("合成记忆", topic="test")
        return ToolOutcome(True, "saved", "saved")

    class TimedOutFuture:
        def result(self, timeout=None):
            calls.append(timeout)
            if timeout is not None:
                raise TimeoutError()
            return write({})

        def cancel(self):
            return cancelled

    class Executor:
        def submit(self, handler, arguments):
            return TimedOutFuture()

    tools = ToolRegistry()
    tools._executors["default"] = Executor()
    tools.register(ToolSpec("write", "", {}, write, timeout=0.01, mutating=True))
    outcome = tools.execute("write", {})
    assert outcome.ok is (not cancelled and not fails)
    assert len(service.store.entries()) == int(not cancelled and not fails)
    assert calls == ([0.01] if cancelled else [0.01, None])


def test_memory_tools_are_marked_mutating(tmp_path):
    from syvrio.agent.tools import build_tools

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    tools = build_tools(service, runtime.ensure_session(), config=service.config)
    assert {name for name, spec in tools._tools.items() if spec.mutating} == {
        "memorize",
        "resolve_memory_proposals",
        "correct",
        "deprecate",
        "transition",
        "create_pending",
    }


def test_system_prompt_includes_current_date(tmp_path):
    from datetime import date

    runtime, _ = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="好")]))
    runtime.ask("今天几号")

    system_message = runtime.model.calls[0][0][0]["content"]
    assert date.today().isoformat() in system_message


def test_resume_restores_prior_messages_for_model(tmp_path):
    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="第一次回答")]))
    runtime.ask("第一问")
    session_id = runtime.current.id
    runtime.close()

    model = FakeChatModel([AssistantTurn(content="第二次回答")])
    resumed = AgentRuntime(
        service,
        model,
        SessionStore(service.config.vault),
        summarizer=FakeSummarizer(),
        config=service.config.chat,
    )
    resumed.resume(session_id)
    resumed.ask("第二问")

    contents = [message.get("content") for message in model.calls[0][0]]
    assert "第一问" in contents
    assert "第一次回答" in contents
    assert "第二问" in contents


def test_undo_excludes_original_turn_from_next_model_call(tmp_path):
    model = FakeChatModel([AssistantTurn(content="旧回答"), AssistantTurn(content="新回答")])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.ask("原始提问")

    assert runtime.undo() == "原始提问"
    runtime.ask("编辑后的提问")

    contents = [message.get("content") for message in model.calls[1][0]]
    assert "编辑后的提问" in contents
    assert "原始提问" not in contents
    assert "旧回答" not in contents
    assert runtime.current.user_texts() == ["编辑后的提问"]
    assert any(record.get("type") == "undo" for record in runtime.current.records)


def reviewed_calls(*operations):
    turns = []
    for index, (name, payload) in enumerate(operations):
        turns.append(AssistantTurn(tool_calls=[ToolCall(f"{index}-review", name, payload)]))
        if name == "import_file":
            turns.append(AssistantTurn(tool_calls=[ToolCall(f"{index}-apply", name, payload)]))
        else:
            turns.append(decision_turn(f"{index}-apply"))
    return turns + [AssistantTurn(content="已处理")]


def test_file_import_uses_model_selected_at_turn_start(tmp_path, monkeypatch):
    from dataclasses import replace

    path = tmp_path / "synthetic.txt"
    path.write_text("合成材料", encoding="utf-8")
    model = FakeChatModel(
        [
            AssistantTurn(tool_calls=[ToolCall("import", "import_file", {"path": str(path)})]),
            AssistantTurn(content="done"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    selected = service.config.llm
    calls = []

    def extractor(config, **kwargs):
        calls.append(config)
        return StaticExtractor(ExtractionResult(memories=[]))

    monkeypatch.setattr("syvrio.memory.service.ImportExtractor", extractor)

    def switch(phase):
        if phase == "Preparing context":
            service.config.llm = replace(selected, model="synthetic-next-model")

    runtime.ask(f'分析 "{path}"', on_progress=switch)
    assert calls == [selected]
    assert selected is not service.config.llm


def test_turn_undo_restores_vault_files_after_resume(tmp_path, monkeypatch):
    model = FakeChatModel([])
    runtime, service = build_runtime(tmp_path, model, max_steps=15)
    old = service.note("合成活动是读书", topic="activities")
    obsolete = service.note("合成计划是骑车", topic="plans")
    before = rows(service.paths, "audit")
    source = tmp_path / "synthetic-input.txt"
    source.write_text("合成进口资料", encoding="utf-8")
    proposal = Candidate(text="合成进口资料", topic="imported")
    monkeypatch.setattr(
        "syvrio.memory.service.ImportExtractor",
        lambda *a, **kw: StaticExtractor(ExtractionResult(memories=[proposal])),
    )
    actions = [
        ("memorize", {"text": "合成新增偏好", "topic": "preferences"}),
        ("correct", {"entry_id": old.id, "text": "合成活动是绘画", "topic": "activities"}),
        ("deprecate", {"entry_id": obsolete.id}),
        (
            "create_pending",
            {"text": "合成进口资料归属待核", "topic": "other", "question": "是谁的？"},
        ),
        ("import_file", {"path": str(source), "apply": True}),
    ]
    model.turns.extend(reviewed_calls(*actions))
    prompt = f'请处理合成样例并导入 "{source}"'
    runtime.ask(prompt)
    assert service.pending() and service.store.entries("imported")
    assert service.store.find(old.id).text == "合成活动是绘画"
    assert service.store.find(old.id).status == "active"
    assert service.store.find(obsolete.id).status == "deprecated"
    pre_undo = rows(service.paths, "audit")
    tool_changes = [
        event["seq"]
        for event in pre_undo[len(before) :]
        if event["action"] in {"add", "correct", "update"}
    ]
    assert len(tool_changes) == 4
    reopened = AgentRuntime(service, model, SessionStore(service.paths), config=service.config.chat)
    reopened.resume(runtime.current.id)
    assert reopened.undo() == prompt
    audit = rows(service.paths, "audit")
    assert audit[: len(pre_undo)] == pre_undo
    assert (
        sorted(row["reverts"] for row in audit if row["action"] == "undo_memory_change")
        == tool_changes
    )
    assert service.pending() == [] and "general/imported" not in {
        t.topic for t in service.store.topics()
    }
    assert [item.id for item in service.store.entries()] == [old.id, obsolete.id]
    assert source.read_text(encoding="utf-8") == "合成进口资料"
    assert any(row["action"] == "undo_tool_writes" for row in audit)


def test_turn_undo_leaves_all_files_and_messages_when_a_file_changed(tmp_path):
    operations = [
        ("memorize", {"text": f"合成资料 {topic}", "topic": topic}) for topic in ("alpha", "beta")
    ]
    model = FakeChatModel(reviewed_calls(*operations))
    runtime, service = build_runtime(tmp_path, model, max_steps=6)
    runtime.ask("合成请求")
    later = service.note("之后的独立修改", topic="beta")
    target = service.store.entries("beta")[0]
    service.deprecate(target.id)
    before = service.store.entries("alpha")
    with pytest.raises(TurnUndoError):
        runtime.undo()
    assert runtime.current.user_texts() == ["合成请求"]
    assert service.store.entries("alpha") == before and service.store.find(later.id)


def test_turn_undo_rejects_new_inference_source_without_partial_rollback(tmp_path):
    args = {"text": "样品采用蓝色分类", "topic": "samples"}
    model = FakeChatModel(reviewed_calls(("memorize", args)))
    runtime, service = build_runtime(tmp_path, model)
    runtime.ask(args["text"])
    source = service.store.entries()[0]
    service.propose_inference("合成推断", [source.id], "samples")
    names = ("session_events", "inferences", "memories", "audit", "undo_effects")
    before = {n: rows(service.paths, n) for n in names}
    with pytest.raises(TurnUndoError):
        runtime.undo()
    assert all(rows(service.paths, n) == before[n] for n in names)


def test_turn_undo_checks_persisted_tail_for_stale_session(tmp_path):
    runtime, _ = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="complete")]))
    runtime.ask("first")
    stale = runtime.sessions.load(runtime.current.id)
    target = stale.last_user_message_id()
    runtime.sessions.append(
        runtime.current, {"type": "message", "role": "user", "id": "later", "content": "second"}
    )
    before = runtime.sessions.load(runtime.current.id).records
    with pytest.raises(TurnUndoError, match="latest turn"):
        runtime.undo(session=stale, message_id=target)
    assert runtime.sessions.load(runtime.current.id).records == before


def test_memorize_tool_writes_with_session_provenance(tmp_path):
    args = {"text": "我住在合成街区", "topic": "housing"}
    model = FakeChatModel(reviewed_calls(("memorize", args)))
    runtime, service = build_runtime(tmp_path, model)
    runtime.ask(args["text"])
    (entry,) = service.store.entries()
    assert entry.source_session_id == runtime.current.id
    assert entry.source_message_id == runtime.current.last_user_message_id()
    assert service.read_memory_source(entry.id)["message"]["content"] == args["text"]
    evidence, success = [
        json.loads(row["content"]) for row in runtime.current.records if row.get("type") == "tool"
    ]
    assert evidence["status"] == "review_required" and evidence["results"] == []
    assert success["decisions"][0]["memory"]["id"] == entry.id


@pytest.mark.parametrize(
    "change", ["same_batch", "arguments", "concurrent_fact", "pending", "compression"]
)
def test_writes_wait_for_the_model_to_review_current_evidence(service, change):
    from syvrio.agent.evidence import RecallContext
    from syvrio.agent.tools import build_tools
    from syvrio.memory.models import MemoryDraft

    sessions = SessionStore(service.paths)
    session = sessions.create()
    sessions.append(
        session, {"type": "message", "id": "user-a", "role": "user", "content": "合成文件"}
    )
    guard = RecallContext(service)
    tools = build_tools(service, session, config=service.config, evidence=guard)
    pending = change == "pending"
    name = "create_pending" if pending else "memorize"
    args = {"topic": "tests", "text": "模拟审阅"}
    if pending:
        args["question"] = "发生过吗？"
    guard.begin_model_request()
    initial = tools.execute(name, args).data
    assert initial["status"] == "review_required"
    if change == "arguments":
        args["text"] = "模拟审阅改写"
    elif change == "concurrent_fact":
        service.note("其他会话模拟审阅", topic="outside")
    elif pending:
        service.create_pending(MemoryDraft(topic="tests", text="另一个模拟审阅"), "主题？", [])
    elif change == "compression":
        guard.invalidate()
    if change != "same_batch":
        guard.begin_model_request()
    renewed = tools.execute(name, args).data
    assert renewed["status"] == "review_required"
    if pending:
        assert len(renewed["pending"]) == 1 and "session_id" not in renewed["pending"][0]
    assert all(entry.text != args["text"] for entry in service.store.entries())
    guard.begin_model_request()
    decision = "pending" if pending else "memory"
    result = tools.execute(
        "resolve_memory_proposals",
        {"decisions": [{"proposal_id": renewed["proposal_id"], "decision": decision}]},
    )
    assert result.ok and decision in result.data["decisions"][0]


def test_recall_tool_result_reaches_model(tmp_path):
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})]
            ),
            AssistantTurn(content="你喜欢清淡风味。"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    service.note("喜欢清淡风味", topic="preferences")

    reply = runtime.ask("我喜欢什么风味？")

    assert reply.content == "你喜欢清淡风味。"
    second_call_messages = model.calls[1][0]
    tool_messages = [message for message in second_call_messages if message["role"] == "tool"]
    assert tool_messages and "清淡" in tool_messages[0]["content"]
    assert any("Recalled" in event for event in reply.events)


def test_recall_tool_default_and_explicit_limits(service):
    from syvrio.agent.tools import build_tools

    query = "旅行清单"
    for number in range(12):
        service.store.append("travel", f"{query}中的测试项目 {number}")
    ranked = [entry.id for entry in service.recall(query, limit=12)]
    assert len(ranked) == 12
    assert [entry.id for entry in service.recall(query)] == ranked[:10]

    tools = build_tools(service, SessionStore(service.paths).create(), config=service.config)
    schema = next(
        item["function"] for item in tools.schemas() if item["function"]["name"] == "recall"
    )
    assert schema["parameters"]["properties"]["limit"]["default"] == 10
    for arguments, expected in [({}, 10), ({"limit": 3}, 3), ({"limit": 12}, 12)]:
        result = tools.execute("recall", {"query": query, **arguments})
        assert result.ok
        assert [item["id"] for item in json.loads(result.content)["results"]] == ranked[:expected]


def test_reopening_session_with_interrupted_tool_results_does_not_reexecute(tmp_path, monkeypatch):
    candidate = Candidate(text="偏好使用合成测试资料。", topic="preferences")
    args = candidate.model_dump(include={"text", "topic"})
    model = FakeChatModel(
        [
            AssistantTurn(tool_calls=[ToolCall("prior-review", "memorize", args)]),
            decision_turn(
                "write",
                before=[ToolCall("read", "recall", {"query": "合成档案"})],
                content="Synthetic tool request",
            ),
            AssistantTurn(content="Synthetic resumed reply"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0
    append = runtime.sessions.append
    memorize = service.memorize
    writes = []

    def append_until_interrupted(session, record):
        if record.get("tool_call_id") == "write":
            raise OSError("Synthetic interruption before recording the tool result")
        return append(session, record)

    def count_write(*args, **kwargs):
        writes.append(1)
        return memorize(*args, **kwargs)

    monkeypatch.setattr(runtime.sessions, "append", append_until_interrupted)
    monkeypatch.setattr(service, "memorize", count_write)
    with pytest.raises(OSError, match="Synthetic interruption"):
        runtime.ask("Synthetic first request")
    interrupted = runtime.current
    original = runtime.sessions.load(interrupted.id).records
    assert [entry.text for entry in service.store.entries()] == [candidate.text]
    assert [r["tool_call_id"] for r in interrupted.records if r["type"] == "tool"] == [
        "prior-review",
        "read",
    ]

    monkeypatch.setattr(runtime.sessions, "append", append)
    runtime.start()
    runtime.resume(interrupted.id)
    assert runtime.ask("Synthetic next request").content == "Synthetic resumed reply"
    request = model.calls[-1][0]
    batch = next(
        i for i, message in enumerate(request) if message.get("content") == "Synthetic tool request"
    )
    assert [message["tool_call_id"] for message in request[batch + 1 : batch + 3]] == [
        "read",
        "write",
    ]
    assert request[batch + 1]["content"] == next(
        r["content"] for r in interrupted.records if r.get("tool_call_id") == "read"
    )
    assert "side effects are unknown" in request[batch + 2]["content"]
    assert request[batch + 3]["role"] == "user"
    assert writes == [1]
    assert runtime.sessions.load(interrupted.id).records[: len(original)] == original
    assert [r["tool_call_id"] for r in runtime.current.records if r["type"] == "tool"] == [
        "prior-review",
        "read",
    ]


def test_chat_search_tool_reads_other_sessions_without_echoing_query(tmp_path):
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="s1", name="search_chats", arguments={"query": "信封"})]
            ),
            AssistantTurn(content="在第七层。"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    store = SessionStore(service.paths)
    other = store.create()
    store.append(other, {"type": "message", "id": "m7", "role": "user", "content": "信封在第七层"})
    runtime.ask("以前在哪提过信封？")
    payload = next(m["content"] for m in model.calls[1][0] if m["role"] == "tool")
    assert "第七层" in payload and "m7" in payload
    assert "以前在哪" not in payload


def test_read_topic_tool_result_reaches_model(tmp_path):
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c1", name="read_topic", arguments={"topic": "preferences"})
                ]
            ),
            AssistantTurn(content="这是该主题的完整内容。"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    service.note("喜欢清淡风味", topic="preferences")
    service.note("晚上少量喝茶", topic="preferences")

    reply = runtime.ask("把偏好主题完整告诉我")

    second_call_messages = model.calls[1][0]
    tool_messages = [message for message in second_call_messages if message["role"] == "tool"]
    assert tool_messages and "清淡风味" in tool_messages[0]["content"]
    assert "晚上少量喝茶" in tool_messages[0]["content"]
    assert any('Read topic "general/preferences": 2 entries' in event for event in reply.events)


def test_auto_recall_injects_long_term_memory(tmp_path):
    model = FakeChatModel([AssistantTurn(content="记得。")])
    runtime, service = build_runtime(tmp_path, model)
    entry = service.note("喜欢清淡风味", topic="preferences")

    runtime.ask("我喜欢清淡吗")

    messages = model.calls[0][0]
    assert all("raw_markdown" not in m["content"] for m in messages if m["role"] == "system")
    i = next(
        i
        for i, m in enumerate(messages)
        if "<long_term_memory>" in m["content"] and m["role"] == "user"
    )
    assert entry.text in messages[i]["content"] and "raw_markdown" not in messages[i]["content"]
    assert messages[i + 1]["content"] == "我喜欢清淡吗"


def test_auto_recall_uses_recent_context_for_followup(tmp_path, monkeypatch):
    model = FakeChatModel([AssistantTurn(content="第一次"), AssistantTurn(content="第二次")])
    runtime, service = build_runtime(tmp_path, model)
    queries = []
    recall = service.recall

    def capture(query, limit=10, **kwargs):
        queries.append(query)
        return recall(query, limit, **kwargs)

    monkeypatch.setattr(service, "recall", capture)

    runtime.ask("我在看滨江的房子")
    runtime.ask("那附近便宜吗")

    assert "滨江" in queries[1]


def test_attribute_question_rewrites_with_synonyms(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from syvrio.agent.recall import RecallQueryBuilder
    from syvrio.infra.retrieval.keyword import KeywordRetriever
    from syvrio.memory.store import MemoryStore

    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")

    def fake_complete(config, messages, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="姓名 名字 用户称呼"))]
        )

    query = RecallQueryBuilder(LLMConfig(), completer=fake_complete).build("我叫什么名字", [])

    assert query.rewritten is True
    assert "姓名" in query.text

    store = MemoryStore(tmp_path / "vault")
    store.append("01-identity", "姓名：张三")
    store.append("preferences", "喜欢清淡风味")
    store.append("devices", "使用便携键盘")
    hits = KeywordRetriever(store).search(query.text)
    assert hits and hits[0].topic == "general/01-identity"


def test_original_short_field_reaches_model_via_prefetch_and_recall_tool(tmp_path, monkeypatch):
    from syvrio.agent.recall import RecallQuery
    from syvrio.infra.retrieval import HybridRetriever, KeywordRetriever, SearchHit

    target = "虚构人物甲"

    class SemanticNoise:
        def search(self, query, limit, *, snapshot=None):
            return [SearchHit(id=age.id, text=age.text, topic=age.topic)]

    class Model:
        def chat(self, messages, tools):
            if not any(message["role"] == "tool" for message in messages):
                assert any(
                    name.text in message["content"] and "<long_term_memory>" in message["content"]
                    for message in messages
                    if message["role"] == "user"
                )
                return AssistantTurn(
                    tool_calls=[
                        ToolCall(
                            id="check",
                            name="recall",
                            arguments={
                                "query": "姓名 名字 称呼",
                                "limit": 1,
                            },
                        )
                    ]
                )
            result = next(message["content"] for message in messages if message["role"] == "tool")
            fact = json.loads(result)["results"][0]
            assert fact["text"] == name.text and "raw_markdown" not in fact
            return AssistantTurn(content=fact["text"].split("姓名：", 1)[1])

    runtime, service = build_runtime(tmp_path, Model())
    name = service.note(f"姓名：{target}", topic="identity")
    age = service.note("年龄：41 岁", topic="identity")
    service.retriever = HybridRetriever(KeywordRetriever(service.store), SemanticNoise())
    runtime.config.auto_recall = 1
    monkeypatch.setattr(
        runtime.recall_queries,
        "build",
        lambda text, recent: RecallQuery("年龄", rewritten=True, original=text),
    )
    assert runtime.ask("我指的是姓名").content == target


@pytest.mark.parametrize(
    ("question", "scenario", "fast"),
    [
        ("我的姓名是什么？只回复姓名。", "unique", True),
        ("我的测试编号是什么？", "custom", True),
        ("我的姓名是什么？", "conflict", False),
        ("我的姓名是什么？", "future", False),
        ("我的姓名是什么？", "deleted", False),
        ("我的姓名和职业是什么？", "unique", False),
        ("那我的姓名是什么？", "unique", False),
        ("我叫什么名字？", "unique", False),
    ],
)
def test_auto_recall_skips_rewrite_only_for_direct_unique_current_field(
    tmp_path, monkeypatch, question, scenario, fast
):
    from syvrio.agent.recall import RecallQuery
    from syvrio.infra.retrieval import HybridRetriever, KeywordRetriever
    from syvrio.memory.models import MemoryContext

    rewritten = []
    vector_calls = []

    class Vector:
        def search(self, query, limit, *, snapshot=None):
            vector_calls.append(query)
            return []

    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, service = build_runtime(tmp_path, model)
    context = MemoryContext(valid_from="2099-01-01") if scenario == "future" else None
    label = "测试编号" if scenario == "custom" else "姓名"
    fact = service.store.append("identity", f"{label}：合成字段甲", context=context)
    if scenario == "conflict":
        service.note("姓名：合成字段乙", topic="identity")
    if scenario == "deleted":
        service.store.set_status(fact.id, "deprecated")
    service.retriever = HybridRetriever(KeywordRetriever(service.store), Vector())

    def rewrite(text, recent):
        rewritten.append(text)
        return RecallQuery("姓名", rewritten=True, original=text)

    monkeypatch.setattr(runtime.recall_queries, "build", rewrite)
    runtime.ask(question)
    if fast:
        assert rewritten == vector_calls == []
        assert any(fact.text in m["content"] for m in model.calls[0][0])
    else:
        assert rewritten == [question]
        assert vector_calls


def test_memory_cue_increases_auto_recall_limit(tmp_path, monkeypatch):
    model = FakeChatModel([AssistantTurn(content="记得。")])
    runtime, service = build_runtime(tmp_path, model)
    limits = []

    def capture(query, limit=10):
        limits.append(limit)
        return []

    monkeypatch.setattr(service, "recall", capture)

    runtime.ask("你还记得我的咖啡偏好吗")

    assert limits == [7]


def test_force_conclude_after_max_steps(tmp_path):
    class LoopModel:
        def __init__(self):
            self.tool_rounds = []

        def chat(self, messages, tools):
            self.tool_rounds.append(bool(tools))
            if tools:
                return AssistantTurn(
                    tool_calls=[ToolCall(id="c1", name="memorize", arguments={"text": "不断调用"})]
                )
            return AssistantTurn(content="收敛回答")

    model = LoopModel()
    runtime, _ = build_runtime(tmp_path, model, max_steps=1)

    reply = runtime.ask("开始")

    assert reply.content == "收敛回答"
    assert model.tool_rounds == [True, False]
    assert any("forced wrap-up" in event for event in reply.events)


def test_retries_transient_model_failure(tmp_path):
    class FlakyModel:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return AssistantTurn(content="恢复了")

    model = FlakyModel()
    runtime, _ = build_runtime(tmp_path, model)

    reply = runtime.ask("你好")

    assert reply.content == "恢复了"
    assert model.calls == 2


def test_close_enqueues_once_and_returns_before_extraction(tmp_path):
    runtime, service = build_runtime(
        tmp_path, FakeChatModel([AssistantTurn(content="好的。")]), extract_on_exit=True
    )
    queued = []
    runtime.on_distillation = lambda *args: queued.append(args)
    runtime.ask("这是一条值得记住的事实")
    session = runtime.current
    runtime.close()
    runtime.close()
    assert queued == [(session.id, session.last_user_message_id())]
    assert not service.current_facts()


def test_reload_llm_updates_chat_model_and_context(tmp_path, monkeypatch):
    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="ok")]))
    new_llm = LLMConfig(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        model="deepseek-ai/DeepSeek-V3",
    )
    monkeypatch.setattr(
        "syvrio.agent.runtime.load_config",
        lambda *args, **kwargs: Config(vault=service.config.vault, llm=new_llm),
    )

    runtime.reload_llm()

    assert runtime.model_label == "siliconflow/deepseek-ai/DeepSeek-V3"
    assert service.config.llm is new_llm
    assert runtime.context.config is service.config.chat


def test_ask_streams_events_to_callback(tmp_path):
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})]
            ),
            AssistantTurn(content="你喜欢清淡风味。"),
        ]
    )
    runtime, service = build_runtime(tmp_path, model)
    service.note("喜欢清淡风味", topic="preferences")

    received = []
    reply = runtime.ask("我喜欢什么风味？", on_event=received.append)

    assert any("Recalled" in event for event in received)
    assert received == reply.events


def _config_with_vault(tmp_path_factory=None):
    import pathlib
    import tempfile

    from syvrio.infra.config import Config, LLMConfig, RetrievalConfig

    tmp = pathlib.Path(tempfile.mkdtemp())
    return Config(vault=tmp / "vault", llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword"))


def test_recall_tool_guides_query_with_synonyms():
    """身份类查询召回：自然问句还要带常见同义词/字段名，避免“名字”漏掉“姓名”。"""
    from syvrio.agent.tools import build_tools
    from syvrio.infra.session import SessionStore
    from syvrio.memory.service import MemoryService

    config = _config_with_vault()
    service = MemoryService(config)
    service.initialize_files()
    session = SessionStore(config.vault).create()
    spec = build_tools(service, session, config=service.config)._tools["recall"]
    assert "同义词" in spec.description
    assert "自然问句" in spec.parameters["properties"]["query"]["description"]
    assert "姓名" in spec.parameters["properties"]["query"]["description"]
    assert "不要拼凑关键词" not in spec.description


def test_import_prompt_forbids_section_prefix():
    """身份类查询召回缺口的修复之二：导入条目不带小节前缀，超短条目正文即事实。"""
    from syvrio.memory.extractor import IMPORT_PROMPT

    assert "小节编号或标题前缀" in IMPORT_PROMPT
    assert "超短条目" in IMPORT_PROMPT


def test_system_prompt_default_without_soul(tmp_path):
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)

    runtime.ask("你好")

    content = model.calls[0][0][0]["content"]
    assert "个人记忆助手" in content
    assert "可用工具：" in content


def test_soul_md_replaces_persona_keeps_mechanism(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "SOUL.md").write_text("你是阿澈，语气简洁带冷幽默。", encoding="utf-8")
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)

    runtime.ask("你好")

    content = model.calls[0][0][0]["content"]
    assert "阿澈" in content
    assert "个人记忆助手" not in content
    assert content.index("阿澈") < content.index("规则：")


def test_soul_md_hot_reload_next_turn(tmp_path):
    model = FakeChatModel([AssistantTurn(content="一"), AssistantTurn(content="二")])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.ask("第一句")

    (tmp_path / "vault" / "SOUL.md").write_text("你是测试人设B。", encoding="utf-8")
    runtime.ask("第二句")

    assert "测试人设B" not in model.calls[0][0][0]["content"]
    assert "测试人设B" in model.calls[1][0][0]["content"]


def test_soul_md_truncated_by_tokens(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "SOUL.md").write_text("长" * 8000, encoding="utf-8")
    model = FakeChatModel([AssistantTurn(content="收到")])
    runtime, _ = build_runtime(tmp_path, model)

    runtime.ask("你好")

    content = model.calls[0][0][0]["content"]
    assert len(content) < 9000
    assert "规则：" in content


def test_init_vault_seeds_default_soul(tmp_path):
    from syvrio.backend.maintenance import initialize_vault

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    initialize_vault(service.config, force=True)

    soul = service.config.vault / "SOUL.md"
    assert "个人记忆助手" in soul.read_text(encoding="utf-8")


def _resident_for_test(runtime, service, monkeypatch, call=None):
    from syvrio.memory.background import BackgroundMemory, distillation
    from syvrio.memory.models import MemoryDraft

    owner = BackgroundMemory(service)
    runtime.on_distillation = owner.request_distillation

    def extract(_service, _prompt, data, schema):
        if call:
            return call(data, schema)
        return schema(memories=[MemoryDraft(text=data["source_text"], topic="test")])

    monkeypatch.setattr(distillation, "ask", extract)
    return owner


def _user(sessions, session, mid, text):
    sessions.append(session, {"type": "message", "id": mid, "role": "user", "content": text})


def _drain(owner, session_id):
    from threading import Event

    from syvrio.memory.background import distillation
    from syvrio.memory.background.jobs import job_key, read_job

    path = job_key(owner.service.paths, "distillation", session_id)
    for _ in range(50):
        if not read_job(path):
            break
        assert distillation.process(owner.service, owner.sessions, path, owner.busy, Event())
    assert not read_job(path)


def test_resident_discovers_session_that_crashed_before_close(tmp_path, monkeypatch):
    from threading import Event

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.sessions.create()
    _user(runtime.sessions, session, "source", "我喜欢清淡风味")
    owner._discover(Event())
    _drain(owner, session.id)
    assert service.store.has_text("我喜欢清淡风味")
    assert not runtime.sessions.load(session.id).needs_consolidation()


def test_catchup_covers_messages_added_after_resume_then_crash(tmp_path, monkeypatch):
    from threading import Event

    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    _user(runtime.sessions, session, "first", "第一条事实")
    runtime.close()
    _drain(owner, session.id)
    current = runtime.resume(session.id)
    _user(runtime.sessions, current, "second", "第二条事实")
    owner._discover(Event())
    _drain(owner, session.id)
    assert [(f.source_message_id, f.text) for f in service.current_facts()] == [
        ("first", "第一条事实"),
        ("second", "第二条事实"),
    ]


def test_background_distills_every_chunk_of_a_long_source(tmp_path, monkeypatch):
    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    seen = []

    def capture(data, schema):
        seen.append(data["source_text"])
        return schema(memories=[])

    owner = _resident_for_test(runtime, service, monkeypatch, capture)
    session = runtime.ensure_session()
    text = "整条消息" * 3600
    _user(runtime.sessions, session, "long", text)
    runtime.close()
    _drain(owner, session.id)
    assert len(seen) > 1 and "".join(seen) == text
    assert not runtime.sessions.load(session.id).needs_consolidation()


def test_backend_does_not_enqueue_session_without_user_messages(tmp_path, monkeypatch):
    from threading import Event

    from syvrio.memory.background.jobs import list_jobs

    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    owner = _resident_for_test(runtime, service, monkeypatch)
    runtime.ensure_session()
    runtime.close()
    owner._discover(Event())
    assert not list(list_jobs(service.paths, "distillation"))


def test_close_respects_extract_on_exit_but_allows_background_recovery(tmp_path, monkeypatch):
    from threading import Event

    from syvrio.memory.background.jobs import job_key, read_job

    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=False)
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    _user(runtime.sessions, session, "x", "待恢复事实")
    runtime.close()
    assert not read_job(job_key(service.paths, "distillation", session.id))
    owner._discover(Event())
    _drain(owner, session.id)
    assert service.store.has_text("待恢复事实")


def test_close_enqueue_failure_preserves_messages_for_startup(tmp_path, monkeypatch):
    from threading import Event

    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    _user(runtime.sessions, session, "pending", "退出后恢复")

    def fail(*args):
        raise OSError("synthetic")

    runtime.on_distillation = fail
    runtime.close()
    assert runtime.sessions.load(session.id).needs_consolidation()
    owner._discover(Event())
    _drain(owner, session.id)
    assert service.store.has_text("退出后恢复")


def test_ask_streams_reply_deltas(tmp_path):
    model = FakeChatModel([AssistantTurn(content="你好世界")])
    runtime, _ = build_runtime(tmp_path, model)
    deltas: list[str] = []

    reply = runtime.ask("测试", on_delta=deltas.append)

    assert deltas == ["你好", "世界"]
    assert reply.content == "你好世界"
    # 会话记录仍是完整内容
    session = runtime.current
    assert any(
        record.get("type") == "assistant" and record.get("content") == "你好世界"
        for record in session.records
    )


def test_streaming_failure_does_not_retry_or_duplicate(tmp_path):
    # 回归：流中断后循环级重试会让界面收到重复 delta，必须单次直败。

    class StreamBoom(FakeChatModel):
        def chat(self, messages, tools, on_delta=None):
            self.calls.append((messages, tools))
            if on_delta is not None:
                on_delta("半截")
            raise RuntimeError("连接中断")

    model = StreamBoom([])
    runtime, _ = build_runtime(tmp_path, model)
    deltas: list[str] = []

    with pytest.raises(ChatModelError):
        runtime.ask("测试", on_delta=deltas.append)

    assert len(model.calls) == 1  # 未重试
    assert deltas == ["半截"]  # 已发内容不重复


def test_fast_switch_registers_same_durable_job_as_close(tmp_path, monkeypatch):
    from syvrio.memory.background.jobs import job_key, read_job

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    _user(runtime.sessions, session, "m", "切换时保留")
    runtime.close(distill=False)
    assert read_job(job_key(service.paths, "distillation", session.id))
    assert not service.current_facts()
    _drain(owner, session.id)
    assert service.store.has_text("切换时保留")


def test_coalesced_requests_keep_the_latest_original_message_boundary(tmp_path, monkeypatch):
    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    for mid in ("a", "b", "c"):
        _user(runtime.sessions, session, mid, "事实" + mid)
    for mid in ("a", "b", "a"):
        owner.request_distillation(session.id, mid)
    _drain(owner, session.id)
    assert [f.source_message_id for f in service.current_facts()] == ["a", "b"]
    assert [r["id"] for r in runtime.sessions.load(session.id).undistilled_messages()] == ["c"]


def test_long_search_results_share_the_whole_session_window(tmp_path, monkeypatch):
    from syvrio.agent.tools import ToolOutcome, ToolRegistry, ToolSpec
    from syvrio.infra.llm.tokens import estimate_tokens

    # 三项都超过 2K，合计超过 50K，但仍能一起放入 200K 窗口。
    results = [
        json.dumps({"text": " ref" * 34_000, "url": f"https://example.org/{i}"}) for i in range(3)
    ]
    assert 100_000 < sum(map(estimate_tokens, results)) < 110_000
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "search",
            "Search",
            {},
            lambda args: ToolOutcome(True, results[args["index"]], "searched"),
        )
    )
    monkeypatch.setattr("syvrio.agent.runtime.build_tools", lambda *args, **kwargs: registry)
    monkeypatch.setattr(
        "syvrio.infra.llm.client.model_metadata", lambda *_: {"max_input_tokens": 200_000}
    )
    calls = [ToolCall(id=str(i), name="search", arguments={"index": i}) for i in range(3)]
    model = FakeChatModel([AssistantTurn(tool_calls=calls), AssistantTurn(content="Done")])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.context_window = 200_000
    runtime.config.auto_recall = 0

    reply = runtime.ask("Compare the three sources.")

    assert not reply.compacted
    assert [msg["content"] for msg in model.calls[1][0] if msg["role"] == "tool"] == results
    records = runtime.sessions.load(runtime.current.id).records
    assert [r["content"] for r in records if r["type"] == "tool"] == results


def schema_cost(runtime, paths=()):
    from syvrio.agent.tools import build_tools
    from syvrio.infra.llm.tokens import estimate_tokens

    schemas = build_tools(
        runtime.service, runtime.ensure_session(), config=runtime.service.config, file_paths=paths
    ).schemas()
    return estimate_tokens(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))


@pytest.mark.parametrize("oversize", ["tools", "text", "system", "image"])
def test_small_window_rejects_oversized_fixed_input_before_model(tmp_path, monkeypatch, oversize):
    from io import BytesIO

    from PIL import Image

    from syvrio.infra.session.attachments import AttachmentBatch

    model = FakeChatModel([])
    runtime, _ = build_runtime(tmp_path, model)
    cost = schema_cost(runtime)
    runtime.config.context_window = (
        2000 if oversize == "tools" else runtime.config.reserve_output_tokens + cost + 512
    )
    runtime.config.auto_recall = 0
    monkeypatch.setattr(
        runtime, "_system_prompt", lambda: "系统" * (900 if oversize == "system" else 1)
    )
    text = "问题" * (900 if oversize == "text" else 1)
    attachments = None
    if oversize == "image":
        image = BytesIO()
        Image.new("RGB", (1, 1)).save(image, format="PNG")
        image.seek(0)
        attachments = AttachmentBatch.stage(
            runtime.sessions.attachments_dir, [("sample.png", image)]
        )

    with pytest.raises(
        ChatModelError, match=rf"{cost} for tool definitions.*Increase the context window"
    ):
        runtime.ask(text, attachments=attachments)

    assert not model.calls
    assert runtime.sessions.load(runtime.current.id).user_texts() == [text]


def test_real_tool_budget_triggers_history_compaction_and_counts_toward_meter(
    tmp_path, monkeypatch
):
    from syvrio.infra.llm.tokens import estimate_messages_tokens

    runtime, _ = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="完成")]))
    monkeypatch.setattr(runtime, "_system_prompt", lambda: "系统")
    cost = schema_cost(runtime)
    runtime.config.context_window = runtime.config.reserve_output_tokens + cost + 512
    runtime.config.auto_recall = 0
    session = runtime.current
    for index in range(6):
        _user(runtime.sessions, session, f"u{index}", "合成历史" * 65)
        runtime.sessions.append(session, {"type": "assistant", "content": "收到"})
    usage = runtime.context_usage()
    assert usage > runtime.context.input_budget > usage - cost
    assert usage == runtime.context.estimate_usage(session, "系统") + cost

    reply = runtime.ask("继续")
    assert reply.compacted and reply.estimated_tokens <= runtime.context.input_budget
    sent = runtime.last_plan.messages[:-1]  # 省略分发后追加的最终回答。
    assert reply.estimated_tokens == estimate_messages_tokens(sent) + cost


def test_usage_meter_reuses_tool_cost_until_settings_change(tmp_path, monkeypatch):
    from syvrio.agent import runtime as runtime_module

    runtime, _ = build_runtime(tmp_path, FakeChatModel([]))
    session = runtime.ensure_session()
    builds = []
    original = runtime_module.build_tools

    def count_build(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "build_tools", count_build)
    first = runtime.estimate_usage(session)
    assert runtime.estimate_usage(session) == first
    assert len(builds) == 1
    runtime.service.config.search.enabled = False
    smaller = runtime.estimate_usage(session)
    assert smaller < first
    assert runtime.estimate_usage(session) == smaller
    assert len(builds) == 2

    runtime.service.config.retrieval.default_limit = 123456789
    updated = runtime.estimate_usage(session)
    assert updated != smaller
    assert runtime.estimate_usage(session) == updated
    assert len(builds) == 3


@pytest.mark.parametrize(
    ("turns", "above_threshold"), [(1, 0), (1, 1), (2, 0)], ids=["at", "above", "all-turns"]
)
def test_manual_compaction_summarizes_all_history_at_the_context_threshold(
    tmp_path, turns, above_threshold
):
    responses = [
        AssistantTurn(
            tool_calls=[ToolCall(id="topic", name="read_topic", arguments={"topic": "sample"})]
        ),
        AssistantTurn(content="已读完合成资料。"),
    ]
    if turns == 2:
        responses.append(AssistantTurn(content="标记是蓝色。"))
    model = FakeChatModel(responses)
    runtime, service = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0
    service.note("蓝色样本标记", topic="sample")
    runtime.ask("请阅读样本主题。")
    if turns == 2:
        runtime.ask("标记是什么颜色？")
    session = runtime.current
    original = runtime.sessions.load(session.id).active_records()
    assert len(session.user_texts()) == turns
    assert original[2]["ok"] and "蓝色样本标记" in original[2]["content"]

    messages_only = runtime.context.estimate_usage(session, runtime._system_prompt())
    full_input = messages_only + schema_cost(runtime)
    assert full_input == runtime.context_usage()
    runtime.config.compact_threshold = 0.5
    runtime.config.context_window = runtime.config.reserve_output_tokens + 2 * (full_input + 1)
    assert runtime.turn_settings().context.trigger_tokens == full_input + 1
    phases = []
    notice = " ".join(runtime.force_compact(on_compaction=phases.append)).replace(",", "")
    assert str(full_input) in notice and str(full_input + 1) in notice
    assert phases == [] and session.latest_summary() is None

    runtime.config.context_window -= 2 * (1 + above_threshold)
    threshold = runtime.turn_settings().context.trigger_tokens
    assert messages_only < threshold == full_input - above_threshold
    runtime.force_compact(on_compaction=phases.append)
    assert phases == ["running", "done"]
    saved = runtime.sessions.load(session.id)
    assert saved.summary_text() == "会话摘要"
    assert saved.active_records() == [*original, saved.latest_summary()]
    assert saved.records_after_summary() == []

    phases.clear()
    runtime.force_compact(on_compaction=phases.append)
    assert phases == []
    assert runtime.sessions.load(session.id).records == saved.records


@pytest.mark.parametrize("max_steps", [1, 4], ids=["wrapup", "followup"])
def test_overflowing_tool_batch_uses_session_summary_and_keeps_original(
    tmp_path, monkeypatch, max_steps
):
    path = tmp_path / "sample.txt"
    page = "资料" * 1999 + "终点"
    path.write_text(page + "续页", encoding="utf-8")
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": str(path)})]
            ),
            AssistantTurn(content="已读"),
        ]
    )
    runtime, _ = build_runtime(tmp_path, model, max_steps=max_steps)
    # 即使收尾请求没有携带工具定义，也要覆盖小窗口的情况。
    monkeypatch.setattr(runtime, "_system_prompt", lambda: "系统")
    runtime.config.context_window = (
        runtime.config.reserve_output_tokens + schema_cost(runtime, [path]) + 512
    )
    runtime.config.auto_recall = 0
    summaries = []

    def summarize(previous, transcript, max_chars):
        summaries.append(transcript)
        return "已读到终点"

    monkeypatch.setattr(runtime.summarizer, "summarize", summarize)
    phases = []
    question = f'请读取 "{path}"'
    reply = runtime.ask(question, on_compaction=phases.append)

    assert reply.compacted and phases == ["running", "done"]
    assert len(summaries) == 1 and page in summaries[0] and question in summaries[0]
    sent = model.calls[1][0]
    assert any(m["content"] == question for m in sent)
    assert "已读到终点" in str(sent)
    if max_steps == 1:
        assert "不要再调用工具" in str(sent) and not model.calls[1][1]
    records = runtime.sessions.load(runtime.current.id).records
    tool = next(r for r in records if r.get("tool_call_id") == "c1")
    payload = json.loads(tool["content"])
    assert payload["text"] == page + "续页" and payload["next_offset"] is None


def test_small_window_summarizes_the_whole_batch_without_splitting_tool_pairs(
    tmp_path, monkeypatch
):
    from syvrio.infra.llm.tokens import estimate_messages_tokens, estimate_tokens

    oldest = tmp_path / "long.txt"
    oldest.write_text("合成旧资料" * 350, encoding="utf-8")
    latest = tmp_path / "short.txt"
    latest.write_text("近期合成资料", encoding="utf-8")
    calls = [
        ToolCall(id=f"c{i}", name="read_file", arguments={"path": str(path)})
        for i, path in enumerate((oldest, latest, latest))
    ]
    sent_sizes = []

    class RecordingModel(FakeChatModel):
        def chat(self, messages, tools, on_delta=None):
            sent_sizes.append(
                estimate_messages_tokens(messages)
                + estimate_tokens(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
            )
            return super().chat(messages, tools, on_delta)

    runtime, _ = build_runtime(
        tmp_path,
        RecordingModel(
            [
                AssistantTurn(tool_calls=calls),
                AssistantTurn(content="已读"),
            ]
        ),
    )
    runtime.config.context_window = (
        runtime.config.reserve_output_tokens + schema_cost(runtime, [oldest, latest]) + 1000
    )
    runtime.config.auto_recall = 0
    monkeypatch.setattr(runtime, "_system_prompt", lambda: "系统")
    reply = runtime.ask(f'请读取 "{oldest}" 和 "{latest}"')

    assert reply.content == "已读" and len(sent_sizes) == 2
    assert max(sent_sizes) <= runtime.context.input_budget
    assert reply.compacted
    assert not any(m.get("tool_calls") or m["role"] == "tool" for m in runtime.last_plan.messages)
    assert "会话摘要" in str(runtime.last_plan.messages)
    saved = [r for r in runtime.sessions.load(runtime.current.id).records if r["type"] == "tool"]
    assert [r["tool_call_id"] for r in saved] == ["c0", "c1", "c2"]
    assert "合成旧资料" * 350 in saved[0]["content"]
    assert all("近期合成资料" in r["content"] for r in saved[1:])


def test_wrapup_only_charges_for_messages_when_no_tools_are_sent(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_text("合成参考材料" * 220, encoding="utf-8")
    model = FakeChatModel(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": str(path)})]
            ),
            AssistantTurn(content="已读"),
        ]
    )
    runtime, _ = build_runtime(tmp_path, model, max_steps=1)
    runtime.config.auto_recall = 0
    runtime.config.context_window = (
        runtime.config.reserve_output_tokens + schema_cost(runtime, [path]) + 512
    )
    monkeypatch.setattr(runtime, "_system_prompt", lambda: "系统")
    assert runtime.ask(f'请读取 "{path}"').content == "已读"
    assert model.calls[0][1] and model.calls[1][1] == []


def test_resident_defers_distillation_until_the_generating_session_is_idle(tmp_path, monkeypatch):
    from threading import Event

    from syvrio.memory.background import distillation
    from syvrio.memory.background.jobs import job_key, read_job

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    _user(runtime.sessions, session, "active", "等待本回合结束")
    owner._discover(Event())
    path = job_key(service.paths, "distillation", session.id)
    assert not distillation.process(service, owner.sessions, path, lambda _: True, Event())
    assert read_job(path) and not service.current_facts()
    _drain(owner, session.id)
    assert service.store.has_text("等待本回合结束")


def test_failed_compaction_extraction_can_be_resumed_without_losing_progress(tmp_path, monkeypatch):
    from threading import Event

    from syvrio.memory.background import distillation
    from syvrio.memory.background.jobs import job_key, read_job

    runtime, service = build_runtime(tmp_path, FakeChatModel([]), extract_on_exit=True)
    owner = _resident_for_test(runtime, service, monkeypatch)
    session = runtime.ensure_session()
    for n in range(8):
        _user(runtime.sessions, session, str(n), "一条很长的历史。" * 100)
        runtime.sessions.append(
            session, {"type": "assistant", "id": f"reply{n}", "content": "收到"}
        )
    runtime.config.context_window = runtime.config.reserve_output_tokens + runtime.context_usage()
    runtime.force_compact()
    assert session.latest_summary()["records"] == 16
    path = job_key(service.paths, "distillation", session.id)
    assert read_job(path)
    with monkeypatch.context() as patch:

        def fail(*args):
            raise TimeoutError

        patch.setattr(distillation, "ask", fail)
        with pytest.raises(TimeoutError):
            distillation.process(service, owner.sessions, path, owner.busy, Event())
    assert runtime.sessions.load(session.id).latest_summary()["distilled_ids"] == []
    assert len(runtime.sessions.load(session.id).undistilled_messages()) == 8
    runtime.close()
    _drain(owner, session.id)
    assert not runtime.sessions.load(session.id).needs_consolidation()


def test_startup_discovers_all_sessions_without_starving_older_ones(tmp_path, monkeypatch):
    from threading import Event

    from syvrio.memory.background.jobs import list_jobs

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    owner = _resident_for_test(runtime, service, monkeypatch)
    for n in range(6):
        session = runtime.sessions.create()
        _user(runtime.sessions, session, f"m{n}", f"合成事实 {n}")
    owner._discover(Event())
    assert len(list(list_jobs(service.paths, "distillation"))) == 6


def test_general_question_skips_automatic_memory(tmp_path, monkeypatch):
    runtime, service = build_runtime(tmp_path, FakeChatModel([AssistantTurn(content="答案")]))
    monkeypatch.setattr(service, "recall", lambda *a, **k: pytest.fail("不应预取记忆"))
    monkeypatch.setattr(runtime.recall_queries, "build", lambda *a: pytest.fail("不应改写查询"))
    runtime.ask("解释 Python 的列表推导式")


def test_progress_precedes_model_and_tool_execution(tmp_path, monkeypatch):
    from syvrio.agent.tools import ToolOutcome, ToolRegistry

    progress = []

    class ProgressModel(FakeChatModel):
        def chat(self, messages, tools, on_delta=None):
            assert progress[-1] == "Waiting for model"
            return super().chat(messages, tools, on_delta)

    def execute(registry, name, arguments):
        assert progress[-1] == f"Calling tool: {name}"
        return ToolOutcome(ok=True, content="合成结果", display="工具完成")

    monkeypatch.setattr(ToolRegistry, "execute", execute)
    runtime, _ = build_runtime(
        tmp_path,
        ProgressModel(
            [
                AssistantTurn(
                    tool_calls=[ToolCall(id="t1", name="recall", arguments={"query": "合成"})]
                ),
                AssistantTurn(content="完成"),
            ]
        ),
    )
    runtime.ask("你好", on_progress=progress.append)
    assert progress == [
        "Preparing context",
        "Waiting for model",
        "Calling tool: recall",
        "Waiting for model",
    ]


def test_network_batch_runs_eight_calls_at_once():
    from threading import Barrier

    from syvrio.agent.runtime import _tool_results
    from syvrio.agent.tools import ToolOutcome, ToolRegistry, ToolSpec

    overlap = Barrier(8, timeout=4)

    def search(args):
        overlap.wait()
        return ToolOutcome(True, str(args["index"]), "searched")

    registry = ToolRegistry()
    registry.register(ToolSpec("search", "", {}, search, timeout=5, executor="network"))
    calls = [ToolCall(id=str(i), name="search", arguments={"index": i}) for i in range(8)]
    results = list(_tool_results(registry, calls, None, None))
    assert [(call.id, outcome.content) for call, outcome in results if outcome.ok] == [
        (str(i), str(i)) for i in range(8)
    ]


def test_network_tools_share_a_batch_but_keep_results_and_writes_ordered(tmp_path, monkeypatch):
    from threading import Barrier, Event

    from syvrio.agent import runtime as module
    from syvrio.agent.tools import ToolOutcome, ToolRegistry, ToolSpec

    overlap = Barrier(2, timeout=3)
    fetch_finished = Event()
    search_finished = Event()
    write_finished = Event()

    def search(args):
        if args.get("after_write"):
            assert write_finished.is_set()
        else:
            overlap.wait()
            assert fetch_finished.wait(3)
            search_finished.set()
        return ToolOutcome(True, "search result", "searched")

    def fetch(args):
        overlap.wait()
        fetch_finished.set()
        return ToolOutcome(True, "fetch result", "fetched")

    def write(args):
        assert search_finished.is_set() and fetch_finished.is_set()
        write_finished.set()
        return ToolOutcome(True, "write result", "wrote")

    registry = ToolRegistry()
    registry.register(ToolSpec("search", "", {}, search, timeout=4, executor="network"))
    registry.register(ToolSpec("fetch_web", "", {}, fetch, timeout=4, executor="network"))
    registry.register(ToolSpec("memorize", "", {}, write, timeout=4, mutating=True))
    monkeypatch.setattr(module, "build_tools", lambda *args, **kwargs: registry)
    calls = [
        ToolCall(id="one", name="search", arguments={}),
        ToolCall(id="two", name="fetch_web", arguments={}),
        ToolCall(id="three", name="memorize", arguments={}),
        ToolCall(id="four", name="search", arguments={"after_write": True}),
    ]
    model = FakeChatModel([AssistantTurn(tool_calls=calls), AssistantTurn(content="done")])
    runtime, _ = build_runtime(tmp_path, model)
    assert runtime.ask("hello").content == "done"
    records = [r for r in runtime.current.records if r["type"] == "tool"]
    assert [r["tool_call_id"] for r in records] == ["one", "two", "three", "four"]
    assert all(r["ok"] for r in records)
    messages = model.calls[1][0]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "one",
        "two",
        "three",
        "four",
    ]


@pytest.mark.parametrize("stop_at", ["context", "stream", "tool"])
def test_cancel_preserves_partial_and_tool_pairs(tmp_path, monkeypatch, stop_at):
    from threading import Event

    from syvrio.agent import runtime as module
    from syvrio.agent.tools import ToolOutcome
    from syvrio.infra.llm import GenerationCancelled

    cancel = Event()
    calls = []

    class Tools:
        def schemas(self):
            return []

        def execute(self, name, arguments):
            calls.append(name)
            cancel.set()
            return ToolOutcome(ok=True, content="done", display="done")

    monkeypatch.setattr(module, "build_tools", lambda *a, **kw: Tools())
    turn = AssistantTurn(
        content="partial text",
        tool_calls=[
            ToolCall(id="one", name="first", arguments={}),
            ToolCall(id="two", name="second", arguments={}),
        ]
        if stop_at == "tool"
        else [],
    )
    model = FakeChatModel([turn])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.recall_rewrite = False

    def progress(phase):
        if stop_at == "context":
            cancel.set()

    def delta(text):
        if stop_at == "stream":
            cancel.set()

    with pytest.raises(GenerationCancelled):
        runtime.ask("hello", on_delta=delta, on_progress=progress, cancel=cancel)
    loaded = runtime.sessions.load(runtime.current.id)
    stopped = loaded.records[-1]
    assert "stopped by user" in stopped["content"]
    assert runtime._active_ask is None
    if stop_at == "stream":
        assert stopped["content"].startswith("partia\n\n")
        assert len(model.calls) == 1
    if stop_at == "tool":
        assert calls == ["first"]
        results = [r for r in loaded.records if r.get("type") == "tool"]
        assert [r["tool_call_id"] for r in results] == ["one", "two"]
        assert results[1]["ok"] is False
    runtime.model = FakeChatModel([AssistantTurn(content="next reply")])
    assert runtime.ask("continue", on_delta=lambda _: None).content == "next reply"


def test_startup_consolidation_excludes_archived_session(tmp_path, caplog, monkeypatch):
    from threading import Event

    runtime, service = build_runtime(tmp_path, FakeChatModel([]))
    owner = _resident_for_test(runtime, service, monkeypatch)
    healthy = runtime.sessions.create()
    _user(runtime.sessions, healthy, "m-good", "合成事实")
    broken = runtime.sessions.create()
    _user(runtime.sessions, broken, "m-stopped", "archived conversation")
    runtime.sessions.set_archived(broken.id, True)
    owner._discover(Event())
    _drain(owner, healthy.id)
    assert service.store.has_text("合成事实")
    assert not runtime.sessions.load(healthy.id).needs_consolidation()
    assert runtime.sessions.load(broken.id, archived=True).user_texts() == ["archived conversation"]


def test_chat_correct_rejects_stale_revision_before_evidence_review(service):
    from syvrio.agent.tools import build_tools
    from syvrio.memory.models import MemoryCorrection

    sessions = SessionStore(service.paths)
    session = sessions.create()
    sessions.append(session, {"type": "message", "role": "user", "content": "记忆修正"})
    tools = build_tools(service, session, config=service.config)
    old = service.note("初始事实", topic="tests")
    current = service.correct(old.id, MemoryCorrection(text="已核准事实"))

    result = tools.execute(
        "correct",
        {
            "entry_id": old.id,
            "text": "过期提案",
            "expected_revision": old.revision,
        },
    )

    assert not result.ok and "memory changed" in result.content.lower()
    assert service.store.find(old.id) == current
