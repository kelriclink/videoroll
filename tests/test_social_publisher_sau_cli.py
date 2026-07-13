from pathlib import Path
from unittest.mock import Mock

from videoroll.apps.social_publisher import sau_cli
from videoroll.apps.social_publisher.sau_cli import build_check_command, build_upload_video_command


def _settings() -> Mock:
    return Mock(sau_executable="sau", sau_headless=True)


def test_build_check_command_uses_account_identifier() -> None:
    assert build_check_command(_settings(), "douyin", "creator") == ["sau", "douyin", "check", "--account", "creator"]


def test_build_login_command_uses_headed_browser() -> None:
    assert hasattr(sau_cli, "build_login_command")
    assert sau_cli.build_login_command(_settings(), "douyin", "creator") == [
        "sau", "douyin", "login", "--account", "creator", "--headed",
    ]


def test_build_upload_command_maps_common_video_fields() -> None:
    command = build_upload_video_command(
        _settings(),
        platform="xiaohongshu",
        account_name="creator",
        video_path=Path("/work/video.mp4"),
        cover_path=Path("/work/cover.jpg"),
        meta={"title": "标题", "desc": "简介", "tags": ["一", "二"]},
        platform_options={"schedule": "2026-07-11 20:30"},
    )
    assert command == [
        "sau", "xiaohongshu", "upload-video", "--account", "creator", "--file", "/work/video.mp4",
        "--title", "标题", "--desc", "简介", "--tags", "一,二", "--thumbnail", "/work/cover.jpg",
        "--schedule", "2026-07-11 20:30", "--headless",
    ]


def test_douyin_upload_uses_headed_browser_for_live_observation() -> None:
    command = build_upload_video_command(
        _settings(),
        platform="douyin",
        account_name="creator",
        video_path=Path("/work/video.mp4"),
        cover_path=None,
        meta={"title": "标题", "desc": "简介", "tags": []},
        platform_options={},
    )

    assert command[-1] == "--headed"
    assert "--headless" not in command


def test_douyin_upload_clamps_description_and_topics() -> None:
    command = build_upload_video_command(
        _settings(),
        platform="douyin",
        account_name="creator",
        video_path=Path("/work/video.mp4"),
        cover_path=None,
        meta={
            "title": "标题",
            "desc": "原作者：" + ("作者" * 600),
            "tags": ["videoroll", "一", "二", "三", "四", "五"],
        },
        platform_options={},
    )

    desc = command[command.index("--desc") + 1]
    tags = command[command.index("--tags") + 1]
    assert len(desc) == 1000
    assert tags == "一,二,三,四"
