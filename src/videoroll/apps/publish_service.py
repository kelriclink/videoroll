from __future__ import annotations

import uuid
import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from videoroll.apps.publish_gateway import normalize_publish_platform, publish_backend_url, publish_meta_key
from videoroll.apps.publish_lifecycle import (
    PublishBatchState,
    enqueue_publish_batch_cleanup,
    publish_target_key,
    record_publish_batch_dispatch_error,
    reconcile_publish_batch,
)
from videoroll.apps.publish_platform_settings_store import get_publish_platform_settings
from videoroll.apps.publish_request_builder import build_publish_gateway_request
from videoroll.db.models import Account, Asset, AssetKind, Platform, PublishBatch, PublishJob, PublishState, Task
from videoroll.storage.s3 import S3Store


@dataclass
class PublishAllResult:
    """多平台投稿的结果汇总。"""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    batch_id: str | None = None

    @property
    def all_accepted(self) -> bool:
        return bool(self.results) and all(
            r.get("status") in {"accepted", "ok"} for r in self.results.values()
        )

    @property
    def has_any_accepted(self) -> bool:
        return any(r.get("status") in {"accepted", "ok"} for r in self.results.values())

    @property
    def all_published(self) -> bool:
        """Whether every accepted channel has already reached ``published``.

        A social ``submitted`` result is a successful terminal state for the
        batch, but it is not evidence that the remote post is visible yet.
        """
        return bool(self.results) and all(r.get("state") == PublishState.published.value for r in self.results.values())

    @property
    def all_succeeded(self) -> bool:
        return bool(self.results) and all(
            r.get("state") in {PublishState.submitted.value, PublishState.published.value}
            for r in self.results.values()
        )

    @property
    def all_ok(self) -> bool:
        """Backward-compatible alias for request acceptance, not publication."""
        return self.all_accepted

    @property
    def has_any_ok(self) -> bool:
        """Backward-compatible alias for request acceptance, not publication."""
        return self.has_any_accepted

    @property
    def errors(self) -> dict[str, str]:
        return {
            platform: str(r.get("detail") or r.get("error") or "unknown")
            for platform, r in self.results.items()
            if r.get("status") not in {"accepted", "ok"}
        }

    @property
    def platform_count(self) -> int:
        return len(self.results)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results.values() if r.get("status") in {"accepted", "ok"})

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
        payload = dict(publish_payload or {})
        if not self._get_enabled_platforms():
            return PublishAllResult(results={})
        batch, targets = self._get_or_create_batch(task_id, payload)
        return self._publish_to_platforms(task_id, batch, targets, payload)

    def publish_one(
        self,
        task_id: uuid.UUID,
        *,
        platform: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit one already-built publisher request through a one-target batch."""
        platform = normalize_publish_platform(platform)
        request = dict(payload or {})
        batch_id = request.get("batch_id")
        if batch_id:
            batch = self._db.get(PublishBatch, uuid.UUID(str(batch_id)))
            if not batch:
                raise ValueError("publish batch not found")
        else:
            account_id = request.get("account_id")
            batch = self._create_batch(
                task_id,
                [{
                    "key": publish_target_key(platform, account_id),
                    "platform": platform,
                    "account_id": str(account_id) if account_id else None,
                }],
                request,
            )
        request["batch_id"] = str(batch.id)
        data = self._publish_single(task_id, platform, request)
        reconciliation = reconcile_publish_batch(self._db, batch.id)
        self._db.commit()
        self._schedule_cleanup(task_id, reconciliation.cleanup_enqueued, batch.id)
        return self._accepted_response(platform, data)

    # ── 内部实现 ──────────────────────────────────────────────

    def _get_headers(self) -> dict[str, str]:
        if callable(self._http_headers):
            return self._http_headers()
        return self._http_headers or {}

    def _get_enabled_platforms(self) -> list[str]:
        settings_map = get_publish_platform_settings(self._db)
        return [p for p, enabled in settings_map.items() if enabled]

    def _create_batch(
        self,
        task_id: uuid.UUID,
        targets: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> PublishBatch:
        task = self._db.get(Task, task_id, with_for_update=True)
        if not task:
            raise ValueError("task not found")
        batch = PublishBatch(
            task_id=task_id,
            expected_targets=targets,
            request_json=payload,
            state=PublishBatchState.active.value,
        )
        self._db.add(batch)
        self._db.commit()
        self._db.refresh(batch)
        return batch

    def _get_or_create_batch(
        self,
        task_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> tuple[PublishBatch, list[dict[str, Any]]]:
        task = self._db.get(Task, task_id, with_for_update=True)
        if not task:
            raise ValueError("task not found")
        force_retry = bool(payload.get("force_retry"))
        active_batch = (
            self._db.query(PublishBatch)
            .filter(
                PublishBatch.task_id == task_id,
                PublishBatch.state.in_([PublishBatchState.active.value, PublishBatchState.partial_failed.value]),
            )
            .order_by(PublishBatch.created_at.desc())
            .first()
        )
        if active_batch and not force_retry:
            return active_batch, list(active_batch.expected_targets or [])

        targets: list[dict[str, Any]] = []
        for platform in self._get_enabled_platforms():
            account_id: str | None = None
            if platform == "bilibili":
                account_ids = payload.get("account_ids")
                account_id = (
                    str(account_ids.get(platform))
                    if isinstance(account_ids, dict) and account_ids.get(platform)
                    else (str(payload.get("account_id")) if payload.get("account_id") else None)
                )
            else:
                try:
                    account_id = self._resolve_social_account_id(platform, payload)
                except ValueError:
                    account_ids = payload.get("account_ids")
                    account_id = (
                        str(account_ids.get(platform))
                        if isinstance(account_ids, dict) and account_ids.get(platform)
                        else None
                    )
            targets.append(
                {
                    "key": publish_target_key(platform, account_id),
                    "platform": platform,
                    "account_id": account_id,
                }
            )
        if not targets:
            raise ValueError("no publish platforms are enabled")
        batch = self._create_batch(task_id, targets, payload)
        return batch, targets

    def _publish_to_platforms(
        self,
        task_id: uuid.UUID,
        batch: PublishBatch,
        targets: list[dict[str, Any]],
        base_payload: dict[str, Any] | None,
    ) -> PublishAllResult:
        results: dict[str, dict[str, Any]] = {}
        payload = dict(batch.request_json or base_payload or {})
        if base_payload and base_payload.get("force_retry"):
            payload["force_retry"] = True
        for target in targets:
            platform = str(target["platform"])
            account_id = target.get("account_id")
            existing = self._latest_batch_target_job(batch, platform, account_id)
            if existing and existing.state in {PublishState.submitting, PublishState.submitted, PublishState.published}:
                results[platform] = self._accepted_response(
                    platform,
                    self._response_from_job(existing),
                )
                continue
            if existing and existing.state == PublishState.unknown and not bool(payload.get("force_retry")):
                results[platform] = {
                    "status": "pending",
                    "platform": platform,
                    "state": existing.state.value,
                    "job_id": str(existing.id),
                    "detail": "publish state is unknown; use force_retry after checking the platform",
                }
                continue
            try:
                request = self._build_backend_request(task_id, platform, payload, account_id=account_id)
                request["batch_id"] = str(batch.id)
                result = self._publish_single(task_id, platform, request)
                results[platform] = self._accepted_response(platform, result)
            except httpx.HTTPStatusError as exc:
                detail = self._http_status_error_detail(exc)
                results[platform] = {
                    "status": "error",
                    "detail": detail,
                }
                reconciliation = record_publish_batch_dispatch_error(
                    self._db, batch.id, platform=platform, account_id=account_id, detail=detail
                )
                self._db.commit()
                self._schedule_cleanup(task_id, reconciliation.cleanup_enqueued, batch.id)
            except Exception as exc:
                detail = str(exc)
                results[platform] = {"status": "error", "detail": detail}
                reconciliation = record_publish_batch_dispatch_error(
                    self._db, batch.id, platform=platform, account_id=account_id, detail=detail
                )
                self._db.commit()
                self._schedule_cleanup(task_id, reconciliation.cleanup_enqueued, batch.id)
        reconciliation = reconcile_publish_batch(self._db, batch.id)
        self._db.commit()
        self._schedule_cleanup(task_id, reconciliation.cleanup_enqueued, batch.id)
        return PublishAllResult(results=results, batch_id=str(batch.id))

    def _latest_batch_target_job(
        self,
        batch: PublishBatch,
        platform: str,
        account_id: str | None,
    ) -> PublishJob | None:
        query = self._db.query(PublishJob).filter(
            PublishJob.batch_id == batch.id,
            PublishJob.platform == Platform(platform),
        )
        if account_id:
            query = query.filter(PublishJob.account_id == uuid.UUID(str(account_id)))
        else:
            query = query.filter(PublishJob.account_id.is_(None))
        return query.order_by(PublishJob.updated_at.desc(), PublishJob.created_at.desc()).first()

    @staticmethod
    def _response_from_job(job: PublishJob) -> dict[str, Any]:
        return {
            "job_id": str(job.id),
            "state": job.state.value,
            "aid": job.aid,
            "bvid": job.bvid,
            "external_id": job.external_id,
            "external_url": job.external_url,
        }

    @staticmethod
    def _accepted_response(platform: str, data: dict[str, Any]) -> dict[str, Any]:
        result = dict(data)
        result["platform"] = platform
        result["status"] = "accepted"
        return result

    @staticmethod
    def _schedule_cleanup(task_id: uuid.UUID, cleanup_enqueued: bool, batch_id: uuid.UUID) -> None:
        try:
            enqueue_publish_batch_cleanup(task_id, batch_id, needed=cleanup_enqueued)
        except Exception:
            # The state marker prevents duplicates; the periodic/manual cleanup
            # path can safely retry if the broker is temporarily unavailable.
            pass

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
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """
        将 auto mode 的原始 publish_payload 转换为后端期望的 gateway request 格式。
        复用 build_publish_gateway_request 的核心逻辑。
        """
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
        resolved_account_id = account_id
        if resolved_account_id is None:
            resolved_account_id = payload.get("account_id")
        if platform != "bilibili" and resolved_account_id is None:
            resolved_account_id = self._resolve_social_account_id(platform, payload)
        platform_meta = payload.get("platform_meta")
        meta = platform_meta.get(platform) if isinstance(platform_meta, dict) else None
        if meta is None:
            meta = payload.get("meta")

        request = build_publish_gateway_request(
            task=task,
            task_id=task_id,
            payload={
                **payload,
                "platform": platform,
                "account_id": resolved_account_id,
                "meta": meta,
            },
            video_key=video_key,
            db=self._db,
            s3=self._s3,
        )
        self._persist_platform_meta(task_id, platform, request.get("meta"))
        return request

    def _persist_platform_meta(self, task_id: uuid.UUID, platform: str, meta: Any) -> None:
        if not isinstance(meta, dict):
            return
        self._s3.put_bytes(
            json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
            publish_meta_key(task_id, platform),
            content_type="application/json",
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
            data.setdefault("state", PublishState.submitting.value)
            if platform == "bilibili" and data.get("bvid") and not data.get("external_url"):
                data["external_url"] = f"https://www.bilibili.com/video/{data['bvid']}"
            if platform == "bilibili" and not data.get("external_id"):
                data["external_id"] = data.get("bvid") or data.get("aid")
        return data
