from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from videoroll.apps.publish_gateway import SUPPORTED_SOCIAL_PLATFORMS, normalize_publish_platform
from videoroll.apps.social_publisher.account_store import validate_account_name
from videoroll.config import SocialPublisherSettings


@dataclass(frozen=True)
class SauCommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def build_check_command(settings: SocialPublisherSettings, platform: str, account_name: str) -> list[str]:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS:
        raise ValueError(f"unsupported SAU platform: {value}")
    return [settings.sau_executable, value, "check", "--account", validate_account_name(account_name)]


def build_login_command(settings: SocialPublisherSettings, platform: str, account_name: str) -> list[str]:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS:
        raise ValueError(f"unsupported SAU platform: {value}")
    return [settings.sau_executable, value, "login", "--account", validate_account_name(account_name), "--headed"]


def build_upload_video_command(
    settings: SocialPublisherSettings,
    *,
    platform: str,
    account_name: str,
    video_path: Path,
    cover_path: Path | None,
    meta: Mapping[str, Any],
    platform_options: Mapping[str, Any],
) -> list[str]:
    value = normalize_publish_platform(platform)
    if value not in SUPPORTED_SOCIAL_PLATFORMS:
        raise ValueError(f"unsupported SAU platform: {value}")
    title = str(meta.get("title") or "").strip()
    if not title:
        raise ValueError("meta.title is required")
    description = str(meta.get("desc") or "")
    tags = [str(tag).strip() for tag in meta.get("tags", []) if str(tag).strip()]
    if value == "douyin":
        description = description[:1000]
        normalized_tags: list[str] = []
        seen: set[str] = set()
        for item in tags:
            tag = item.lstrip("#").strip()
            key = tag.casefold()
            if not tag or key == "videoroll" or key in seen:
                continue
            seen.add(key)
            normalized_tags.append(tag)
            if len(normalized_tags) >= 4:
                break
        tags = normalized_tags
    command = [
        settings.sau_executable,
        value,
        "upload-video",
        "--account",
        validate_account_name(account_name),
        "--file",
        str(video_path),
        "--title",
        title,
        "--desc",
        description,
    ]
    if tags:
        command.extend(["--tags", ",".join(tags)])
    if cover_path is not None:
        command.extend(["--thumbnail", str(cover_path)])
    schedule = str(platform_options.get("schedule") or "").strip()
    if schedule:
        datetime.strptime(schedule, "%Y-%m-%d %H:%M")
        command.extend(["--schedule", schedule])
    command.append("--headed" if value == "douyin" else "--headless")
    return command


def _read_tail(file_obj, max_bytes: int) -> str:
    file_obj.flush()
    size = file_obj.tell()
    file_obj.seek(max(0, size - max(1, int(max_bytes))))
    return file_obj.read().decode("utf-8", errors="replace")


def run_sau_command(
    settings: SocialPublisherSettings,
    command: Sequence[str],
    *,
    timeout_seconds: float,
) -> SauCommandResult:
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
        process = subprocess.Popen(
            list(command),
            cwd=settings.sau_runtime_dir,
            shell=False,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        timed_out = False
        try:
            returncode = process.wait(timeout=max(1.0, float(timeout_seconds)))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                returncode = process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5.0)
        return SauCommandResult(
            returncode=int(returncode),
            stdout=_read_tail(stdout_file, settings.output_max_bytes),
            stderr=_read_tail(stderr_file, settings.output_max_bytes),
            timed_out=timed_out,
        )
