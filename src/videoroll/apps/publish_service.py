from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import normalize_publish_platform, publish_backend_url
from videoroll.apps.publish_platform_settings_store import get_publish_platform_settings
from videoroll.db.models import Account, Asset, AssetKind, Platform, Task
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

    def __init__(
        self,
        db: Session,
        settings: Any,
        s3: S3Store,
        *,
        http_headers: dict[str, str] | Callable[[], dict[str, str]] | None = None,
    ):
        self._db = db
        self._settings = settings
        self._s3 = s3
        self._http_headers = http_headers

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
        """只投指定平台。payload 应为已构建好的 gateway request dict。"""
        platform = normalize_publish_platform(platform)
        return self._publish_single(task_id, platform, payload)

    # ── 内部实现 ──────────────────────────────────────────────

    def _get_headers(self) -> dict[str, str]:
        if callable(self._http_headers):
            return self._http_headers()
        return self._http_headers or {}

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
                request = self._build_backend_request(task_id, platform, base_payload)
                result = self._publish_single(task_id, platform, request)
                results[platform] = result
            except httpx.HTTPStatusError as exc:
                results[platform] = {
                    "status": "error",
                    "detail": self._http_status_error_detail(exc),
                }
            except Exception as exc:
                results[platform] = {"status": "error", "detail": str(exc)}
        return PublishAllResult(results=results)

    @staticmethod
    def _http_status_error_detail(exc: httpx.HTTPStatusError) -> str:
        response = exc.response
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("message")
            if isinstance(detail, list):
                messages = []
                for item in detail:
                    if not isinstance(item, dict):
                        messages.append(str(item))
                        continue
                    location = ".".join(
                        str(part) for part in item.get("loc", []) if part != "body"
                    )
                    message = str(item.get("msg") or item)
                    messages.append(f"{location}: {message}" if location else message)
                detail = "; ".join(messages)
            if detail:
                return f"HTTP {response.status_code}: {detail}"
        text = response.text.strip()
        if text:
            return f"HTTP {response.status_code}: {text}"
        return f"HTTP {response.status_code}"

    def _resolve_social_account_id(
        self,
        platform: str,
        payload: dict[str, Any],
    ) -> str:
        account_ids = payload.get("account_ids")
        requested_account_id = None
        if isinstance(account_ids, dict):
            requested_account_id = account_ids.get(platform)
        if requested_account_id is None:
            requested_account_id = payload.get("account_id")

        if requested_account_id:
            try:
                account_uuid = uuid.UUID(str(requested_account_id))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid account_id configured for {platform}") from exc
            account = self._db.get(Account, account_uuid)
            if (
                not account
                or account.platform != Platform(platform)
                or not account.is_active
                or account.check_state != "valid"
            ):
                raise ValueError(f"active validated account not found for {platform}")
            return str(account.id)

        account = (
            self._db.query(Account)
            .filter(
                Account.platform == Platform(platform),
                Account.is_active.is_(True),
                Account.check_state == "valid",
            )
            .order_by(Account.created_at.desc())
            .first()
        )
        if not account:
            raise ValueError(f"no active validated account configured for {platform}")
        return str(account.id)

    def _build_backend_request(
        self,
        task_id: uuid.UUID,
        platform: str,
        base_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        将 auto mode 的原始 publish_payload 转换为后端期望的 gateway request 格式。
        复用 build_publish_gateway_request 的核心逻辑。
        """
        from videoroll.apps.orchestrator_api.services.publishing_service import (
            build_publish_gateway_request,
        )
        from videoroll.apps.orchestrator_api.schemas import PublishActionRequest

        task = self._db.get(Task, task_id)
        if not task:
            raise ValueError("task not found")

        payload = base_payload or {}

        # 解析 video_key：优先用传入的，否则找最新的 final video asset
        video_key = payload.get("video_key")
        if not video_key:
            final_asset = (
                self._db.query(Asset)
                .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_final)
                .order_by(Asset.created_at.desc())
                .first()
            )
            if not final_asset:
                raise ValueError("no final video asset found")
            video_key = final_asset.storage_key

        # 构造 PublishActionRequest 供 build_publish_gateway_request 使用
        account_id = payload.get("account_id")
        if platform != "bilibili":
            account_id = self._resolve_social_account_id(platform, payload)

        action_req = PublishActionRequest(
            platform=platform,
            account_id=account_id,
            video_key=video_key,
            cover_key=payload.get("cover_key"),
            typeid_mode=payload.get("typeid_mode"),
            meta=payload.get("meta"),
            platform_options=payload.get("platform_options") or {},
            skip_review=bool(payload.get("skip_review")),
            force_retry=bool(payload.get("force_retry")),
        )

        return build_publish_gateway_request(
            task=task,
            task_id=task_id,
            payload=action_req,
            video_key=video_key,
            db=self._db,
            s3=self._s3,
        )

    def _publish_single(
        self,
        task_id: uuid.UUID,
        platform: str,
        base_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """调用单个平台的 publish API。payload 应为 gateway request 格式。"""
        task = self._db.get(Task, task_id)
        if not task:
            raise ValueError("task not found")
        url = publish_backend_url(self._settings, platform)

        payload: dict[str, Any] = dict(base_payload or {})
        payload.setdefault("platform", platform)

        headers = self._get_headers()

        with httpx.Client(timeout=60.0, headers=headers) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}

        if isinstance(data, dict):
            data.setdefault("platform", platform)
            data.setdefault("status", "ok")
            if platform == "bilibili" and data.get("bvid") and not data.get("external_url"):
                data["external_url"] = f"https://www.bilibili.com/video/{data['bvid']}"
        return data
