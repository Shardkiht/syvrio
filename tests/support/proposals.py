"""模拟模型从上一轮工具回执读取提案 ID。"""

import json

from syvrio.infra.llm import AssistantTurn, ToolCall


def decision_turn(call_id, *, before=(), after=(), content=""):
    def respond(messages):
        message = next(row for row in reversed(messages) if row["role"] == "tool")
        review = json.loads(message["content"])
        disposition = "pending" if review["operation"] == "create_pending" else "memory"
        call = ToolCall(
            call_id,
            "resolve_memory_proposals",
            {"decisions": [{"proposal_id": review["proposal_id"], "decision": disposition}]},
        )
        return AssistantTurn(content=content, tool_calls=[*before, call, *after])

    return respond
