from __future__ import annotations

import math
import shutil
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoroll.ai.usage import PRICING_SETTINGS_KEY
from videoroll.config import OrchestratorSettings
from videoroll.db.models import (
    AIUsageEvent,
    Account,
    AlertEvent,
    AppSetting,
    RenderJob,
    RenderJobStatus,
    SubtitleJob,
    SubtitleJobStatus,
)


_MANAGED_ALERT_PREFIXES = (
    "disk:",
    "ai:http:",
    "account:",
    "queue:stale:",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _money(microusd: int | None) -> float | None:
    if microusd is None:
        return None
    return round(int(microusd) / 1_000_000.0, 6)


def _estimate_cost_from_pricing(
    pricing_models: dict[str, Any],
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> int | None:
    price = pricing_models.get(f"{provider}:{model}") or pricing_models.get(model)
    if not isinstance(price, dict):
        return None
    try:
        input_rate = float(price.get("input_per_million_usd") or 0.0)
        output_rate = float(price.get("output_per_million_usd") or 0.0)
    except (TypeError, ValueError):
        return None
    if input_rate < 0 or output_rate < 0:
        return None
    return max(0, int(round(int(input_tokens or 0) * input_rate + int(output_tokens or 0) * output_rate)))


def ai_usage_summary(db: Session, *, hours: int = 24, recent_limit: int = 50) -> dict[str, Any]:
    hours = max(1, min(24 * 90, int(hours or 24)))
    recent_limit = max(1, min(200, int(recent_limit or 50)))
    since = _utcnow() - timedelta(hours=hours)
    pricing_models = get_ai_pricing(db).get("models", {})
    if not isinstance(pricing_models, dict):
        pricing_models = {}

    totals = (
        db.query(
            func.count(AIUsageEvent.id),
            func.coalesce(func.sum(case((AIUsageEvent.success.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((AIUsageEvent.success.is_(False), 1), else_=0)), 0),
            func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
        )
        .filter(AIUsageEvent.created_at >= since)
        .one()
    )

    model_rows = (
        db.query(
            AIUsageEvent.provider,
            AIUsageEvent.model,
            func.count(AIUsageEvent.id),
            func.coalesce(func.sum(case((AIUsageEvent.success.is_(False), 1), else_=0)), 0),
            func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
            func.coalesce(
                func.sum(
                    case(
                        (AIUsageEvent.estimated_cost_microusd.is_not(None), AIUsageEvent.estimated_cost_microusd),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((AIUsageEvent.estimated_cost_microusd.is_not(None), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((AIUsageEvent.estimated_cost_microusd.is_(None), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (AIUsageEvent.estimated_cost_microusd.is_(None), AIUsageEvent.input_tokens),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (AIUsageEvent.estimated_cost_microusd.is_(None), AIUsageEvent.output_tokens),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .filter(AIUsageEvent.created_at >= since)
        .group_by(AIUsageEvent.provider, AIUsageEvent.model)
        .all()
    )

    by_model: list[dict[str, Any]] = []
    total_cost = 0
    priced_requests = 0
    for (
        provider,
        model,
        requests,
        failures,
        input_tokens,
        output_tokens,
        total_tokens,
        stored_cost,
        stored_priced,
        unpriced_requests,
        unpriced_input_tokens,
        unpriced_output_tokens,
    ) in model_rows:
        model_cost = int(stored_cost or 0)
        model_priced = int(stored_priced or 0)
        estimated_unpriced = _estimate_cost_from_pricing(
            pricing_models,
            provider=str(provider or ""),
            model=str(model or ""),
            input_tokens=int(unpriced_input_tokens or 0),
            output_tokens=int(unpriced_output_tokens or 0),
        )
        if estimated_unpriced is not None:
            model_cost += estimated_unpriced
            model_priced += int(unpriced_requests or 0)
        total_cost += model_cost
        priced_requests += model_priced
        by_model.append(
            {
                "provider": str(provider or ""),
                "model": str(model or ""),
                "requests": int(requests or 0),
                "failures": int(failures or 0),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "estimated_cost_usd": _money(model_cost) if model_priced else None,
            }
        )
    by_model.sort(key=lambda item: (-int(item["requests"]), str(item["provider"]), str(item["model"])))

    status_counts = [
        (status, int(count or 0))
        for status, count in (
            db.query(AIUsageEvent.status_code, func.count(AIUsageEvent.id))
            .filter(AIUsageEvent.created_at >= since, AIUsageEvent.success.is_(False))
            .group_by(AIUsageEvent.status_code)
            .all()
        )
    ]
    status_counts.sort(key=lambda item: (-item[1], -1 if item[0] is None else item[0]))

    recent_rows = (
        db.query(AIUsageEvent)
        .filter(AIUsageEvent.created_at >= since)
        .order_by(AIUsageEvent.created_at.desc())
        .limit(recent_limit)
        .all()
    )
    recent: list[dict[str, Any]] = []
    for row in recent_rows:
        effective_cost = (
            int(row.estimated_cost_microusd)
            if row.estimated_cost_microusd is not None
            else _estimate_cost_from_pricing(
                pricing_models,
                provider=str(row.provider or ""),
                model=str(row.model or ""),
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
            )
        )
        recent.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "provider": row.provider,
                "model": row.model,
                "operation": row.operation,
                "success": row.success,
                "status_code": row.status_code,
                "input_tokens": int(row.input_tokens or 0),
                "output_tokens": int(row.output_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "latency_ms": int(row.latency_ms or 0),
                "estimated_cost_usd": _money(effective_cost),
                "error_type": row.error_type,
                "error_message": row.error_message,
                "created_at": row.created_at,
            }
        )

    return {
        "hours": hours,
        "requests": int(totals[0] or 0),
        "successes": int(totals[1] or 0),
        "failures": int(totals[2] or 0),
        "input_tokens": int(totals[3] or 0),
        "output_tokens": int(totals[4] or 0),
        "total_tokens": int(totals[5] or 0),
        "priced_requests": priced_requests,
        "estimated_cost_usd": _money(total_cost) if priced_requests else None,
        "by_model": by_model,
        "by_status": [{"status_code": status, "count": count} for status, count in status_counts],
        "recent": recent,
    }


def get_ai_pricing(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, PRICING_SETTINGS_KEY)
    payload = row.value_json if row and isinstance(row.value_json, dict) else {}
    models = payload.get("models") if isinstance(payload, dict) else {}
    return {"models": models if isinstance(models, dict) else {}}


def set_ai_pricing(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw_models = payload.get("models") if isinstance(payload, dict) else {}
    if not isinstance(raw_models, dict):
        raise ValueError("models must be an object")
    models: dict[str, dict[str, float]] = {}
    for raw_key, raw_value in raw_models.items():
        key = str(raw_key or "").strip()
        if not key or len(key) > 320 or not isinstance(raw_value, dict):
            raise ValueError("each pricing model must have a non-empty model key and an object value")
        try:
            input_price = float(raw_value.get("input_per_million_usd") or 0.0)
            output_price = float(raw_value.get("output_per_million_usd") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid pricing for {key}") from exc
        if not math.isfinite(input_price) or not math.isfinite(output_price):
            raise ValueError(f"pricing for {key} must be finite")
        if input_price < 0 or output_price < 0:
            raise ValueError(f"pricing for {key} must be non-negative")
        models[key] = {
            "input_per_million_usd": input_price,
            "output_per_million_usd": output_price,
        }

    row = db.get(AppSetting, PRICING_SETTINGS_KEY)
    if row is None:
        row = AppSetting(key=PRICING_SETTINGS_KEY, value_json={"models": models})
    else:
        row.value_json = {"models": models}
    db.add(row)
    db.commit()
    return {"models": models}


def _alert_dict(row: AlertEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "fingerprint": row.fingerprint,
        "source": row.source,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "message": row.message,
        "details": dict(row.details_json or {}),
        "occurrence_count": int(row.occurrence_count or 1),
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "acknowledged_at": row.acknowledged_at,
        "resolved_at": row.resolved_at,
    }


def list_alerts(db: Session, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    query = db.query(AlertEvent)
    if status == "active":
        query = query.filter(AlertEvent.status.in_(["open", "acknowledged"]))
    elif status and status != "all":
        query = query.filter(AlertEvent.status == status)
    rows = query.order_by(AlertEvent.last_seen_at.desc()).limit(max(1, min(1000, int(limit)))).all()
    return [_alert_dict(row) for row in rows]


def upsert_alert(
    db: Session,
    *,
    fingerprint: str,
    source: str,
    severity: str,
    title: str,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> AlertEvent:
    now = _utcnow()
    fingerprint = str(fingerprint or "").strip()[:255]
    if not fingerprint:
        raise ValueError("alert fingerprint is required")
    row = (
        db.query(AlertEvent)
        .filter(AlertEvent.fingerprint == fingerprint)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        candidate = AlertEvent(
            fingerprint=fingerprint,
            source=str(source or "system")[:64],
            severity=str(severity or "warning")[:16],
            status="open",
            title=str(title or fingerprint)[:255],
            message=str(message or ""),
            details_json=dict(details or {}),
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            return candidate
        except IntegrityError:
            row = (
                db.query(AlertEvent)
                .filter(AlertEvent.fingerprint == fingerprint)
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                raise

    row.source = str(source or row.source)[:64]
    row.severity = str(severity or row.severity)[:16]
    row.title = str(title or row.title)[:255]
    row.message = str(message or "")
    row.details_json = dict(details or {})
    row.last_seen_at = now
    row.occurrence_count = int(row.occurrence_count or 0) + 1
    if row.status == "resolved":
        row.status = "open"
        row.resolved_at = None
        row.acknowledged_at = None
    db.add(row)
    db.flush()
    return row


def set_alert_status(db: Session, alert_id: uuid.UUID, *, status: str) -> AlertEvent | None:
    row = db.get(AlertEvent, alert_id)
    if row is None:
        return None
    now = _utcnow()
    if status == "acknowledged":
        row.status = "acknowledged"
        row.acknowledged_at = now
    elif status == "resolved":
        row.status = "resolved"
        row.resolved_at = now
    elif status == "open":
        row.status = "open"
        row.acknowledged_at = None
        row.resolved_at = None
    else:
        raise ValueError("invalid alert status")
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def report_alert(db: Session, payload: dict[str, Any]) -> AlertEvent | None:
    fingerprint = str(payload.get("fingerprint") or "").strip()
    if bool(payload.get("resolved")):
        row = db.query(AlertEvent).filter(AlertEvent.fingerprint == fingerprint).one_or_none()
        if row is None:
            return None
        row.status = "resolved"
        row.last_seen_at = _utcnow()
        row.resolved_at = row.last_seen_at
        row.details_json = dict(payload.get("details") or {})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    row = upsert_alert(
        db,
        fingerprint=fingerprint,
        source=str(payload.get("source") or "external"),
        severity=str(payload.get("severity") or "warning"),
        title=str(payload.get("title") or fingerprint),
        message=str(payload.get("message") or ""),
        details=dict(payload.get("details") or {}),
    )
    db.commit()
    db.refresh(row)
    return row


def _condition(
    active: dict[str, dict[str, Any]],
    *,
    fingerprint: str,
    source: str,
    severity: str,
    title: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    active[fingerprint] = {
        "fingerprint": fingerprint,
        "source": source,
        "severity": severity,
        "title": title,
        "message": message,
        "details": details or {},
    }


def _collect_scan_conditions(settings: OrchestratorSettings, db: Session) -> dict[str, dict[str, Any]]:
    now = _utcnow()
    active: dict[str, dict[str, Any]] = {}

    # Storage capacity.
    try:
        root = Path(settings.storage_root)
        usage = shutil.disk_usage(root)
        percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
        if percent >= 90.0:
            _condition(
                active,
                fingerprint="disk:storage",
                source="storage",
                severity="critical" if percent >= 95.0 else "warning",
                title="存储空间不足",
                message=f"{root} 已使用 {percent:.1f}%",
                details={"path": str(root), "used_percent": round(percent, 2), "free_bytes": int(usage.free)},
            )
    except Exception as exc:
        existing_storage_alert = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.fingerprint == "disk:storage",
                AlertEvent.status.in_(["open", "acknowledged"]),
            )
            .one_or_none()
        )
        if existing_storage_alert is not None:
            _condition(
                active,
                fingerprint=existing_storage_alert.fingerprint,
                source=existing_storage_alert.source,
                severity=existing_storage_alert.severity,
                title=existing_storage_alert.title,
                message=existing_storage_alert.message,
                details=dict(existing_storage_alert.details_json or {}),
            )
        _condition(
            active,
            fingerprint="disk:storage:probe",
            source="storage",
            severity="critical",
            title="存储状态探测失败",
            message=f"无法读取 {root} 的磁盘使用情况：{type(exc).__name__}",
            details={"path": str(root), "error": str(exc)[:500]},
        )

    # Account login/check failures.
    for account in db.query(Account).filter(Account.is_active.is_(True)).all():
        state = str(account.check_state or "").strip().lower()
        if state in {"error", "failed", "invalid", "expired", "unauthorized"}:
            _condition(
                active,
                fingerprint=f"account:{account.id}",
                source="account",
                severity="warning",
                title=f"{account.platform.value} 账号检查失败",
                message=account.last_check_message or f"账号 {account.name} 状态：{state}",
                details={"account_id": str(account.id), "account": account.name, "state": state},
            )

    # Stale worker heartbeats.
    stale_before = now - timedelta(minutes=10)
    stale_subtitles = db.query(SubtitleJob).filter(SubtitleJob.status == SubtitleJobStatus.running).limit(5000).all()
    for job in stale_subtitles:
        heartbeat = job.heartbeat_at or job.updated_at
        if heartbeat and heartbeat < stale_before:
            _condition(
                active,
                fingerprint=f"queue:stale:subtitle:{job.id}",
                source="queue",
                severity="critical",
                title="字幕任务疑似卡死",
                message=f"超过 10 分钟没有 heartbeat：{job.id}",
                details={"job_id": str(job.id), "task_id": str(job.task_id), "heartbeat_at": heartbeat.isoformat()},
            )
    stale_renders = db.query(RenderJob).filter(RenderJob.status == RenderJobStatus.running).limit(5000).all()
    for job in stale_renders:
        heartbeat = job.heartbeat_at or job.updated_at
        if heartbeat and heartbeat < stale_before:
            _condition(
                active,
                fingerprint=f"queue:stale:render:{job.id}",
                source="queue",
                severity="critical",
                title="渲染任务疑似卡死",
                message=f"超过 10 分钟没有 heartbeat：{job.id}",
                details={"job_id": str(job.id), "task_id": str(job.task_id), "heartbeat_at": heartbeat.isoformat()},
            )

    # Recent provider failures. 402 is immediately actionable; transient
    # throttling/server errors alert after repeated occurrences.
    ai_since = now - timedelta(minutes=15)
    failure_counts = (
        db.query(AIUsageEvent.status_code, func.count(AIUsageEvent.id))
        .filter(AIUsageEvent.created_at >= ai_since, AIUsageEvent.success.is_(False))
        .group_by(AIUsageEvent.status_code)
        .all()
    )
    counts = Counter({status: int(count or 0) for status, count in failure_counts})
    for status, count in counts.items():
        should_alert = (
            status == 402
            or (status == 429 and count >= 3)
            or (status is not None and 500 <= status <= 599 and count >= 3)
            or (status is None and count >= 3)
        )
        if should_alert:
            severity = "critical" if status == 402 else "warning"
            title = (
                "AI 余额/计费失败"
                if status == 402
                else "AI 网络错误过多"
                if status is None
                else f"AI HTTP {status} 错误过多"
            )
            status_label = "transport" if status is None else str(status)
            _condition(
                active,
                fingerprint=f"ai:http:{status_label}",
                source="ai",
                severity=severity,
                title=title,
                message=(
                    f"最近 15 分钟出现 {count} 次网络/传输错误。"
                    if status is None
                    else f"最近 15 分钟出现 {count} 次 HTTP {status}。"
                ),
                details={"status_code": status, "count": count, "window_minutes": 15},
            )

    return active


def scan_alerts(settings: OrchestratorSettings, db: Session) -> dict[str, Any]:
    now = _utcnow()
    active = _collect_scan_conditions(settings, db)
    updated = 0
    for condition in active.values():
        upsert_alert(db, **condition)
        updated += 1

    managed_open = (
        db.query(AlertEvent)
        .filter(AlertEvent.status.in_(["open", "acknowledged"]))
        .all()
    )
    resolved = 0
    for row in managed_open:
        if not row.fingerprint.startswith(_MANAGED_ALERT_PREFIXES):
            continue
        if row.fingerprint in active:
            continue
        row.status = "resolved"
        row.resolved_at = now
        row.updated_at = now
        db.add(row)
        resolved += 1

    db.commit()
    return {
        "scanned_at": now,
        "active": len(active),
        "opened_or_updated": updated,
        "resolved": resolved,
        "alerts": list_alerts(db, status="open", limit=200),
    }
