from __future__ import annotations

import json
import random
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Iterator, Literal, Mapping, TypeVar

from pydantic import BaseModel, Field, ValidationError


AgentStepKind = Literal["agent", "llm", "tool", "policy", "retrieval", "error"]


class AgentBudget(BaseModel):
    max_llm_calls: int = Field(default=12, ge=0, le=100)
    max_tool_calls: int = Field(default=12, ge=0, le=100)
    max_fetch_calls: int = Field(default=4, ge=0, le=50)
    max_external_requests: int = Field(default=24, ge=0, le=1000)
    max_input_tokens: int = Field(default=120_000, ge=0, le=10_000_000)
    max_output_tokens: int = Field(default=40_000, ge=0, le=10_000_000)
    max_total_tokens: int = Field(default=160_000, ge=0, le=20_000_000)
    max_cost_microusd: int = Field(default=0, ge=0, le=10_000_000_000)
    max_parallel_tools: int = Field(default=2, ge=1, le=16)
    timeout_seconds: float = Field(default=120.0, ge=1.0, le=3600.0)


class AgentDecision(BaseModel):
    action: Literal["rag_lookup", "dictionary_lookup", "wiki_search", "search_web", "fetch_url", "finish"]
    query: str = ""
    url: str = ""
    skill_name: str = ""
    reason: str = ""
    final_answer_ready: bool = False


class SearchQueryPlan(BaseModel):
    queries: list[str] = Field(default_factory=list)


class GlossaryCandidate(BaseModel):
    term: str
    translation: str
    domain: str = ""
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class VerificationResult(BaseModel):
    supported: bool = False
    context_consistent: bool = False
    should_write: bool = False
    should_auto_approve: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    failure_category: str = ""


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 20.0
    retry_count: int = 0
    cost: dict[str, Any] = Field(default_factory=dict)
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    guardrails: list[str] = Field(default_factory=list)
    redact_fields: list[str] = Field(default_factory=list)
    idempotent: bool = False


class ToolCallResult(BaseModel):
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    duration_ms: int = 0
    error_type: str = ""
    error: str = ""


class AgentTraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    span_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_span_id: str | None = None
    kind: AgentStepKind = "agent"
    action: str
    agent_name: str = ""
    tool_name: str = ""
    status: str = "ok"
    duration_ms: int | None = None
    model: str | None = None
    tokens: dict[str, Any] | None = None
    cost: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error_type: str = ""
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    at: str = ""

    def as_legacy_step(self) -> dict[str, Any]:
        data = self.model_dump(exclude_none=True)
        # Keep old Dashboard compatibility while exposing normalized trace fields.
        data["kind"] = self.kind
        data["action"] = self.action
        if self.tool_name:
            data["tool"] = self.tool_name
        if self.input is not None:
            data["input"] = self.input
        if self.output is not None:
            data["output"] = self.output
        for key, value in self.metadata.items():
            data.setdefault(key, value)
        return data


class AgentState(BaseModel):
    run_id: str | None = None
    agent_name: str
    status: str = "running"
    node: str = "start"
    term: str = ""
    observations: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


@dataclass(frozen=True)
class RegisteredTool(Generic[TIn, TOut]):
    spec: ToolSpec
    input_model: type[TIn]
    output_model: type[TOut] | None = None
    handler: Callable[[TIn], TOut | dict[str, Any]] | None = None


@dataclass
class ToolRegistry:
    _tools: dict[str, RegisteredTool[Any, Any]] = field(default_factory=dict)

    def register(self, tool: RegisteredTool[Any, Any]) -> None:
        self._tools[tool.spec.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec:
        return self._tools[name].spec

    def get(self, name: str) -> RegisteredTool[Any, Any]:
        return self._tools[name]

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def invoke(
        self,
        name: str,
        arguments: Any,
        *,
        runtime: AgentRuntime | None = None,
        input_guardrails: dict[str, list[Callable[[BaseModel], None]]] | None = None,
        output_guardrails: dict[str, list[Callable[[Any], None]]] | None = None,
        enforced_guardrails: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """Execute through the policy runtime; direct unbudgeted execution is forbidden."""

        if runtime is None:
            raise RuntimeError("ToolRegistry.invoke requires an AgentRuntime; use ToolExecutor for agent execution")
        return ToolExecutor(
            registry=self,
            runtime=runtime,
            input_guardrails=input_guardrails or {},
            output_guardrails=output_guardrails or {},
            enforced_guardrails=enforced_guardrails or {},
        ).invoke(name, arguments)


class AgentBudgetExceeded(RuntimeError):
    pass


class AgentCancelled(RuntimeError):
    pass


class AgentToolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_error",
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "tool_error")
        self.retryable = bool(retryable)
        self.status_code = int(status_code) if status_code is not None else None
        self.retry_after = max(0.0, float(retry_after)) if retry_after is not None else None


class AgentToolTimeout(AgentToolError):
    def __init__(self, message: str = "tool execution timed out", *, retryable: bool = False) -> None:
        super().__init__(message, code="tool_timeout", retryable=retryable)


class AgentToolPolicyDenied(AgentToolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="policy_denied", retryable=False)


class AgentRateLimited(AgentToolError):
    def __init__(self, message: str = "tool provider rate limited", *, retry_after: float | None = None) -> None:
        super().__init__(message, code="rate_limited", retryable=True, status_code=429, retry_after=retry_after)


@dataclass
class AgentCancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""
    parent: AgentCancellationToken | None = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self.parent is not None and self.parent.cancelled)

    @property
    def effective_reason(self) -> str:
        if self._event.is_set():
            return self.reason or "agent cancelled"
        if self.parent is not None and self.parent.cancelled:
            return self.parent.effective_reason
        return self.reason or "agent cancelled"

    def cancel(self, reason: str = "agent cancelled") -> None:
        self.reason = str(reason or "agent cancelled")
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AgentCancelled(self.effective_reason)


@dataclass
class AgentRuntime:
    agent_name: str
    budget: AgentBudget
    trace_recorder: Callable[[dict[str, Any]], None] | None = None
    run_id: str | None = None
    lease_owner: str | None = None
    parent_span_id: str | None = None
    parent_runtime: AgentRuntime | None = None
    cancellation: AgentCancellationToken = field(default_factory=AgentCancellationToken)
    started_monotonic: float = field(default_factory=time.monotonic)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    llm_calls: int = 0
    tool_calls: int = 0
    fetch_calls: int = 0
    external_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: int = 0

    def __post_init__(self) -> None:
        if self.parent_runtime is not None and self.cancellation.parent is None:
            self.cancellation.parent = self.parent_runtime.cancellation

    def check_budget(
        self,
        *,
        tool_name: str = "",
        is_llm: bool = False,
        external_requests: int = 0,
        additional_cost_microusd: int = 0,
        additional_input_tokens: int = 0,
        additional_output_tokens: int = 0,
    ) -> None:
        with self._lock:
            self.cancellation.raise_if_cancelled()
            if time.monotonic() - self.started_monotonic > self.budget.timeout_seconds:
                self.cancellation.cancel("agent timeout budget exceeded")
                raise AgentBudgetExceeded("agent timeout budget exceeded")
            if is_llm and self.llm_calls >= self.budget.max_llm_calls:
                raise AgentBudgetExceeded("agent LLM call budget exceeded")
            if tool_name:
                if self.tool_calls >= self.budget.max_tool_calls:
                    raise AgentBudgetExceeded("agent tool call budget exceeded")
                if tool_name in {"fetch", "fetch_url"} and self.fetch_calls >= self.budget.max_fetch_calls:
                    raise AgentBudgetExceeded("agent fetch call budget exceeded")
            if external_requests and self.external_requests + int(external_requests) > self.budget.max_external_requests:
                raise AgentBudgetExceeded("agent external request budget exceeded")
            if (
                self.budget.max_cost_microusd
                and self.cost_microusd + max(0, int(additional_cost_microusd)) > self.budget.max_cost_microusd
            ):
                raise AgentBudgetExceeded("agent cost budget exceeded")
            reserved_input = max(0, int(additional_input_tokens or 0))
            reserved_output = max(0, int(additional_output_tokens or 0))
            if (
                self.budget.max_input_tokens
                and self.input_tokens + reserved_input > self.budget.max_input_tokens
            ):
                raise AgentBudgetExceeded("agent input token budget exceeded")
            if (
                self.budget.max_output_tokens
                and self.output_tokens + reserved_output > self.budget.max_output_tokens
            ):
                raise AgentBudgetExceeded("agent output token budget exceeded")
            if (
                self.budget.max_total_tokens
                and self.total_tokens + reserved_input + reserved_output > self.budget.max_total_tokens
            ):
                raise AgentBudgetExceeded("agent total token budget exceeded")
            if self.budget.max_cost_microusd and self.cost_microusd > self.budget.max_cost_microusd:
                raise AgentBudgetExceeded("agent cost budget exceeded")

    def before_llm(self) -> None:
        with self._lock:
            self.check_budget(is_llm=True)
            if self.parent_runtime is not None:
                self.parent_runtime.before_llm()
            self.llm_calls += 1

    def before_tool(self, tool_name: str) -> None:
        with self._lock:
            self.check_budget(tool_name=tool_name)
            if self.parent_runtime is not None:
                self.parent_runtime.before_tool(tool_name)
            self.tool_calls += 1
            if tool_name in {"fetch", "fetch_url"}:
                self.fetch_calls += 1

    def before_external_request(self, count: int = 1) -> None:
        amount = max(1, int(count or 1))
        with self._lock:
            self.check_budget(external_requests=amount)
            if self.parent_runtime is not None:
                self.parent_runtime.before_external_request(amount)
            self.external_requests += amount

    def account_external_requests(self, count: int = 1) -> None:
        """Record requests that were already spent, even after cancellation."""

        amount = max(1, int(count or 1))
        with self._lock:
            if self.parent_runtime is not None:
                self.parent_runtime.account_external_requests(amount)
            self.external_requests += amount

    def consume_usage(self, usage: Mapping[str, Any] | None, *, cost_microusd: int | None = None) -> None:
        data = usage if isinstance(usage, Mapping) else {}

        def _count(*keys: str) -> int:
            for key in keys:
                try:
                    value = data.get(key)
                    if value is not None:
                        return max(0, int(value))
                except Exception:
                    continue
            return 0

        input_tokens = _count("prompt_tokens", "input_tokens")
        output_tokens = _count("completion_tokens", "output_tokens")
        total_tokens = _count("total_tokens") or input_tokens + output_tokens
        if cost_microusd is None:
            raw_microusd = data.get("cost_microusd")
            raw_usd = data.get("cost_usd")
            try:
                if raw_microusd is not None:
                    cost_microusd = max(0, int(raw_microusd))
                elif raw_usd is not None:
                    cost_microusd = max(0, int(float(raw_usd) * 1_000_000))
            except (TypeError, ValueError, OverflowError):
                cost_microusd = None
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.total_tokens += total_tokens
            if cost_microusd is not None:
                self.cost_microusd += max(0, int(cost_microusd))
            if self.parent_runtime is not None:
                self.parent_runtime.consume_usage(usage, cost_microusd=cost_microusd)
            self.check_budget()

    def consume_cost(self, cost_microusd: int) -> None:
        amount = max(0, int(cost_microusd or 0))
        if amount <= 0:
            return
        with self._lock:
            self.check_budget(additional_cost_microusd=amount)
            if self.parent_runtime is not None:
                self.parent_runtime.consume_cost(amount)
            self.cost_microusd += amount
            self.check_budget()

    def consume_estimated_tokens(self, *, input_chars: int = 0, output_chars: int = 0) -> None:
        # Deliberately conservative and dependency-free. Provider-reported usage
        # supersedes estimates when it is available.
        input_tokens = max(0, (int(input_chars) + 2) // 3)
        output_tokens = max(0, (int(output_chars) + 2) // 3)
        if input_tokens or output_tokens:
            self.consume_usage(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            )

    def remaining_seconds(self) -> float:
        with self._lock:
            local = max(0.0, self.budget.timeout_seconds - (time.monotonic() - self.started_monotonic))
        if self.parent_runtime is None:
            return local
        return min(local, self.parent_runtime.remaining_seconds())

    def check_token_reservation(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        reserved_input = max(0, int(input_tokens or 0))
        reserved_output = max(0, int(output_tokens or 0))
        with self._lock:
            self.check_budget(
                additional_input_tokens=reserved_input,
                additional_output_tokens=reserved_output,
            )
            if self.parent_runtime is not None:
                self.parent_runtime.check_token_reservation(
                    input_tokens=reserved_input,
                    output_tokens=reserved_output,
                )

    def check_cost_reservation(self, cost_microusd: int) -> None:
        amount = max(0, int(cost_microusd or 0))
        if amount <= 0:
            return
        with self._lock:
            self.check_budget(additional_cost_microusd=amount)
            if self.parent_runtime is not None:
                self.parent_runtime.check_cost_reservation(amount)

    def has_cost_budget(self) -> bool:
        if self.budget.max_cost_microusd:
            return True
        return bool(self.parent_runtime is not None and self.parent_runtime.has_cost_budget())

    def completion_token_limit(self, *, reserved_input_tokens: int = 0, cap: int = 2048) -> int:
        reserved_input = max(0, int(reserved_input_tokens or 0))
        requested_cap = max(1, int(cap or 1))
        with self._lock:
            self.check_budget(additional_input_tokens=reserved_input)
            candidates = [requested_cap]
            if self.budget.max_output_tokens:
                candidates.append(max(0, self.budget.max_output_tokens - self.output_tokens))
            if self.budget.max_total_tokens:
                candidates.append(
                    max(0, self.budget.max_total_tokens - self.total_tokens - reserved_input)
                )
            local_limit = min(candidates)
        if self.parent_runtime is not None:
            local_limit = min(
                local_limit,
                self.parent_runtime.completion_token_limit(
                    reserved_input_tokens=reserved_input,
                    cap=requested_cap,
                ),
            )
        if local_limit <= 0:
            raise AgentBudgetExceeded("agent completion token budget exhausted")
        return int(local_limit)

    def cancel(self, reason: str = "agent cancelled") -> None:
        self.cancellation.cancel(reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agent_name": self.agent_name,
                "run_id": self.run_id,
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "fetch_calls": self.fetch_calls,
                "external_requests": self.external_requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "cost_microusd": self.cost_microusd,
                "elapsed_seconds": max(0.0, time.monotonic() - self.started_monotonic),
                "cancelled": self.cancellation.cancelled,
                "cancel_reason": self.cancellation.reason,
            }

    def _absorb_restored_usage(self, delta: Mapping[str, int]) -> None:
        with self._lock:
            self.check_budget()
            llm_calls = max(0, int(delta.get("llm_calls") or 0))
            tool_calls = max(0, int(delta.get("tool_calls") or 0))
            fetch_calls = max(0, int(delta.get("fetch_calls") or 0))
            external_requests = max(0, int(delta.get("external_requests") or 0))
            input_tokens = max(0, int(delta.get("input_tokens") or 0))
            output_tokens = max(0, int(delta.get("output_tokens") or 0))
            total_tokens = max(0, int(delta.get("total_tokens") or 0))
            cost_microusd = max(0, int(delta.get("cost_microusd") or 0))
            if self.llm_calls + llm_calls > self.budget.max_llm_calls:
                raise AgentBudgetExceeded("agent LLM call budget exceeded while restoring checkpoint")
            if self.tool_calls + tool_calls > self.budget.max_tool_calls:
                raise AgentBudgetExceeded("agent tool call budget exceeded while restoring checkpoint")
            if self.fetch_calls + fetch_calls > self.budget.max_fetch_calls:
                raise AgentBudgetExceeded("agent fetch call budget exceeded while restoring checkpoint")
            if self.external_requests + external_requests > self.budget.max_external_requests:
                raise AgentBudgetExceeded("agent external request budget exceeded while restoring checkpoint")
            if self.budget.max_input_tokens and self.input_tokens + input_tokens > self.budget.max_input_tokens:
                raise AgentBudgetExceeded("agent input token budget exceeded while restoring checkpoint")
            if self.budget.max_output_tokens and self.output_tokens + output_tokens > self.budget.max_output_tokens:
                raise AgentBudgetExceeded("agent output token budget exceeded while restoring checkpoint")
            if self.budget.max_total_tokens and self.total_tokens + total_tokens > self.budget.max_total_tokens:
                raise AgentBudgetExceeded("agent total token budget exceeded while restoring checkpoint")
            if self.budget.max_cost_microusd and self.cost_microusd + cost_microusd > self.budget.max_cost_microusd:
                raise AgentBudgetExceeded("agent cost budget exceeded while restoring checkpoint")
            if self.parent_runtime is not None:
                self.parent_runtime._absorb_restored_usage(delta)
            self.llm_calls += llm_calls
            self.tool_calls += tool_calls
            self.fetch_calls += fetch_calls
            self.external_requests += external_requests
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.total_tokens += total_tokens
            self.cost_microusd += cost_microusd

    def restore_counters(
        self,
        snapshot: Mapping[str, Any] | None,
        *,
        propagate_to_parent: bool = False,
    ) -> None:
        data = snapshot if isinstance(snapshot, Mapping) else {}
        with self._lock:
            previous: dict[str, int] = {}
            for attr in (
                "llm_calls",
                "tool_calls",
                "fetch_calls",
                "external_requests",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_microusd",
            ):
                previous[attr] = max(0, int(getattr(self, attr, 0) or 0))
                try:
                    setattr(self, attr, max(0, int(data.get(attr) or 0)))
                except Exception:
                    continue
            try:
                elapsed = max(0.0, float(data.get("elapsed_seconds") or 0.0))
            except (TypeError, ValueError, OverflowError):
                elapsed = 0.0
            if elapsed:
                self.started_monotonic = time.monotonic() - elapsed
            delta = {
                attr: max(0, int(getattr(self, attr, 0) or 0) - previous.get(attr, 0))
                for attr in previous
            }
            self.check_budget()
        if propagate_to_parent and self.parent_runtime is not None and any(delta.values()):
            self.parent_runtime._absorb_restored_usage(delta)

    def record(self, event: AgentTraceEvent) -> None:
        if self.trace_recorder is None:
            return
        if not event.agent_name:
            event.agent_name = self.agent_name
        if self.parent_span_id and event.parent_span_id is None:
            event.parent_span_id = self.parent_span_id
        payload = event.as_legacy_step()
        payload.setdefault("run_id", self.run_id)
        payload.setdefault(
            "budget",
            {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "fetch_calls": self.fetch_calls,
                "external_requests": self.external_requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "cost_microusd": self.cost_microusd,
                "max_llm_calls": self.budget.max_llm_calls,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_fetch_calls": self.budget.max_fetch_calls,
                "max_external_requests": self.budget.max_external_requests,
                "max_input_tokens": self.budget.max_input_tokens,
                "max_output_tokens": self.budget.max_output_tokens,
                "max_total_tokens": self.budget.max_total_tokens,
                "max_cost_microusd": self.budget.max_cost_microusd,
                "timeout_seconds": self.budget.timeout_seconds,
            },
        )
        self.trace_recorder(payload)


def _redact_mapping(value: Any, fields: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if str(key).lower() in fields else _redact_mapping(item, fields))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_mapping(item, fields) for item in value]
    return value


_TOOL_RATE_LOCK = threading.Lock()
_TOOL_RATE_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_TOOL_RATE_NEXT_ALLOWED: dict[str, float] = {}


def _tool_rate_semaphore(key: str, limit: int) -> threading.BoundedSemaphore:
    map_key = (key, limit)
    with _TOOL_RATE_LOCK:
        semaphore = _TOOL_RATE_SEMAPHORES.get(map_key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit)
            _TOOL_RATE_SEMAPHORES[map_key] = semaphore
        return semaphore


@dataclass(frozen=True)
class ToolInvocationOutcome:
    tool_name: str
    arguments: Any
    output: dict[str, Any] | None = None
    validated_output: Any = None
    error: Exception | None = None


@dataclass
class ToolExecutor:
    registry: ToolRegistry
    runtime: AgentRuntime
    input_guardrails: dict[str, list[Callable[[BaseModel], None]]] = field(default_factory=dict)
    output_guardrails: dict[str, list[Callable[[Any], None]]] = field(default_factory=dict)
    enforced_guardrails: dict[str, set[str]] = field(default_factory=dict)
    sleep: Callable[[float], None] = time.sleep

    def redact_arguments(self, name: str, arguments: Any) -> Any:
        try:
            tool = self.registry.get(name)
        except KeyError:
            return arguments
        redact = {str(field).lower() for field in tool.spec.redact_fields}
        if not redact:
            return arguments
        return _redact_mapping(arguments, redact)

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            self.runtime.cancellation.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.sleep(min(0.1, remaining))

    def _validate_guardrail_bindings(self, tool: RegisteredTool[Any, Any]) -> None:
        declared = {str(value) for value in tool.spec.guardrails if str(value)}
        enforced = {str(value) for value in self.enforced_guardrails.get(tool.spec.name, set()) if str(value)}
        missing = sorted(declared - enforced)
        if missing:
            raise AgentToolPolicyDenied(
                f"tool {tool.spec.name} has unbound runtime guardrails: {', '.join(missing)}"
            )

    def _declared_cost(self, tool: RegisteredTool[Any, Any]) -> tuple[int, int]:
        cost = tool.spec.cost if isinstance(tool.spec.cost, dict) else {}
        try:
            network_requests = max(0, int(cost.get("network_requests") or 0))
        except (TypeError, ValueError):
            network_requests = 0
        try:
            cost_microusd = max(0, int(cost.get("cost_microusd") or 0))
        except (TypeError, ValueError):
            cost_microusd = 0
        return network_requests, cost_microusd

    @contextmanager
    def _rate_limit_slot(self, tool: RegisteredTool[Any, Any]) -> Iterator[None]:
        policy = tool.spec.rate_limit if isinstance(tool.spec.rate_limit, dict) else {}
        try:
            max_concurrency = max(1, int(policy.get("max_concurrency") or 1))
        except (TypeError, ValueError):
            max_concurrency = 1
        try:
            min_interval = max(0.0, float(policy.get("min_interval_seconds") or 0.0))
        except (TypeError, ValueError):
            min_interval = 0.0
        if not policy:
            yield
            return
        key = str(policy.get("key") or tool.spec.name or "tool")[:128]
        semaphore = _tool_rate_semaphore(key, max_concurrency)
        deadline = time.monotonic() + max(0.0, self.runtime.remaining_seconds())
        acquired = False
        while not acquired:
            self.runtime.cancellation.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            acquired = semaphore.acquire(timeout=min(0.1, remaining))
        if not acquired:
            raise AgentToolTimeout(f"tool {tool.spec.name} rate-limit slot wait timed out", retryable=True)
        try:
            if min_interval > 0:
                with _TOOL_RATE_LOCK:
                    now = time.monotonic()
                    scheduled = max(now, _TOOL_RATE_NEXT_ALLOWED.get(key, 0.0))
                    _TOOL_RATE_NEXT_ALLOWED[key] = scheduled + min_interval
                self._interruptible_sleep(max(0.0, scheduled - now))
            yield
        finally:
            semaphore.release()

    def _run_handler(self, tool: RegisteredTool[Any, Any], value: BaseModel, timeout_seconds: float) -> Any:
        if tool.handler is None:
            raise AgentToolError(f"tool has no server handler: {tool.spec.name}", code="tool_unavailable")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"agent-tool-{tool.spec.name}")
        future = executor.submit(tool.handler, value)
        try:
            result = future.result(timeout=max(0.01, timeout_seconds))
            self.runtime.cancellation.raise_if_cancelled()
            return result
        except FuturesTimeoutError as exc:
            future.cancel()
            # A running Python thread cannot be killed safely. Retrying here
            # could overlap a still-running timed-out handler and duplicate
            # network traffic or side effects, even for an idempotent tool.
            self.runtime.cancel(f"tool {tool.spec.name} timed out")
            raise AgentToolTimeout(
                f"tool {tool.spec.name} exceeded {timeout_seconds:.2f}s",
                retryable=False,
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def invoke(self, name: str, arguments: Any) -> tuple[dict[str, Any], Any]:
        self.runtime.cancellation.raise_if_cancelled()
        self.runtime.before_tool(name)
        try:
            tool = self.registry.get(name)
        except KeyError as exc:
            raise AgentToolError(f"unknown tool: {name}", code="not_found", retryable=False) from exc
        self._validate_guardrail_bindings(tool)
        try:
            validated_input = validate_model(tool.input_model, arguments)
        except ValidationError as exc:
            raise AgentToolError(
                f"invalid arguments for tool {name}: {exc.errors(include_url=False, include_input=False)}",
                code="invalid_arguments",
                retryable=False,
            ) from exc
        try:
            for guardrail in self.input_guardrails.get(name, []):
                guardrail(validated_input)
        except AgentToolError:
            raise
        except Exception as exc:
            raise AgentToolPolicyDenied(f"input guardrail rejected tool {name}: {exc}") from exc
        declared_network_requests, declared_cost = self._declared_cost(tool)
        cost_charged = False
        attempts = max(1, int(tool.spec.retry_count) + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            self.runtime.cancellation.raise_if_cancelled()
            remaining = self.runtime.remaining_seconds()
            if remaining <= 0:
                self.runtime.cancel("agent timeout budget exceeded")
                raise AgentBudgetExceeded("agent timeout budget exceeded")
            timeout_seconds = min(max(0.01, float(tool.spec.timeout_seconds)), remaining)
            self.runtime.check_budget(
                external_requests=declared_network_requests,
            )
            try:
                with self._rate_limit_slot(tool):
                    if declared_cost and not cost_charged:
                        # Fixed tool cost is charged once, at the point where
                        # execution is actually admitted. Queue/cooldown
                        # rejection therefore does not create phantom spend.
                        self.runtime.consume_cost(declared_cost)
                        cost_charged = True
                    raw_output = self._run_handler(tool, validated_input, timeout_seconds)
                validated_output = (
                    validate_model(tool.output_model, raw_output)
                    if tool.output_model is not None
                    else raw_output
                )
                try:
                    for guardrail in self.output_guardrails.get(name, []):
                        guardrail(validated_output)
                except AgentToolError:
                    raise
                except Exception as exc:
                    raise AgentToolPolicyDenied(f"output guardrail rejected tool {name}: {exc}") from exc
                if isinstance(validated_output, BaseModel):
                    wire_output = validated_output.model_dump(mode="json")
                elif isinstance(validated_output, dict):
                    wire_output = dict(validated_output)
                else:
                    raise AgentToolError(
                        f"tool returned unsupported output type: {name}",
                        code="invalid_tool_output",
                    )
                redact = {str(field).lower() for field in tool.spec.redact_fields}
                return _redact_mapping(wire_output, redact), validated_output
            except AgentBudgetExceeded:
                raise
            except AgentCancelled:
                raise
            except Exception as exc:
                if isinstance(exc, AgentToolError):
                    normalized = exc
                elif isinstance(exc, TimeoutError):
                    normalized = AgentToolTimeout(str(exc), retryable=bool(tool.spec.idempotent))
                else:
                    normalized = AgentToolError(
                        str(exc) or type(exc).__name__,
                        code="permanent_failure",
                        retryable=False,
                    )
                last_error = normalized
                retryable = normalized.retryable and bool(tool.spec.idempotent)
                if not retryable or attempt >= attempts - 1:
                    raise normalized from exc
                delay = normalized.retry_after
                if delay is None:
                    delay = min(8.0, float(2**attempt)) + random.random() * 0.25
                self._interruptible_sleep(min(max(0.0, delay), max(0.0, self.runtime.remaining_seconds())))
        if last_error is not None:
            raise last_error
        raise AgentToolError(f"tool execution failed: {name}")

    def invoke_many(self, calls: list[tuple[str, Any]]) -> list[ToolInvocationOutcome]:
        if not calls:
            return []

        def _one(name: str, arguments: Any) -> ToolInvocationOutcome:
            try:
                output, validated = self.invoke(name, arguments)
                return ToolInvocationOutcome(
                    tool_name=name,
                    arguments=arguments,
                    output=output,
                    validated_output=validated,
                )
            except Exception as exc:
                return ToolInvocationOutcome(tool_name=name, arguments=arguments, error=exc)

        max_workers = min(len(calls), max(1, int(self.runtime.budget.max_parallel_tools)))
        if max_workers <= 1:
            return [_one(name, arguments) for name, arguments in calls]
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-tool-batch")
        try:
            futures = [pool.submit(_one, name, arguments) for name, arguments in calls]
            return [future.result() for future in futures]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)


def validate_model(model: type[TOut], data: Any, *, fallback: TOut | None = None) -> TOut:
    try:
        if isinstance(data, model):
            return data
        return model.model_validate(data)
    except ValidationError:
        if fallback is not None:
            return fallback
        raise


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return json.loads(json.dumps(model.model_json_schema(), ensure_ascii=False))
