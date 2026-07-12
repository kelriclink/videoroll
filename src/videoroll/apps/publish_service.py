from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import normalize_publish_platform, publish_backend_url
from videoroll.apps.publish_platform_settings_store import get_publish_platform_settings
from videoroll.db.models import PublishJob, PublishState, Task, TaskStatus
from videoroll.storage.s3 import S3Store


@dataclass
class PublishAllResult:
    """多平台投稿的结果汇总。"""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(
            r.get("status") == "ok" for r in self.results.values()
        )

    @property
    def has_any_ok(self) -> bool:
        return any(r.get("status") == "ok" for r in self.results.values())

    @property
    def errors(self) -> dict[str, str]:
        return {
            platform: str(r.get("detail") or r.get("error") or "unknown")
            for platform, r in self.results.items()
            if r.get("status") != "ok"
        }

    @property
    def platform_count(self) -> int:
        return len(self.results)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results.values() if r.get("status") == "ok")

    @property
    def error_count(self) -> int:
        return self.platform_count - self.ok_count


class PublishService:
    """
    统一投稿编排层。

    - publish():       读取投稿设置里所有已启用平台，逐个投稿（自动模式用）
    - publish_one():   只投指定平台（手动模式用）

    自动模式和手动模式都通过它来投稿，不再直接 httpx 调后端。
    """

    def __init__(self, db: Session, settings: Any, s3: S3Store):
        self._db = db
        self._settings = settings
        self._s3 = s3

    # ── 公开 API ──────────────────────────────────────────────

    def publish(
        self,
        task_id: uuid.UUID,
        *,
        publish_payload: dict[str, Any] | None = None,
    ) -> PublishAllResult:
        """读取投稿设置里所有已启用平台，逐个投稿。"""
        enabled = self._get_enabled_platforms()
        if not enabled:
            return PublishAllResult(results={})
        return self._publish_to_platforms(task_id, enabled, publish_payload)

    def publish_one(
        self,
        task_id: uuid.UUID,
        *,
        platform: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """只投指定平台。返回该平台的 publish 结果 dict。"""
        platform = normalize_publish_platform(platform)
        return self._publish_single(task_id, platform, payload)

    # ── 内部实现 ──────────────────────────────────────────────

    def _get_enabled_platforms(self) -> list[str]:
        settings_map = get_publish_platform_settings(self._db)
        return [p for p, enabled in settings_map.items() if enabled]

    def _publish_to_platforms(
        self,
        task_id: uuid.UUID,
        platforms: list[str],
        base_payload: dict[str, Any] | None,
    ) -> PublishAllResult:
        results: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            try:
                result = self._publish_single(task_id, platform, base_payload)
                results[platform] = result
            except Exception as exc:
                results[platform] = {"status": "error", "detail": str(exc)}
        return PublishAllResult(results=results)

    def _publish_single(
        self,
        task_id: uuid.UUID,
        platform: str,
        base_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """调用单个平台的 publish API。"""
        task = self._db.get(Task, task_id)
        if not task:
            raise ValueError("task not found")
        if task.status == TaskStatus.published:
            return {"status": "skipped", "detail": "already published"}

        url = publish_backend_url(self._settings, platform)

        payload: dict[str, Any] = dict(base_payload or {})
        payload.setdefault("platform", platform)

        # 延迟导入避免循环依赖（orchestrator_api → publish_service）
        from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers

        headers = internal_http_headers(self._settings)

        with httpx.Client(timeout=60.0, headers=headers) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}

        if isinstance(data, dict):
            data.setdefault("platform", platform)
            data.setdefault("status", "ok")
            # 为 bilibili 生成 external_url
            if platform == "bilibili" and data.get("bvid") and not data.get("external_url"):
                data["external_url"] = f"https://www.bilibili.com/video/{data['bvid']}"
        return data
