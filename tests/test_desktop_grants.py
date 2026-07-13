from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.desktop_grants import (
    authorize_desktop_request,
    create_desktop_grant,
    desktop_session_fingerprint,
)
from videoroll.db.models import DesktopAccessGrant


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    DesktopAccessGrant.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _desktop_request(
    db: Session,
    *,
    admin_session: str,
    desktop_path: str,
    token: str,
    resource_id: str,
    query_credentials: bool = True,
) -> Request:
    app = FastAPI()
    original_uri = f"{desktop_path}?grant={token}&resource={resource_id}" if query_credentials else desktop_path
    headers = [(b"x-original-uri", original_uri.encode("ascii"))]
    if not query_credentials:
        headers.extend(
            [
                (b"x-desktop-grant", token.encode("ascii")),
                (b"x-desktop-resource", resource_id.encode("ascii")),
            ]
        )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/desktop/authorize",
            "query_string": b"",
            "headers": headers,
            "app": app,
        }
    )
    request.state.desktop_grant_db = db
    request.state.admin_session = admin_session
    return request


def test_grant_is_scoped_to_admin_session_desktop_and_resource() -> None:
    db = _db()
    grant = create_desktop_grant(
        db,
        "session-a",
        "publish",
        "7b085ce5-38c0-4da4-ae2f-4f576b701da6",
        reconnect_limit=2,
    )

    authorize_desktop_request(
        _desktop_request(
            db,
            admin_session="session-a",
            desktop_path="/social-publish/vnc.html",
            token=grant.token,
            resource_id=grant.resource_id,
        )
    )

    for session, path, resource in (
        ("session-b", "/social-publish/vnc.html", grant.resource_id),
        ("session-a", "/social-login/vnc.html", grant.resource_id),
        ("session-a", "/social-publish/vnc.html", "9df598b3-77c4-4ed0-8b24-c84b22a41f2b"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            authorize_desktop_request(
                _desktop_request(
                    db,
                    admin_session=session,
                    desktop_path=path,
                    token=grant.token,
                    resource_id=resource,
                )
            )
        assert exc_info.value.status_code == 403

    stored = db.query(DesktopAccessGrant).one()
    assert stored.token_hash != grant.token
    assert grant.token not in str(stored.scope_json)
    assert stored.subject == desktop_session_fingerprint("session-a")
    db.close()


def test_grant_reconnects_are_bounded_and_consumed_atomically() -> None:
    db = _db()
    grant = create_desktop_grant(
        db,
        "session-a",
        "login",
        "bd513369-18f6-4f24-afdc-2855f4ad95f4",
        reconnect_limit=2,
    )
    request = lambda: _desktop_request(
        db,
        admin_session="session-a",
        desktop_path="/social-login/websockify",
        token=grant.token,
        resource_id=grant.resource_id,
    )

    authorize_desktop_request(request())
    authorize_desktop_request(request())
    with pytest.raises(HTTPException) as exc_info:
        authorize_desktop_request(request())

    assert exc_info.value.status_code == 403
    stored = db.query(DesktopAccessGrant).one()
    assert stored.scope_json["reconnect_count"] == 2
    assert stored.status == "consumed"
    assert stored.consumed_at is not None
    db.close()


def test_novnc_http_assets_are_authorized_without_spending_reconnects() -> None:
    db = _db()
    grant = create_desktop_grant(
        db,
        "session-a",
        "login",
        "2a1d8770-7c5a-41db-8372-9afab2d0f0a8",
        reconnect_limit=1,
    )
    authorize_desktop_request(
        _desktop_request(
            db,
            admin_session="session-a",
            desktop_path="/social-login/vnc.html",
            token=grant.token,
            resource_id=grant.resource_id,
        )
    )
    authorize_desktop_request(
        _desktop_request(
            db,
            admin_session="session-a",
            desktop_path="/social-login/app/ui.css",
            token=grant.token,
            resource_id=grant.resource_id,
            query_credentials=False,
        )
    )
    assert db.query(DesktopAccessGrant).one().scope_json["reconnect_count"] == 0
    authorize_desktop_request(
        _desktop_request(
            db,
            admin_session="session-a",
            desktop_path="/social-login/websockify",
            token=grant.token,
            resource_id=grant.resource_id,
        )
    )
    db.close()


def test_grant_expiry_is_rejected_without_authorizing_desktop() -> None:
    db = _db()
    grant = create_desktop_grant(
        db,
        "session-a",
        "login",
        "c23a2e8b-d47b-4ef1-b55e-e7dac6d2d468",
        ttl_seconds=1,
    )
    stored = db.query(DesktopAccessGrant).one()
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        authorize_desktop_request(
            _desktop_request(
                db,
                admin_session="session-a",
                desktop_path="/social-login/vnc.html",
                token=grant.token,
                resource_id=grant.resource_id,
            )
        )

    assert exc_info.value.status_code == 403
    assert db.query(DesktopAccessGrant).one().status == "expired"
    db.close()
