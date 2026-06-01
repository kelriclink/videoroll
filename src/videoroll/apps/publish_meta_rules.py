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


def bilibili_text_units(text: str) -> int:
    # B 站投稿简介的边界用例会把中文/全角字符按 2 个单位计算。
    return sum(1 if ord(ch) < 128 else 2 for ch in str(text or ""))


def _take_bilibili_text_units(text: str, max_units: int) -> str:
    if max_units <= 0:
        return ""

    out: list[str] = []
    used = 0
    for ch in str(text or ""):
        units = 1 if ord(ch) < 128 else 2
        if used + units > max_units:
            break
        out.append(ch)
        used += units
    return "".join(out)


def clamp_bilibili_text(text: str, max_units: int) -> str:
    s = str(text or "").strip()
    if bilibili_text_units(s) <= max_units:
        return s
    if max_units <= 0:
        return ""

    marker = "…"
    marker_units = bilibili_text_units(marker)
    if max_units <= marker_units:
        return _take_bilibili_text_units(s, max_units)

    body = _take_bilibili_text_units(s, max_units - marker_units).rstrip()
    return f"{body}{marker}"


def append_title_uploader_suffix(title: str, source_uploader: str, *, max_len: int = 80) -> str:
    title_text = str(title or "").strip() or "未命名"
    uploader_text = " ".join(str(source_uploader or "").strip().split())
    if not uploader_text:
        return clamp_text(title_text, max_len)

    if title_text.endswith(f"-{uploader_text}") or title_text.endswith(f" - {uploader_text}"):
        return clamp_text(title_text, max_len)

    suffix = f"-{uploader_text}"
    available = max_len - len(suffix)
    if available <= 0:
        return clamp_text(uploader_text, max_len)

    return f"{clamp_text(title_text, available).rstrip()}{suffix}"


def _strip_existing_source_prefix(text: str) -> str:
    source_text = str(text or "").strip()
    if not source_text:
        return ""

    lines = source_text.splitlines()
    idx = 0
    removed = False
    while idx < len(lines):
        line = str(lines[idx] or "").strip()
        if not line:
            if removed:
                idx += 1
                continue
            break
        if line.startswith(("原视频：", "原视频:")):
            removed = True
            idx += 1
            continue
        if removed and line.startswith(("博主：", "博主:", "作者：", "作者:", "UP：", "UP:")):
            idx += 1
            continue
        break
    if not removed:
        return source_text
    return "\n".join(lines[idx:]).strip()


def build_bilibili_source_block(source_url: str, source_uploader: str = "", *, max_len: int = BILIBILI_DESC_MAX_CHARS) -> str:
    source_url_text = str(source_url or "").strip()
    source_uploader_text = str(source_uploader or "").strip()
    if source_url_text:
        source_line = f"原视频：{source_url_text}"
        if not source_uploader_text:
            return clamp_bilibili_text(source_line, max_len)
        uploader_prefix = "博主："
        avail = max_len - bilibili_text_units(source_line) - bilibili_text_units("\n") - bilibili_text_units(uploader_prefix)
        if avail <= 0:
            return clamp_bilibili_text(source_line, max_len)
        return f"{source_line}\n{uploader_prefix}{clamp_bilibili_text(source_uploader_text, avail)}"
    if source_uploader_text:
        return clamp_bilibili_text(f"博主：{source_uploader_text}", max_len)
    return ""


def build_bilibili_desc(youtube_desc: str, source_url: str, source_uploader: str = "") -> str:
    source_line = build_bilibili_source_block(source_url, source_uploader, max_len=BILIBILI_DESC_MAX_CHARS)
    max_len = BILIBILI_DESC_MAX_CHARS
    base = _strip_existing_source_prefix(youtube_desc)

    if not source_line:
        return clamp_bilibili_text(base, max_len)
    if bilibili_text_units(source_line) >= max_len:
        return source_line

    sep = "\n\n" if base else ""
    avail = max_len - bilibili_text_units(source_line) - bilibili_text_units(sep)
    if avail <= 0:
        return source_line
    if bilibili_text_units(base) > avail:
        base = clamp_bilibili_text(base, avail)
    return f"{source_line}{sep}{base}" if base else source_line


def apply_publish_source_overrides(
    meta: dict[str, Any],
    *,
    source_title: str,
    source_description: str,
    source_url: str,
    source_uploader: str = "",
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
    title_out = append_title_uploader_suffix(title_out, source_uploader, max_len=80)
    meta_out["title"] = title_out or clamp_text(str(source_title or ""), 80) or "未命名"
    if str(source_description or "").strip() or source_url_out:
        meta_out["desc"] = build_bilibili_desc(source_description, source_url_out, source_uploader)

    if enable_reprint:
        meta_out["copyright"] = 2
        meta_out["source"] = source_url_out
    else:
        meta_out["copyright"] = 1
        meta_out["source"] = ""
    return meta_out
