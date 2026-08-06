from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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


def test_live_input_source_crud_and_playlist_selection(db: Session) -> None:
    source = live_service.create_live_input_source(
        db,
        {
            "url": "https://www.youtube.com/watch?v=live-id#player",
            "display_name": "频道直播",
        },
    )

    assert source["display_name"] == "频道直播"
    assert source["url"] == "https://www.youtube.com/watch?v=live-id"
    assert live_service.list_live_input_sources(db) == [source]

    playlist = live_service.update_live_playlist(
        db,
        {"video_items": [{"source": "live_source", "id": source["id"]}]},
    )
    assert playlist["video_items"] == [{"source": "live_source", "id": source["id"]}]

    with pytest.raises(HTTPException, match="先从播放列表移除"):
        live_service.delete_live_input_source(uuid.UUID(source["id"]), db=db)

    live_service.update_live_playlist(db, {"video_items": []})
    updated = live_service.update_live_input_source(
        uuid.UUID(source["id"]),
        {"url": "https://www.youtube.com/live/new-id", "display_name": "新的直播"},
        db=db,
    )
    assert updated["url"] == "https://www.youtube.com/live/new-id"
    assert updated["display_name"] == "新的直播"
    assert live_service.delete_live_input_source(uuid.UUID(source["id"]), db=db) == {"deleted": True}
    assert live_service.list_live_input_sources(db) == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/live.m3u8",
        "http://127.0.0.1/live.m3u8",
        "http://user:password@example.com/live.m3u8",
        "https://example.com:8443/live.m3u8",
    ],
)
def test_live_input_source_rejects_unsafe_urls(db: Session, url: str) -> None:
    with pytest.raises(ValueError):
        live_service.create_live_input_source(db, {"url": url})


def test_resolve_live_input_uses_ytdlp_and_preserves_safe_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeYdl:
        def __init__(self, options: dict[str, object]) -> None:
            captured["options"] = options

        def __enter__(self) -> "_FakeYdl":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            captured["url"] = url
            captured["download"] = download
            return {
                "url": "https://manifest.googlevideo.com/live/index.m3u8?token=temporary",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "http_headers": {
                    "User-Agent": "Live Agent",
                    "Referer": "https://www.youtube.com/",
                    "Cookie": "must-not-be-forwarded",
                },
            }

    monkeypatch.setattr(live_service.yt_dlp, "YoutubeDL", _FakeYdl)
    checked_urls: list[str] = []
    monkeypatch.setattr(live_service, "resolve_public_endpoint", lambda url: checked_urls.append(url))
    settings = SimpleNamespace(
        youtube_user_agent="VideoRoll Test",
        youtube_cookie_file=None,
        youtube_proxy=None,
        youtube_extractor_args_json=None,
        ffmpeg_path="ffmpeg",
    )

    resolved = live_service._resolve_live_input(settings, "https://www.youtube.com/watch?v=live-id")

    assert resolved.url.startswith("https://manifest.googlevideo.com/live/index.m3u8")
    assert resolved.has_audio is True
    assert resolved.http_headers == {"User-Agent": "Live Agent", "Referer": "https://www.youtube.com/"}
    assert checked_urls == [
        "https://www.youtube.com/watch?v=live-id",
        "https://manifest.googlevideo.com/live/index.m3u8?token=temporary",
    ]
    assert captured["url"] == "https://www.youtube.com/watch?v=live-id"
    assert captured["download"] is False
    assert "best[protocol^=m3u8]" in str(captured["options"])


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
            "mix_audio": True,
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
        "audio_playback_mode": "sequential",
        "loop_playlist": False,
        "mix_audio": True,
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


def test_live_playlist_lists_and_imports_raw_task_video(db: Session) -> None:
    task = _task()
    db.add(task)
    db.flush()
    raw = Asset(
        task_id=task.id,
        kind=AssetKind.video_raw,
        storage_key=f"raw/{task.id}/source.webm",
        sha256="a" * 64,
        size_bytes=456,
    )
    db.add(raw)
    db.commit()

    available = live_service.list_raw_live_videos(db)
    assert len(available) == 1
    assert available[0]["id"] == str(raw.id)
    assert available[0]["task_id"] == str(task.id)

    fake_s3 = _FakeS3()
    playlist = live_service.update_live_playlist(
        db,
        {"video_items": [{"source": "task_asset", "id": str(raw.id)}]},
        s3=fake_s3,  # type: ignore[arg-type]
    )

    imported_item = playlist["video_items"][0]
    assert imported_item["source"] == "library"
    assert fake_s3.copies == [
        (
            raw.storage_key,
            f"live/video/{imported_item['id']}/imported_{raw.id}.webm",
        )
    ]
    imported = db.get(AppSetting, f"{live_service.LIVE_MEDIA_PREFIX}{imported_item['id']}")
    assert imported is not None
    assert imported.value_json["origin"] == "raw_video"
    assert imported.value_json["source_task_id"] == str(task.id)
    assert imported.value_json["source_asset_id"] == str(raw.id)
    assert imported.value_json["sha256"] == raw.sha256

    repeated = live_service.update_live_playlist(
        db,
        {"video_items": [{"source": "task_asset", "id": str(raw.id)}]},
        s3=fake_s3,  # type: ignore[arg-type]
    )
    assert repeated["video_items"] == [imported_item]
    assert len(fake_s3.copies) == 1


def test_import_task_videos_persists_independent_library_media_without_playlist(db: Session) -> None:
    task = _task()
    db.add(task)
    db.flush()
    raw = Asset(
        task_id=task.id,
        kind=AssetKind.video_raw,
        storage_key=f"raw/{task.id}/source.webm",
        sha256="b" * 64,
        size_bytes=789,
    )
    db.add(raw)
    db.commit()
    fake_s3 = _FakeS3()

    imported = live_service.import_task_videos([raw.id], db=db, s3=fake_s3)  # type: ignore[arg-type]

    assert len(imported) == 1
    media = imported[0]
    assert media["origin"] == "raw_video"
    assert media["source_asset_id"] == str(raw.id)
    assert media["storage_key"].startswith(f"live/video/{media['id']}/imported_{raw.id}")
    assert live_service.get_live_playlist(db)["video_items"] == []
    assert live_service.list_live_library_media(db, media_type="video") == imported

    repeated = live_service.import_task_videos([raw.id], db=db, s3=fake_s3)  # type: ignore[arg-type]
    assert repeated == imported
    assert len(fake_s3.copies) == 1


def test_live_audio_playlist_groups_songs_and_deleting_it_keeps_media(db: Session) -> None:
    audio_id = uuid.uuid4()
    db.add(
        AppSetting(
            key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
            value_json={
                "id": str(audio_id),
                "media_type": "audio",
                "display_name": "theme.mp3",
                "storage_key": f"live/audio/{audio_id}/theme.mp3",
                "content_type": "audio/mpeg",
            },
        )
    )
    db.commit()

    playlist = live_service.create_live_audio_playlist(db, {"display_name": "开场音乐"})
    playlist_id = uuid.UUID(playlist["id"])
    live_service._add_audio_to_live_audio_playlist(db, playlist_id, audio_id)
    db.commit()

    listed = live_service.list_live_audio_playlists(db)
    assert listed == [
        {
            "id": str(playlist_id),
            "display_name": "开场音乐",
            "audio_media_ids": [str(audio_id)],
            "created_at": playlist["created_at"],
            "updated_at": listed[0]["updated_at"],
        }
    ]

    assert live_service.delete_live_audio_playlist(playlist_id, db=db) == {"deleted": True}
    assert live_service.list_live_audio_playlists(db) == []
    assert live_service.get_live_media(db, audio_id)["display_name"] == "theme.mp3"


def test_rename_live_video_media_only_changes_display_name(db: Session) -> None:
    video_id = uuid.uuid4()
    audio_id = uuid.uuid4()
    db.add_all(
        [
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{video_id}",
                value_json={
                    "id": str(video_id),
                    "media_type": "video",
                    "display_name": "before.mp4",
                    "storage_key": f"live/video/{video_id}/before.mp4",
                },
            ),
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
                value_json={
                    "id": str(audio_id),
                    "media_type": "audio",
                    "display_name": "audio.mp3",
                    "storage_key": f"live/audio/{audio_id}/audio.mp3",
                },
            ),
        ]
    )
    db.commit()

    renamed = live_service.rename_live_video_media(video_id, "直播片头", db=db)

    assert renamed["display_name"] == "直播片头"
    assert renamed["storage_key"] == f"live/video/{video_id}/before.mp4"
    with pytest.raises(HTTPException, match="只能修改视频"):
        live_service.rename_live_video_media(audio_id, "不应改名", db=db)


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
    assert "-filter_complex" not in command
    assert command[-3:] == ["-f", "flv", "rtmps://live.example.test/app/secret"]


def test_ffmpeg_command_can_mix_video_audio_with_independent_audio() -> None:
    command = live_service._ffmpeg_command(
        ffmpeg_path="ffmpeg",
        video_path=Path("/tmp/video.mp4"),
        audio_path=Path("/tmp/audio.mp3"),
        config={"fps": 30, "video_bitrate_kbps": 4500, "audio_bitrate_kbps": 160, "keyframe_interval_seconds": 2},
        target="rtmp://live.example.test/app/secret",
        mix_audio=True,
        video_has_audio=True,
    )

    assert command[command.index("-filter_complex") + 1] == (
        "[0:a:0][1:a:0]amix=inputs=2:duration=longest:dropout_transition=2:normalize=1[aout]"
    )
    assert command[command.index("-filter_complex") + 2 : command.index("-filter_complex") + 6] == [
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
    ]


def test_ffmpeg_command_uses_independent_audio_when_video_has_no_audio() -> None:
    command = live_service._ffmpeg_command(
        ffmpeg_path="ffmpeg",
        video_path=Path("/tmp/silent.mp4"),
        audio_path=Path("/tmp/audio.mp3"),
        config={"fps": 30, "video_bitrate_kbps": 4500, "audio_bitrate_kbps": 160, "keyframe_interval_seconds": 2},
        target="rtmp://live.example.test/app/secret",
        mix_audio=True,
        video_has_audio=False,
    )

    assert "-filter_complex" not in command
    audio_map = command.index("1:a:0")
    assert command[audio_map - 1 : audio_map + 1] == ["-map", "1:a:0"]


def test_ffmpeg_command_applies_live_input_headers_before_input() -> None:
    command = live_service._ffmpeg_command(
        ffmpeg_path="ffmpeg",
        video_path="https://manifest.example.test/live.m3u8",
        audio_path=None,
        config={"fps": 30, "video_bitrate_kbps": 4500, "audio_bitrate_kbps": 160, "keyframe_interval_seconds": 2},
        target="rtmp://live.example.test/app/secret",
        video_has_audio=True,
        video_input_args=["-reconnect", "1", "-user_agent", "Live Agent"],
    )

    input_index = command.index("-i")
    assert command[input_index - 4 : input_index] == ["-reconnect", "1", "-user_agent", "Live Agent"]
    assert command[input_index + 1] == "https://manifest.example.test/live.m3u8"


def test_ffmpeg_command_uses_intel_vaapi_when_enabled() -> None:
    command = live_service._ffmpeg_command(
        ffmpeg_path="ffmpeg",
        video_path=Path("/tmp/video.mp4"),
        audio_path=None,
        config={
            "fps": 30,
            "video_bitrate_kbps": 4500,
            "audio_bitrate_kbps": 160,
            "keyframe_interval_seconds": 2,
            "use_intel_gpu": True,
            "intel_gpu_render_device": "/dev/dri/renderD129",
        },
        target="rtmp://live.example.test/app/secret",
    )

    assert command[command.index("-vaapi_device") : command.index("-vaapi_device") + 2] == [
        "-vaapi_device",
        "/dev/dri/renderD129",
    ]
    assert command[command.index("-vf") : command.index("-vf") + 2] == ["-vf", "format=nv12,hwupload"]
    assert "h264_vaapi" in command
    assert "libx264" not in command
    assert command[command.index("-bf") : command.index("-bf") + 2] == ["-bf", "0"]


def test_mixer_command_keeps_rtmp_and_hls_preview_in_one_output(tmp_path: Path) -> None:
    command = live_service._mixer_ffmpeg_command(
        ffmpeg_path="ffmpeg",
        video_pipe=tmp_path / "video.yuv",
        source_audio_pipe=tmp_path / "source.pcm",
        music_audio_pipe=tmp_path / "music.pcm",
        config={"fps": 30, "video_bitrate_kbps": 4500, "audio_bitrate_kbps": 160, "keyframe_interval_seconds": 2},
        target="rtmps://live.example.test/app/secret",
        preview_dir=tmp_path / "preview",
    )

    assert command[command.index("-f") + 1] == "rawvideo"
    assert "amix=inputs=2" in command[command.index("-filter_complex") + 1]
    assert command[-3:-1] == ["-f", "tee"]
    tee_target = command[-1]
    assert "[f=flv]rtmps://live.example.test/app/secret" in tee_target
    assert "hls_time=2" in tee_target
    assert str(tmp_path / "preview" / live_service.LIVE_PREVIEW_PLAYLIST) in tee_target


def test_media_producers_overwrite_the_precreated_fifo_outputs(tmp_path: Path) -> None:
    output_pipe = tmp_path / "existing.pipe"
    commands = (
        live_service._video_producer_command(
            ffmpeg_path="ffmpeg",
            video_input=tmp_path / "video.mp4",
            video_input_args=[],
            fps=30,
            output_pipe=output_pipe,
        ),
        live_service._source_audio_producer_command(
            ffmpeg_path="ffmpeg",
            video_input=tmp_path / "video.mp4",
            video_input_args=[],
            output_pipe=output_pipe,
        ),
        live_service._music_audio_producer_command(
            ffmpeg_path="ffmpeg",
            audio_input=tmp_path / "music.mp3",
            output_pipe=output_pipe,
        ),
        live_service._silence_audio_producer_command(ffmpeg_path="ffmpeg", output_pipe=output_pipe),
    )

    assert all("-y" in command for command in commands)


def test_music_audio_producer_seeks_before_opening_the_input(tmp_path: Path) -> None:
    command = live_service._music_audio_producer_command(
        ffmpeg_path="ffmpeg",
        audio_input=tmp_path / "music.mp3",
        output_pipe=tmp_path / "music.pcm",
        seek_seconds=12.5,
    )

    assert command[command.index("-ss") : command.index("-ss") + 2] == ["-ss", "12.500"]
    assert command.index("-ss") < command.index("-i")


def test_music_audio_producer_applies_the_independent_track_volume(tmp_path: Path) -> None:
    command = live_service._music_audio_producer_command(
        ffmpeg_path="ffmpeg",
        audio_input=tmp_path / "music.mp3",
        output_pipe=tmp_path / "music.pcm",
        volume_percent=65,
    )

    assert command[command.index("-af") : command.index("-af") + 2] == ["-af", "volume=0.65"]


def test_music_audio_producer_reaches_eof_unless_explicitly_looped(tmp_path: Path) -> None:
    command = live_service._music_audio_producer_command(
        ffmpeg_path="ffmpeg",
        audio_input=tmp_path / "music.mp3",
        output_pipe=tmp_path / "music.pcm",
    )
    looped = live_service._music_audio_producer_command(
        ffmpeg_path="ffmpeg",
        audio_input=tmp_path / "music.mp3",
        output_pipe=tmp_path / "music.pcm",
        loop=True,
    )

    assert "-stream_loop" not in command
    assert looped[looped.index("-stream_loop") : looped.index("-stream_loop") + 2] == ["-stream_loop", "-1"]


def test_audio_queue_keeps_the_full_list_after_a_manual_selection() -> None:
    controller = live_service.LiveStreamController()
    sources = [
        live_service.LiveSource(source="library", id=str(uuid.uuid4())),
        live_service.LiveSource(source="library", id=str(uuid.uuid4())),
        live_service.LiveSource(source="library", id=str(uuid.uuid4())),
    ]

    order, position = controller._order_from_selected_audio(sources, sources[1], "sequential")
    following_order, following_position = controller._advance_audio_queue(sources, order, position, "sequential")

    assert order == [0, 1, 2]
    assert position == 1
    assert following_order == [0, 1, 2]
    assert following_position == 2


def test_control_live_audio_queues_an_audio_only_operation(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    audio_id = uuid.uuid4()
    db.add_all(
        [
            AppSetting(
                key=live_service.LIVE_SESSION_KEY,
                value_json={
                    "status": "running",
                    "current_audio": {"source": "library", "id": str(audio_id), "display_name": "bed.mp3"},
                },
            ),
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
                value_json={
                    "id": str(audio_id),
                    "media_type": "audio",
                    "display_name": "bed.mp3",
                    "storage_key": f"live/audio/{audio_id}/bed.mp3",
                },
            ),
        ]
    )
    db.commit()
    controller = live_service.LiveStreamController()
    controller._thread = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
    monkeypatch.setattr(live_service, "_CONTROLLER", controller)

    result = live_service.control_live_audio(
        {
            "action": "seek",
            "position_seconds": 42.5,
        },
        db=db,
    )

    assert result["status"] == "running"
    assert controller._audio_control_requested.is_set()
    assert controller._take_pending_audio_control() == {
        "action": "seek",
        "audio_items": [],
        "position_seconds": 42.5,
    }


def test_control_live_audio_keeps_the_full_queue_when_starting_a_selected_song(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_audio_id = uuid.uuid4()
    selected_audio_id = uuid.uuid4()
    db.add_all(
        [
            AppSetting(key=live_service.LIVE_SESSION_KEY, value_json={"status": "running"}),
            *[
                AppSetting(
                    key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
                    value_json={
                        "id": str(audio_id),
                        "media_type": "audio",
                        "display_name": f"song-{index}.mp3",
                        "storage_key": f"live/audio/{audio_id}/song-{index}.mp3",
                    },
                )
                for index, audio_id in enumerate((first_audio_id, selected_audio_id), start=1)
            ],
        ]
    )
    db.commit()
    controller = live_service.LiveStreamController()
    controller._thread = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
    monkeypatch.setattr(live_service, "_CONTROLLER", controller)

    live_service.control_live_audio(
        {
            "action": "play",
            "audio_item": {"source": "library", "id": str(selected_audio_id)},
            "audio_items": [
                {"source": "library", "id": str(first_audio_id)},
                {"source": "library", "id": str(selected_audio_id)},
            ],
        },
        db=db,
    )

    assert controller._take_pending_audio_control() == {
        "action": "play",
        "audio_items": [
            {"source": "library", "id": str(first_audio_id)},
            {"source": "library", "id": str(selected_audio_id)},
        ],
        "audio_item": {"source": "library", "id": str(selected_audio_id)},
    }


def test_live_audio_control_request_accepts_a_full_music_library_queue() -> None:
    from videoroll.apps.orchestrator_api.schemas import LiveAudioControlRequest

    request = LiveAudioControlRequest(
        action="next",
        audio_items=[{"source": "library", "id": str(uuid.uuid4())} for _ in range(108)],
    )

    assert len(request.audio_items) == 108


def test_live_audio_control_request_accepts_switching_back_to_original_video_audio() -> None:
    from videoroll.apps.orchestrator_api.schemas import LiveAudioControlRequest

    assert LiveAudioControlRequest(action="original").action == "original"


def test_live_audio_control_request_accepts_player_mode_and_volume_updates() -> None:
    from videoroll.apps.orchestrator_api.schemas import LiveAudioControlRequest

    assert LiveAudioControlRequest(action="set_playback_mode", playback_mode="shuffle").playback_mode == "shuffle"
    assert LiveAudioControlRequest(action="set_volume", volume_percent=65).volume_percent == 65


def test_live_media_batch_uploads_every_selected_file_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoroll.apps.orchestrator_api.routers import live as live_router

    received: list[tuple[str, str, uuid.UUID | None]] = []
    playlist_id = uuid.uuid4()

    async def fake_upload(
        media_type: str,
        file: object,
        *,
        db: object,
        s3: object,
        audio_playlist_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        filename = str(getattr(file, "filename", ""))
        received.append((media_type, filename, audio_playlist_id))
        return {
            "id": str(uuid.uuid4()),
            "media_type": media_type,
            "display_name": filename,
            "storage_key": f"live/{media_type}/{filename}",
            "content_type": "audio/mpeg",
        }

    monkeypatch.setattr(live_service, "upload_live_media", fake_upload)
    files = [SimpleNamespace(filename="one.mp3"), SimpleNamespace(filename="two.mp3")]

    result = asyncio.run(
        live_router._upload_live_media_batch(
            "audio",
            files,  # type: ignore[arg-type]
            db=object(),  # type: ignore[arg-type]
            s3=object(),  # type: ignore[arg-type]
            audio_playlist_id=playlist_id,
        )
    )

    assert [item.display_name for item in result] == ["one.mp3", "two.mp3"]
    assert received == [("audio", "one.mp3", playlist_id), ("audio", "two.mp3", playlist_id)]


def test_live_media_proxy_supports_byte_ranges(db: Session) -> None:
    media_id = uuid.uuid4()
    db.add(
        AppSetting(
            key=f"{live_service.LIVE_MEDIA_PREFIX}{media_id}",
            value_json={
                "id": str(media_id),
                "media_type": "video",
                "display_name": "large.webm",
                "storage_key": f"live/video/{media_id}/large.webm",
                "content_type": "video/webm",
            },
        )
    )
    db.commit()

    class _StreamS3:
        def __init__(self) -> None:
            self.requested_ranges: list[str | None] = []

        def head_object(self, _key: str) -> dict[str, object]:
            return {"ContentLength": 1000, "ContentType": "video/webm"}

        def get_object(self, _key: str, *, range_bytes: str | None = None) -> dict[str, object]:
            self.requested_ranges.append(range_bytes)
            return {"Body": object(), "ContentType": "video/webm", "ContentLength": 10}

    s3 = _StreamS3()
    result = live_service.prepare_live_media_stream(db, s3, media_id, range_header="bytes=10-19")  # type: ignore[arg-type]

    assert result.status_code == 206
    assert result.headers["Accept-Ranges"] == "bytes"
    assert result.headers["Content-Range"] == "bytes 10-19/1000"
    assert result.headers["Content-Length"] == "10"
    assert s3.requested_ranges == ["bytes=10-19"]


def test_live_media_proxy_encodes_unicode_filename_in_content_disposition(db: Session) -> None:
    media_id = uuid.uuid4()
    db.add(
        AppSetting(
            key=f"{live_service.LIVE_MEDIA_PREFIX}{media_id}",
            value_json={
                "id": str(media_id),
                "media_type": "video",
                "display_name": "蓝色睡觉雨声.webm",
                "storage_key": f"live/video/{media_id}/video.webm",
                "content_type": "video/webm",
            },
        )
    )
    db.commit()

    class _StreamS3:
        def head_object(self, _key: str) -> dict[str, object]:
            return {"ContentLength": 1000, "ContentType": "video/webm"}

        def get_object(self, _key: str, *, range_bytes: str | None = None) -> dict[str, object]:
            return {"Body": object(), "ContentType": "video/webm", "ContentLength": 1000}

    result = live_service.prepare_live_media_stream(db, _StreamS3(), media_id)  # type: ignore[arg-type]

    assert result.headers["Content-Disposition"].startswith('inline; filename="______.webm"')
    assert "filename*=UTF-8''%E8%93%9D%E8%89%B2" in result.headers["Content-Disposition"]


def test_library_media_resolves_to_authenticated_loopback_proxy(db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video_id = uuid.uuid4()
    audio_id = uuid.uuid4()
    db.add_all(
        [
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{video_id}",
                value_json={
                    "id": str(video_id),
                    "media_type": "video",
                    "display_name": "large.webm",
                    "storage_key": f"live/video/{video_id}/large.webm",
                },
            ),
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
                value_json={
                    "id": str(audio_id),
                    "media_type": "audio",
                    "display_name": "bed.mp3",
                    "storage_key": f"live/audio/{audio_id}/bed.mp3",
                },
            ),
        ]
    )
    db.commit()
    monkeypatch.setattr(live_service, "get_sessionmaker", lambda _url: lambda: db)
    monkeypatch.setattr(live_service, "_video_has_audio", lambda *_args: True)
    settings = SimpleNamespace(
        database_url="sqlite://",
        ffmpeg_path="ffmpeg",
        internal_api_secret="test-internal-secret",
        development_mode=False,
    )

    resolved = live_service.LiveStreamController()._resolve_media_pair(
        settings,
        work_dir=tmp_path,
        video_source=live_service.LiveSource(source="library", id=str(video_id)),
        audio_source=live_service.LiveSource(source="library", id=str(audio_id)),
        sequence=0,
    )

    _video, _audio, video_path, video_input, video_input_args, source_has_audio, audio_input, audio_input_args = resolved
    assert video_path is None
    assert video_input == f"http://127.0.0.1:8000/live/media/{video_id}/stream"
    assert audio_input == f"http://127.0.0.1:8000/live/media/{audio_id}/stream"
    assert video_input_args[0] == "-headers"
    assert "X-Videoroll-Internal-Token: v1." in video_input_args[1]
    assert audio_input_args == video_input_args
    assert source_has_audio is True


def test_live_preview_path_rejects_traversal_and_only_exposes_hls_artifacts(tmp_path: Path) -> None:
    settings = SimpleNamespace(work_dir=str(tmp_path))

    assert live_service.live_preview_path(settings, "stream.m3u8") == tmp_path / "live" / "preview" / "stream.m3u8"
    assert live_service.live_preview_path(settings, "segment_000001.ts") == tmp_path / "live" / "preview" / "segment_000001.ts"
    for name in ("../secret", "mixer.log", "segment_000001.m3u8", ".hidden"):
        with pytest.raises(HTTPException, match="预览资源不存在"):
            live_service.live_preview_path(settings, name)


def test_live_source_can_be_added_while_stream_is_running(db: Session) -> None:
    db.add(AppSetting(key=live_service.LIVE_SESSION_KEY, value_json={"status": "running"}))
    db.commit()

    source = live_service.create_live_input_source(db, {"url": "https://www.youtube.com/watch?v=live-id"})

    assert source["url"] == "https://www.youtube.com/watch?v=live-id"


def test_controller_switch_keeps_the_mixer_process_running() -> None:
    controller = live_service.LiveStreamController()
    terminated = False

    class _FakeProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

    controller._thread = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
    controller._mixer_process = _FakeProcess()  # type: ignore[assignment]

    controller.switch({"video_items": [{"source": "library", "id": str(uuid.uuid4())}], "audio_items": []})

    assert terminated is False
    assert controller._switch_requested.is_set()


def test_validate_intel_live_encoder_requires_h264_vaapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_service.Path, "exists", lambda _path: True)
    monkeypatch.setattr(
        live_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="V..... libx264", stderr=""),
    )

    with pytest.raises(RuntimeError, match="h264_vaapi"):
        live_service._validate_intel_live_encoder("ffmpeg", "/dev/dri/renderD128")


def test_start_live_stream_returns_conflict_when_intel_device_is_unavailable(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add(AppSetting(key="subtitle.auto_profile", value_json={"use_intel_gpu": True}))
    db.commit()
    monkeypatch.setattr(live_service, "_stream_target", lambda _db: ({}, "rtmp://live.example.test/app/key"))
    monkeypatch.setattr(live_service, "get_subtitle_settings", lambda: SimpleNamespace(intel_gpu_render_device="/dev/dri/renderD128"))
    monkeypatch.setattr(
        live_service,
        "_validate_intel_live_encoder",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError("Intel GPU render device not found: /dev/dri/renderD128")),
    )

    with pytest.raises(HTTPException) as exc_info:
        live_service.start_live_stream(SimpleNamespace(ffmpeg_path="ffmpeg"), db=db)

    assert exc_info.value.status_code == 409
    assert "Intel iGPU 直播编码不可用" in str(exc_info.value.detail)
    assert "/dev/dri" in str(exc_info.value.detail)


def test_start_live_stream_inherits_intel_gpu_from_auto_profile(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    video_id = uuid.uuid4()
    db.add_all(
        [
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{video_id}",
                value_json={
                    "id": str(video_id),
                    "media_type": "video",
                    "display_name": "video.mp4",
                    "storage_key": f"live/video/{video_id}/video.mp4",
                },
            ),
            AppSetting(key="subtitle.auto_profile", value_json={"use_intel_gpu": True}),
        ]
    )
    db.commit()
    live_service.update_live_playlist(db, {"video_items": [{"source": "library", "id": str(video_id)}]})

    captured: dict[str, object] = {}

    class _FakeController:
        def is_active(self) -> bool:
            return False

        def start(self, _settings: object, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(live_service, "_CONTROLLER", _FakeController())
    monkeypatch.setattr(live_service, "_stream_target", lambda _db: ({"fps": 30}, "rtmp://target/live/key"))
    monkeypatch.setattr(
        live_service,
        "get_subtitle_settings",
        lambda: SimpleNamespace(intel_gpu_render_device="/dev/dri/renderD129"),
    )
    monkeypatch.setattr(live_service, "_validate_intel_live_encoder", lambda *_args: None)
    settings = SimpleNamespace(ffmpeg_path="ffmpeg")

    session = live_service.start_live_stream(settings, db=db)

    assert session["status"] == "starting"
    assert captured["config"] == {
        "fps": 30,
        "use_intel_gpu": True,
        "intel_gpu_render_device": "/dev/dri/renderD129",
    }


def test_play_live_selection_starts_selected_library_media(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    video_id = uuid.uuid4()
    audio_id = uuid.uuid4()
    db.add_all(
        [
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{video_id}",
                value_json={
                    "id": str(video_id),
                    "media_type": "video",
                    "display_name": "selected.mp4",
                    "storage_key": f"live/video/{video_id}/selected.mp4",
                },
            ),
            AppSetting(
                key=f"{live_service.LIVE_MEDIA_PREFIX}{audio_id}",
                value_json={
                    "id": str(audio_id),
                    "media_type": "audio",
                    "display_name": "selected.mp3",
                    "storage_key": f"live/audio/{audio_id}/selected.mp3",
                },
            ),
        ]
    )
    db.commit()
    captured: dict[str, object] = {}

    class _FakeController:
        def is_active(self) -> bool:
            return False

        def start(self, _settings: object, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(live_service, "_CONTROLLER", _FakeController())
    monkeypatch.setattr(live_service, "_live_output_config", lambda _settings, *, db: ({"fps": 30}, "rtmp://target/live/key"))

    session = live_service.play_live_selection(
        SimpleNamespace(ffmpeg_path="ffmpeg"),
        {
            "video_item": {"source": "library", "id": str(video_id)},
            "audio_item": {"source": "library", "id": str(audio_id)},
            "mix_audio": True,
        },
        db=db,
    )

    assert session["status"] == "starting"
    assert captured["playlist"] == {
        "video_items": [{"source": "library", "id": str(video_id)}],
        "audio_items": [{"source": "library", "id": str(audio_id)}],
        "playback_mode": "sequential",
        "audio_playback_mode": "sequential",
        "loop_playlist": True,
        "mix_audio": True,
    }


def test_play_live_selection_switches_an_active_stream(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    video_id = uuid.uuid4()
    db.add(
        AppSetting(
            key=f"{live_service.LIVE_MEDIA_PREFIX}{video_id}",
            value_json={
                "id": str(video_id),
                "media_type": "video",
                "display_name": "replacement.mp4",
                "storage_key": f"live/video/{video_id}/replacement.mp4",
            },
        )
    )
    db.commit()
    captured: dict[str, object] = {}

    class _FakeController:
        def is_active(self) -> bool:
            return True

        def switch(self, playlist: dict[str, object]) -> None:
            captured["playlist"] = playlist

    monkeypatch.setattr(live_service, "_CONTROLLER", _FakeController())

    session = live_service.play_live_selection(
        SimpleNamespace(ffmpeg_path="ffmpeg"),
        {"video_item": {"source": "library", "id": str(video_id)}},
        db=db,
    )

    assert session["status"] == "running"
    assert session["current_video"]["display_name"] == "replacement.mp4"
    assert captured["playlist"] == {
        "video_items": [{"source": "library", "id": str(video_id)}],
        "audio_items": [],
        "playback_mode": "sequential",
        "audio_playback_mode": "sequential",
        "loop_playlist": True,
        "mix_audio": False,
    }
