from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.translation_checkpoint import TranslationCheckpointStore


class _Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def download_file(self, key: str, destination: Path) -> None:
        shutil.copyfile(self._path(key), destination)

    def upload_file(self, source: Path, key: str, *, content_type: str | None = None) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def delete_object(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def test_translation_checkpoint_round_trip(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    checkpoint = TranslationCheckpointStore(
        store=_Store(tmp_path / "objects"),
        task_id=task_id,
        local_path=tmp_path / "checkpoint.json",
    )
    source = [
        Segment(start=0.0, end=1.0, text="hello"),
        Segment(start=1.0, end=2.0, text="world"),
    ]
    translated = [
        Segment(start=0.0, end=1.0, text="你好"),
    ]

    checkpoint.save("sub/source.json", translated, summary="summary")
    resumed, summary = checkpoint.load(source, source_segments_key="sub/source.json")

    assert resumed == translated
    assert summary == "summary"


    checkpoint.save(
        "sub/source.json",
        translated,
        summary="summary",
        context_state={
            "topic": "hardware",
            "characters": [{"name": "Alice", "target_name": "爱丽丝"}],
            "terminology": [{"source": "POST", "target": "开机自检"}],
        },
    )
    resumed_with_context, summary_with_context, context = checkpoint.load_with_context(
        source,
        source_segments_key="sub/source.json",
    )
    assert resumed_with_context == translated
    assert summary_with_context == "summary"
    assert context["topic"] == "hardware"
    assert context["characters"][0]["target_name"] == "爱丽丝"
    assert context["terminology"][0]["target"] == "开机自检"


def test_translation_checkpoint_load_with_context_accepts_legacy_payload(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    store = _Store(tmp_path / "objects")
    checkpoint = TranslationCheckpointStore(
        store=store,
        task_id=task_id,
        local_path=tmp_path / "checkpoint.json",
    )
    source = [Segment(start=0.0, end=1.0, text="hello")]
    translated = [Segment(start=0.0, end=1.0, text="你好")]
    checkpoint.save("sub/source.json", translated, summary="legacy")

    resumed, summary, context = checkpoint.load_with_context(
        source,
        source_segments_key="sub/source.json",
    )

    assert resumed == translated
    assert summary == "legacy"
    assert context == {}


def test_translation_checkpoint_rejects_other_source_or_timeline(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    checkpoint = TranslationCheckpointStore(
        store=_Store(tmp_path / "objects"),
        task_id=task_id,
        local_path=tmp_path / "checkpoint.json",
    )
    translated = [Segment(start=0.0, end=1.0, text="你好")]
    checkpoint.save("sub/source.json", translated, summary="summary")

    assert checkpoint.load(
        [Segment(start=0.0, end=1.0, text="hello")],
        source_segments_key="sub/other.json",
    ) == ([], "")
    assert checkpoint.load(
        [Segment(start=0.2, end=1.0, text="hello")],
        source_segments_key="sub/source.json",
    ) == ([], "")


def test_translation_checkpoint_clear_is_idempotent(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    store = _Store(tmp_path / "objects")
    checkpoint = TranslationCheckpointStore(
        store=store,
        task_id=task_id,
        local_path=tmp_path / "checkpoint.json",
    )
    checkpoint.save(
        "sub/source.json",
        [Segment(start=0.0, end=1.0, text="你好")],
        summary="",
    )

    checkpoint.clear()
    checkpoint.clear()

    assert not (store.root / checkpoint.key).exists()
