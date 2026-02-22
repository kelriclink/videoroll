from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import AppSetting
from videoroll.utils.fernet import decrypt_str, encrypt_str


BILIBILI_AUTH_SETTINGS_KEY = "bilibili.auth"


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _get_row(db: Session) -> AppSetting:
    row = db.get(AppSetting, BILIBILI_AUTH_SETTINGS_KEY)
    if row:
        return row
    row = AppSetting(key=BILIBILI_AUTH_SETTINGS_KEY, value_json={})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _parse_cookie(cookie: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        out[k] = v
    return out


def _normalize_cookie_input(cookie: str) -> str:
    cookie = (cookie or "").replace("\r", " ").replace("\n", " ").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    return cookie


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


def get_bilibili_cookie_header(db: Session) -> str:
    row = db.get(AppSetting, BILIBILI_AUTH_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    cookie = _decrypt_opt(stored.get("cookie_enc"))
    if cookie:
        return cookie

    parts: list[str] = []
    sessdata = _decrypt_opt(stored.get("sessdata_enc"))
    if sessdata:
        parts.append(f"SESSDATA={sessdata}")
    bili_jct = _decrypt_opt(stored.get("bili_jct_enc"))
    if bili_jct:
        parts.append(f"bili_jct={bili_jct}")
    return "; ".join(parts)


def get_bilibili_csrf_token(db: Session) -> str:
    """
    Return bili_jct (csrf token) from stored cookie/settings.

    NOTE: Do not log this value.
    """
    row = db.get(AppSetting, BILIBILI_AUTH_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    cookie = _decrypt_opt(stored.get("cookie_enc"))
    if cookie:
        parsed = _parse_cookie(cookie)
        token = str(parsed.get("bili_jct") or "").strip()
        if token:
            return token

    token = _decrypt_opt(stored.get("bili_jct_enc"))
    return token


def get_bilibili_auth_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, BILIBILI_AUTH_SETTINGS_KEY)
    stored = dict(_as_dict(row.value_json)) if row else {}

    cookie = _decrypt_opt(stored.get("cookie_enc"))
    cookie_set = bool(cookie)
    cookie_map = _parse_cookie(cookie) if cookie else {}

    sessdata = cookie_map.get("SESSDATA") or _decrypt_opt(stored.get("sessdata_enc"))
    bili_jct = cookie_map.get("bili_jct") or _decrypt_opt(stored.get("bili_jct_enc"))

    return {
        "cookie_set": cookie_set,
        "sessdata_set": bool(sessdata),
        "bili_jct_set": bool(bili_jct),
    }


def update_bilibili_auth_settings(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    row = _get_row(db)
    stored = dict(_as_dict(row.value_json))

    if "cookie" in update:
        cookie = update.get("cookie")
        if cookie is None:
            pass
        else:
            cookie = _normalize_cookie_input(str(cookie))
            if not cookie:
                stored.pop("cookie_enc", None)
                stored.pop("sessdata_enc", None)
                stored.pop("bili_jct_enc", None)
            else:
                stored["cookie_enc"] = encrypt_str(cookie)
                parsed = _parse_cookie(cookie)
                if parsed.get("SESSDATA"):
                    stored["sessdata_enc"] = encrypt_str(parsed["SESSDATA"])
                if parsed.get("bili_jct"):
                    stored["bili_jct_enc"] = encrypt_str(parsed["bili_jct"])

    if "sessdata" in update:
        sessdata = update.get("sessdata")
        if sessdata is None:
            pass
        else:
            sessdata = str(sessdata).strip()
            if not sessdata:
                stored.pop("sessdata_enc", None)
            else:
                stored["sessdata_enc"] = encrypt_str(sessdata)

    if "bili_jct" in update:
        bili_jct = update.get("bili_jct")
        if bili_jct is None:
            pass
        else:
            bili_jct = str(bili_jct).strip()
            if not bili_jct:
                stored.pop("bili_jct_enc", None)
            else:
                stored["bili_jct_enc"] = encrypt_str(bili_jct)

    row.value_json = stored
    db.add(row)
    db.commit()

    return get_bilibili_auth_settings(db)
