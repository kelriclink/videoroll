from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.publish_review import clamp_review_text, normalize_blocked_words
from videoroll.db.models import AppSetting


PUBLISH_REVIEW_SETTINGS_KEY = "publish.review.settings"
_PUBLISH_REVIEW_RESULT_PREFIX = "publish.review.task."


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _settings_defaults() -> dict[str, Any]:
    return {
        "enabled": True,
        "blocked_words": [],
        "ai_rules": "",
    }


def _result_defaults() -> dict[str, Any]:
    return {
        "checked": False,
        "ok": None,
        "reason": None,
        "matched_blocked_words": [],
        "review_mode": None,
        "risk_tags": [],
        "title": None,
        "summary": None,
        "subtitle_chars": 0,
        "checked_at": None,
    }


def _normalize_settings(data: dict[str, Any]) -> dict[str, Any]:
    baseline = _settings_defaults()
    merged = {**baseline, **_as_dict(data)}
    return {
        "enabled": bool(merged.get("enabled")),
        "blocked_words": normalize_blocked_words(merged.get("blocked_words")),
        "ai_rules": clamp_review_text(merged.get("ai_rules"), 4000),
    }


def _normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    baseline = _result_defaults()
    merged = {**baseline, **_as_dict(data)}

    checked = bool(merged.get("checked"))
    ok_raw = merged.get("ok")
    ok = bool(ok_raw) if ok_raw is not None else None
    reason = clamp_review_text(merged.get("reason"), 400) or None
    matched_blocked_words = normalize_blocked_words(merged.get("matched_blocked_words"))

    review_mode = str(merged.get("review_mode") or "").strip() or None

    risk_tags_raw = merged.get("risk_tags")
    risk_tags: list[str] = []
    seen_tags: set[str] = set()
    if isinstance(risk_tags_raw, list):
        for item in risk_tags_raw:
            s = str(item or "").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen_tags:
                continue
            seen_tags.add(key)
            risk_tags.append(s[:40])

    try:
        subtitle_chars = max(0, int(merged.get("subtitle_chars") or 0))
    except Exception:
        subtitle_chars = 0

    checked_at = str(merged.get("checked_at") or "").strip() or None
    title = clamp_review_text(merged.get("title"), 160) or None
    summary = clamp_review_text(merged.get("summary"), 500) or None

    return {
        "checked": checked,
        "ok": ok,
        "reason": reason,
        "matched_blocked_words": matched_blocked_words,
        "review_mode": review_mode,
        "risk_tags": risk_tags,
        "title": title,
        "summary": summary,
        "subtitle_chars": subtitle_chars,
        "checked_at": checked_at,
    }


def _result_key(task_id: str) -> str:
    return f"{_PUBLISH_REVIEW_RESULT_PREFIX}{task_id}"


def _get_row(db: Session, key: str) -> AppSetting:
    row = db.get(AppSetting, key)
    if row:
        return row
    row = AppSetting(key=key, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_publish_review_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, PUBLISH_REVIEW_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    return _normalize_settings(stored)


def update_publish_review_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db, PUBLISH_REVIEW_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json))

    if "enabled" in update and update["enabled"] is not None:
        stored["enabled"] = bool(update["enabled"])
    if "blocked_words" in update and update["blocked_words"] is not None:
        stored["blocked_words"] = list(update["blocked_words"])
    if "ai_rules" in update and update["ai_rules"] is not None:
        stored["ai_rules"] = str(update["ai_rules"])

    row.value_json = _normalize_settings(stored)
    db.add(row)
    db.commit()
    return get_publish_review_settings(db)


def get_task_publish_review(db: Session, task_id: str) -> dict[str, Any]:
    row = db.get(AppSetting, _result_key(task_id))
    stored = dict(_as_dict(row.value_json)) if row else {}
    return _normalize_result(stored)


def set_task_publish_review(
    db: Session,
    task_id: str,
    *,
    ok: bool,
    reason: str,
    matched_blocked_words: list[str] | None = None,
    review_mode: str | None = None,
    risk_tags: list[str] | None = None,
    title: str | None = None,
    summary: str | None = None,
    subtitle_chars: int | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    row = _get_row(db, _result_key(task_id))
    payload: dict[str, Any] = {
        "checked": True,
        "ok": bool(ok),
        "reason": reason,
        "matched_blocked_words": list(matched_blocked_words or []),
        "review_mode": review_mode,
        "risk_tags": list(risk_tags or []),
        "title": title,
        "summary": summary,
        "subtitle_chars": subtitle_chars,
        "checked_at": checked_at or _now_iso(),
    }
    row.value_json = _normalize_result(payload)
    db.add(row)
    db.commit()
    return get_task_publish_review(db, task_id)
