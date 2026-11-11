"""记忆、待确认、推断审查、导入与画像读取用例。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..infra.documents import SUPPORTED_SUFFIXES, DocumentError, read_document
from ..memory.extractor import split_chunks
from ..memory.service import MemoryService
from .contracts import (
    ImportChunkProgress,
    ImportResult,
    InferenceReview,
    PendingSnapshot,
    ProfileSnapshot,
)


class MemoryUseCases:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    # ---------- Pending ----------

    def pending(self) -> PendingSnapshot:
        return PendingSnapshot(list(self.service.pending()))

    def pending_count(self) -> int:
        return len(self.service.pending())

    def pending_item(self, item_id: str):
        return next((item for item in self.service.pending() if item.id == item_id), None)

    def approve(self, item_id: str, text_override: str | None = None):
        return self.service.approve(item_id, text_override=text_override)

    def reject(self, item_id: str):
        return self.service.reject(item_id)

    # ---------- Inferences ----------

    def load_inference_review(self) -> InferenceReview:
        items = self.service.list_inferences()
        return InferenceReview(items, self._sources(items))

    def review_inference(
        self, item_id: str, action: str, text: str | None = None
    ) -> InferenceReview:
        updated = self.service.review_inference(item_id, action, text)
        items = self.service.list_inferences()
        feedback = {
            "confirm": "Confirmed.",
            "reject": "Rejected.",
            "correct": "Edit saved as a new pending hypothesis.",
        }.get(action)
        return InferenceReview(
            items,
            self._sources(items),
            action=action,
            updated_id=updated.id,
            feedback=feedback,
        )

    def _sources(self, items) -> dict[str, str]:
        source_ids = {source_id for item in items for source_id in item.source_ids}
        # 来源摘要共用一次事实库读取，避免每个来源 ID 都重复解析全部主题文件。
        return {
            entry.id: entry.text
            for entry in self.service.store.entries(include_inactive=True)
            if entry.id in source_ids
        }

    # ---------- Topics ----------

    def topics(self) -> list[dict]:
        return self.service.topics()

    def topic_entries(self, topic: str):
        return self.service.topic_entries(topic)

    # ---------- Import ----------

    def import_path(
        self,
        path: Path,
        apply: bool,
        on_progress: Callable[[ImportChunkProgress], None] | None = None,
        extractor=None,
    ) -> ImportResult:
        files = (
            sorted(
                file
                for file in path.iterdir()
                if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES
            )
            if path.is_dir()
            else [path]
        )
        if not files:
            raise ValueError("No supported files in this directory (non-recursive).")
        total = ImportResult(apply=apply)
        for file_index, file in enumerate(files, 1):
            try:
                chunks = split_chunks(read_document(file))
            except DocumentError as exc:
                total.errors.append(f"{file.name}: {exc}")
                self._emit(on_progress, file, 0, 0, file_index, len(files), apply, error=str(exc))
                continue
            total.files += 1
            for index, chunk in enumerate(chunks, 1):
                try:
                    result = self.service.import_text(
                        chunk,
                        source=f"import:{file.name}#chunk:{index}",
                        dry_run=not apply,
                        extractor=extractor,
                    )
                except Exception as exc:  # 单块失败保留已完成写入
                    total.errors.append(f"{file.name}#chunk:{index}: {exc}")
                    self._emit(
                        on_progress,
                        file,
                        index,
                        len(chunks),
                        file_index,
                        len(files),
                        apply,
                        error=(
                            "Check the format, parser dependencies and model settings; "
                            "completed changes are retained."
                        ),
                    )
                    continue
                total.chunks += 1
                total.saved += len(result.admitted)
                total.queued += len(result.queued)
                total.duplicates += len(result.duplicates)
                total.blocked += len(result.blocked)
                self._emit(
                    on_progress,
                    file,
                    index,
                    len(chunks),
                    file_index,
                    len(files),
                    apply,
                    saved=len(result.admitted),
                    queued=len(result.queued),
                    duplicates=len(result.duplicates),
                    blocked=len(result.blocked),
                    admitted=[(entry.topic, entry.text) for entry in result.admitted],
                    pending=[(item.topic, item.text) for item in result.queued],
                )
        return total

    @staticmethod
    def _emit(
        callback: Callable[[ImportChunkProgress], None] | None,
        file: Path,
        chunk: int,
        total_chunks: int,
        file_index: int,
        total_files: int,
        apply: bool,
        **kwargs,
    ) -> None:
        if callback is not None:
            callback(
                ImportChunkProgress(
                    file=file.name,
                    chunk=chunk,
                    total_chunks=total_chunks,
                    file_index=file_index,
                    total_files=total_files,
                    apply=apply,
                    **kwargs,
                )
            )

    # ---------- Profile ----------

    def profile(self) -> ProfileSnapshot:
        summary = self.service.reflection.profile_summary()
        if summary is None:
            return ProfileSnapshot(
                body=(
                    "No profile summary yet. Save preferences, goals or habits, then run "
                    "/profile refresh to generate a cross-topic summary.\n\n"
                    "The summary is a derived view over valid facts; source facts stay unchanged."
                ),
                status="Not generated",
            )
        return ProfileSnapshot(
            body=summary.text,
            status="Outdated · Source facts changed; run /profile refresh."
            if summary.stale
            else "Up to date",
            stale=summary.stale,
            generated_at=summary.generated_at,
            fact_count=summary.fact_count,
            unique_count=summary.unique_count,
        )
