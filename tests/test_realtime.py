from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from videoroll import realtime
from videoroll.apps.orchestrator_api import realtime as orchestrator_realtime
from videoroll.apps.orchestrator_api.admin_auth_store import DEVICE_COOKIE_NAME, mint_device_cookie_value
from videoroll.db.models import SourceLicense, SourceType, Task


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RecordingRedis:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[tuple[str, bytes]] = []

    def publish(self, channel: str, raw: bytes) -> int:
        if self.error:
            raise self.error
        self.messages.append((channel, raw))
        return 1


def test_publish_ui_event_serializes_envelope_and_enforces_size(monkeypatch) -> None:
    client = _RecordingRedis()
    monkeypatch.setattr(realtime, "_redis_client", lambda _url: client)

    assert realtime.publish_ui_event(
        "redis://test",
        topics=["tasks", "tasks", "task:123"],
        name="task.updated",
        entity_id="123",
        data={"status": "PUBLISHING"},
    )
    assert len(client.messages) == 1
    channel, raw = client.messages[0]
    payload = json.loads(raw)
    assert channel == realtime.UI_EVENT_CHANNEL
    assert payload["v"] == realtime.UI_EVENT_VERSION
    assert payload["type"] == "event"
    assert payload["topics"] == ["task:123", "tasks"]
    assert payload["name"] == "task.updated"
    assert payload["entity_id"] == "123"
    assert payload["occurred_at"]

    assert not realtime.publish_ui_event(
        "redis://test",
        topics=["tasks"],
        name="too.large",
        data={"blob": "x" * realtime.UI_EVENT_MAX_BYTES},
    )
    assert len(client.messages) == 1


def test_publish_ui_event_is_best_effort_when_redis_is_down(monkeypatch) -> None:
    client = _RecordingRedis(error=RedisError("redis unavailable"))
    monkeypatch.setattr(realtime, "_redis_client", lambda _url: client)

    assert not realtime.publish_ui_event("redis://test", topics=["tasks"], name="task.updated")


def test_session_events_emit_after_commit_and_clear_after_rollback(monkeypatch) -> None:
    realtime.install_session_event_emitter()
    engine = create_engine("sqlite:///:memory:")
    Task.__table__.create(engine)
    emitted: list[dict[str, object]] = []
    monkeypatch.setenv("REDIS_URL", "redis://test")
    monkeypatch.setattr(realtime, "publish_ui_event", lambda _url, **event: emitted.append(event) or True)

    with Session(engine) as db:
        task = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(task)
        db.flush()
        assert emitted == []
        db.commit()

        assert len(emitted) == 1
        assert emitted[0]["name"] == "task.updated"
        assert emitted[0]["entity_id"] == str(task.id)

        rolled_back = Task(source_type=SourceType.local, source_license=SourceLicense.own)
        db.add(rolled_back)
        db.flush()
        db.rollback()
        assert len(emitted) == 1


@pytest.mark.anyio
async def test_hub_filters_topics_and_rejects_invalid_subscriptions() -> None:
    hub = orchestrator_realtime.RealtimeHub("redis://test")
    task_connection = await hub.register()
    resource_connection = await hub.register()
    await hub.set_topics(task_connection, ["tasks"])
    await hub.set_topics(resource_connection, ["resources"])

    event = {"type": "event", "topics": ["tasks"], "name": "task.updated", "data": {}}
    await hub.broadcast(event)
    assert task_connection.queue.get_nowait() == event
    assert resource_connection.queue.empty()

    with pytest.raises(ValueError):
        await hub.set_topics(task_connection, ["task:not-a-uuid"])
    with pytest.raises(ValueError):
        await hub.set_topics(task_connection, [f"task:{uuid.uuid4()}" for _ in range(33)])


def test_hub_queue_overflow_requests_one_resync() -> None:
    connection = orchestrator_realtime.RealtimeConnection(id="client")
    for index in range(orchestrator_realtime.CLIENT_QUEUE_SIZE):
        orchestrator_realtime.RealtimeHub._enqueue(connection, {"type": "event", "index": index})

    orchestrator_realtime.RealtimeHub._enqueue(connection, {"type": "event", "index": "overflow"})
    orchestrator_realtime.RealtimeHub._enqueue(connection, {"type": "event", "index": "overflow-again"})
    messages = [connection.queue.get_nowait() for _ in range(connection.queue.qsize())]
    assert sum(message.get("type") == "resync_required" for message in messages) == 1


@pytest.mark.anyio
async def test_resource_sampler_only_runs_for_resource_subscribers() -> None:
    samples = 0

    async def sampler() -> dict[str, object]:
        nonlocal samples
        samples += 1
        return {"sampled_at": "now", "cpu": {"cores": 1}}

    hub = orchestrator_realtime.RealtimeHub("redis://test", resource_sampler=sampler)
    assert not await hub.sample_resources_once()
    assert samples == 0

    connection = await hub.register()
    await hub.set_topics(connection, ["resources"])
    assert await hub.sample_resources_once()
    assert samples == 1
    assert connection.queue.get_nowait()["name"] == "system.resources.sample"


def test_websocket_origin_and_cookie_authentication() -> None:
    password_hash = "stored-password-hash"
    cookie_secret = "cookie-secret"
    cookie = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=password_hash)
    state = SimpleNamespace(
        admin_password_hash=password_hash,
        admin_cookie_secret=cookie_secret,
        cors_origins=("https://admin.example",),
    )
    websocket = SimpleNamespace(
        headers={"origin": "https://video.example", "host": "video.example"},
        cookies={DEVICE_COOKIE_NAME: cookie},
        app=SimpleNamespace(state=state),
    )
    assert orchestrator_realtime._origin_allowed(websocket)
    assert orchestrator_realtime._admin_authorized(websocket)

    websocket.headers["origin"] = "https://admin.example"
    assert orchestrator_realtime._origin_allowed(websocket)
    websocket.headers["origin"] = "https://attacker.example"
    assert not orchestrator_realtime._origin_allowed(websocket)
    websocket.cookies[DEVICE_COOKIE_NAME] = "invalid"
    assert not orchestrator_realtime._admin_authorized(websocket)


def test_websocket_endpoint_uses_auth_and_origin_close_codes() -> None:
    password_hash = "stored-password-hash"
    cookie_secret = "cookie-secret"
    app = FastAPI()
    app.include_router(orchestrator_realtime.router)
    app.state.admin_password_hash = password_hash
    app.state.admin_cookie_secret = cookie_secret
    app.state.cors_origins = ()
    app.state.realtime_hub = orchestrator_realtime.RealtimeHub("redis://test")
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as unauthorized:
        with client.websocket_connect("/ws/events", headers={"origin": "http://testserver"}):
            pass
    assert unauthorized.value.code == 4401

    client.cookies.set(
        DEVICE_COOKIE_NAME,
        mint_device_cookie_value(internal_secret=cookie_secret, password_hash=password_hash),
    )
    with pytest.raises(WebSocketDisconnect) as rejected_origin:
        with client.websocket_connect("/ws/events", headers={"origin": "https://attacker.example"}):
            pass
    assert rejected_origin.value.code == 4403

    with client.websocket_connect("/ws/events", headers={"origin": "http://testserver"}) as websocket:
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        websocket.send_json({"op": "set_subscriptions", "topics": ["tasks"]})
        assert websocket.receive_json() == {"type": "subscribed", "topics": ["tasks"]}


def test_frontend_proxies_websocket_upgrades() -> None:
    nginx = Path("src/web/nginx.conf").read_text(encoding="utf-8")
    vite = Path("src/web/vite.config.ts").read_text(encoding="utf-8")
    assert "location /api/ws/" in nginx
    assert "proxy_set_header Upgrade $http_upgrade" in nginx
    assert "proxy_set_header Host $http_host" in nginx
    assert "access_log off" in nginx
    assert "ws: true" in vite


def test_realtime_pages_have_no_periodic_api_polling() -> None:
    for page in (
        "TaskDetailPage.tsx",
        "RenderQueuePage.tsx",
        "DashboardPage.tsx",
        "SettingsPublishPage.tsx",
    ):
        source = Path("src/web/src/pages", page).read_text(encoding="utf-8")
        assert "setInterval" not in source
        assert "setTimeout(load" not in source
