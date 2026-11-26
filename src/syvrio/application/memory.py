"""记忆、待确认、推断审查、导入与画像读取用例。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sized
from pathlib import Path

from ..infra.storage.documents import DocumentError, read_document
from ..memory.extractor import split_chunks
from ..memory.service import MemoryService
from .contracts import (
    ImportChunkProgress,
    ImportDocument,
    ImportResult,
    InferenceReview,
    PendingSnapshot,
    ProfileSnapshot,
)


class MemoryUseCases:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    # 待确认记忆

    def pending(self) -> PendingSnapshot:
        return PendingSnapshot(list(self.service.pending()))

    def pending_count(self) -> int:
        return self.service.queue.count()

    def pending_item(self, item_id: str):
        return self.service.queue.get(item_id)

    def pending_evidence(self, item_id: str) -> dict:
        item = self.pending_item(item_id)
        if item is None:
            raise KeyError(item_id)
        source = None
        state = "unrecorded"
        if item.session_id and item.message_id:
            try:
                source = self.service.session_query.review_source(item.session_id, item.message_id)
                state = "found"
            except (KeyError, ValueError):
                state = "unavailable"
        related = self._source_texts(set(item.related_ids))
        return {
            "state": state,
            "source": source,
            "related": [{"id": key, "text": related.get(key)} for key in item.related_ids],
        }

    def approve(self, item_id: str, text_override: str | None = None):
        return self.service.approve(item_id, text_override=text_override)

    def reject(self, item_id: str):
        return self.service.reject(item_id)

    # 推断

    def load_inference_review(self, *, include_sources: bool = True) -> InferenceReview:
        items = self.service.list_inferences()
        source_ids = (
            {key for item in items for key in item.source_ids} if include_sources else set()
        )
        return InferenceReview(items, self._source_texts(source_ids))

    def review_inference(
        self, item_id: str, action: str, text: str | None = None, *, include_sources: bool = True
    ) -> InferenceReview:
        updated = self.service.review_inference(item_id, action, text)
        review = self.load_inference_review(include_sources=include_sources)
        feedback = {
            "confirm": "Confirmed.",
            "reject": "Rejected.",
            "correct": "Edit saved as a confirmed inference."
            if updated.id != item_id
            else "No changes.",
        }.get(action)
        return InferenceReview(
            review.items,
            review.sources,
            action=action,
            updated_id=updated.id,
            feedback=feedback,
        )

    def _source_texts(self, source_ids: set[str]) -> dict[str, str]:
        return self.service.store.source_texts(source_ids)

    # 主题

    def topics(self) -> list[dict]:
        return self.service.topics()

    def topic_directory(self) -> list[dict[str, str]]:
        return self.service.store.topic_directory()

    def topic_entries(self, topic: str):
        return self.service.topic_entries(topic)

    # 导入

    def import_documents(
        self,
        documents: Iterable[ImportDocument],
        apply: bool,
        on_progress: Callable[[ImportChunkProgress], None] | None = None,
        extractor=None,
        *,
        total_files: int | None = None,
    ) -> ImportResult:
        if total_files is None:
            if not isinstance(documents, Sized):
                raise ValueError("Provide a file count for streamed documents.")
            total_files = len(documents)
        if not total_files:
            raise ValueError("没有找到支持导入的文件；目录只扫描当前层。")
        total = ImportResult(apply=apply)
        for file_index, document in enumerate(documents, 1):
            file = document.name
            try:
                if document.read_error:
                    raise DocumentError(document.read_error)
                chunks = split_chunks(read_document(Path(file), content=document.content))
            except DocumentError as exc:
                total.errors.append(f"{file}: {exc}")
                total.failed_files += 1
                self._emit(on_progress, file, 0, 0, file_index, total_files, apply, error=str(exc))
                continue
            total.file_chunks.append((file, len(chunks)))
            total.files += 1
            for index, chunk in enumerate(chunks, 1):
                try:
                    result = self.service.import_text(
                        chunk,
                        source=f"import:{file}#chunk:{index}",
                        dry_run=not apply,
                        extractor=extractor,
                    )
                except Exception as exc:  # 单块失败保留已完成写入
                    total.failed_chunks += 1
                    total.errors.append(f"{file}#chunk:{index}: {exc}")
                    self._emit(
                        on_progress,
                        file,
                        index,
                        len(chunks),
                        file_index,
                        total_files,
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
                    total_files,
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
        file: str,
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
                    file=file,
                    chunk=chunk,
                    total_chunks=total_chunks,
                    file_index=file_index,
                    total_files=total_files,
                    apply=apply,
                    **kwargs,
                )
            )

    # 画像

    def profile(self) -> ProfileSnapshot:
        summary = self.service.profile.profile_summary()
        if summary is None:
            return ProfileSnapshot(body="Run /dream to generate a profile.", status="Not generated")
        return ProfileSnapshot(
            body=summary.text,
            status=f"Last generated {summary.generated_at}"
            if summary.generated_at
            else "Generated profile",
            generated_at=summary.generated_at,
            fact_count=summary.fact_count,
            unique_count=summary.unique_count,
        )
