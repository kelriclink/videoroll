from __future__ import annotations

from typing import Any, Callable

from videoroll.apps.bilibili_publisher.constants import BILIBILI_DESC_MAX_CHARS


def has_cjk(text: str) -> bool:
    return bool(text) and any(
        "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"
        for ch in text
    )


def clamp_text(text: str, max_len: int) -> str:
    s = str(text or "").strip()
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1] + "…"


def build_bilibili_desc(youtube_desc: str, source_url: str) -> str:
    source_line = f"原视频：{str(source_url or '').strip()}".strip()
    max_len = BILIBILI_DESC_MAX_CHARS
    base = str(youtube_desc or "").strip()
    if source_line:
        base = base.replace(source_line, "").strip()

    if not source_line:
        return clamp_text(base, max_len)
    if len(source_line) >= max_len:
        return clamp_text(source_line, max_len)

    sep = "\n\n" if base else ""
    avail = max_len - len(source_line) - len(sep)
    if len(base) > avail:
        base = clamp_text(base, avail)
    out = f"{source_line}{sep}{base}" if base else source_line
    return clamp_text(out, max_len)


def apply_publish_source_overrides(
    meta: dict[str, Any],
    *,
    source_title: str,
    source_description: str,
    source_url: str,
    title_prefix: str = "",
    enable_reprint: bool = True,
    translated_title: str | None = None,
    title_transform: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    meta_out = dict(meta if isinstance(meta, dict) else {})
    title_out = str(translated_title or "").strip() or str(source_title or "").strip() or str(meta_out.get("title") or "").strip() or "未命名"
    if not str(translated_title or "").strip() and title_transform is not None:
        title_out = title_transform(title_out)

    prefix = str(title_prefix or "").strip()
    if prefix and title_out and not title_out.startswith(prefix):
        title_out = prefix + title_out

    source_url_out = str(source_url or "").strip()
    meta_out["title"] = clamp_text(title_out, 80) or clamp_text(str(source_title or ""), 80) or "未命名"
    if str(source_description or "").strip() or source_url_out:
        meta_out["desc"] = build_bilibili_desc(source_description, source_url_out)

    if enable_reprint:
        meta_out["copyright"] = 2
        meta_out["source"] = source_url_out
    else:
        meta_out["copyright"] = 1
        meta_out["source"] = ""
    return meta_out
