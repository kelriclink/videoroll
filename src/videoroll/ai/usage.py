from __future__ import annotations

import contextvars
import os
import uuid
from typing import Any, Mapping
from urllib.parse import urlsplit

from videoroll.db.models import AIUsageEvent, AppSetting
from videoroll.db.session import get_sessionmaker


_TASK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("videoroll_ai_usage_task_id", default=None)
_OPERATION: contextvars.ContextVar[str | None] = contextvars.ContextVar("videoroll_ai_usage_operation", default=None)
PRICING_SETTINGS_KEY = "ai.usage.pricing"


def set_ai_usage_context(*, task_id: str | uuid.UUID | None = None, operation: str | None = None) -> tuple[Any, Any]:
    task_token = _TASK_ID.set(str(task_id) if task_id else None)
    operation_token = _OPERATION.set(str(operation or "").strip() or None)
    return task_token, operation_token


def reset_ai_usage_context(tokens: tuple[Any, Any] | None) -> None:
    if not tokens:
        return
    task_token, operation_token = tokens
    _TASK_ID.reset(task_token)
    _OPERATION.reset(operation_token)


def _provider_from_url(url: str) -> str:
    try:
        host = str(urlsplit(url).hostname or "").strip().lower()
    except Exception:
        host = ""
    if not host:
        return "openai"
    if "cerebras" in host:
        return "cerebras"
    if host.endswith("openai.com"):
        return "openai"
    return host[:64]


def _usage_counts(usage: Mapping[str, Any] | None) -> tuple[int, int, int]:
    data = usage if isinstance(usage, Mapping) else {}

    def _int(*keys: str) -> int:
        for key in keys:
            try:
                value = data.get(key)
                if value is not None:
                    return max(0, int(value))
            except Exception:
                continue
        return 0

    input_tokens = _int("prompt_tokens", "input_tokens")
    output_tokens = _int("completion_tokens", "output_tokens")
    total_tokens = _int("total_tokens")
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def _estimate_cost_microusd(db: Any, *, provider: str, model: str, input_tokens: int, output_tokens: int) -> int | None:
    row = db.get(AppSetting, PRICING_SETTINGS_KEY)
    settings = row.value_json if row and isinstance(row.value_json, dict) else {}
    models = settings.get("models") if isinstance(settings, dict) else {}
    if not isinstance(models, dict):
        return None

    price = models.get(f"{provider}:{model}") or models.get(model)
    if not isinstance(price, dict):
        return None
    try:
        input_per_million = float(price.get("input_per_million_usd") or 0.0)
        output_per_million = float(price.get("output_per_million_usd") or 0.0)
    except (TypeError, ValueError):
        return None
    if input_per_million < 0 or output_per_million < 0:
        return None
    # USD / 1,000,000 tokens converts to micro-USD by multiplying token count
    # directly by the per-million USD price.
    return max(0, int(round(input_tokens * input_per_million + output_tokens * output_per_million)))


def estimate_ai_cost_microusd(
    db: Any,
    *,
    url: str,
    model: str,
    usage: Mapping[str, Any] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> int | None:
    """Estimate request cost from the configured AI pricing table.

    This intentionally reuses the same provider/model lookup as usage
    telemetry so budget enforcement and reporting cannot drift apart.
    """

    if usage is not None:
        usage_input, usage_output, _ = _usage_counts(usage)
        if input_tokens is None:
            input_tokens = usage_input
        if output_tokens is None:
            output_tokens = usage_output
    return _estimate_cost_microusd(
        db,
        provider=_provider_from_url(url),
        model=str(model or ""),
        input_tokens=max(0, int(input_tokens or 0)),
        output_tokens=max(0, int(output_tokens or 0)),
    )


def record_ai_usage(
    *,
    url: str,
    model: str,
    operation: str,
    success: bool,
    status_code: int | None = None,
    usage: Mapping[str, Any] | None = None,
    latency_ms: int = 0,
    error: Exception | str | None = None,
) -> None:
    """Best-effort telemetry; metrics must never break an AI request."""

    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        return
    provider = _provider_from_url(url)
    input_tokens, output_tokens, total_tokens = _usage_counts(usage)
    context_operation = str(_OPERATION.get() or "").strip()
    task_id_raw = str(_TASK_ID.get() or "").strip()
    task_id: uuid.UUID | None = None
    if task_id_raw:
        try:
            task_id = uuid.UUID(task_id_raw)
        except ValueError:
            task_id = None

    SessionLocal = get_sessionmaker(database_url)
    db = SessionLocal()
    try:
        cost = _estimate_cost_microusd(
            db,
            provider=provider,
            model=str(model or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        error_type: str | None = None
        error_message: str | None = None
        if error is not None:
            error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
            error_message = str(error)[:2000]
        db.add(
            AIUsageEvent(
                task_id=task_id,
                provider=provider,
                model=str(model or "")[:255],
                operation=(context_operation or str(operation or "chat"))[:96],
                success=bool(success),
                status_code=int(status_code) if status_code is not None else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=max(0, int(latency_ms or 0)),
                estimated_cost_microusd=cost,
                error_type=error_type,
                error_message=error_message,
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
