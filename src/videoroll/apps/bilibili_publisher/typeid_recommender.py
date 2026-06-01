from __future__ import annotations

from typing import Any

from videoroll.ai.client import OpenAIChatConfig
from videoroll.ai.service import recommend_typeid_openai as _recommend_typeid_openai


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
    return _recommend_typeid_openai(
        text,
        options=options,
        config=OpenAIChatConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=float(temperature),
            timeout_seconds=float(timeout_seconds),
        ),
    )
