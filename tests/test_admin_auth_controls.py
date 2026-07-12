from __future__ import annotations

from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI, HTTPException, Request

from videoroll.apps.orchestrator_api import middleware as auth_middleware
from videoroll.apps.orchestrator_api.services import auth_service
from videoroll.apps.security.audit import build_security_audit_event
from videoroll.apps.security import service_auth
from videoroll.apps.security.rate_limits import (
    check_login_rate_limit,
    record_login_failure,
)
from videoroll.apps.security.service_auth import ADMIN_BOOTSTRAP_HEADER, consume_bootstrap_secret


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        value = self.values.get(key)
        return None if value is None else str(value).encode("ascii")

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    def eval(self, _script: str, _keys: int, key: str, window_seconds: int) -> list[int]:
        count = self.values.get(key, 0) + 1
        self.values[key] = count
        self.ttls.setdefault(key, int(window_seconds))
        return [count, self.ttls[key]]

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)


def test_setup_requires_bootstrap_secret_and_consumes_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"consumed": False}

    def fake_consume(_request: object, presented: str) -> None:
        if presented != "one-time-secret" or state["consumed"]:
            raise HTTPException(status_code=403, detail="invalid bootstrap secret")
        state["consumed"] = True

    monkeypatch.setattr(auth_service, "consume_bootstrap_secret", fake_consume)
    request = SimpleNamespace(headers={ADMIN_BOOTSTRAP_HEADER: "one-time-secret"})

    auth_service.require_bootstrap_secret(request)
    with pytest.raises(HTTPException) as exc_info:
        auth_service.require_bootstrap_secret(request)

    assert exc_info.value.status_code == 403


def test_consume_bootstrap_secret_rejects_missing_database_state() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(admin_bootstrap_secret="secret"),
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        consume_bootstrap_secret(request, "secret")

    assert exc_info.value.status_code == 503


def test_bootstrap_consume_refreshes_state_after_acquiring_row_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SimpleNamespace(value_json={"bootstrap_consumed": False}, version=1)

    class _Result:
        def scalar_one_or_none(self) -> object:
            return row

    class _Db:
        def execute(self, _statement: object) -> _Result:
            return _Result()

        def refresh(self, target: object, attribute_names: list[str]) -> None:
            assert target is row
            assert "value_json" in attribute_names
            row.value_json = {"bootstrap_consumed": True}

    monkeypatch.setattr(service_auth, "_bootstrap_db", lambda _request: (_Db(), False))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(admin_bootstrap_secret="one-time-secret")),
    )

    with pytest.raises(HTTPException) as exc_info:
        service_auth.consume_bootstrap_secret(request, "one-time-secret")

    assert exc_info.value.status_code == 403


def test_login_rate_limit_returns_retry_after_after_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "videoroll.apps.security.rate_limits.Redis.from_url",
        lambda *_args, **_kwargs: fake_redis,
    )

    key = "login:203.0.113.7"
    for _ in range(5):
        decision = record_login_failure("redis://unused", key)

    assert decision.allowed is False
    assert decision.retry_after > 0
    assert check_login_rate_limit("redis://unused", key).allowed is False


def test_threshold_crossing_failure_returns_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis_url="redis://unused")))
    db = SimpleNamespace()
    audits: list[dict[str, object]] = []
    monkeypatch.setattr(
        auth_service,
        "record_login_failure",
        lambda *_args: SimpleNamespace(allowed=False, retry_after=42, attempts=5),
    )
    monkeypatch.setattr(
        auth_service,
        "_audit",
        lambda *_args, **kwargs: audits.append(kwargs),
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_service._record_failure(request, db, "login", "login:203.0.113.7", "invalid_password")

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "42"}
    assert audits[0]["event_type"] == "admin.login.throttle"
    assert audits[0]["outcome"] == "throttled"


def test_source_ip_ignores_forwarded_header_from_untrusted_client() -> None:
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.8"},
        client=SimpleNamespace(host="203.0.113.7"),
    )

    assert auth_service._source_ip(request) == "203.0.113.7"


def test_security_audit_event_drops_sensitive_values_and_bounds_text() -> None:
    event = build_security_audit_event(
        event_type="admin.login.failure",
        outcome="failure",
        source_ip="203.0.113.7",
        payload={
            "password": "password-123",
            "authorization": "Bearer top-secret",
            "cookie": "session=top-secret",
            "reason": "x" * 1000,
        },
        error_message="y" * 1000,
    )

    assert "password" not in event.payload_json
    assert "authorization" not in event.payload_json
    assert "cookie" not in event.payload_json
    assert len(event.payload_json["reason"]) <= 256
    assert event.error_message is not None
    assert len(event.error_message) <= 512


@pytest.mark.anyio
async def test_admin_session_injects_internal_header_for_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_middleware, "get_admin_password_hash", lambda _request: "password-hash")
    monkeypatch.setattr(auth_middleware, "verify_device_cookie_value", lambda *_args, **_kwargs: True)

    app = FastAPI()
    app.state.internal_header_token = "internal-token"
    app.state.admin_cookie_secret = "cookie-secret"
    app.add_middleware(auth_middleware.AdminAuthMiddleware)

    @app.get("/private")
    async def private(request: Request) -> dict[str, str | None]:
        return {"internal": request.headers.get(auth_middleware.INTERNAL_TOKEN_HEADER)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth_middleware.DEVICE_COOKIE_NAME: "device-cookie"},
    ) as client:
        response = await client.get("/private")

    assert response.status_code == 200
    assert response.json() == {"internal": "internal-token"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
