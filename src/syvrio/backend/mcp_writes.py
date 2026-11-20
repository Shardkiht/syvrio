"""Give external models the same write-before/after-recall boundary as chat."""

import secrets
import threading
from collections import OrderedDict

from ..agent.evidence import RecallContext
from ..memory.models import MemoryDraft


class MCPWrites:
    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._challenges: OrderedDict[str, tuple[str, RecallContext]] = OrderedDict()

    def apply(self, view: str, action: str, arguments: dict, review_token: str | None = None):
        service = self.service
        draft = MemoryDraft.model_validate(arguments["draft"]) if "draft" in arguments else None
        entry_id = arguments.get("entry_id")
        if action not in {"memorize", "correct", "transition", "deprecate", "create_pending"}:
            raise ValueError("Unknown memory operation.")
        if draft:
            service.validate_draft(draft)
        query = draft.text if draft else ""
        if entry_id:
            previous = service.store.find(entry_id)
            if previous is None:
                raise ValueError("The target memory does not exist.")
            query = f"{previous.text} {query}".strip()
        if not query:
            raise ValueError("The operation requires a draft or a target memory.")
        payload = dict(arguments)
        if draft is not None:
            payload["draft"] = draft

        def write():
            operations = {"memorize": service.memorize, "correct": service.correct, "transition": service.transition,
                          "deprecate": service.deprecate, "create_pending": service.create_pending}
            record = operations[action](**payload)
            if action == "create_pending":
                return record.model_dump(exclude_none=True, exclude={"created_at"})
            return record.as_recall().model_dump(exclude_none=True)

        with self._lock:
            challenge = self._challenges.pop(review_token, None) if review_token else None
        context = challenge[1] if challenge and challenge[0] == view else RecallContext(service)
        context.begin_model_request()
        result = context.run(action, arguments, query, write, target_id=entry_id,
                             include_pending=action == "create_pending", related_ids=tuple(arguments.get("related_ids", ())))
        if result.get("status") == "review_required":
            token = secrets.token_urlsafe(24)
            with self._lock:
                self._challenges[token] = (view, context)
                if len(self._challenges) > 256:
                    self._challenges.popitem(last=False)
            result["review_token"] = token
            result["instruction"] = "Review these facts, then repeat the same operation with this review_token if appropriate."
        return result
