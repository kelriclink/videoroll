from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from videoroll.apps.orchestrator_api.schemas import PublishActionRequest
from videoroll.db.models import AppSetting
from videoroll.db.models import SourceLicense, SourceType, Task


ROOT = Path(__file__).resolve().parents[1]


class _FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {}

    def get(self, model: object, key: str) -> AppSetting | None:
        assert model is AppSetting
        return self.rows.get(key)

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row

    def commit(self) -> None:
        return None

    def refresh(self, row: AppSetting) -> None:
        self.rows[row.key] = row

    def rollback(self) -> None:
        return None


def test_publish_platforms_are_disabled_until_checked() -> None:
    from videoroll.apps.publish_platform_settings_store import get_publish_platform_settings

    assert get_publish_platform_settings(_FakeDb()) == {
        "bilibili": False,
        "douyin": False,
        "xiaohongshu": False,
        "kuaishou": False,
    }


def test_publish_platforms_can_be_enabled_independently() -> None:
    from videoroll.apps.publish_platform_settings_store import (
        get_publish_platform_settings,
        update_publish_platform_setting,
    )

    db = _FakeDb()
    updated = update_publish_platform_setting(db, "douyin", True)

    assert updated["douyin"] is True
    assert updated["bilibili"] is False
    assert get_publish_platform_settings(db) == updated


def test_orchestrator_registers_publish_platform_settings_routes() -> None:
    from videoroll.apps.orchestrator_api.main import app

    paths = {route.path for route in app.routes}
    assert "/settings/publish/platforms" in paths
    assert "/settings/publish/platforms/{platform}" in paths


def test_publish_action_rejects_a_platform_that_is_not_checked() -> None:
    from videoroll.apps.orchestrator_api.services.publishing_service import enqueue_publish_job

    task = Task(id=uuid.uuid4(), source_type=SourceType.youtube, source_license=SourceLicense.own)
    db = Mock()
    db.get.return_value = task

    with (
        patch(
            "videoroll.apps.orchestrator_api.services.publishing_service.is_publish_platform_enabled",
            return_value=False,
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        enqueue_publish_job(
            task.id,
            PublishActionRequest(platform="douyin", account_id=str(uuid.uuid4())),
            settings=Mock(),
            db=db,
            s3=Mock(),
        )

    assert exc_info.value.status_code == 409
    assert "publish platform is disabled: douyin" in str(exc_info.value.detail)


def test_social_publish_mode_is_removed_from_runtime_configuration() -> None:
    paths = [
        ROOT / "src" / "videoroll" / "config.py",
        ROOT / "src" / "videoroll" / "apps" / "social_publisher" / "main.py",
        ROOT / "src" / "videoroll" / "apps" / "social_publisher" / "worker.py",
        ROOT / "compose.yml",
        ROOT / "docker-compose.yml",
        ROOT / "fromprod" / "docker-compose.yml",
        ROOT / ".env.example",
        ROOT / "fromprod" / ".env",
    ]
    for path in paths:
        assert "SOCIAL_PUBLISH_MODE" not in path.read_text(encoding="utf-8")
