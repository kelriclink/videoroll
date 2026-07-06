from __future__ import annotations

import re
from typing import Any, Iterable

from videoroll.ai.client import OpenAIChatConfig
from videoroll.ai.service import AIService, review_publish_content_openai


_SPLIT_WORDS_RE = re.compile(r"[\n,，;；]+")
_SRT_INDEX_RE = re.compile(r"^\d+$")
_ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_REVIEW_TITLE_MAX_CHARS = 120
_REVIEW_SUMMARY_MAX_CHARS = 3000
_REVIEW_SUBTITLE_MAX_CHARS = 12000
_REVIEW_RULES_MAX_CHARS = 4000


def clamp_review_text(text: Any, max_chars: int) -> str:
    s = str(text or "").strip()
    if len(s) <= max_chars:
        return s
    if max_chars <= 1:
        return s[:max_chars]
    return s[: max_chars - 1] + "…"


def normalize_blocked_words(words: Iterable[str] | str | None) -> list[str]:
    items: list[str]
    if words is None:
        items = []
    elif isinstance(words, str):
        items = _SPLIT_WORDS_RE.split(words)
    else:
        items = [str(item or "") for item in words]

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = str(item or "").strip()
        if not s:
            continue
        if len(s) > 80:
            s = s[:80]
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def extract_subtitle_plain_text(raw_text: str) -> str:
    lines: list[str] = []
    for raw_line in str(raw_text or "").splitlines():
        line = raw_line.strip()
        if not line or _SRT_INDEX_RE.fullmatch(line) or "-->" in line:
            continue
        line = line.replace("\\N", "\n").replace("\\n", "\n")
        line = _ASS_TAG_RE.sub("", line)
        line = _HTML_TAG_RE.sub("", line)
        for part in line.splitlines():
            s = part.strip()
            if s:
                lines.append(s)

    merged = " ".join(lines)
    return _WS_RE.sub(" ", merged).strip()


def find_blocked_words(*texts: str, blocked_words: Iterable[str] | str | None) -> list[str]:
    candidates = normalize_blocked_words(blocked_words)
    haystacks = [str(text or "").lower() for text in texts if str(text or "").strip()]
    if not haystacks:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for word in candidates:
        key = word.lower()
        if key in seen:
            continue
        if any(key in haystack for haystack in haystacks):
            seen.add(key)
            out.append(word)
    return out


def review_publish_materials(
    *,
    title: str,
    summary: str,
    subtitle_text: str,
    blocked_words: Iterable[str] | str | None,
    reject_rules: str,
    config: OpenAIChatConfig | None = None,
    ai_service: AIService | None = None,
) -> dict[str, Any]:
    title_out = clamp_review_text(title, _REVIEW_TITLE_MAX_CHARS)
    summary_out = clamp_review_text(summary, _REVIEW_SUMMARY_MAX_CHARS)
    subtitle_plain = extract_subtitle_plain_text(subtitle_text)
    subtitle_excerpt = clamp_review_text(subtitle_plain, _REVIEW_SUBTITLE_MAX_CHARS)
    rules_out = clamp_review_text(reject_rules, _REVIEW_RULES_MAX_CHARS)

    matched_words = find_blocked_words(title_out, summary_out, subtitle_excerpt, blocked_words=blocked_words)
    if matched_words:
        return {
            "ok": False,
            "reason": f"命中违禁词：{', '.join(matched_words)}",
            "matched_blocked_words": matched_words,
            "review_mode": "blocked_words",
            "risk_tags": ["blocked_word"],
            "title": title_out or None,
            "summary": summary_out or None,
            "subtitle_chars": len(subtitle_plain),
        }

    if not (title_out or summary_out or subtitle_excerpt):
        return {
            "ok": False,
            "reason": "缺少可供审核的标题、总结或字幕内容",
            "matched_blocked_words": [],
            "review_mode": "input_missing",
            "risk_tags": ["input_missing"],
            "title": None,
            "summary": None,
            "subtitle_chars": 0,
        }

    if config is None and ai_service is None:
        return {
            "ok": False,
            "reason": "AI 审核已启用，但未配置 OpenAI API Key",
            "matched_blocked_words": [],
            "review_mode": "config_missing",
            "risk_tags": ["config_missing"],
            "title": title_out or None,
            "summary": summary_out or None,
            "subtitle_chars": len(subtitle_plain),
        }

    if ai_service is not None:
        data = ai_service.review_publish_content(
            title=title_out,
            summary=summary_out,
            subtitle_excerpt=subtitle_excerpt,
            reject_rules=rules_out,
        )
    else:
        data = review_publish_content_openai(
            title=title_out,
            summary=summary_out,
            subtitle_excerpt=subtitle_excerpt,
            reject_rules=rules_out,
            config=config,
        )
    ok = bool(data.get("approved"))
    reason = str(data.get("reason") or "").strip() or ("审核通过" if ok else "AI 未提供不通过原因")

    risk_tags_raw = data.get("risk_tags")
    risk_tags: list[str] = []
    if isinstance(risk_tags_raw, list):
        seen_risk: set[str] = set()
        for item in risk_tags_raw:
            s = str(item or "").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen_risk:
                continue
            seen_risk.add(key)
            risk_tags.append(s[:40])

    return {
        "ok": ok,
        "reason": clamp_review_text(reason, 160),
        "matched_blocked_words": [],
        "review_mode": "ai",
        "risk_tags": risk_tags,
        "title": title_out or None,
        "summary": summary_out or None,
        "subtitle_chars": len(subtitle_plain),
    }
