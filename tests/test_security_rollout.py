from __future__ import annotations

import socket
import uuid
from datetime import timedelta
from pathlib import Path
import re

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from videoroll.apps.egress_gateway.client import EgressDenied, EgressGatewayClient, resolve_public_endpoint
from videoroll.apps.orchestrator_api.desktop_grants import authorize_desktop_request, create_desktop_grant
from videoroll.apps.orchestrator_api.routers.youtube import remote_auto_youtube_legacy
from videoroll.apps.outbox.service import (
    claim_outbox_events,
    create_outbox_event,
    mark_outbox_dispatch_failed,
)
from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER, require_internal_service
from videoroll.db.auto_migrate import _backfill_publish_batch_lifecycle
from videoroll.db.models import DesktopAccessGrant, OutboxEvent


ROOT = Path(__file__).resolve().parents[1]


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: object, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


def _request(path: str, *, token: str | None = None) -> Request:
    headers = [(INTERNAL_TOKEN_HEADER.lower().encode(), token.encode())] if token else []
    app = FastAPI()
    app.state.internal_service_token = "expected-service-token"
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers, "app": app})


def _desktop_request(db, *, admin_session: str, token: str, resource_id: str) -> Request:
    app = FastAPI()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/desktop/authorize",
            "headers": [
                (
                    b"x-original-uri",
                    f"/social-login/websockify?grant={token}&resource={resource_id}".encode(),
                )
            ],
            "app": app,
        }
    )
    request.state.desktop_grant_db = db
    request.state.admin_session = admin_session
    return request


def _compose_service_block(compose: str, service: str) -> str:
    start = re.search(rf"(?m)^  {re.escape(service)}:\n", compose)
    assert start is not None
    end = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\n", compose[start.end() :])
    return compose[start.start() : start.end() + end.start() if end else len(compose)]


def test_legacy_publish_rows_do_not_manufacture_a_second_active_batch() -> None:
    """A rollout preserves old publish jobs as history and keeps the task pointer authoritative."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tasks (id TEXT PRIMARY KEY, active_publish_batch_id TEXT)"))
        conn.execute(
            text(
                """
                CREATE TABLE publish_batches (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cleanup_enqueued_at TEXT,
                    cleanup_delivery_version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        conn.execute(
            text("CREATE TABLE publish_jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, batch_id TEXT, state TEXT)")
        )
        conn.execute(text("INSERT INTO tasks VALUES ('task-1', 'batch-current')"))
        conn.execute(
            text(
                "INSERT INTO publish_batches VALUES "
                "('batch-legacy', 'task-1', 'failed', '2026-01-01T00:00:00Z', NULL, 0), "
                "('batch-current', 'task-1', 'active', '2026-01-02T00:00:00Z', NULL, 0)"
            )
        )
        conn.execute(text("INSERT INTO publish_jobs VALUES ('legacy-job', 'task-1', NULL, 'published')"))

    _backfill_publish_batch_lifecycle(engine)
    _backfill_publish_batch_lifecycle(engine)  # startup retries are safe

    with engine.connect() as conn:
        active_batch = conn.execute(text("SELECT active_publish_batch_id FROM tasks WHERE id = 'task-1'")).scalar_one()
        batch_count = conn.execute(text("SELECT COUNT(*) FROM publish_batches WHERE task_id = 'task-1'")).scalar_one()
        legacy_job_batch = conn.execute(text("SELECT batch_id FROM publish_jobs WHERE id = 'legacy-job'")).scalar_one()

    assert active_batch == "batch-current"
    assert batch_count == 2
    assert legacy_job_batch is None


def test_rollout_blocks_query_credentials_and_requires_internal_service_authentication() -> None:
    with pytest.raises(HTTPException) as legacy:
        remote_auto_youtube_legacy()
    assert legacy.value.status_code == 410

    with pytest.raises(HTTPException) as missing:
        require_internal_service(_request("/youtube/sources"))
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as invalid:
        require_internal_service(_request("/youtube/sources", token="wrong"))
    assert invalid.value.status_code == 403

    require_internal_service(_request("/health"))

    with pytest.raises(Exception, match="invalid"):
        EgressGatewayClient("http://user:password@egress-gateway:8020", "service-token")


def test_compose_keeps_internal_services_off_host_ports_and_grant_is_scoped() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service in (
        "orchestrator",
        "subtitle-service",
        "youtube-ingest",
        "bilibili-publisher",
        "social-publisher-api",
        "egress-gateway",
        "outbox-dispatcher",
    ):
        block = _compose_service_block(compose, service)
        assert "ports:" not in block
    assert "internal:\n    internal: true" in compose

    engine = create_engine("sqlite://")
    DesktopAccessGrant.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    resource_id = str(uuid.uuid4())
    grant = create_desktop_grant(db, "admin-session", "login", resource_id, reconnect_limit=1)
    authorize_desktop_request(_desktop_request(db, admin_session="admin-session", token=grant.token, resource_id=resource_id))
    with pytest.raises(HTTPException) as reused:
        authorize_desktop_request(_desktop_request(db, admin_session="admin-session", token=grant.token, resource_id=resource_id))
    assert reused.value.status_code == 403
    db.close()


def test_outbox_retry_and_private_egress_rejection_are_preserved(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    OutboxEvent.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    event = create_outbox_event(
        db,
        event_type="publish.bilibili",
        aggregate_type="publish_job",
        aggregate_id="job-1",
        task_name="bilibili_publisher.process_job",
        args={"args": ["job-1"], "queue": "publish"},
        operation_key="security-rollout:job-1",
    )
    now = event.available_at
    assert claim_outbox_events(db, owner="security-smoke", limit=1, now=now) == [event]
    mark_outbox_dispatch_failed(db, event.id, owner="security-smoke", error="broker unavailable", now=now)
    assert event.status == "pending"
    assert event.available_at == now + timedelta(seconds=2)
    db.close()

    def private_answer(host: str, port: int, **_kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private_answer)
    with pytest.raises(EgressDenied, match="non-global"):
        resolve_public_endpoint("https://private.example.test/resource")
