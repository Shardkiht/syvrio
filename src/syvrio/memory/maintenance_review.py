"""维护建议独立于普通候选；只有人工审核可以修改正式事实。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool

from ..infra.errors import SyvrioError
from ..infra.storage.database import transaction


class ReviewConflict(SyvrioError, ValueError):
    pass


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: StrictBool
    action: Literal["ignore", "deprecate", "transition"]
    selected_id: str | None = None  # 保留的事实，或状态变化后的事实
    previous_end: str | None = None
    current_start: str | None = None


def pair_key(a, b):
    first, second = sorted((a, b), key=lambda entry: entry.id)
    return first.id, first.revision, second.id, second.revision


class MaintenanceReviews:
    def __init__(self, service):
        self.service = service

    def known(self, a, b):
        with transaction(self.service.paths, write=False) as db:
            return (
                db.execute(
                    """SELECT 1 FROM maintenance_suggestions WHERE
                   (first_id,first_revision,second_id,second_revision)=(?,?,?,?)""",
                    pair_key(a, b),
                ).fetchone()
                is not None
            )

    def suggest(self, a, b, kind, reason):
        with transaction(self.service.paths) as db:
            db.execute(
                """INSERT INTO maintenance_suggestions
                   (first_id,first_revision,second_id,second_revision,kind,reason)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(first_id,first_revision,second_id,second_revision)
                   DO NOTHING""",
                (*pair_key(a, b), kind, reason),
            )

    def _fact(self, memory_id):
        entry = self.service.require_entry(memory_id)
        source = {}
        if entry.source_session_id and entry.source_message_id:
            try:
                source = self.service.session_query.review_source(
                    entry.source_session_id, entry.source_message_id
                )
            except (KeyError, ValueError):
                pass
        return {
            "id": entry.id,
            "revision": entry.revision,
            "text": entry.text,
            "topic": entry.topic,
            "recorded_at": entry.timestamp,
            "context": entry.context.model_dump(exclude_none=True) if entry.context else {},
            "session_id": entry.source_session_id,
            "message_id": entry.source_message_id,
            "source": source.get("content"),
            "source_at": source.get("at"),
            "source_archived": bool(source.get("archived")),
        }

    def pending(self, after=0, limit=20):
        with transaction(self.service.paths, write=False) as db:
            rows = db.execute(
                """SELECT s.* FROM maintenance_suggestions s
                   JOIN memories a ON a.id=s.first_id AND a.revision=s.first_revision
                   JOIN memories b ON b.id=s.second_id AND b.revision=s.second_revision
                   JOIN topics ta ON ta.name=a.topic JOIN topics tb ON tb.name=b.topic
                   WHERE s.state='pending' AND s.id>? AND a.status='active' AND b.status='active'
                     AND NOT ta.archived AND NOT tb.archived ORDER BY s.id LIMIT ?""",
                (after, limit + 1),
            ).fetchall()
            items = [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "reason": r["reason"],
                    "facts": [self._fact(r["first_id"]), self._fact(r["second_id"])],
                }
                for r in rows[:limit]
            ]
            return {"items": items, "next": items[-1]["id"] if len(rows) > limit else None}

    def resolve(self, suggestion_id, decision: ReviewDecision):
        if not decision.confirmed:
            raise ReviewConflict("Confirm the selected action before applying it.")
        with transaction(self.service.paths) as db:
            row = db.execute(
                "SELECT * FROM maintenance_suggestions WHERE id=?", (suggestion_id,)
            ).fetchone()
            if row is None:
                raise KeyError(suggestion_id)
            if row["state"] != "pending":
                raise ReviewConflict("This suggestion has already been reviewed.")
            ids = (row["first_id"], row["second_id"])
            revisions = (row["first_revision"], row["second_revision"])
            for key, revision in zip(ids, revisions, strict=True):
                entry = self.service.store.find(key)
                if not entry or entry.revision != revision or entry.status != "active":
                    raise ReviewConflict("The facts changed. Refresh before reviewing.")
            action = decision.action
            if action != "transition" and (decision.previous_end or decision.current_start):
                raise ValueError("Dates belong only to a state change.")
            if action == "ignore":
                if decision.selected_id is not None:
                    raise ValueError("Ignoring a suggestion does not select a fact.")
            else:
                selected = decision.selected_id
                if selected not in ids:
                    raise ValueError("Select one of the two facts.")
                other = next(key for key in ids if key != selected)
                if action == "deprecate":
                    self.service.deprecate(other)
                elif (
                    row["kind"] != "conflict"
                    or not decision.previous_end
                    or not decision.current_start
                ):
                    raise ValueError(
                        "A state change requires a conflict and both effective boundaries."
                    )
                else:
                    self.service.transition_existing(
                        other, selected, decision.previous_end, decision.current_start
                    )
            state = "ignored" if action == "ignore" else "applied"
            self.service.audit.write(
                "maintenance_" + state,
                suggestion_id=suggestion_id,
                ids=ids,
                revisions=revisions,
                decision=decision.model_dump(exclude_none=True),
            )
            db.execute(
                "UPDATE maintenance_suggestions SET state=? WHERE id=?", (state, suggestion_id)
            )
            return {"state": state}
