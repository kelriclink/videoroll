from __future__ import annotations

import threading
import time
import uuid

import httpx
import pytest
from pydantic import BaseModel

from videoroll.ai.client import (
    OpenAIChatConfig,
    OpenAIToolCall,
    OpenAIToolTurn,
    request_openai_json_object,
)
from videoroll.ai.usage import estimate_ai_cost_microusd
from videoroll.apps.subtitle_service.agent_runtime import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentCancelled,
    AgentRateLimited,
    AgentRuntime,
    AgentToolError,
    AgentToolTimeout,
    AgentToolPolicyDenied,
    RegisteredTool,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
)
from videoroll.apps.subtitle_service.agent_skills import AgentSkill, SkillRegistry
from videoroll.apps.subtitle_service.provider_rate_limit import ProviderGateTimeout, ProviderRateGate


class _Input(BaseModel):
    value: int


class _Output(BaseModel):
    value: int
    secret: str = ""


def _registry(*, spec: ToolSpec, handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            spec=spec,
            input_model=_Input,
            output_model=_Output,
            handler=handler,
        )
    )
    return registry


def test_tool_registry_forbids_unbudgeted_direct_execution() -> None:
    registry = _registry(
        spec=ToolSpec(name="echo"),
        handler=lambda value: _Output(value=value.value),
    )

    with pytest.raises(RuntimeError, match="AgentRuntime"):
        registry.invoke("echo", {"value": 1})


def test_tool_executor_retries_transient_error_redacts_and_charges_declared_cost() -> None:
    attempts = 0

    def handler(value: _Input) -> _Output:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AgentRateLimited("slow down", retry_after=0)
        return _Output(value=value.value, secret="do-not-leak")

    runtime = AgentRuntime(
        agent_name="test",
        budget=AgentBudget(
            max_llm_calls=2,
            max_tool_calls=4,
            max_fetch_calls=2,
            max_external_requests=8,
            max_cost_microusd=100,
            timeout_seconds=5,
        ),
    )
    registry = _registry(
        spec=ToolSpec(
            name="echo",
            retry_count=1,
            idempotent=True,
            cost={"cost_microusd": 7},
            redact_fields=["secret"],
        ),
        handler=handler,
    )
    executor = ToolExecutor(registry=registry, runtime=runtime)

    output, validated = executor.invoke("echo", {"value": 9})

    assert attempts == 2
    assert validated.value == 9
    assert output == {"value": 9, "secret": "[REDACTED]"}
    assert runtime.tool_calls == 1
    # ToolSpec cost is the declared logical tool charge. Physical provider
    # retries are accounted separately through request/token usage.
    assert runtime.cost_microusd == 7


def test_configured_ai_pricing_drives_runtime_cost_budget() -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _PricingDb:
        def get(self, _model, key):
            assert key == "ai.usage.pricing"
            return SimpleNamespace(
                value_json={
                    "models": {
                        "openai:test-model": {
                            "input_per_million_usd": 2.0,
                            "output_per_million_usd": 4.0,
                        }
                    }
                }
            )

    db = _PricingDb()
    assert estimate_ai_cost_microusd(
        db,
        url="https://api.openai.com/v1",
        model="test-model",
        input_tokens=100,
        output_tokens=50,
    ) == 400

    runtime = AgentRuntime(
        agent_name="priced",
        budget=AgentBudget(max_cost_microusd=300, timeout_seconds=5),
    )
    with pytest.raises(AgentBudgetExceeded, match="cost budget exceeded"):
        rag_module._consume_runtime_ai_usage(
            runtime,
            db,
            base_url="https://api.openai.com/v1",
            model="test-model",
            input_chars=300,
            output_chars=150,
        )
    assert runtime.cost_microusd == 400


def test_cost_budget_fails_closed_before_request_when_pricing_is_missing() -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _MissingPricingDb:
        def get(self, _model, _key):
            return SimpleNamespace(value_json={"models": {}})

    runtime = AgentRuntime(
        agent_name="priced",
        budget=AgentBudget(max_cost_microusd=1_000, timeout_seconds=5),
    )

    with pytest.raises(AgentBudgetExceeded, match="pricing is not configured"):
        rag_module._check_runtime_ai_request_budget(
            runtime,
            _MissingPricingDb(),
            base_url="https://api.openai.com/v1",
            model="missing-model",
            input_chars=300,
        )

    assert runtime.llm_calls == 0
    assert runtime.external_requests == 0
    assert runtime.cost_microusd == 0


def test_completion_token_limit_respects_child_and_parent_remaining_budgets() -> None:
    parent = AgentRuntime(
        agent_name="parent",
        budget=AgentBudget(
            max_input_tokens=1_000,
            max_output_tokens=150,
            max_total_tokens=250,
            timeout_seconds=5,
        ),
    )
    child = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(
            max_input_tokens=1_000,
            max_output_tokens=80,
            max_total_tokens=120,
            timeout_seconds=5,
        ),
        parent_runtime=parent,
    )
    parent.consume_usage({"input_tokens": 20, "output_tokens": 60, "total_tokens": 80})
    child.consume_usage({"input_tokens": 10, "output_tokens": 50, "total_tokens": 60})

    limit = child.completion_token_limit(reserved_input_tokens=10, cap=50)

    # Child: output leaves 30, total leaves 50. Parent also includes the
    # child's consumed usage and remains looser. The strictest bound is 30.
    assert limit == 30
    child.check_token_reservation(input_tokens=10, output_tokens=30)

    with pytest.raises(AgentBudgetExceeded):
        child.check_token_reservation(input_tokens=10, output_tokens=31)


def test_ai_request_budget_reserves_worst_case_completion_cost() -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _PricingDb:
        def get(self, _model, _key):
            return SimpleNamespace(
                value_json={
                    "models": {
                        "openai:test-model": {
                            "input_per_million_usd": 1.0,
                            "output_per_million_usd": 10.0,
                        }
                    }
                }
            )

    runtime = AgentRuntime(
        agent_name="priced",
        budget=AgentBudget(
            max_input_tokens=10_000,
            max_output_tokens=1_000,
            max_total_tokens=10_000,
            max_cost_microusd=5_000,
            timeout_seconds=5,
        ),
    )

    with pytest.raises(AgentBudgetExceeded, match="cost budget exceeded"):
        rag_module._check_runtime_ai_request_budget(
            runtime,
            _PricingDb(),
            base_url="https://api.openai.com/v1",
            model="test-model",
            input_chars=300,
            completion_cap=512,
        )

    assert runtime.cost_microusd == 0


def test_tool_executor_fails_closed_for_unbound_declared_guardrail() -> None:
    called = False

    def handler(value: _Input) -> _Output:
        nonlocal called
        called = True
        return _Output(value=value.value)

    registry = _registry(
        spec=ToolSpec(name="guarded", guardrails=["must_be_enforced"]),
        handler=handler,
    )
    runtime = AgentRuntime(agent_name="test", budget=AgentBudget(timeout_seconds=5))

    with pytest.raises(AgentToolPolicyDenied, match="unbound runtime guardrails"):
        ToolExecutor(registry=registry, runtime=runtime).invoke("guarded", {"value": 1})

    assert called is False


def test_child_runtime_inherits_parent_cancellation() -> None:
    parent = AgentRuntime(agent_name="parent", budget=AgentBudget(timeout_seconds=5))
    child = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(timeout_seconds=5),
        parent_runtime=parent,
    )

    parent.cancel("parent timed out")

    with pytest.raises(AgentCancelled, match="parent timed out"):
        child.before_tool("anything")


def test_tool_timeout_cancels_runtime_so_zombie_handler_stops_cooperatively() -> None:
    runtime = AgentRuntime(
        agent_name="test",
        budget=AgentBudget(max_tool_calls=2, timeout_seconds=5),
    )
    handler_exited = threading.Event()

    def handler(value: _Input) -> _Output:
        try:
            while not runtime.cancellation.cancelled:
                time.sleep(0.005)
            return _Output(value=value.value)
        finally:
            handler_exited.set()

    registry = _registry(
        spec=ToolSpec(
            name="slow",
            timeout_seconds=0.05,
            idempotent=True,
            cost={"network_requests": 1},
        ),
        handler=handler,
    )

    with pytest.raises(AgentToolTimeout):
        ToolExecutor(registry=registry, runtime=runtime).invoke("slow", {"value": 1})

    assert runtime.cancellation.cancelled
    # ToolSpec.network_requests is a reservation hint only; physical request
    # accounting happens at the HTTP/provider boundary.
    assert runtime.external_requests == 0
    assert handler_exited.wait(timeout=1.0)


def test_non_idempotent_tool_does_not_auto_retry_transient_failure() -> None:
    attempts = 0

    def handler(value: _Input) -> _Output:
        nonlocal attempts
        attempts += 1
        raise AgentRateLimited("temporary", retry_after=0)

    runtime = AgentRuntime(
        agent_name="test",
        budget=AgentBudget(max_tool_calls=4, timeout_seconds=5),
    )
    registry = _registry(
        spec=ToolSpec(name="write_once", retry_count=3, idempotent=False),
        handler=handler,
    )

    with pytest.raises(AgentRateLimited):
        ToolExecutor(registry=registry, runtime=runtime).invoke("write_once", {"value": 1})

    assert attempts == 1


def test_declared_tool_cost_is_not_charged_when_execution_slot_is_rejected() -> None:
    called = False

    def handler(value: _Input) -> _Output:
        nonlocal called
        called = True
        return _Output(value=value.value)

    class _RejectedSlot:
        def __enter__(self):
            raise AgentToolTimeout("queue saturated", retryable=True)

        def __exit__(self, *_args):
            return False

    runtime = AgentRuntime(
        agent_name="test",
        budget=AgentBudget(max_tool_calls=4, max_cost_microusd=100, timeout_seconds=5),
    )
    registry = _registry(
        spec=ToolSpec(name="charged", cost={"cost_microusd": 7}),
        handler=handler,
    )
    executor = ToolExecutor(registry=registry, runtime=runtime)
    executor._rate_limit_slot = lambda _tool: _RejectedSlot()  # type: ignore[method-assign]

    with pytest.raises(AgentToolTimeout):
        executor.invoke("charged", {"value": 1})

    assert called is False
    assert runtime.cost_microusd == 0


def test_tool_argument_redaction_hides_nested_trace_fields() -> None:
    runtime = AgentRuntime(agent_name="test", budget=AgentBudget(timeout_seconds=5))
    registry = _registry(
        spec=ToolSpec(name="redacted", redact_fields=["secret"]),
        handler=lambda value: _Output(value=value.value),
    )
    executor = ToolExecutor(registry=registry, runtime=runtime)

    safe = executor.redact_arguments(
        "redacted",
        {
            "value": 1,
            "secret": "top-level",
            "nested": {"secret": "nested-value"},
        },
    )

    assert safe["secret"] == "[REDACTED]"
    assert safe["nested"]["secret"] == "[REDACTED]"


def test_checkpoint_restore_propagates_historical_usage_to_parent_budget() -> None:
    parent = AgentRuntime(
        agent_name="parent",
        budget=AgentBudget(
            max_llm_calls=10,
            max_tool_calls=10,
            max_fetch_calls=10,
            max_external_requests=20,
            max_input_tokens=10_000,
            max_output_tokens=10_000,
            max_total_tokens=20_000,
            max_cost_microusd=100,
            timeout_seconds=30,
        ),
    )
    child = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(
            max_llm_calls=10,
            max_tool_calls=10,
            max_fetch_calls=10,
            max_external_requests=20,
            max_input_tokens=10_000,
            max_output_tokens=10_000,
            max_total_tokens=20_000,
            max_cost_microusd=100,
            timeout_seconds=30,
        ),
        parent_runtime=parent,
    )

    child.restore_counters(
        {
            "llm_calls": 2,
            "tool_calls": 3,
            "fetch_calls": 1,
            "external_requests": 5,
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost_microusd": 7,
        },
        propagate_to_parent=True,
    )

    assert parent.llm_calls == 2
    assert parent.tool_calls == 3
    assert parent.fetch_calls == 1
    assert parent.external_requests == 5
    assert parent.total_tokens == 120
    assert parent.cost_microusd == 7


def test_tool_executor_invoke_many_runs_independent_calls_concurrently() -> None:
    barrier = threading.Barrier(2)

    def handler(value: _Input) -> _Output:
        barrier.wait(timeout=1.0)
        return _Output(value=value.value)

    registry = _registry(
        spec=ToolSpec(name="parallel", timeout_seconds=2, idempotent=True),
        handler=handler,
    )
    runtime = AgentRuntime(
        agent_name="test",
        budget=AgentBudget(max_tool_calls=4, max_parallel_tools=2, timeout_seconds=5),
    )
    outcomes = ToolExecutor(registry=registry, runtime=runtime).invoke_many(
        [("parallel", {"value": 1}), ("parallel", {"value": 2})]
    )

    assert [item.error for item in outcomes] == [None, None]
    assert [item.output["value"] for item in outcomes if item.output] == [1, 2]


def test_provider_gate_local_semaphore_fails_closed_when_saturated() -> None:
    gate = ProviderRateGate("", f"test-{uuid.uuid4().hex}", max_concurrency=1)
    assert gate._local_semaphore.acquire(timeout=0)
    try:
        with pytest.raises(ProviderGateTimeout, match="local concurrency"):
            with gate.slot(wait_seconds=0.01):
                raise AssertionError("slot must not be entered")
    finally:
        gate._local_semaphore.release()


def test_provider_gate_enforces_local_cooldown_without_redis() -> None:
    gate = ProviderRateGate("", f"test-{uuid.uuid4().hex}", max_concurrency=1)
    gate.set_cooldown(0.05)

    with pytest.raises(ProviderGateTimeout, match="cooldown"):
        with gate.slot(wait_seconds=0.001):
            raise AssertionError("cooldown must prevent request")


def test_provider_gate_timeout_does_not_count_as_remote_failure(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _Gate:
        def __init__(self) -> None:
            self.failures = 0

        def cache_get(self, _key):
            return None

        def is_circuit_open(self):
            return False

        def slot(self, **_kwargs):
            class _Slot:
                def __enter__(self):
                    raise ProviderGateTimeout("local queue saturated")

                def __exit__(self, *_args):
                    return False

            return _Slot()

        def record_failure(self, **_kwargs):
            self.failures += 1

    gate = _Gate()
    monkeypatch.setattr(rag_module, "ProviderRateGate", lambda *_args, **_kwargs: gate)

    with pytest.raises(AgentToolError) as exc_info:
        rag_module._provider_public_get(
            type("_Client", (), {"timeout": 1.0})(),
            "https://example.test/",
            provider="audit-gate",
            max_concurrency=1,
            retries=2,
        )

    assert exc_info.value.code == "provider_gate_timeout"
    assert gate.failures == 0


def test_provider_circuit_open_stops_internal_retry_loop(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _Response:
        status_code = 500
        headers: dict[str, str] = {}

    class _Client:
        timeout = 1.0

        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return _Response()

    class _Gate:
        def __init__(self) -> None:
            self.open = False

        def cache_get(self, _key):
            return None

        def is_circuit_open(self):
            return self.open

        def slot(self, **_kwargs):
            class _Slot:
                def __enter__(self):
                    return None

                def __exit__(self, *_args):
                    return False

            return _Slot()

        def record_failure(self, **_kwargs):
            self.open = True

        def record_success(self):
            raise AssertionError("500 response must not record success")

    gate = _Gate()
    client = _Client()
    monkeypatch.setattr(rag_module, "ProviderRateGate", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr(rag_module.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(AgentToolError) as exc_info:
        rag_module._provider_public_get(
            client,
            "https://example.test/",
            provider="audit-circuit",
            max_concurrency=1,
            retries=3,
        )

    assert exc_info.value.code == "circuit_open"
    assert client.calls == 1


def test_evidence_helpers_do_not_swallow_agent_cancellation(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _DummyClient:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(rag_module, "_PublicFetchClient", lambda **_kwargs: _DummyClient())

    def cancelled(*_args, **_kwargs):
        raise AgentCancelled("cancelled during provider request")

    monkeypatch.setattr(rag_module, "_provider_public_get", cancelled)

    with pytest.raises(AgentCancelled, match="cancelled during provider request"):
        rag_module.fetch_wikipedia_evidence("alpha", domain="test")

    with pytest.raises(AgentCancelled, match="cancelled during provider request"):
        rag_module.fetch_search_evidence(
            "alpha",
            domain="test",
            search_url="https://search.example.test",
        )


def test_semantic_tool_call_key_normalizes_plain_search_and_urls() -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    left = rag_module._canonical_tool_call_arguments(
        "wiki_search",
        {"query": "吸积   天体物理学"},
    )
    right = rag_module._canonical_tool_call_arguments(
        "wiki_search",
        {"query": "天体物理学 吸积"},
    )
    assert left == right

    first_url = rag_module._canonical_tool_call_arguments(
        "fetch_url",
        {"url": "HTTPS://Example.COM/path?b=2&a=1#fragment"},
    )
    second_url = rag_module._canonical_tool_call_arguments(
        "fetch_url",
        {"url": "https://example.com/path?a=1&b=2"},
    )
    assert first_url == second_url


def test_lease_fence_cancels_stale_worker() -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _FakeDb:
        def __init__(self) -> None:
            self.rolled_back = False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(rowcount=0)

        def rollback(self) -> None:
            self.rolled_back = True

        def commit(self) -> None:
            raise AssertionError("lost lease must not commit")

    runtime = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(timeout_seconds=5),
        lease_owner="worker-a",
    )
    db = _FakeDb()

    with pytest.raises(AgentCancelled, match="lease lost"):
        rag_module._renew_agent_lease(db, "run-id", runtime, commit=False)

    assert db.rolled_back
    assert runtime.cancellation.cancelled


def test_checkpoint_save_fence_rejects_stale_worker() -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _FakeDb:
        def __init__(self) -> None:
            self.rolled_back = False
            self.committed = False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(rowcount=0)

        def rollback(self) -> None:
            self.rolled_back = True

        def commit(self) -> None:
            self.committed = True

    runtime = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(timeout_seconds=5),
        lease_owner="worker-a",
    )
    db = _FakeDb()

    with pytest.raises(AgentCancelled, match="lease lost"):
        rag_module._save_agent_checkpoint(
            db,
            "00000000-0000-0000-0000-000000000001",
            node="native_tool_loop",
            state={"completed": False},
            runtime=runtime,
        )

    assert db.rolled_back
    assert not db.committed


def test_finish_fence_rejects_stale_worker(monkeypatch) -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _FakeDb:
        def __init__(self) -> None:
            self.rolled_back = False
            self.committed = False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(rowcount=0)

        def rollback(self) -> None:
            self.rolled_back = True

        def commit(self) -> None:
            self.committed = True

    runtime = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(timeout_seconds=5),
        lease_owner="worker-a",
    )
    db = _FakeDb()
    monkeypatch.setattr(rag_module, "publish_agent_event", lambda *_args, **_kwargs: None)

    with pytest.raises(AgentCancelled, match="lease lost"):
        rag_module._finish_agent_run(
            db,
            "00000000-0000-0000-0000-000000000001",
            status="succeeded",
            runtime=runtime,
        )

    assert db.rolled_back
    assert not db.committed


def test_finish_agent_run_uses_independent_postgres_trace_session(monkeypatch) -> None:
    from types import SimpleNamespace

    from videoroll.apps.subtitle_service import rag as rag_module

    class _OuterDb:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    class _TraceDb:
        def __init__(self) -> None:
            self.committed = False
            self.closed = False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(rowcount=1)

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    outer = _OuterDb()
    trace = _TraceDb()
    monkeypatch.setattr(rag_module, "Session", lambda *, bind: trace)
    monkeypatch.setattr(rag_module, "publish_agent_event", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(rag_module, "get_subtitle_settings", lambda: SimpleNamespace(redis_url="redis://test"))

    rag_module._finish_agent_run(
        outer,  # type: ignore[arg-type]
        "00000000-0000-0000-0000-000000000001",
        status="succeeded",
    )

    assert trace.committed
    assert trace.closed
    assert not outer.committed
    assert not outer.rolled_back


def test_openai_retry_observer_counts_each_physical_attempt(monkeypatch) -> None:
    from videoroll.ai import client as client_module

    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "limited"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    monkeypatch.setattr(client_module, "_sleep_before_retry", lambda *_args, **_kwargs: None)
    observed: list[int] = []
    config = OpenAIChatConfig(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model="test-model",
        max_retries=2,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_openai_json_object(
            config=config,
            system_prompt="system",
            user_prompt="user",
            format_retries=1,
            network_retries=2,
            client=client,
            before_request=lambda: observed.append(1),
        )

    assert result == {"ok": True}
    assert attempts == 2
    assert len(observed) == 2


def test_openai_retry_after_sleep_is_cooperatively_cancelled() -> None:
    attempts = 0
    checks = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "10"}, json={"error": "limited"})

    def cancel_check() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise AgentCancelled("cancelled during retry backoff")

    config = OpenAIChatConfig(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model="test-model",
        max_retries=2,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AgentCancelled, match="retry backoff"):
            request_openai_json_object(
                config=config,
                system_prompt="system",
                user_prompt="user",
                format_retries=1,
                network_retries=2,
                client=client,
                cancel_check=cancel_check,
            )

    assert attempts == 1


def test_skill_registry_ignores_non_runnable_and_unsupported_run_modes() -> None:
    registry = SkillRegistry(
        (
            AgentSkill(name="disabled", description="alpha", triggers=["alpha"], runnable=False),
            AgentSkill(name="unsupported", description="alpha", triggers=["alpha"], run_mode="workflow"),
            AgentSkill(name="active", description="alpha", triggers=["alpha"], source="user"),
        )
    )

    selected = registry.select(term="alpha", context="alpha context", domain="")

    assert [skill.name for skill in selected] == ["active"]
    assert selected[0].prompt_payload()["trust"] == "untrusted_user_guidance"


def test_skill_prompt_does_not_disclose_local_path() -> None:
    skill = AgentSkill(
        name="private-path",
        instructions="guidance",
        source="user",
        path="/srv/private/agent-skills/private-path",
    )

    payload = skill.prompt_payload()

    assert "path" not in payload
    assert payload["trust"] == "untrusted_user_guidance"


def test_skill_registry_rejects_directory_symlink_escape(tmp_path) -> None:
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escaped\ntriggers: alpha\n---\n# Escaped\nexternal guidance",
        encoding="utf-8",
    )
    try:
        (root / "escaped").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    registry = SkillRegistry.load(
        include_builtin=False,
        include_user=True,
        user_dir=root,
    )

    assert registry.skills == ()


def _turn(*calls: OpenAIToolCall) -> OpenAIToolTurn:
    wire_calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": call.raw_arguments or "{}",
            },
        }
        for call in calls
    ]
    return OpenAIToolTurn(
        content="",
        assistant_message={"role": "assistant", "content": None, "tool_calls": wire_calls},
        tool_calls=list(calls),
        finish_reason="tool_calls",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def _finish_turn(call_id: str = "finish-1") -> OpenAIToolTurn:
    return _turn(
        OpenAIToolCall(
            id=call_id,
            name="finish",
            arguments={"reason": "enough evidence"},
            raw_arguments='{"reason":"enough evidence"}',
        )
    )


def _prepare_native_loop_test(monkeypatch, rag_module) -> None:
    monkeypatch.setattr(rag_module, "_append_agent_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "_append_llm_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "_save_agent_checkpoint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "_load_agent_checkpoint", lambda *_args, **_kwargs: {})


def test_native_agent_executes_independent_tool_calls_in_parallel(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    _prepare_native_loop_test(monkeypatch, rag_module)
    barrier = threading.Barrier(2)

    def evidence(label: str) -> list[dict[str, str]]:
        barrier.wait(timeout=1.0)
        return [
            {
                "title": label,
                "url": f"https://example.test/{label}",
                "snippet": f"{label} evidence",
                "content": f"{label} evidence body",
            }
        ]

    monkeypatch.setattr(
        rag_module,
        "fetch_wikipedia_evidence",
        lambda *_args, **_kwargs: evidence("wiki"),
    )
    monkeypatch.setattr(
        rag_module,
        "fetch_search_evidence",
        lambda *_args, **_kwargs: evidence("search"),
    )
    turns = iter(
        [
            _turn(
                OpenAIToolCall(
                    id="wiki-1",
                    name="wiki_search",
                    arguments={"query": "alpha"},
                    raw_arguments='{"query":"alpha"}',
                ),
                OpenAIToolCall(
                    id="search-1",
                    name="search_web",
                    arguments={"query": "alpha"},
                    raw_arguments='{"query":"alpha"}',
                ),
            ),
            _finish_turn(),
        ]
    )
    monkeypatch.setattr(rag_module, "request_openai_tool_turn", lambda **_kwargs: next(turns))
    settings = rag_module.RagSettings(
        wiki_enabled=True,
        search_enabled=True,
        search_url="https://search.example.test",
        agent_max_external_requests=32,
    )
    runtime = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(
            max_llm_calls=5,
            max_tool_calls=8,
            max_external_requests=32,
            max_total_tokens=10_000,
            max_input_tokens=8_000,
            max_output_tokens=4_000,
            max_parallel_tools=2,
            timeout_seconds=5,
        ),
    )

    evidence_items, tools_used, rounds = rag_module._collect_evidence_with_tool_agent(
        object(),
        agent_run_id=None,
        term="alpha",
        domain_hint="test",
        target_lang="zh",
        rag_settings=settings,
        chat_config=OpenAIChatConfig(
            api_key="test",
            base_url="https://api.example.test/v1",
            model="test",
        ),
        llm_context="alpha context",
        search_queries=["alpha"],
        max_steps=2,
        runtime=runtime,
    )

    assert rounds == 2
    assert {item["title"] for item in evidence_items} == {"wiki", "search"}
    assert {"wiki_search", "search_web"}.issubset(set(tools_used))


def test_native_agent_allows_same_call_again_only_after_retryable_failure(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    _prepare_native_loop_test(monkeypatch, rag_module)
    calls = 0

    def flaky_wiki(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AgentRateLimited("temporary", retry_after=0)
        return [
            {
                "title": "alpha",
                "url": "https://example.test/alpha",
                "snippet": "alpha evidence",
                "content": "alpha evidence body",
            }
        ]

    monkeypatch.setattr(rag_module, "fetch_wikipedia_evidence", flaky_wiki)
    turns = iter(
        [
            _turn(
                OpenAIToolCall(
                    id="wiki-1",
                    name="wiki_search",
                    arguments={"query": "alpha"},
                    raw_arguments='{"query":"alpha"}',
                )
            ),
            _turn(
                OpenAIToolCall(
                    id="wiki-2",
                    name="wiki_search",
                    arguments={"query": "alpha"},
                    raw_arguments='{"query":"alpha"}',
                )
            ),
            _finish_turn(),
        ]
    )
    monkeypatch.setattr(rag_module, "request_openai_tool_turn", lambda **_kwargs: next(turns))
    settings = rag_module.RagSettings(wiki_enabled=True, agent_max_external_requests=32)
    runtime = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(
            max_llm_calls=6,
            max_tool_calls=8,
            max_external_requests=32,
            max_total_tokens=10_000,
            max_input_tokens=8_000,
            max_output_tokens=4_000,
            max_parallel_tools=2,
            timeout_seconds=5,
        ),
    )

    evidence_items, _tools_used, rounds = rag_module._collect_evidence_with_tool_agent(
        object(),
        agent_run_id=None,
        term="alpha",
        domain_hint="test",
        target_lang="zh",
        rag_settings=settings,
        chat_config=OpenAIChatConfig(
            api_key="test",
            base_url="https://api.example.test/v1",
            model="test",
        ),
        llm_context="alpha context",
        search_queries=["alpha"],
        max_steps=3,
        runtime=runtime,
    )

    assert calls == 2
    assert rounds == 3
    assert [item["title"] for item in evidence_items] == ["alpha"]


def test_agent_context_compaction_preserves_complete_tool_turns() -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "old", "type": "function", "function": {"name": "wiki_search", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "old", "content": "x" * 9000},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "new", "type": "function", "function": {"name": "search_web", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "new", "content": "y" * 9000},
    ]

    compacted = rag_module._compact_agent_messages(messages, max_tokens=2_000)

    assert compacted[:2] == messages[:2]
    assert rag_module._agent_messages_estimated_tokens(compacted) < rag_module._agent_messages_estimated_tokens(messages)
    tool_ids = [item.get("tool_call_id") for item in compacted if item.get("role") == "tool"]
    assistant_ids = {
        call.get("id")
        for item in compacted
        if item.get("role") == "assistant"
        for call in item.get("tool_calls") or []
        if isinstance(call, dict)
    }
    assert set(tool_ids).issubset(assistant_ids)
    assert "new" in assistant_ids


def test_checkpoint_message_compaction_preserves_system_and_user_when_many_short_turns() -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system policy"},
        {"role": "user", "content": "research alpha"},
    ]
    for index in range(20):
        call_id = f"call-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "wiki_search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call_id, "content": "ok"},
            ]
        )

    compacted = rag_module._compact_agent_messages(
        messages,  # type: ignore[arg-type]
        max_tokens=24_000,
        max_messages=24,
    )

    assert compacted[0]["role"] == "system"
    assert compacted[1]["role"] == "user"
    assert len(compacted) <= 24
    tool_ids = [item.get("tool_call_id") for item in compacted if item.get("role") == "tool"]
    assistant_ids = {
        call.get("id")
        for item in compacted
        if item.get("role") == "assistant"
        for call in item.get("tool_calls") or []
        if isinstance(call, dict)
    }
    assert set(tool_ids).issubset(assistant_ids)


def test_native_agent_resumes_checkpoint_without_replaying_completed_tool(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    _prepare_native_loop_test(monkeypatch, rag_module)
    restored_evidence = [
        {
            "title": "restored",
            "url": "https://example.test/restored",
            "snippet": "restored evidence",
            "content": "restored evidence body",
        }
    ]
    checkpoint = {
        "version": rag_module._AGENT_CHECKPOINT_VERSION,
        "node": "native_tool_loop",
        "state": {
            "term": "alpha",
            "transport": "native_tool_calling",
            "completed": False,
            "next_round": 2,
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "alpha"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "wiki-1",
                            "type": "function",
                            "function": {"name": "wiki_search", "arguments": '{"query":"alpha"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "wiki-1",
                    "content": '{"ok":true,"output":{"results":[]}}',
                },
            ],
            "evidence": restored_evidence,
            "observations": [{"action": "wiki_search", "ok": True}],
            "tools_used": ["wiki_search"],
            "calls": [
                {
                    "name": "wiki_search",
                    "arguments": '{"query":"alpha"}',
                    "success": True,
                    "attempts": 1,
                    "retryable": False,
                }
            ],
            "runtime": {
                "llm_calls": 1,
                "tool_calls": 1,
                "external_requests": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        },
    }
    monkeypatch.setattr(rag_module, "_load_agent_checkpoint", lambda *_args, **_kwargs: checkpoint)

    def must_not_replay(*_args, **_kwargs):
        raise AssertionError("completed checkpointed tool must not be replayed")

    monkeypatch.setattr(rag_module, "fetch_wikipedia_evidence", must_not_replay)
    monkeypatch.setattr(rag_module, "request_openai_tool_turn", lambda **_kwargs: _finish_turn("finish-resume"))
    runtime = AgentRuntime(
        agent_name="child",
        budget=AgentBudget(
            max_llm_calls=6,
            max_tool_calls=8,
            max_external_requests=32,
            max_total_tokens=10_000,
            max_input_tokens=8_000,
            max_output_tokens=4_000,
            timeout_seconds=5,
        ),
    )

    evidence_items, tools_used, rounds = rag_module._collect_evidence_with_tool_agent(
        object(),
        agent_run_id="run-resume",
        term="alpha",
        domain_hint="test",
        target_lang="zh",
        rag_settings=rag_module.RagSettings(wiki_enabled=True, agent_max_external_requests=32),
        chat_config=OpenAIChatConfig(
            api_key="test",
            base_url="https://api.example.test/v1",
            model="test",
        ),
        llm_context="alpha context",
        search_queries=["alpha"],
        max_steps=3,
        runtime=runtime,
    )

    assert len(evidence_items) == 1
    assert evidence_items[0]["title"] == "restored"
    assert evidence_items[0]["url"] == restored_evidence[0]["url"]
    assert evidence_items[0].get("evidence_id")
    assert "wiki_search" in tools_used
    assert rounds == 2
    assert runtime.llm_calls == 2
    assert runtime.tool_calls == 2
