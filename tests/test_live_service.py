from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.services import live_service
from videoroll.db.base import Base
from videoroll.db.models import AppSetting, Asset, AssetKind, SourceLicense, SourceType, Task, TaskStatus


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AppSetting.__table__, Task.__table__, Asset.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine, tables=[Asset.__table__, Task.__table__, AppSetting.__table__])


def _task() -> Task:
    return Task(source_type=SourceType.local, source_license=SourceLicense.own, status=TaskStatus.rendered)


class _FakeS3:
    def __init__(self) -> None:
        self.copies: list[tuple[str, str]] = []

    def head_object(self, _key: str) -> dict[str, object]:
        return {"ContentType": "video/mp4", "ContentLength": 123}

    def copy_object(self, source_key: str, destination_key: str) -> None:
        self.copies.append((source_key, destination_key))


def test_live_settings_encrypt_stream_key_and_never_return_it(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_service, "encrypt_str", lambda value: f"enc:{value}")
    monkeypatch.setattr(live_service, "decrypt_str", lambda value: value.removeprefix("enc:"))

    result = live_service.update_live_settings(
        db,
        {
            "rtmp_url": "rtmps://live.example.test/app/",
            "stream_key": "secret-stream-key",
            "video_bitrate_kbps": 6000,
            "audio_bitrate_kbps": 192,
            "fps": 30,
            "keyframe_interval_seconds": 2,
        },
    )

    assert result["rtmp_url"] == "rtmps://live.example.test/app"
    assert result["stream_key_set"] is True
    assert "secret-stream-key" not in result.values()
    assert db.get(AppSetting, live_service.LIVE_SETTINGS_KEY).value_json["stream_key_enc"] == "enc:secret-stream-key"  # type: ignore[index]
    _settings, target = live_service._stream_target(db)
    assert target == "rtmps://live.example.test/app/secret-stream-key"


def test_live_settings_reject_stream_key_embedded_in_url(db: Session) -> None:
    with pytest.raises(ValueError, match="推流码"):
        live_service.update_live_settings(db, {"rtmp_url": "rtmp://live.example.test/app?key=secret"})


def test_live_playlist_accepts_completed_video_and_uploaded_audio(db: Session) -> None:
    task = _task()
    db.add(task)
    db.flush()
    final = Asset(task_id=task.id, kind=AssetKind.video_final, storage_key=f"final/{task.id}/video.mp4")
    audio_id = uuid.uuid4()
    db.add_all(
        [
            final,
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
                value_json={
                    "id": str(audio_id),
                    "media_type": "audio",
                    "display_name": "bed.mp3",
                    "storage_key": f"live/audio/{audio_id}/bed.mp3",
                    "content_type": "audio/mpeg",
                    "size_bytes": 12,
                },
            ),
        ]
    )
    db.commit()

    fake_s3 = _FakeS3()
    playlist = live_service.update_live_playlist(
        db,
        {
            "video_items": [{"source": "task_asset", "id": str(final.id)}],
            "audio_items": [{"source": "library", "id": str(audio_id)}],
            "playback_mode": "shuffle",
            "loop_playlist": False,
        },
        s3=fake_s3,  # type: ignore[arg-type]
    )

    imported_item = playlist["video_items"][0]
    assert imported_item["source"] == "library"
    assert imported_item["id"] != str(final.id)
    assert playlist == {
        "video_items": [imported_item],
        "audio_items": [{"source": "library", "id": str(audio_id)}],
        "playback_mode": "shuffle",
        "loop_playlist": False,
    }
    assert len(fake_s3.copies) == 1
    assert fake_s3.copies[0][0] == final.storage_key
    assert fake_s3.copies[0][1].startswith(f"live/video/{imported_item['id']}/imported_{final.id}")

    imported = db.get(AppSetting, f"{live_service.LIVE_MEDIA_PREFIX}{imported_item['id']}")
    assert imported is not None
    assert imported.value_json["origin"] == "completed_video"
    assert imported.value_json["source_task_id"] == str(task.id)
    assert imported.value_json["source_asset_id"] == str(final.id)
    assert imported.value_json["storage_key"] == fake_s3.copies[0][1]
    assert imported.value_json["size_bytes"] == 123

    repeated = live_service.update_live_playlist(
        db,
        {"video_items": [{"source": "task_asset", "id": str(final.id)}]},
        s3=fake_s3,  # type: ignore[arg-type]
    )
    assert repeated["video_items"] == [imported_item]
    assert len(fake_s3.copies) == 1


def test_ffmpeg_command_reencodes_to_live_compatible_h264_aac() -> None:
    command = live_service._ffmpeg_command(
        ffmpeg_path="ffmpeg",
        video_path=Path("/tmp/video.mp4"),
        audio_path=Path("/tmp/audio.mp3"),
        config={"fps": 30, "video_bitrate_kbps": 4500, "audio_bitrate_kbps": 160, "keyframe_interval_seconds": 2},
        target="rtmps://live.example.test/app/secret",
    )

    assert command[:8] == ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-re", "-i", "/tmp/video.mp4"]
    assert ["-c:v", "libx264"] == command[command.index("-c:v") : command.index("-c:v") + 2]
    assert ["-c:a", "aac"] == command[command.index("-c:a") : command.index("-c:a") + 2]
    assert command[-3:] == ["-f", "flv", "rtmps://live.example.test/app/secret"]
