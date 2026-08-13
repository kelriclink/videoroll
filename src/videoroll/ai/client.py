from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

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
    api_type: str = "openai"
    cerebras_reasoning_effort: str = "medium"
    cerebras_reasoning_format: str = "parsed"


@dataclass(frozen=True)
class OpenAIToolCall:
    """A normalized function call returned by an OpenAI-compatible chat API."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    argument_error: str = ""


@dataclass(frozen=True)
class OpenAIToolTurn:
    """One assistant turn, including zero or more native tool calls."""

    content: str
    assistant_message: dict[str, Any]
    tool_calls: list[OpenAIToolCall]
    finish_reason: str = ""
    usage: dict[str, Any] | None = None


def openai_chat_config_from_settings(settings: Mapping[str, Any]) -> OpenAIChatConfig:
    api_type = str(settings.get("openai_api_type") or "openai").strip().lower()
    if api_type not in {"openai", "cerebras"}:
        api_type = "openai"
    reasoning_effort = str(settings.get("cerebras_reasoning_effort") or "medium").strip().lower()
    if reasoning_effort not in {"low", "medium", "high"}:
        reasoning_effort = "medium"
    reasoning_format = str(settings.get("cerebras_reasoning_format") or "parsed").strip().lower()
    if reasoning_format not in {"parsed", "raw", "hidden"}:
        reasoning_format = "parsed"
    return OpenAIChatConfig(
        api_key=str(settings.get("openai_api_key") or "").strip() or None,
        base_url=str(settings.get("openai_base_url") or "").strip(),
        model=str(settings.get("openai_model") or "").strip(),
        temperature=float(settings.get("openai_temperature") or 0.2),
        timeout_seconds=float(settings.get("openai_timeout_seconds") or 60.0),
        max_retries=max(1, min(10, int(settings.get("openai_max_retries") or 3))),
        api_type=api_type,
        cerebras_reasoning_effort=reasoning_effort,
        cerebras_reasoning_format=reasoning_format,
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


def _stream_delta_text(value: Any) -> str:
    """Normalize the several reasoning fields used by OpenAI-compatible APIs."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_stream_delta_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        # Qwen/NewAPI and a few gateways return reasoning_details objects,
        # while DeepSeek-style APIs normally return reasoning_content.
        for key in ("text", "content", "reasoning_content", "reasoning"):
            if key in value:
                text = _stream_delta_text(value.get(key))
                if text:
                    return text
    return ""


def _stream_choice_delta(payload: dict[str, Any]) -> tuple[str, str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", ""
    choice = choices[0]
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        # A small number of compatibility gateways use message even while
        # streaming. Supporting it costs nothing and makes the parser less
        # brittle.
        delta = choice.get("message")
    if not isinstance(delta, dict):
        return "", ""
    reasoning = "".join(
        _stream_delta_text(delta.get(key))
        for key in ("reasoning_content", "reasoning", "reasoning_details")
        if delta.get(key) is not None
    )
    content = _stream_delta_text(delta.get("content"))
    return reasoning, content


def _parse_openai_tool_turn(resp_json: dict[str, Any]) -> OpenAIToolTurn:
    try:
        message = resp_json["choices"][0]["message"]
    except Exception as e:
        raise RuntimeError(f"unexpected OpenAI tool response shape: {resp_json}") from e
    if not isinstance(message, dict):
        raise RuntimeError(f"unexpected OpenAI tool message shape: {message!r}")

    calls: list[OpenAIToolCall] = []
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise RuntimeError("OpenAI tool_calls is not a list")
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            raise RuntimeError("OpenAI tool call is not an object")
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise RuntimeError("OpenAI tool call is missing function payload")
        name = str(function.get("name") or "").strip()
        if not name:
            raise RuntimeError("OpenAI tool call is missing function name")
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = dict(raw_arguments)
            encoded_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        else:
            encoded_arguments = str(raw_arguments or "")
            argument_error = ""
            try:
                parsed_arguments = json.loads(encoded_arguments or "{}")
                if not isinstance(parsed_arguments, dict):
                    argument_error = "tool arguments are not an object"
                    parsed_arguments = {}
            except Exception as e:
                argument_error = f"tool arguments are invalid JSON: {e}"
                parsed_arguments = {}
            arguments = parsed_arguments
        if isinstance(raw_arguments, dict):
            argument_error = ""
        call_id = str(raw_call.get("id") or f"call_{index}").strip()
        calls.append(OpenAIToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=encoded_arguments, argument_error=argument_error))

    choice = resp_json.get("choices", [{}])[0] if isinstance(resp_json.get("choices"), list) else {}
    assistant_message = dict(message)
    assistant_message.setdefault("role", "assistant")
    return OpenAIToolTurn(
        content=str(message.get("content") or ""),
        assistant_message=assistant_message,
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or "") if isinstance(choice, dict) else "",
        usage=resp_json.get("usage") if isinstance(resp_json.get("usage"), dict) else None,
    )


def parse_openai_tool_turn(resp_json: dict[str, Any]) -> OpenAIToolTurn:
    """Parse a native tool-calling response for tests and custom transports."""

    return _parse_openai_tool_turn(resp_json)


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


def _request_openai_json_object_with_thinking_with_client(
    *,
    client: httpx.Client,
    config: OpenAIChatConfig,
    system_prompt: str,
    user_prompt: str,
    format_retry_notice: str,
    format_retries: int,
    network_retries: int,
    on_thinking_delta: Callable[[str], None] | None,
) -> dict[str, Any]:
    """Request JSON through SSE and forward reasoning deltas as they arrive.

    The standard compatibility mode opts in with `enable_thinking`. Cerebras
    instead uses `reasoning_effort` and `reasoning_format`, and does not allow
    streaming together with `response_format=json_object`.
    """

    if not config.api_key:
        raise RuntimeError("OpenAI API key is not set")

    url = build_openai_chat_completions_url(config.base_url)
    headers = {"Authorization": f"Bearer {config.api_key}", "Accept": "text/event-stream"}
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
            "stream": True,
        }
        api_type = str(config.api_type or "openai").strip().lower()
        if api_type == "cerebras":
            req["reasoning_effort"] = config.cerebras_reasoning_effort
            req["reasoning_format"] = config.cerebras_reasoning_format
        else:
            req["response_format"] = {"type": "json_object"}
            req["enable_thinking"] = True

        for net_attempt in range(attempts_network):
            content_parts: list[str] = []
            try:
                with client.stream("POST", url, headers=headers, json=req) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        # Read the response first: httpx does not populate
                        # .text while a response is still in streaming mode.
                        raw_body = resp.read().decode("utf-8", errors="replace")
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
                        snippet = " ".join(raw_body.replace("\r", " ").replace("\n", " ").split())[:200]
                        protocol_hint = (
                            "The configured Cerebras model or gateway may not support the selected "
                            "reasoning_effort/reasoning_format values."
                            if api_type == "cerebras"
                            else "The configured model or gateway may not support enable_thinking."
                        )
                        raise RuntimeError(
                            "OpenAI Think request failed "
                            f"(status={resp.status_code}, content-type={ct}, url={url}). {snippet} "
                            f"{protocol_hint}"
                        ) from e

                    content_type = (resp.headers.get("content-type") or "").lower()
                    if "text/event-stream" not in content_type:
                        # Some older OpenAI-compatible gateways accept the
                        # Think fields but ignore `stream`. Preserve a usable
                        # translation and record any returned reasoning as one
                        # terminal trace event instead of failing obscurely on
                        # an otherwise valid JSON response.
                        raw_body = resp.read()
                        try:
                            regular_payload = json.loads(raw_body)
                        except Exception as e:
                            raise RuntimeError(
                                "OpenAI Think endpoint did not return an SSE or JSON response "
                                f"(content-type={content_type or 'unknown'}, url={url})"
                            ) from e
                        if not isinstance(regular_payload, dict):
                            raise RuntimeError("OpenAI Think endpoint returned a non-object JSON response")
                        reasoning, content = _stream_choice_delta(regular_payload)
                        if reasoning and on_thinking_delta is not None:
                            on_thinking_delta(reasoning)
                        if not content:
                            raise RuntimeError(f"unexpected OpenAI Think response shape: {regular_payload}")
                        data = json.loads(_strip_code_fence(content))
                        if not isinstance(data, dict):
                            raise RuntimeError("OpenAI Think output is not a JSON object")
                        return data

                    for raw_line in resp.iter_lines():
                        line = str(raw_line or "").strip()
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if not body or body == "[DONE]":
                            continue
                        try:
                            payload = json.loads(body)
                        except json.JSONDecodeError:
                            # Ignore gateway keep-alives / non-JSON event
                            # lines. A missing final JSON body is still an
                            # error below, rather than silently succeeding.
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if isinstance(payload.get("error"), dict):
                            error = payload["error"]
                            raise RuntimeError(str(error.get("message") or error))
                        reasoning, content = _stream_choice_delta(payload)
                        if reasoning and on_thinking_delta is not None:
                            on_thinking_delta(reasoning)
                        if content:
                            content_parts.append(content)

                content = "".join(content_parts)
                data = json.loads(_strip_code_fence(content))
                if not isinstance(data, dict):
                    raise RuntimeError("OpenAI Think output is not a JSON object")
                return data
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
    raise RuntimeError("OpenAI Think request failed")


def request_openai_json_object_with_thinking(
    *,
    config: OpenAIChatConfig,
    system_prompt: str,
    user_prompt: str,
    on_thinking_delta: Callable[[str], None] | None = None,
    format_retry_notice: str = "注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。",
    format_retries: int = 2,
    network_retries: int | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """OpenAI-compatible JSON request with protocol-aware reasoning and SSE parsing."""

    kwargs = {
        "config": config,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "format_retry_notice": format_retry_notice,
        "format_retries": format_retries,
        "network_retries": max(1, int(network_retries if network_retries is not None else config.max_retries)),
        "on_thinking_delta": on_thinking_delta,
    }
    if client is not None:
        return _request_openai_json_object_with_thinking_with_client(client=client, **kwargs)
    with create_openai_http_client(config.timeout_seconds) as owned_client:
        return _request_openai_json_object_with_thinking_with_client(client=owned_client, **kwargs)


def _request_openai_tool_turn_with_client(
    *,
    client: httpx.Client,
    config: OpenAIChatConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any] = "auto",
    network_retries: int,
) -> OpenAIToolTurn:
    if not config.api_key:
        raise RuntimeError("OpenAI API key is not set")
    if not messages:
        raise ValueError("tool turn messages are empty")
    url = build_openai_chat_completions_url(config.base_url)
    headers = {"Authorization": f"Bearer {config.api_key}"}
    req: dict[str, Any] = {
        "model": config.model,
        "temperature": float(config.temperature),
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
    }
    last_err: Exception | None = None
    for net_attempt in range(max(1, int(network_retries))):
        try:
            resp = client.post(url, headers=headers, json=req)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = resp.status_code
                if status in _RETRYABLE_STATUS_CODES and net_attempt < max(1, int(network_retries)) - 1:
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
                raise RuntimeError(
                    f"OpenAI tool request failed (status={resp.status_code}, content-type={ct}, url={url}). {_resp_snippet(resp)}"
                ) from e
            try:
                payload = resp.json()
            except Exception as e:
                raise RuntimeError(f"OpenAI tool endpoint did not return JSON (status={resp.status_code}, url={url})") from e
            return _parse_openai_tool_turn(payload)
        except httpx.TimeoutException as e:
            last_err = e
            if net_attempt < max(1, int(network_retries)) - 1:
                _sleep_backoff(net_attempt)
                continue
            break
        except httpx.TransportError as e:
            last_err = e
            if net_attempt < max(1, int(network_retries)) - 1:
                _sleep_backoff(net_attempt)
                continue
            break
        except Exception as e:
            last_err = e
            break
    if last_err is not None:
        raise last_err
    raise RuntimeError("OpenAI tool request failed")


def request_openai_tool_turn(
    *,
    config: OpenAIChatConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any] = "auto",
    network_retries: int | None = None,
    client: httpx.Client | None = None,
) -> OpenAIToolTurn:
    """Request one native OpenAI-compatible function-calling turn."""

    kwargs = {
        "config": config,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "network_retries": max(1, int(network_retries if network_retries is not None else config.max_retries)),
    }
    if client is not None:
        return _request_openai_tool_turn_with_client(client=client, **kwargs)
    with create_openai_http_client(config.timeout_seconds) as owned_client:
        return _request_openai_tool_turn_with_client(client=owned_client, **kwargs)


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
