from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from videoroll.utils.openai_compat import build_openai_chat_completions_url
from videoroll.utils.openai_compat import build_openai_embeddings_url

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class OpenAIChatConfig:
    api_key: str | None
    base_url: str
    model: str
    temperature: float = 0.2
    timeout_seconds: float = 60.0
    max_retries: int = 3
    embedding_dimensions: int | None = None


def openai_chat_config_from_settings(settings: Mapping[str, Any]) -> OpenAIChatConfig:
    return OpenAIChatConfig(
        api_key=str(settings.get("openai_api_key") or "").strip() or None,
        base_url=str(settings.get("openai_base_url") or "").strip(),
        model=str(settings.get("openai_model") or "").strip(),
        temperature=float(settings.get("openai_temperature") or 0.2),
        timeout_seconds=float(settings.get("openai_timeout_seconds") or 60.0),
        max_retries=max(1, min(10, int(settings.get("openai_max_retries") or 3))),
    )


def create_openai_http_client(timeout_seconds: float) -> httpx.Client:
    t = float(timeout_seconds)
    timeout = httpx.Timeout(t, connect=min(10.0, t), read=t, write=t, pool=t)
    return httpx.Client(timeout=timeout)


def _sleep_backoff(attempt: int) -> None:
    base = min(8.0, float(2**attempt))
    time.sleep(base + random.random() * 0.25)


def _resp_snippet(resp: httpx.Response, limit: int = 200) -> str:
    try:
        text = resp.text or ""
    except Exception:
        return ""
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _strip_code_fence(text: str) -> str:
    out = (text or "").strip()
    if out.startswith("```"):
        lines = out.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        out = "\n".join(lines).strip()
    return out


def _extract_content(resp_json: dict[str, Any]) -> str:
    try:
        return str(resp_json["choices"][0]["message"]["content"])
    except Exception as e:
        raise RuntimeError(f"unexpected OpenAI response shape: {resp_json}") from e


def _parse_json_object(resp_json: dict[str, Any]) -> dict[str, Any]:
    content = _extract_content(resp_json)
    data = json.loads(_strip_code_fence(content))
    if not isinstance(data, dict):
        raise RuntimeError("OpenAI output is not a JSON object")
    return data


def _request_openai_json_object_with_client(
    *,
    client: httpx.Client,
    config: OpenAIChatConfig,
    system_prompt: str,
    user_prompt: str,
    format_retry_notice: str,
    format_retries: int,
    network_retries: int,
) -> dict[str, Any]:
    if not config.api_key:
        raise RuntimeError("OpenAI API key is not set")

    url = build_openai_chat_completions_url(config.base_url)
    headers = {"Authorization": f"Bearer {config.api_key}"}

    attempts_format = max(1, int(format_retries))
    attempts_network = max(1, int(network_retries))
    base_user_prompt = str(user_prompt or "")
    last_err: Exception | None = None

    for format_attempt in range(attempts_format):
        current_user_prompt = base_user_prompt
        if format_attempt > 0 and format_retry_notice.strip():
            current_user_prompt = base_user_prompt + "\n\n" + format_retry_notice.strip()

        req: dict[str, Any] = {
            "model": config.model,
            "temperature": float(config.temperature),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": current_user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        for net_attempt in range(attempts_network):
            try:
                resp = client.post(url, headers=headers, json=req)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    status = resp.status_code
                    if status in _RETRYABLE_STATUS_CODES and net_attempt < attempts_network - 1:
                        retry_after = (resp.headers.get("retry-after") or "").strip()
                        if retry_after:
                            try:
                                time.sleep(min(30.0, float(retry_after)))
                            except Exception:
                                _sleep_backoff(net_attempt)
                        else:
                            _sleep_backoff(net_attempt)
                        continue

                    ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    snippet = _resp_snippet(resp)
                    raise RuntimeError(
                        f"OpenAI request failed (status={resp.status_code}, content-type={ct}, url={url}). {snippet}"
                    ) from e

                try:
                    resp_json = resp.json()
                except Exception as e:
                    ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    hint = " (check openai_base_url; most providers require it to end with /v1)" if "text/html" in ct else ""
                    raise RuntimeError(
                        f"OpenAI endpoint did not return JSON (status={resp.status_code}, content-type={ct}, url={url}){hint}."
                    ) from e

                return _parse_json_object(resp_json)
            except httpx.TimeoutException as e:
                last_err = e
                if net_attempt < attempts_network - 1:
                    _sleep_backoff(net_attempt)
                    continue
                break
            except httpx.TransportError as e:
                last_err = e
                if net_attempt < attempts_network - 1:
                    _sleep_backoff(net_attempt)
                    continue
                break
            except Exception as e:
                last_err = e
                break

    if last_err is not None:
        raise last_err
    raise RuntimeError("OpenAI request failed")


def request_openai_json_object(
    *,
    config: OpenAIChatConfig,
    system_prompt: str,
    user_prompt: str,
    format_retry_notice: str = "注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。",
    format_retries: int = 2,
    network_retries: int | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    if client is not None:
        return _request_openai_json_object_with_client(
            client=client,
            config=config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            format_retry_notice=format_retry_notice,
            format_retries=format_retries,
            network_retries=max(1, int(network_retries if network_retries is not None else config.max_retries)),
        )

    with create_openai_http_client(config.timeout_seconds) as owned_client:
        return _request_openai_json_object_with_client(
            client=owned_client,
            config=config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            format_retry_notice=format_retry_notice,
            format_retries=format_retries,
            network_retries=max(1, int(network_retries if network_retries is not None else config.max_retries)),
        )


def request_openai_embedding(
    *,
    config: OpenAIChatConfig,
    text: str,
    client: httpx.Client | None = None,
    network_retries: int = 3,
) -> list[float]:
    if not config.api_key:
        raise RuntimeError("OpenAI API key is not set")

    source = str(text or "").strip()
    if not source:
        raise ValueError("embedding text is empty")

    url = build_openai_embeddings_url(config.base_url)
    headers = {"Authorization": f"Bearer {config.api_key}"}
    req = {"model": config.model, "input": source}
    if config.embedding_dimensions is not None and config.embedding_dimensions > 0:
        req["dimensions"] = int(config.embedding_dimensions)
    attempts_network = max(1, int(network_retries))
    last_err: Exception | None = None

    def _with_client(c: httpx.Client) -> list[float]:
        nonlocal last_err
        for net_attempt in range(attempts_network):
            try:
                resp = c.post(url, headers=headers, json=req)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    status = resp.status_code
                    if status in _RETRYABLE_STATUS_CODES and net_attempt < attempts_network - 1:
                        retry_after = (resp.headers.get("retry-after") or "").strip()
                        if retry_after:
                            try:
                                time.sleep(min(30.0, float(retry_after)))
                            except Exception:
                                _sleep_backoff(net_attempt)
                        else:
                            _sleep_backoff(net_attempt)
                        continue

                    ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    snippet = _resp_snippet(resp)
                    raise RuntimeError(
                        f"OpenAI embedding request failed (status={resp.status_code}, content-type={ct}, url={url}). {snippet}"
                    ) from e

                try:
                    resp_json = resp.json()
                except Exception as e:
                    ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    hint = " (check openai_base_url; most providers require it to end with /v1)" if "text/html" in ct else ""
                    raise RuntimeError(
                        f"OpenAI embedding endpoint did not return JSON (status={resp.status_code}, content-type={ct}, url={url}){hint}."
                    ) from e

                try:
                    raw = resp_json["data"][0]["embedding"]
                except Exception as e:
                    raise RuntimeError(f"unexpected OpenAI embedding response shape: {resp_json}") from e

                if not isinstance(raw, list) or not raw:
                    raise RuntimeError("OpenAI embedding output is empty")
                return [float(x) for x in raw]
            except httpx.TimeoutException as e:
                last_err = e
                if net_attempt < attempts_network - 1:
                    _sleep_backoff(net_attempt)
                    continue
                break
            except httpx.TransportError as e:
                last_err = e
                if net_attempt < attempts_network - 1:
                    _sleep_backoff(net_attempt)
                    continue
                break
            except Exception as e:
                last_err = e
                break

        if last_err is not None:
            raise last_err
        raise RuntimeError("OpenAI embedding request failed")

    if client is not None:
        return _with_client(client)

    with create_openai_http_client(config.timeout_seconds) as owned_client:
        return _with_client(owned_client)
