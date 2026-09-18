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
        if not source or not source_segments_key:
            return [], ""
        try:
            self._store.download_file(self._key, self._local_path)
            payload = json.loads(self._local_path.read_text(encoding="utf-8"))
        except Exception:
            return [], ""

        if not isinstance(payload, dict):
            return [], ""
        if str(payload.get("source_segments_key") or "").strip() != str(source_segments_key or "").strip():
            return [], ""

        translated_prefix = segments_from_json_data(payload.get("translated_segments"))
        if not translated_prefix or not self._matches(source, translated_prefix):
            return [], ""
        summary = str(payload.get("summary") or "").strip()[:500]
        return translated_prefix, summary

    def save(
        self,
        source_segments_key: str | None,
        translated_prefix: list[Segment],
        *,
        summary: str,
    ) -> None:
        if not source_segments_key or not translated_prefix:
            return
        payload = {
            "source_segments_key": str(source_segments_key).strip(),
            "summary": str(summary or "").strip()[:500],
            "translated_segments": segments_to_json_data(translated_prefix),
        }
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
