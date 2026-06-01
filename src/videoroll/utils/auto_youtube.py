from __future__ import annotations

from typing import Any


_AUTO_YOUTUBE_KV_SEP = ";"
_AUTO_YOUTUBE_ALLOWED_ORIGINS = {
    "auto_youtube",
    "youtube_home_scan",
    "youtube_task_restart",
}


def encode_auto_youtube_created_by(origin: str, *, auto_publish: bool | None) -> str:
    raw_origin = str(origin or "").strip().lower() or "auto_youtube"
    safe_origin = raw_origin if raw_origin in _AUTO_YOUTUBE_ALLOWED_ORIGINS else "auto_youtube"
    publish_bit = ""
    if auto_publish is not None:
        publish_bit = "1" if bool(auto_publish) else "0"
    return f"{safe_origin}{_AUTO_YOUTUBE_KV_SEP}auto_publish={publish_bit}"


def parse_auto_youtube_created_by(value: Any) -> dict[str, Any] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(_AUTO_YOUTUBE_KV_SEP) if str(part).strip()]
    if not parts:
        return None
    origin = parts[0].lower()
    if origin not in _AUTO_YOUTUBE_ALLOWED_ORIGINS:
        return None

    auto_publish: bool | None = None
    for part in parts[1:]:
        if not part.startswith("auto_publish="):
            continue
        flag = part.split("=", 1)[1].strip().lower()
        if flag in {"1", "true", "yes", "on"}:
            auto_publish = True
        elif flag in {"0", "false", "no", "off"}:
            auto_publish = False
        else:
            auto_publish = None
        break
    return {"origin": origin, "auto_publish": auto_publish}
