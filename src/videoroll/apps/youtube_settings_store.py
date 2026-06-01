from __future__ import annotations

import io
import http.cookiejar
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting
from videoroll.utils.fernet import decrypt_str, encrypt_str


YOUTUBE_SETTINGS_KEY = "youtube.settings"

_MAX_PROXY_LEN = 2048
_MAX_COOKIES_LEN = 200_000
_MAX_ERROR_LEN = 1000
_DEFAULT_HOME_SCAN_INTERVAL_MINUTES = 60
_MIN_HOME_SCAN_INTERVAL_MINUTES = 1
_MAX_HOME_SCAN_INTERVAL_MINUTES = 1440
_DEFAULT_HOME_SCAN_LIMIT = 10
_MIN_HOME_SCAN_LIMIT = 1
_MAX_HOME_SCAN_LIMIT = 100
_DEFAULT_HOME_SCAN_MIN_DURATION_SECONDS = 180
_MIN_HOME_SCAN_MIN_DURATION_SECONDS = 0
_MAX_HOME_SCAN_MIN_DURATION_SECONDS = 86_400
_MAX_HOME_SCAN_SAMPLE_URLS = 10
_MAX_HOME_SCAN_LOG_LINES = 40
_MAX_HOME_SCAN_LOG_LINE_LEN = 240
_AUTH_COOKIE_NAMES = {
    # Common Google/YouTube auth cookies. Presence usually means "logged in".
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "LOGIN_INFO",
}
_BOT_CHECK_BYPASS_COOKIE_NAMES = {
    # Typically set after solving Google's "unusual traffic" CAPTCHA.
    "GOOGLE_ABUSE_EXEMPTION",
}
_NETSCAPE_HEADER_TEXT = (
    "# Netscape HTTP Cookie File\n"
    "# http://curl.haxx.se/rfc/cookie_spec.html\n"
    "# This is a generated file!  Do not edit.\n"
    "\n"
)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, YOUTUBE_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=YOUTUBE_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _decrypt_opt(token: Any) -> str:
    if not isinstance(token, str):
        return ""
    token = token.strip()
    if not token:
        return ""
    try:
        return decrypt_str(token).strip()
    except Exception:
        return ""


def _parse_dt_opt(value: Any) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_home_scan_interval_minutes(value: Any) -> int:
    try:
        interval = int(value)
    except Exception:
        interval = _DEFAULT_HOME_SCAN_INTERVAL_MINUTES
    if interval < _MIN_HOME_SCAN_INTERVAL_MINUTES:
        interval = _MIN_HOME_SCAN_INTERVAL_MINUTES
    if interval > _MAX_HOME_SCAN_INTERVAL_MINUTES:
        interval = _MAX_HOME_SCAN_INTERVAL_MINUTES
    return interval


def _normalize_home_scan_limit(value: Any) -> int:
    try:
        limit = int(value)
    except Exception:
        limit = _DEFAULT_HOME_SCAN_LIMIT
    if limit < _MIN_HOME_SCAN_LIMIT:
        limit = _MIN_HOME_SCAN_LIMIT
    if limit > _MAX_HOME_SCAN_LIMIT:
        limit = _MAX_HOME_SCAN_LIMIT
    return limit


def _normalize_home_scan_min_duration_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except Exception:
        seconds = _DEFAULT_HOME_SCAN_MIN_DURATION_SECONDS
    if seconds < _MIN_HOME_SCAN_MIN_DURATION_SECONDS:
        seconds = _MIN_HOME_SCAN_MIN_DURATION_SECONDS
    if seconds > _MAX_HOME_SCAN_MIN_DURATION_SECONDS:
        seconds = _MAX_HOME_SCAN_MIN_DURATION_SECONDS
    return seconds


def _normalize_home_scan_sample_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= _MAX_HOME_SCAN_SAMPLE_URLS:
            break
    return out


def _normalize_home_scan_log_lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = str(item or "").strip()
        if not s:
            continue
        if len(s) > _MAX_HOME_SCAN_LOG_LINE_LEN:
            s = s[: _MAX_HOME_SCAN_LOG_LINE_LEN - 1] + "…"
        out.append(s)
        if len(out) >= _MAX_HOME_SCAN_LOG_LINES:
            break
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _get_locked_row(db: Session) -> AppSetting:
    row = db.execute(select(AppSetting).where(AppSetting.key == YOUTUBE_SETTINGS_KEY).with_for_update()).scalar_one_or_none()
    if row is not None:
        return row
    _get_row(db)
    return db.execute(select(AppSetting).where(AppSetting.key == YOUTUBE_SETTINGS_KEY).with_for_update()).scalar_one()


def get_youtube_cookies_txt(db: Session) -> str:
    row = db.get(AppSetting, YOUTUBE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}
    return _decrypt_opt(stored.get("cookies_txt_enc"))


def normalize_and_validate_netscape_cookies_txt(cookies_txt: str) -> str:
    raw = str(cookies_txt or "")
    raw = raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    raw_stripped = raw.strip()
    if not raw_stripped:
        return ""
    if raw_stripped.lower().startswith("cookie:"):
        raise ValueError("cookies_txt looks like a Cookie request header; please export cookies.txt (Netscape format)")
    if raw_stripped.startswith("{") or raw_stripped.startswith("["):
        raise ValueError("cookies_txt looks like JSON; please export cookies.txt (Netscape format)")
    if "\t" not in raw_stripped and "\n" not in raw_stripped and ";" in raw_stripped and "=" in raw_stripped:
        raise ValueError("cookies_txt looks like a Cookie header string; please export cookies.txt (Netscape format)")

    lines_in = raw.split("\n")
    # Drop leading blank lines so we can normalize the header deterministically.
    while lines_in and not str(lines_in[0] or "").strip():
        lines_in.pop(0)
    # Drop an existing header block if present.
    if lines_in and (lines_in[0].startswith("# Netscape HTTP Cookie File") or lines_in[0].startswith("# HTTP Cookie File")):
        lines_in.pop(0)
        while lines_in:
            line0 = str(lines_in[0] or "")
            if not line0.strip():
                lines_in.pop(0)
                break
            if line0.lstrip().startswith("#") and not line0.lstrip().startswith("#HttpOnly_"):
                lines_in.pop(0)
                continue
            break
    lines_out: list[str] = []
    for line in lines_in:
        if not line:
            lines_out.append("")
            continue

        stripped = line.strip()
        if not stripped:
            lines_out.append("")
            continue

        # Keep normal comments as-is. "#HttpOnly_" lines are data lines.
        if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
            lines_out.append(stripped)
            continue

        data_line = stripped
        httponly = False
        if data_line.startswith("#HttpOnly_"):
            httponly = True
            data_line = data_line[len("#HttpOnly_") :]

        if "\t" not in data_line:
            parts = data_line.split()
            if len(parts) == 7:
                data_line = "\t".join(parts)

        if "\t" in data_line:
            parts = data_line.split("\t")
            if len(parts) == 7:
                domain = str(parts[0] or "").strip()
                include_subdomains = str(parts[1] or "").strip().upper()
                if include_subdomains not in {"TRUE", "FALSE"}:
                    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"

                # Some exporters omit the leading dot for domain cookies even when include_subdomains is TRUE.
                # MozillaCookieJar expects a leading dot for such cookies. Prefer widening rather than shrinking
                # cookie scope to avoid breaking auth cookies unexpectedly.
                if include_subdomains == "TRUE" and domain and not domain.startswith(".") and "." in domain:
                    domain = "." + domain
                if include_subdomains == "FALSE" and domain.startswith("."):
                    include_subdomains = "TRUE"

                parts[0] = domain
                parts[1] = include_subdomains
                data_line = "\t".join(parts)

        if httponly:
            data_line = "#HttpOnly_" + data_line
        lines_out.append(data_line)

    normalized = _NETSCAPE_HEADER_TEXT + ("\n".join(lines_out).rstrip() + "\n")
    try:
        jar = http.cookiejar.MozillaCookieJar()
        jar._really_load(io.StringIO(normalized), "<youtube_cookies>", ignore_discard=True, ignore_expires=True)
    except http.cookiejar.LoadError as e:
        msg = str(e)
        if "does not look like a Netscape format cookies file" in msg:
            raise ValueError("invalid cookies.txt: missing header '# Netscape HTTP Cookie File'") from e
        raise ValueError(f"invalid cookies.txt: {msg}") from e
    except Exception as e:
        raise ValueError(f"invalid cookies.txt: {type(e).__name__}: {e}") from e

    return normalized


def summarize_netscape_cookies_txt(cookies_txt: str) -> dict[str, Any]:
    """
    Returns a lightweight summary for UI/debugging without exposing cookie values.
    """
    raw = str(cookies_txt or "").strip()
    if not raw:
        return {
            "cookies_count": 0,
            "cookies_domains_count": 0,
            "cookies_has_auth": False,
            "cookies_has_bot_check_bypass": False,
            "cookies_has_visitor_info": False,
        }

    try:
        normalized = normalize_and_validate_netscape_cookies_txt(raw)
    except Exception:
        return {
            "cookies_count": 0,
            "cookies_domains_count": 0,
            "cookies_has_auth": False,
            "cookies_has_bot_check_bypass": False,
            "cookies_has_visitor_info": False,
        }
    domains: set[str] = set()
    names: set[str] = set()
    count = 0

    for line in normalized.split("\n"):
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        line = line.strip()
        if not line:
            continue

        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) != 7:
            continue

        domain = str(parts[0] or "").strip().lstrip(".").lower()
        name = str(parts[5] or "").strip()
        if domain:
            domains.add(domain)
        if name:
            names.add(name)
        count += 1

    has_auth = bool(names & _AUTH_COOKIE_NAMES)
    has_bot_check_bypass = bool(names & _BOT_CHECK_BYPASS_COOKIE_NAMES)
    has_visitor_info = "VISITOR_INFO1_LIVE" in names

    return {
        "cookies_count": count,
        "cookies_domains_count": len(domains),
        "cookies_has_auth": has_auth,
        "cookies_has_bot_check_bypass": has_bot_check_bypass,
        "cookies_has_visitor_info": has_visitor_info,
    }


def _cookies_enabled(stored: dict[str, Any], cookies_txt: str) -> bool:
    raw = stored.get("cookies_enabled")
    if isinstance(raw, bool):
        return raw
    # Backward-compatible default: if cookies are set, consider them enabled.
    return bool(cookies_txt)


def get_youtube_settings(db: Session, *, default_proxy: Optional[str] = None) -> dict[str, Any]:
    row = db.get(AppSetting, YOUTUBE_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    if "proxy" in stored:
        proxy = stored.get("proxy")
    else:
        proxy = default_proxy

    proxy_str = str(proxy or "").strip()
    if len(proxy_str) > _MAX_PROXY_LEN:
        proxy_str = proxy_str[:_MAX_PROXY_LEN]

    cookies_txt = _decrypt_opt(stored.get("cookies_txt_enc"))
    summary = summarize_netscape_cookies_txt(cookies_txt) if cookies_txt else summarize_netscape_cookies_txt("")
    cookies_updated_at = str(stored.get("cookies_updated_at") or "").strip() or None
    now = datetime.now(timezone.utc)
    home_scan_lock_until = _parse_dt_opt(stored.get("home_scan_lock_until"))
    home_scan_running = bool(home_scan_lock_until and home_scan_lock_until > now)
    return {
        "proxy": proxy_str,
        "cookies_set": bool(cookies_txt),
        "cookies_enabled": _cookies_enabled(stored, cookies_txt),
        "cookies_updated_at": cookies_updated_at,
        "home_scan_enabled": bool(stored.get("home_scan_enabled")),
        "home_scan_interval_minutes": _normalize_home_scan_interval_minutes(stored.get("home_scan_interval_minutes")),
        "home_scan_limit": _normalize_home_scan_limit(stored.get("home_scan_limit")),
        "home_scan_long_videos_only": bool(stored.get("home_scan_long_videos_only")),
        "home_scan_min_duration_seconds": _normalize_home_scan_min_duration_seconds(stored.get("home_scan_min_duration_seconds")),
        "home_scan_running": home_scan_running,
        "home_scan_last_started_at": str(stored.get("home_scan_last_started_at") or "").strip() or None,
        "home_scan_last_finished_at": str(stored.get("home_scan_last_finished_at") or "").strip() or None,
        "home_scan_last_discovered_count": max(0, _safe_int(stored.get("home_scan_last_discovered_count"), 0)),
        "home_scan_last_started_count": max(0, _safe_int(stored.get("home_scan_last_started_count"), 0)),
        "home_scan_last_skipped_duplicates": max(0, _safe_int(stored.get("home_scan_last_skipped_duplicates"), 0)),
        "home_scan_last_failed_count": max(0, _safe_int(stored.get("home_scan_last_failed_count"), 0)),
        "home_scan_last_candidate_count": max(0, _safe_int(stored.get("home_scan_last_candidate_count"), 0)),
        "home_scan_last_explicit_shorts_count": max(0, _safe_int(stored.get("home_scan_last_explicit_shorts_count"), 0)),
        "home_scan_last_known_duration_count": max(0, _safe_int(stored.get("home_scan_last_known_duration_count"), 0)),
        "home_scan_last_unknown_duration_count": max(0, _safe_int(stored.get("home_scan_last_unknown_duration_count"), 0)),
        "home_scan_last_below_min_duration_count": max(0, _safe_int(stored.get("home_scan_last_below_min_duration_count"), 0)),
        "home_scan_last_kept_unknown_duration_count": max(0, _safe_int(stored.get("home_scan_last_kept_unknown_duration_count"), 0)),
        "home_scan_last_eligible_count": max(0, _safe_int(stored.get("home_scan_last_eligible_count"), 0)),
        "home_scan_last_log_lines": _normalize_home_scan_log_lines(stored.get("home_scan_last_log_lines")),
        "home_scan_last_error": (str(stored.get("home_scan_last_error") or "").strip() or None),
        "home_scan_last_sample_urls": _normalize_home_scan_sample_urls(stored.get("home_scan_last_sample_urls")),
        **summary,
    }


def update_youtube_settings(db: Session, update: dict[str, Any], *, default_proxy: Optional[str] = None) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "proxy" in update and update["proxy"] is not None:
        proxy = str(update.get("proxy") or "").strip()
        if len(proxy) > _MAX_PROXY_LEN:
            raise ValueError(f"proxy is too long (max {_MAX_PROXY_LEN} chars)")
        stored["proxy"] = proxy

    if "cookies_txt" in update:
        cookies_txt = update.get("cookies_txt")
        if cookies_txt is None:
            pass
        else:
            cookies_txt = str(cookies_txt).lstrip("\ufeff")
            if not cookies_txt.strip():
                stored.pop("cookies_txt_enc", None)
                stored["cookies_enabled"] = False
                stored.pop("cookies_updated_at", None)
            else:
                normalized = normalize_and_validate_netscape_cookies_txt(cookies_txt)
                if len(normalized) > _MAX_COOKIES_LEN:
                    raise ValueError(f"cookies_txt is too long (max {_MAX_COOKIES_LEN} chars)")
                stored["cookies_txt_enc"] = encrypt_str(normalized)
                stored["cookies_updated_at"] = datetime.now(timezone.utc).isoformat()
                if "cookies_enabled" not in update:
                    stored["cookies_enabled"] = True

    if "cookies_enabled" in update:
        enabled_raw = update.get("cookies_enabled")
        if enabled_raw is None:
            pass
        else:
            enabled = bool(enabled_raw)
            if enabled:
                cookies_txt = _decrypt_opt(stored.get("cookies_txt_enc"))
                if not cookies_txt:
                    raise ValueError("cookies_txt is not set; paste cookies.txt first")
                # Ensure it still validates (also normalizes legacy stored cookies).
                normalized = normalize_and_validate_netscape_cookies_txt(cookies_txt)
                if len(normalized) > _MAX_COOKIES_LEN:
                    raise ValueError(f"cookies_txt is too long (max {_MAX_COOKIES_LEN} chars)")
                stored["cookies_txt_enc"] = encrypt_str(normalized)
                stored["cookies_enabled"] = True
            else:
                stored["cookies_enabled"] = False

    if "home_scan_enabled" in update and update["home_scan_enabled"] is not None:
        stored["home_scan_enabled"] = bool(update["home_scan_enabled"])

    if "home_scan_interval_minutes" in update and update["home_scan_interval_minutes"] is not None:
        stored["home_scan_interval_minutes"] = _normalize_home_scan_interval_minutes(update["home_scan_interval_minutes"])

    if "home_scan_limit" in update and update["home_scan_limit"] is not None:
        stored["home_scan_limit"] = _normalize_home_scan_limit(update["home_scan_limit"])

    if "home_scan_long_videos_only" in update and update["home_scan_long_videos_only"] is not None:
        stored["home_scan_long_videos_only"] = bool(update["home_scan_long_videos_only"])

    if "home_scan_min_duration_seconds" in update and update["home_scan_min_duration_seconds"] is not None:
        stored["home_scan_min_duration_seconds"] = _normalize_home_scan_min_duration_seconds(update["home_scan_min_duration_seconds"])

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_youtube_settings(db, default_proxy=default_proxy)


def try_acquire_youtube_home_scan_lock(
    db: Session,
    *,
    owner: str,
    ttl_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    lock_owner = str(owner or "").strip()
    if not lock_owner:
        raise ValueError("owner is required")
    ttl = max(30, int(ttl_seconds or 0))
    now_dt = now or datetime.now(timezone.utc)

    row = _get_locked_row(db)
    stored = dict(_as_dict(row.value_json))
    current_owner = str(stored.get("home_scan_lock_owner") or "").strip()
    current_until = _parse_dt_opt(stored.get("home_scan_lock_until"))
    if current_owner and current_until and current_until > now_dt and current_owner != lock_owner:
        db.rollback()
        return False

    stored["home_scan_lock_owner"] = lock_owner
    stored["home_scan_lock_until"] = (now_dt + timedelta(seconds=ttl)).isoformat()
    stored["home_scan_last_started_at"] = now_dt.isoformat()
    row.value_json = stored
    db.add(row)
    db.commit()
    return True


def finish_youtube_home_scan_lock(
    db: Session,
    *,
    owner: str,
    discovered_count: int = 0,
    started_count: int = 0,
    skipped_duplicates: int = 0,
    failed_count: int = 0,
    error: Optional[str] = None,
    sample_urls: Optional[list[str]] = None,
    candidate_count: int = 0,
    explicit_shorts_count: int = 0,
    known_duration_count: int = 0,
    unknown_duration_count: int = 0,
    below_min_duration_count: int = 0,
    kept_unknown_duration_count: int = 0,
    eligible_count: int = 0,
    log_lines: Optional[list[str]] = None,
    finished_at: Optional[datetime] = None,
) -> dict[str, Any]:
    lock_owner = str(owner or "").strip()
    if not lock_owner:
        raise ValueError("owner is required")
    row = _get_locked_row(db)
    stored = dict(_as_dict(row.value_json))
    current_owner = str(stored.get("home_scan_lock_owner") or "").strip()
    if current_owner and current_owner != lock_owner:
        db.rollback()
        return get_youtube_settings(db)

    stored.pop("home_scan_lock_owner", None)
    stored.pop("home_scan_lock_until", None)
    stored["home_scan_last_finished_at"] = (finished_at or datetime.now(timezone.utc)).isoformat()
    stored["home_scan_last_discovered_count"] = max(0, int(discovered_count or 0))
    stored["home_scan_last_started_count"] = max(0, int(started_count or 0))
    stored["home_scan_last_skipped_duplicates"] = max(0, int(skipped_duplicates or 0))
    stored["home_scan_last_failed_count"] = max(0, int(failed_count or 0))
    stored["home_scan_last_sample_urls"] = _normalize_home_scan_sample_urls(sample_urls or [])
    stored["home_scan_last_candidate_count"] = max(0, int(candidate_count or 0))
    stored["home_scan_last_explicit_shorts_count"] = max(0, int(explicit_shorts_count or 0))
    stored["home_scan_last_known_duration_count"] = max(0, int(known_duration_count or 0))
    stored["home_scan_last_unknown_duration_count"] = max(0, int(unknown_duration_count or 0))
    stored["home_scan_last_below_min_duration_count"] = max(0, int(below_min_duration_count or 0))
    stored["home_scan_last_kept_unknown_duration_count"] = max(0, int(kept_unknown_duration_count or 0))
    stored["home_scan_last_eligible_count"] = max(0, int(eligible_count or 0))
    stored["home_scan_last_log_lines"] = _normalize_home_scan_log_lines(log_lines or [])
    msg = str(error or "").strip()
    if msg:
        if len(msg) > _MAX_ERROR_LEN:
            msg = msg[: _MAX_ERROR_LEN - 1] + "…"
        stored["home_scan_last_error"] = msg
    else:
        stored.pop("home_scan_last_error", None)

    row.value_json = stored
    db.add(row)
    db.commit()
    return get_youtube_settings(db)
