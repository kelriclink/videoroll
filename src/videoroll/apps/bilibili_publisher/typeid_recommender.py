from __future__ import annotations

import json
from typing import Any

import httpx

from videoroll.utils.openai_compat import build_openai_chat_completions_url


def flatten_typelist(typelist: Any) -> list[dict[str, Any]]:
    if not isinstance(typelist, list):
        return []

    out: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], parents: list[str]) -> None:
        name = str(node.get("name") or "").strip()
        children = node.get("children")

        next_parents = parents + ([name] if name else [])
        if isinstance(children, list) and children:
            for child in children:
                if isinstance(child, dict):
                    walk(child, next_parents)
            return

        try:
            tid = int(node.get("id") or 0)
        except Exception:
            tid = 0
        if tid <= 0:
            return
        if not name:
            return

        path = "/".join(next_parents) if next_parents else name
        out.append({"id": tid, "name": name, "path": path})

    for item in typelist:
        if isinstance(item, dict):
            walk(item, [])

    deduped: dict[int, dict[str, Any]] = {}
    for item in out:
        tid = int(item.get("id") or 0)
        if tid <= 0 or tid in deduped:
            continue
        deduped[tid] = item
    return list(deduped.values())


def recommend_typeid_openai(
    text: str,
    *,
    options: list[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.0,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("text is empty")
    if not options:
        raise ValueError("options is empty")

    url = build_openai_chat_completions_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}

    # Keep the prompt compact; options can be large.
    options_lines = "\n".join([f"{int(o.get('id') or 0)}\t{str(o.get('path') or '').strip()}" for o in options])

    system_prompt = "Return ONLY valid JSON (no markdown, no extra text)."
    user_prompt = (
        "你是 B 站投稿分区（tid/typeid）助手。请根据输入文本，选择最合适的一个分区。\n"
        "要求：\n"
        "- 必须从提供的候选列表中选择，输出的 typeid 必须在候选列表里；\n"
        "- 只输出 JSON 对象，字段为：typeid（数字）与 reason（字符串，<=80字）。\n\n"
        f"输入文本：\n{s}\n\n"
        "候选分区（每行：typeid<TAB>path）：\n"
        f"{options_lines}\n\n"
        "输出 JSON：\n"
        '{ "typeid": 0, "reason": "" }'
    )

    req: dict[str, Any] = {
        "model": model,
        "temperature": float(temperature),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    t = float(timeout_seconds)
    timeout = httpx.Timeout(t, connect=min(10.0, t), read=t, write=t, pool=t)
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, headers=headers, json=req)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenAI returned empty choices")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenAI returned empty content")

    try:
        obj = json.loads(content)
    except Exception as e:
        raise RuntimeError("OpenAI returned invalid JSON") from e
    if not isinstance(obj, dict):
        raise RuntimeError("OpenAI returned non-object JSON")
    return obj

