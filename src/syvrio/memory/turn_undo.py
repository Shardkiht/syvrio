"""在整个回合的变更仍匹配、没有新增依赖时允许原子撤销。"""

from __future__ import annotations

from ..infra.errors import SyvrioError
from ..infra.storage.paths import SyvrioPaths
from .undo_store import UndoStore


class TurnUndoError(SyvrioError, ValueError):
    pass


class TurnJournal:
    def __init__(self, paths: SyvrioPaths, session: str, turn: str):
        self.store = UndoStore(paths, session, turn)

    def capture(self):
        return self.store.capture()

    def has_effects(self) -> bool:
        return self.store.has_effects()

    def restore(self) -> int:
        with self.store.reversal() as batch:
            if any(batch.was_changed(effect) for effect in batch.effects):
                raise TurnUndoError(
                    "Another operation changed an affected record. The turn was preserved."
                )
            for effect in batch.effects:
                batch.restore(effect)
            if any(batch.has_new_references(effect) for effect in batch.effects):
                raise TurnUndoError("Another record now references an affected memory.")
            return len(batch.effects)
