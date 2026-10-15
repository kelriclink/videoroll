"""Isolated public Index-Translate API adapter; never routes research/tool turns here."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from typing import Callable, Sequence

import httpx

from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.provider_rate_limit import ProviderRateGate

log = logging.getLogger(__name__)
INDEX_API_BASE = "https://index-translate.bilibili.com/v1"
INDEX_MODEL = "Index-Translate-35B-A3B"
INDEX_USER_AGENT = "Index-Translate-Client/1.0"


def _bounded_context(segments: Sequence[Segment], *, start: int, limit: int) -> tuple[str, bool]:
    """Use the whole source transcript when it fits; otherwise local context plus global ends."""
    lines = [f"[{i + 1}] {seg.text.strip()}" for i, seg in enumerate(segments) if seg.text.strip()]
    full = "\n".join(lines)
    if len(full) <= limit:
        return full, True
    # For very long videos, never silently promise the whole source was supplied.
    nearby_start = max(0, start - 30)
    nearby_end = min(len(segments), start + 90)
    local = "\n".join(f"[{i+1}] {segments[i].text.strip()}" for i in range(nearby_start, nearby_end))
    side = max(0, (limit - min(len(local), limit // 2) - 110) // 2)
    return (
        "【视频开头】\n" + full[:side] +
        "\n【当前段落前后】\n" + local[:max(100, limit - side * 2 - 110)] +
        "\n【视频结尾】\n" + full[-side:],
        False,
    )


def _call(messages: list[dict[str, str]], cfg: dict, *, expect_json: bool) -> str:
    # Fixed, verified official endpoint/model; never reads the general OpenAI settings.
    url = INDEX_API_BASE + "/chat/completions"
    payload: dict = {
        "model": INDEX_MODEL,
        "messages": messages,
        "temperature": float(cfg.get("index_temperature", 0)),
        "max_tokens": int(cfg.get("index_max_tokens") or 4096),
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if expect_json:
        payload["response_format"] = {"type": "json_object"}
    retries = cfg.get("default_max_retries")
    retry_count = min(3, max(0, int(2 if retries is None else retries)))
    timeout = max(15.0, min(300.0, float(cfg.get("index_timeout_seconds") or 120.0)))
    gate = ProviderRateGate(
        os.getenv("REDIS_URL", ""), "index-translate-public",
        max_concurrency=2, slot_ttl_seconds=timeout + 90,
    )
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        for attempt in range(retry_count + 1):
            try:
                with gate.slot(wait_seconds=max(240., timeout * 4)):
                    response = client.post(url, json=payload, headers={
                        "User-Agent": INDEX_USER_AGENT,
                        "Content-Type": "application/json",
                    })
                if response.status_code in {429, 500, 502, 503, 504} and attempt < retry_count:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = min(15., max(1., float(2 ** attempt)))
                    if retry_after.isdigit():
                        delay = min(30., max(delay, float(retry_after)))
                    gate.set_cooldown(delay)
                    gate.record_failure()
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                gate.record_success()
                data = response.json()
                return str(data["choices"][0]["message"]["content"] or "").strip()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                gate.record_failure()
                if attempt >= retry_count:
                    raise RuntimeError(f"Index-Translate network failed: {type(exc).__name__}") from exc
                time.sleep(min(15, 2 ** attempt))
    raise RuntimeError("Index-Translate request failed")


def translate_title_index(title: str, *, target_lang: str, style: str,
                          settings: dict, context: str = "") -> str:
    # Context can dominate a small title in a translation-specialized model.
    # Demand a single JSON field and retry WITHOUT context if validation fails.
    title = title.strip()
    if not title:
        return title
    system = (
        "你是视频标题翻译器，仅翻译用户指定的【原标题】。"
        "绝对不可翻译【背景参考】中的文字，不可把背景补充到标题中。"
        '严格返回唯一的 JSON 对象：{"translation":"仅标题的译文"}。'
    )
    for use_context in (True, False) if context.strip() else (False,):
        req = (
            f"【硬性要求】只把下列原标题翻译成{target_lang}，风格：{style}。"
            "译文必须是一个单行视频标题，不允许输出视频脚本、解释或多条字幕。\n"
            f"【原标题】\n{title}\n"
        )
        if use_context:
            req += "【背景参考（软性要求，仅用于理解主题，绝不能翻译）】\n" + context.strip()[:3000]
        raw = _call([
            {"role": "system", "content": system},
            {"role": "user", "content": req},
        ], settings, expect_json=True)
        try:
            candidate = json.loads(raw).get("translation")
            if isinstance(candidate, str):
                candidate = candidate.strip().strip('"“”')
                # Reject accidentally translated transcripts / whole-context outputs.
                if candidate and "\n" not in candidate and len(candidate) <= max(60, len(title) * 3):
                    return candidate
        except (json.JSONDecodeError, AttributeError):
            pass
        log.warning("Index-Translate returned invalid title; retrying without context")
    raise RuntimeError("Index-Translate did not produce a valid single-line title")


def translate_text_index(text: str, *, target_lang: str, style: str, settings: dict) -> str:
    request = f"请将以下文本翻译为{target_lang}，风格为{style}，只输出译文，不要解释。\n\n{text}"
    answer = _call([{"role": "user", "content": request}], settings, expect_json=False)
    if not answer:
        raise RuntimeError("Index-Translate returned empty text")
    return answer


def translate_subtitles_index(
    segments: list[Segment],
    *, target_lang: str,
    style: str,
    settings: dict,
    batch_size: int,
    resume_from: list[Segment] | None = None,
    base_translations: list[Segment] | None = None,
    selected_indices: list[int] | None = None,
    on_batch_done: Callable[[list[Segment]], None] | None = None,
    report: Callable[[str], None] | None = None,
) -> list[Segment]:
    if not segments:
        return []
    selected = set(selected_indices or [])
    selective = bool(selected and base_translations is not None)
    if selective and len(base_translations or []) != len(segments):
        raise ValueError("Index selective translation requires matching source/base lengths")
    out = list(base_translations or []) if selective else list(resume_from or [])
    if len(out) > len(segments):
        raise ValueError("Index translation checkpoint longer than source")
    limit = max(4000, min(100000, int(settings.get("index_context_chars") or 48000)))
    width = max(1, min(50, int(batch_size or 12)))
    source_lang = "视频原始语言"
    glossary = str(settings.get("index_glossary") or "").strip()[:4000]
    extra = str(settings.get("index_instruction") or "").strip()[:2000]
    domain = str(settings.get("index_domain") or "视频字幕").strip()[:90]

    def translate_slice(first: int, last: int) -> list[Segment]:
        context, is_complete = _bounded_context(segments, start=first, limit=limit)
        if report and not is_complete:
            report(f"index context: transcript exceeds {limit} chars; using head/local/tail context for {first+1}-{last}")
        blocks = [{"idx": i+1, "text": segments[i].text} for i in range(first, last)]
        rules = [
            f"请将以下{source_lang}{domain}翻译成{target_lang}，并严格遵守约束。",
            "【硬性要求】仅翻译待翻译 blocks 的 text，保持 idx 与条数及顺序完全相同。",
            "【硬性要求】只返回 JSON 对象 {\"translations\": [{\"idx\": 数字, \"text\": \"译文\"}]}，不得添加额外条目。",
            "【硬性要求】严格保留技术符号、单位、专有名词、数字和代码；不允许凭参考材料增补原文不存在的内容。",
            f"【软性要求】目标风格：{style}；在准确基础上尽量适合字幕阅读和自然中文口播。",
            "【软性要求】下方完整视频原文只用于理解话题、前后文、代词和术语；绝对不能把参考文字当作待翻译字幕。",
        ]
        if glossary:
            rules.append("【硬性要求】固定术语对照：" + glossary)
        if extra:
            rules.append("【软性要求】" + extra)
        req = "\n".join(rules) + "\n\n【整段视频原文（软性参考）】\n" + context
        req += "\n\n【待翻译字幕块（只翻译这里）】\n" + json.dumps(blocks, ensure_ascii=False)
        # One failed/malformed group is split rather than silently losing subtitle lines.
        try:
            raw = _call([{"role": "user", "content": req}], settings, expect_json=True)
            data = json.loads(raw)
            rows = data.get("translations")
            if not isinstance(rows, list):
                raise ValueError("missing translations list")
            mapping: dict[int, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                idx = row.get("idx")
                if type(idx) not in (int, str):
                    continue
                idx = int(idx)
                text = row.get("text")
                if idx in mapping:
                    raise ValueError("duplicate idx")
                if first < idx <= last and isinstance(text, str) and text.strip():
                    mapping[idx] = text.strip()
            if set(mapping) != {i + 1 for i in range(first, last)}:
                raise ValueError("translation index/line mismatch")
            return [replace(segments[i], text=mapping[i+1], secondary_text=None) for i in range(first, last)]
        except (ValueError, json.JSONDecodeError) as exc:
            if last - first <= 1:
                raise RuntimeError(f"Index-Translate invalid translation at line {first+1}: {exc}") from exc
            middle = (first + last) // 2
            if report:
                report(f"index translation: retrying malformed batch {first+1}-{last} as smaller groups")
            return translate_slice(first, middle) + translate_slice(middle, last)

    if selective:
        positions = sorted(i - 1 for i in selected if 1 <= i <= len(segments))
        for pos in positions:
            out[pos] = translate_slice(pos, pos + 1)[0]
            if on_batch_done:
                on_batch_done(out)
        return out
    while len(out) < len(segments):
        first = len(out)
        next_batch = translate_slice(first, min(len(segments), first + width))
        out.extend(next_batch)
        if on_batch_done:
            on_batch_done(out)
        if report:
            report(f"index translation: {len(out)}/{len(segments)} subtitles complete")
    return out
