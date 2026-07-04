from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError


AgentStepKind = Literal["agent", "llm", "tool", "policy", "retrieval", "error"]


class AgentBudget(BaseModel):
    max_llm_calls: int = Field(default=12, ge=0, le=100)
    max_tool_calls: int = Field(default=12, ge=0, le=100)
    max_fetch_calls: int = Field(default=4, ge=0, le=50)
    timeout_seconds: float = Field(default=120.0, ge=1.0, le=3600.0)


class AgentDecision(BaseModel):
    action: Literal["rag_lookup", "wiki_search", "search_web", "fetch_url", "finish"]
    query: str = ""
    url: str = ""
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

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]


class AgentBudgetExceeded(RuntimeError):
    pass


@dataclass
class AgentRuntime:
    agent_name: str
    budget: AgentBudget
    trace_recorder: Callable[[dict[str, Any]], None] | None = None
    run_id: str | None = None
    parent_span_id: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    llm_calls: int = 0
    tool_calls: int = 0
    fetch_calls: int = 0

    def check_budget(self, *, tool_name: str = "", is_llm: bool = False) -> None:
        if time.monotonic() - self.started_monotonic > self.budget.timeout_seconds:
            raise AgentBudgetExceeded("agent timeout budget exceeded")
        if is_llm and self.llm_calls >= self.budget.max_llm_calls:
            raise AgentBudgetExceeded("agent LLM call budget exceeded")
        if tool_name:
            if self.tool_calls >= self.budget.max_tool_calls:
                raise AgentBudgetExceeded("agent tool call budget exceeded")
            if tool_name in {"fetch", "fetch_url"} and self.fetch_calls >= self.budget.max_fetch_calls:
                raise AgentBudgetExceeded("agent fetch call budget exceeded")

    def before_llm(self) -> None:
        self.check_budget(is_llm=True)
        self.llm_calls += 1

    def before_tool(self, tool_name: str) -> None:
        self.check_budget(tool_name=tool_name)
        self.tool_calls += 1
        if tool_name in {"fetch", "fetch_url"}:
            self.fetch_calls += 1

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
                "max_llm_calls": self.budget.max_llm_calls,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_fetch_calls": self.budget.max_fetch_calls,
                "timeout_seconds": self.budget.timeout_seconds,
            },
        )
        self.trace_recorder(payload)


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

