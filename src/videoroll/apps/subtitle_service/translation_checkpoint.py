from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable, Protocol

from videoroll.apps.subtitle_service.processing import (
    Segment,
    segments_from_json_data,
    segments_to_json_data,
    write_json,
)
from videoroll.apps.subtitle_service.translation_context import (
    context_memory_is_empty,
    sanitize_translation_context_memory,
)


class CheckpointObjectStore(Protocol):
    def download_file(self, key: str, destination: Path) -> object: ...
    def upload_file(self, source: Path, key: str, *, content_type: str | None = None) -> object: ...
    def delete_object(self, key: str) -> object: ...


class TranslationCheckpointStore:
    """Persistence boundary for resumable translation progress."""

    def __init__(
        self,
        *,
        store: CheckpointObjectStore,
        task_id: uuid.UUID,
        local_path: Path,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._key = f"sub/{task_id}/translation_checkpoint.json"
        self._local_path = local_path
        self._log = log

    @property
    def key(self) -> str:
        return self._key

    @staticmethod
    def _matches(source: list[Segment], translated_prefix: list[Segment]) -> bool:
        if len(translated_prefix) > len(source):
            return False
        for index, translated_segment in enumerate(translated_prefix):
            source_segment = source[index]
            if abs(float(translated_segment.start) - float(source_segment.start)) > 0.01:
                return False
            if abs(float(translated_segment.end) - float(source_segment.end)) > 0.01:
                return False
        return True

    def load(
        self,
        source: list[Segment],
        *,
        source_segments_key: str | None,
    ) -> tuple[list[Segment], str]:
        translated_prefix, summary, _context_state = self.load_with_context(
            source,
            source_segments_key=source_segments_key,
        )
        return translated_prefix, summary

    def load_with_context(
        self,
        source: list[Segment],
        *,
        source_segments_key: str | None,
    ) -> tuple[list[Segment], str, dict[str, object]]:
        if not source or not source_segments_key:
            return [], "", {}
        try:
            self._store.download_file(self._key, self._local_path)
            payload = json.loads(self._local_path.read_text(encoding="utf-8"))
        except Exception:
            return [], "", {}

        if not isinstance(payload, dict):
            return [], "", {}
        if str(payload.get("source_segments_key") or "").strip() != str(source_segments_key or "").strip():
            return [], "", {}

        translated_prefix = segments_from_json_data(payload.get("translated_segments"))
        if not translated_prefix or not self._matches(source, translated_prefix):
            return [], "", {}
        summary = str(payload.get("summary") or "").strip()[:500]
        context_state = sanitize_translation_context_memory(payload.get("context_state"))
        if context_memory_is_empty(context_state):
            context_state = {}
        return translated_prefix, summary, context_state

    def save(
        self,
        source_segments_key: str | None,
        translated_prefix: list[Segment],
        *,
        summary: str,
        context_state: dict[str, object] | None = None,
    ) -> None:
        if not source_segments_key or not translated_prefix:
            return
        payload = {
            "source_segments_key": str(source_segments_key).strip(),
            "summary": str(summary or "").strip()[:500],
            "translated_segments": segments_to_json_data(translated_prefix),
        }
        clean_context = sanitize_translation_context_memory(context_state)
        if not context_memory_is_empty(clean_context):
            payload["context_state"] = clean_context
        try:
            write_json(self._local_path, payload)
            self._store.upload_file(self._local_path, self._key, content_type="application/json")
        except Exception as exc:
            if self._log is not None:
                self._log(f"translation checkpoint save failed: {type(exc).__name__}: {exc}")

    def clear(self) -> None:
        try:
            self._store.delete_object(self._key)
        except Exception:
            pass
