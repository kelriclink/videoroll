"""Opt-in concise spoken-script proposals using configured translation model.

Proposals never mutate stored translations or dubbing scripts. A human
must review and explicitly save the suggestion before TTS is re-run.
"""
from __future__ import annotations

import httpx
import re


def safe_automatic_rewrite(original: str, candidate: str) -> bool:
    """Conservative lexical gate; not a claim of semantic equivalence.

    Numbers, source-language technical identifiers and explicit negation must
    survive an unattended shortening. Ambiguous cases still require review.
    """
    original, candidate = original.strip(), candidate.strip()
    if not candidate or len(candidate) >= len(original) or len(candidate) > 1000:
        return False
    # Reject suspiciously aggressive deletion of a substantial script.
    if len(original) >= 20 and len(candidate) < len(original) * 0.48:
        return False
    numeric = re.compile(r'(?<![A-Za-z0-9])\d+(?:[.,]\d+)*(?![A-Za-z0-9])')
    if sorted(numeric.findall(original)) != sorted(numeric.findall(candidate)):
        return False
    technical = re.compile(r'[A-Za-z]+\d+[A-Za-z0-9]*|[A-Za-z]+(?:[-_.][A-Za-z0-9]+)*')
    if sorted(x.lower() for x in technical.findall(original)) != sorted(x.lower() for x in technical.findall(candidate)):
        return False
    negations = ('不', '没', '无', '未', '非', '别', '禁止', '不能', '不是', '没有')
    if any(term in original for term in negations) and not any(term in candidate for term in negations):
        return False
    return True


from videoroll.utils.openai_compat import build_openai_chat_completions_url


def propose_concise_script(*, text: str, target_seconds: float, cfg: dict) -> str:
    clean = text.strip()
    if not 0 < len(clean) <= 1000 or not 0.1 < target_seconds < 3600:
        raise ValueError("invalid script or target duration")
    base = str(cfg.get("openai_base_url") or "")
    model = str(cfg.get("openai_model") or "")
    key = str(cfg.get("openai_api_key") or "")
    if not base or not model or not key:
        raise RuntimeError("configure a translation OpenAI-compatible model and API key first")
    prompt = (
        "你是专业中文科技视频配音稿编辑。把下面的中文口播稿改写得更简短、更自然，"
        "目标是尽可能在指定秒数内朗读。必须保留专业术语、数值、否定关系、逻辑和技术含义，"
        "不得编造信息、删掉关键技术点，也不得加入解说。只返回修改后的口播文本，"
        "不返回引号、Markdown、说明或时间戳。若无法安全压缩就原样返回。\n"
        f"目标时长：{target_seconds:.2f}秒\n口播稿：{clean}"
    )
    result = httpx.post(
        build_openai_chat_completions_url(base),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": 0.2, "max_tokens": 300,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=min(max(float(cfg.get("openai_timeout_seconds") or 30), 5), 35),
        follow_redirects=False,
    )
    result.raise_for_status()
    data = result.json()
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("translation model returned no rewrite")
    response = choices[0].get("message", {}).get("content", "")
    if not isinstance(response, str):
        raise RuntimeError("translation model returned invalid rewrite")
    suggestion = response.strip().strip('"“”\'\u0060').strip()
    if not suggestion or len(suggestion) > len(clean) or len(suggestion) > 1000:
        raise ValueError("model did not provide a shorter safe candidate")
    return suggestion
