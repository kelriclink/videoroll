from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from videoroll.apps.subtitle_service import subtitle_finalization
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.subtitle_finalization import (
    build_render_job_payload,
    mark_subtitle_ready,
    persist_subtitle_outputs,
    store_ass_output,
)
from videoroll.db.models import Asset, AssetKind, Subtitle, SubtitleFormat, TaskStatus


class _Query:
    def __init__(self, result: object | None = None) -> None:
        self.result = result

    def filter(self, *_args: object) -> "_Query":
        return self

    def first(self) -> object | None:
        return self.result


class _DB:
    def __init__(self, query_results: list[object | None] | None = None) -> None:
        self.added: list[object] = []
        self.commits = 0
        self._query_results = list(query_results or [])

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1

    def query(self, _model: object) -> _Query:
        result = self._query_results.pop(0) if self._query_results else None
        return _Query(result)


class _Store:
    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str, str | None]] = []

    def upload_file(self, source: Path, key: str, *, content_type: str | None = None) -> None:
        self.uploads.append((source, key, content_type))


def _unique(prefix: str, digest: str, suffix: str) -> str:
    return f"{prefix}_{digest[:12]}{suffix}"


def test_build_render_job_payload_preserves_manual_render_settings() -> None:
    payload = build_render_job_payload(
        request_json={"after_render": {"publish": True}},
        input_key="input/video.mp4",
        srt_key="sub/subtitle.srt",
        ass_key="sub/subtitle.ass",
        automatic_runtime_profile=False,
        burn_in=True,
        soft_sub=False,
        video_codec="av1",
        use_intel_gpu=True,
        video_preset="4",
        video_crf=26,
    )

    assert payload == {
        "input_key": "input/video.mp4",
        "runtime_profile": False,
        "srt_key": "sub/subtitle.srt",
        "ass_key": "sub/subtitle.ass",
        "burn_in": True,
        "soft_sub": False,
        "render": {
            "video_codec": "av1",
            "use_intel_gpu": True,
            "video_preset": "4",
            "video_crf": 26,
        },
        "after_render": {"publish": True},
    }


def test_build_render_job_payload_automatic_uses_runtime_profile_contract() -> None:
    payload = build_render_job_payload(
        request_json={"after_render": {"publish": False}},
        input_key="input/video.mp4",
        srt_key="sub/subtitle.srt",
        ass_key="sub/subtitle.ass",
        automatic_runtime_profile=True,
        burn_in=False,
        soft_sub=False,
        video_codec="h264",
        use_intel_gpu=False,
        video_preset=None,
        video_crf=None,
    )

    assert payload == {
        "input_key": "input/video.mp4",
        "srt_key": "sub/subtitle.srt",
        "ass_key": None,
        "runtime_profile": True,
        "after_render": {"publish": True, "runtime_profile": True},
    }


def test_mark_subtitle_ready_updates_resume_recoverable_state() -> None:
    db = _DB()
    task = SimpleNamespace(status=TaskStatus.failed)
    job = SimpleNamespace(progress=12)

    mark_subtitle_ready(
        db=db,  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        job=job,  # type: ignore[arg-type]
        resume_existing=True,
    )

    assert task.status == TaskStatus.subtitle_ready
    assert job.progress == 80
    assert db.commits == 1


def test_persist_subtitle_outputs_stores_segments_and_srt(tmp_path: Path) -> None:
    db = _DB()
    store = _Store()
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id)
    job = SimpleNamespace(request_json={})
    request_json: dict[str, object] = {}
    cleared: list[bool] = []
    logs: list[str] = []
    segments = [
        Segment(start=0.0, end=1.0, text="你好"),
        Segment(start=1.0, end=2.0, text="世界"),
    ]

    result = persist_subtitle_outputs(
        db=db,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        job=job,  # type: ignore[arg-type]
        request_json=request_json,
        segments=segments,
        subtitle_segments_path=tmp_path / "segments.json",
        srt_path=tmp_path / "subtitle.srt",
        ass_path=tmp_path / "subtitle.ass",
        video_path=tmp_path / "video.mp4",
        ffmpeg_path="ffmpeg",
        render_cfg={},
        need_ass=False,
        ass_bilingual=False,
        unique_storage_key=_unique,
        clear_translation_checkpoint=lambda: cleared.append(True),
        log=logs.append,
    )

    assert result.ass_key is None
    assert result.final_segments_key.endswith(".json")
    assert result.srt_key.endswith(".srt")
    assert request_json["artifacts"]["final_subtitle_segments_key"] == result.final_segments_key  # type: ignore[index]
    assert job.request_json is request_json
    assert (tmp_path / "subtitle.srt").read_text(encoding="utf-8").startswith("1\n00:00:00,000")
    assert [content_type for _path, _key, content_type in store.uploads] == [
        "application/json",
        "text/plain",
    ]
    assert cleared == [True]
    assert any(isinstance(item, Asset) and item.kind == AssetKind.subtitle_srt for item in db.added)
    assert any(
        isinstance(item, Subtitle)
        and item.format == SubtitleFormat.srt
        and item.storage_key == result.srt_key
        for item in db.added
    )
    # Final structured segments are committed before SRT/ASS state transition.
    assert db.commits == 1
    assert any("subtitle segments uploaded" in line for line in logs)
    assert any("subtitle srt uploaded" in line for line in logs)


def test_store_ass_output_persists_asset_and_subtitle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB(query_results=[None, None])
    store = _Store()
    task_id = uuid.uuid4()
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    logs: list[str] = []

    monkeypatch.setattr(
        subtitle_finalization,
        "probe_video_resolution",
        lambda _ffmpeg, _video: (1280, 720),
    )

    key = store_ass_output(
        db=db,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        task_id=task_id,
        segments=[
            Segment(
                start=0.0,
                end=1.0,
                text="你好",
                secondary_text="hello",
            )
        ],
        ass_path=tmp_path / "subtitle.ass",
        video_path=video_path,
        ffmpeg_path="ffmpeg",
        render_cfg={"ass_style": "clean_white"},
        bilingual=True,
        unique_storage_key=_unique,
        log=logs.append,
        log_prefix="subtitle ass uploaded",
    )

    assert key.endswith(".ass")
    assert store.uploads[-1][2] == "text/plain"
    assert any(isinstance(item, Asset) and item.kind == AssetKind.subtitle_ass for item in db.added)
    assert any(isinstance(item, Subtitle) and item.format == SubtitleFormat.ass for item in db.added)
    assert "1280x720" in logs[-1]
    ass_text = (tmp_path / "subtitle.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1280" in ass_text
    assert "PlayResY: 720" in ass_text
