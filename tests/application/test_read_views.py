"""只读用例在其他连接占用写事务时仍应完成。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from syvrio.agent.evidence import RecallContext
from syvrio.application.conversation import ConversationUseCases
from syvrio.application.reflection import ReflectionUseCases
from syvrio.memory.background import BackgroundMemory


def test_read_only_actions_do_not_wait_for_an_unrelated_writer(service):
    background = BackgroundMemory(service)
    session = background.sessions.create()
    background.sessions.set_archived(session.id, True)
    conversation = ConversationUseCases(SimpleNamespace(service=service), background.sessions)

    def read_all():
        assert ReflectionUseCases(background).command("status").status == "ok"
        items, _ = conversation.deletion_preview(session.id, archived=True)
        assert not items
        result = RecallContext(service).run("add", {}, "example", lambda: None)
        assert result["status"] == "review_required"

    with ThreadPoolExecutor(max_workers=1) as worker:
        with service.work.write():
            worker.submit(read_all).result(timeout=5)
