from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import RedisError

from videoroll.apps.orchestrator_api.admin_auth_store import DEVICE_COOKIE_NAME, verify_device_cookie_value
from videoroll.realtime import UI_EVENT_CHANNEL, UI_EVENT_MAX_BYTES


logger = logging.getLogger(__name__)
router = APIRouter()

FIXED_TOPICS = frozenset({"tasks", "queue", "resources", "agents", "publishing"})
MAX_TOPICS_PER_CONNECTION = 32
MAX_CLIENT_MESSAGE_BYTES = 8 * 1024
CLIENT_QUEUE_SIZE = 256


def _valid_topic(topic: str) -> bool:
    if topic in FIXED_TOPICS:
        return True
    if not topic.startswith("task:"):
        return False
    try:
        uuid.UUID(topic.split(":", 1)[1])
    except (TypeError, ValueError):
        return False
    return True


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = str(websocket.headers.get("origin") or "").strip()
    if not origin:
        return False
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return False
    host = str(websocket.headers.get("host") or "").strip().lower()
    if parsed.netloc.lower() == host:
        return True
    allowed = set(getattr(websocket.app.state, "cors_origins", ()) or ())
    return origin.rstrip("/") in {str(value).rstrip("/") for value in allowed}


def _admin_authorized(websocket: WebSocket) -> bool:
    password_hash = str(getattr(websocket.app.state, "admin_password_hash", "") or "").strip()
    cookie_secret = str(getattr(websocket.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_value = str(websocket.cookies.get(DEVICE_COOKIE_NAME) or "").strip()
    return bool(
        password_hash
        and cookie_secret
        and cookie_value
        and verify_device_cookie_value(
            cookie_value,
            internal_secret=cookie_secret,
            password_hash=password_hash,
        )
    )


@dataclass
class RealtimeConnection:
    id: str
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE))
    topics: set[str] = field(default_factory=set)
    resync_queued: bool = False


class RealtimeHub:
    def __init__(
        self,
        redis_url: str,
        *,
        resource_sampler: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.resource_sampler = resource_sampler
        self._connections: dict[str, RealtimeConnection] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._redis_loop(), name="realtime-redis"),
            asyncio.create_task(self._heartbeat_loop(), name="realtime-heartbeat"),
            asyncio.create_task(self._resource_loop(), name="realtime-resources"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def register(self) -> RealtimeConnection:
        connection = RealtimeConnection(id=str(uuid.uuid4()))
        async with self._lock:
            self._connections[connection.id] = connection
        return connection

    async def unregister(self, connection: RealtimeConnection) -> None:
        async with self._lock:
            self._connections.pop(connection.id, None)

    async def set_topics(self, connection: RealtimeConnection, topics: list[str]) -> None:
        normalized = {str(topic or "").strip() for topic in topics}
        if len(normalized) > MAX_TOPICS_PER_CONNECTION or any(not _valid_topic(topic) for topic in normalized):
            raise ValueError("invalid realtime subscription topics")
        connection.topics = normalized

    async def _connections_snapshot(self) -> list[RealtimeConnection]:
        async with self._lock:
            return list(self._connections.values())

    @staticmethod
    def _enqueue(connection: RealtimeConnection, message: dict[str, Any]) -> None:
        try:
            connection.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        if connection.resync_queued:
            return
        connection.resync_queued = True
        with contextlib.suppress(asyncio.QueueEmpty):
            connection.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            connection.queue.put_nowait({"type": "resync_required", "reason": "client_queue_overflow"})

    async def broadcast(self, event: dict[str, Any]) -> None:
        topics = {str(topic) for topic in event.get("topics", []) if str(topic)}
        if not topics:
            return
        for connection in await self._connections_snapshot():
            if connection.topics.intersection(topics):
                self._enqueue(connection, event)

    async def _redis_loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            client: Redis | None = None
            pubsub = None
            try:
                client = Redis.from_url(self.redis_url, decode_responses=False, health_check_interval=30)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(UI_EVENT_CHANNEL)
                delay = 1.0
                while not self._stop.is_set():
                    message = await pubsub.get_message(timeout=1.0)
                    if not message:
                        continue
                    raw = message.get("data")
                    if not isinstance(raw, (bytes, bytearray)) or len(raw) > UI_EVENT_MAX_BYTES:
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(event, dict) and event.get("type") == "event":
                        await self.broadcast(event)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError):
                logger.warning("realtime Redis subscriber disconnected; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.close()
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(20.0)
            for connection in await self._connections_snapshot():
                self._enqueue(connection, {"type": "heartbeat"})

    async def _resource_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(3.0)
            await self.sample_resources_once()

    async def sample_resources_once(self) -> bool:
        if self.resource_sampler is None:
            return False
        connections = await self._connections_snapshot()
        if not any("resources" in connection.topics for connection in connections):
            return False
        try:
            data = await self.resource_sampler()
        except Exception:
            logger.exception("realtime resource sampling failed")
            return False
        await self.broadcast(
            {
                "v": 1,
                "type": "event",
                "event_id": str(uuid.uuid4()),
                "topics": ["resources"],
                "name": "system.resources.sample",
                "data": data,
            }
        )
        return True


async def _sender(websocket: WebSocket, connection: RealtimeConnection) -> None:
    while True:
        message = await connection.queue.get()
        if message.get("type") == "resync_required":
            connection.resync_queued = False
        await websocket.send_json(message)


@router.websocket("/ws/events")
async def realtime_events(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=4403)
        return
    if not _admin_authorized(websocket):
        await websocket.close(code=4401)
        return
    hub: RealtimeHub | None = getattr(websocket.app.state, "realtime_hub", None)
    if hub is None:
        await websocket.close(code=1013)
        return

    await websocket.accept()
    connection = await hub.register()
    sender = asyncio.create_task(_sender(websocket, connection))
    await websocket.send_json({"type": "ready", "connection_id": connection.id, "heartbeat_seconds": 20})
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > MAX_CLIENT_MESSAGE_BYTES:
                await websocket.close(code=1009)
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid JSON"})
                continue
            op = str(message.get("op") or "") if isinstance(message, dict) else ""
            if op == "set_subscriptions":
                topics = message.get("topics") if isinstance(message, dict) else None
                if not isinstance(topics, list):
                    await websocket.send_json({"type": "error", "detail": "topics must be an array"})
                    continue
                try:
                    await hub.set_topics(connection, [str(topic) for topic in topics])
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue
                await websocket.send_json({"type": "subscribed", "topics": sorted(connection.topics)})
            elif op == "pong":
                continue
            else:
                await websocket.send_json({"type": "error", "detail": "unsupported operation"})
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        await hub.unregister(connection)
