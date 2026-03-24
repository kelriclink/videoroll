from __future__ import annotations

from typing import Any

from videoroll.ai.client import OpenAIChatConfig, request_openai_json_object


def translate_text_openai(
    text: str,
    *,
    target_lang: str,
    style: str,
    config: OpenAIChatConfig,
) -> str:
    source = str(text or "").strip()
    if not source:
        return text

    tgt = (target_lang or "zh").strip() or "zh"
    tone = (style or "").strip() or "口语自然"
    data = request_openai_json_object(
        config=config,
        system_prompt="You are a professional translator. Return ONLY valid JSON.",
        user_prompt=(
            "请翻译下面这段文本。\n"
            "要求：\n"
            "- 只输出 JSON 对象，不要输出解释；\n"
            "- 只翻译内容本身，不要补充；\n"
            f"- 目标语言：{tgt}\n"
            f"- 风格：{tone}\n\n"
            f"输入文本：\n{source}\n\n"
            '输出 JSON：{"translation":""}'
        ),
    )
    translated = str(data.get("translation") or "").strip()
    return translated or source


def generate_bilibili_tags_openai(
    *,
    title: str,
    summary: str,
    transcript: str,
    config: OpenAIChatConfig,
    n_tags: int = 6,
) -> list[str]:
    clean_title = str(title or "").strip()
    clean_summary = str(summary or "").strip()
    clean_transcript = str(transcript or "").strip()
    n = max(1, int(n_tags))

    data = request_openai_json_object(
        config=config,
        system_prompt="You are a professional video SEO assistant. Return ONLY valid JSON.",
        user_prompt=(
            "请为 Bilibili 投稿生成视频标签（tags）。\n"
            f"- 只生成 {n} 个标签（不要多也不要少）\n"
            "- 不要包含 'videoroll'\n"
            "- 标签语言优先中文，必要时可保留常用英文缩写\n"
            "- 每个标签尽量短（建议 2~12 字），不要带 # 号，不要带空格或标点\n"
            "- 标签尽量覆盖：主题/领域/核心对象/关键技术/结果或亮点\n\n"
            f"标题：{clean_title}\n\n"
            f"摘要（如有）：{clean_summary}\n\n"
            f"字幕全文片段（可能截断）：\n{clean_transcript}\n\n"
            '输出 JSON（不要 Markdown / 不要解释）：{"tags":["tag1","tag2",...]}'
        ),
    )

    tags = data.get("tags")
    if not isinstance(tags, list):
        raise RuntimeError("OpenAI output missing 'tags' list")

    out: list[str] = []
    seen: set[str] = set()
    for item in tags:
        s = str(item or "").strip().lstrip("#").lstrip("＃")
        s = "".join(s.split())
        if not s:
            continue
        if s.lower() == "videoroll":
            continue
        if len(s) > 20:
            s = s[:20]
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)

    if len(out) < n:
        raise RuntimeError(f"OpenAI output has too few tags (want={n}, got={len(out)})")
    return out[:n]


def recommend_typeid_openai(
    text: str,
    *,
    options: list[dict[str, Any]],
    config: OpenAIChatConfig,
) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        raise ValueError("text is empty")
    if not options:
        raise ValueError("options is empty")

    options_lines = "\n".join([f"{int(o.get('id') or 0)}\t{str(o.get('path') or '').strip()}" for o in options])
    data = request_openai_json_object(
        config=config,
        system_prompt="Return ONLY valid JSON (no markdown, no extra text).",
        user_prompt=(
            "你是 B 站投稿分区（tid/typeid）助手。请根据输入文本，选择最合适的一个分区。\n"
            "要求：\n"
            "- 必须从提供的候选列表中选择，输出的 typeid 必须在候选列表里；\n"
            "- 只输出 JSON 对象，字段为：typeid（数字）与 reason（字符串，<=80字）。\n\n"
            f"输入文本：\n{source}\n\n"
            "候选分区（每行：typeid<TAB>path）：\n"
            f"{options_lines}\n\n"
            "输出 JSON：\n"
            '{ "typeid": 0, "reason": "" }'
        ),
    )
    return data


def review_publish_content_openai(
    *,
    title: str,
    summary: str,
    subtitle_excerpt: str,
    reject_rules: str,
    config: OpenAIChatConfig,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    clean_summary = str(summary or "").strip()
    clean_subtitle = str(subtitle_excerpt or "").strip()
    clean_rules = str(reject_rules or "").strip()

    data = request_openai_json_object(
        config=config,
        system_prompt="You are a strict video content compliance reviewer. Return ONLY valid JSON.",
        user_prompt=(
            "请审核下面这个准备投稿的视频是否应该通过审核。\n"
            "审核时必须综合判断：视频标题、AI 总结、字幕内容片段，以及用户自定义的拦截规则。\n"
            "要求：\n"
            "- 只输出 JSON 对象，不要输出解释；\n"
            "- approved 必须是布尔值；\n"
            "- reason 必须是中文，明确说明通过或不通过的核心原因，<=120字；\n"
            "- risk_tags 为字符串数组，可为空；\n"
            "- 用户自定义规则优先级最高，只要命中就必须不通过；\n"
            "- 如果内容明显涉及违法、危险、诈骗、成人、仇恨、未成年人不当内容，或用户明确禁止的主题，应判定不通过。\n\n"
            f"用户自定义不通过规则：\n{clean_rules or '（无）'}\n\n"
            f"视频标题：\n{clean_title or '（空）'}\n\n"
            f"AI 总结：\n{clean_summary or '（空）'}\n\n"
            f"字幕片段：\n{clean_subtitle or '（空）'}\n\n"
            '输出 JSON：{"approved":true,"reason":"","risk_tags":[]}'
        ),
    )
    return data
