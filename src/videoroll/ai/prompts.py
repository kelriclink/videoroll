from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AIJsonPrompt:
    system_prompt: str
    user_prompt: str
    format_retry_notice: str = "注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。"
    format_retries: int = 2
    network_retries: int | None = None


def build_text_translation_prompt(text: str, *, target_lang: str, style: str) -> AIJsonPrompt:
    source = str(text or "").strip()
    tgt = (target_lang or "zh").strip() or "zh"
    tone = (style or "").strip() or "口语自然"
    return AIJsonPrompt(
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


def build_subtitle_translation_prompt(
    *,
    blocks: list[dict[str, Any]],
    target_lang: str,
    style: str,
    summary: str = "",
    enable_summary: bool = True,
    glossary: dict[str, str] | None = None,
    rag_context: dict[str, Any] | None = None,
    network_retries: int = 3,
) -> AIJsonPrompt:
    tgt = (target_lang or "zh").strip() or "zh"
    tone = (style or "").strip() or "口语自然"
    payload_in: dict[str, Any] = {"target_lang": tgt, "style": tone, "blocks": blocks}
    if enable_summary:
        payload_in["summary"] = str(summary or "")
    if glossary:
        payload_in["glossary"] = glossary
    if rag_context:
        payload_in["rag_context"] = rag_context

    return AIJsonPrompt(
        system_prompt="You are a professional subtitle translator. Return ONLY valid JSON (no markdown, no code fences, no extra text).",
        user_prompt=(
            "你将收到一批字幕 block。请按 block 为单位翻译。\n"
            "要求：\n"
            "- 保留每个 block 的 idx 不变；不得增删 block，不得改变顺序；\n"
            "- 只翻译 text 字段；同一 block 内多行先合并理解再翻译；\n"
            "- 术语、人名保持一致；数字/单位尽量保留原格式；\n"
            "- 如果输入包含 rag_context，请优先参考其中的 term_cards/knowledge_cards 来理解专有名词、梗、作品设定和技术背景；\n"
            "- term_cards 中的 translation 是推荐译法，除非明显不符合当前上下文，否则保持一致；\n"
            "- rag_context 来自主 agent 对当前 block 的本地 RAG/词典预检和必要研究；如果其中已有与当前 block 和 summary 贴切的译法或解释，直接据此翻译，不要假设还必须继续搜索；\n"
            "- 输出必须是 JSON 对象，且必须包含 translations 数组；不要输出任何解释。\n"
            f"- 目标语言：{tgt}\n"
            f"- 风格：{tone}\n\n"
            "如果输入里带 summary，请在翻译时参考它保持前后一致，并输出 updated_summary（<= 500 字符）。\n\n"
            "输入 JSON：\n"
            f"{json.dumps(payload_in, ensure_ascii=False)}\n\n"
            "输出 JSON 结构（必须严格遵守）：\n"
            '{ "updated_summary": "...", "translations": [ {"idx": 1, "text": "..."}, ... ] }'
        ),
        format_retry_notice="注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。",
        format_retries=2,
        network_retries=network_retries,
    )


def build_bilibili_tags_prompt(*, title: str, summary: str, transcript: str, n_tags: int = 6) -> AIJsonPrompt:
    clean_title = str(title or "").strip()
    clean_summary = str(summary or "").strip()
    clean_transcript = str(transcript or "").strip()
    n = max(1, int(n_tags))
    return AIJsonPrompt(
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


def build_typeid_prompt(text: str, *, options: list[dict[str, Any]]) -> AIJsonPrompt:
    source = str(text or "").strip()
    options_lines = "\n".join([f"{int(o.get('id') or 0)}\t{str(o.get('path') or '').strip()}" for o in options])
    return AIJsonPrompt(
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


def build_publish_review_prompt(*, title: str, summary: str, subtitle_excerpt: str, reject_rules: str) -> AIJsonPrompt:
    clean_title = str(title or "").strip()
    clean_summary = str(summary or "").strip()
    clean_subtitle = str(subtitle_excerpt or "").strip()
    clean_rules = str(reject_rules or "").strip()
    return AIJsonPrompt(
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
