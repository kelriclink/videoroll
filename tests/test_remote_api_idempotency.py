from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from videoroll.apps.orchestrator_api.remote_api_settings_store import update_remote_api_settings
from videoroll.apps.orchestrator_api.routers import youtube as youtube_router
from videoroll.apps.orchestrator_api.schemas import AutoYouTubeResponse, RemoteAutoYouTubeRequest
from videoroll.db.base import Base
from videoroll.db.models import RemoteAPIRequest, SourceLicense


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: object, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
OTHER_YOUTUBE_URL = "https://www.youtube.com/watch?v=oHg5SJYRHA0"
REMOTE_TOKEN = "remote-token-for-contract-tests"


@pytest.fixture
def db(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'remote-api.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    update_remote_api_settings(session, {"token": REMOTE_TOKEN})
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request(*, method: str, authorization: str | None = None, idempotency_key: str | None = None, query_string: bytes = b"") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("utf-8")))
    if idempotency_key is not None:
        headers.append((b"idempotency-key", idempotency_key.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/remote/auto/youtube",
            "headers": headers,
            "query_string": query_string,
            "client": ("203.0.113.7", 12345),
        }
    )


def _payload(url: str = YOUTUBE_URL) -> RemoteAutoYouTubeRequest:
    return RemoteAutoYouTubeRequest(
        url=url,
        license=SourceLicense.authorized,
        proof_url="https://example.com/proof",
        auto_publish=True,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(redis_url="")


def test_remote_get_returns_migration_response() -> None:
    with pytest.raises(HTTPException) as raised:
        youtube_router.remote_auto_youtube_legacy()

    assert raised.value.status_code == 410
    assert "POST JSON" in str(raised.value.detail)


def test_remote_post_requires_bearer_not_query_token(db: Session) -> None:
    with pytest.raises(HTTPException) as raised:
        youtube_router.remote_auto_youtube(
            _payload(),
            _request(method="POST", query_string=b"token=" + REMOTE_TOKEN.encode("utf-8")),
            idempotency_key="key-query-token",
            settings=_settings(),
            db=db,
        )

    assert raised.value.status_code == 401


def test_same_idempotency_key_replays_one_pipeline_without_persisting_payload(db: Session) -> None:
    task_id = uuid.uuid4()
    with patch.object(
        youtube_router.youtube_service,
        "start_auto_youtube_pipeline",
        return_value=AutoYouTubeResponse(task_id=task_id, pipeline_job_id="pipeline-1"),
    ) as dispatch:
        first = youtube_router.remote_auto_youtube(
            _payload(),
            _request(method="POST", authorization=f"Bearer {REMOTE_TOKEN}"),
            idempotency_key="key-replay",
            settings=_settings(),
            db=db,
        )
        second = youtube_router.remote_auto_youtube(
            _payload(),
            _request(method="POST", authorization=f"Bearer {REMOTE_TOKEN}"),
            idempotency_key="key-replay",
            settings=_settings(),
            db=db,
        )

    assert first.task_id == second.task_id == task_id
    assert first.pipeline_job_id == second.pipeline_job_id == "pipeline-1"
    assert dispatch.call_count == 1
    record = db.query(RemoteAPIRequest).one()
    assert record.request_json == {}
    assert record.token_hash != REMOTE_TOKEN
    assert record.request_hash
    assert record.response_json == {
        "task_id": str(task_id),
        "pipeline_job_id": "pipeline-1",
        "deduped": False,
        "source_id": None,
    }


def test_same_idempotency_key_with_different_payload_is_conflict(db: Session) -> None:
    task_id = uuid.uuid4()
    with patch.object(
        youtube_router.youtube_service,
        "start_auto_youtube_pipeline",
        return_value=AutoYouTubeResponse(task_id=task_id, pipeline_job_id="pipeline-1"),
    ):
        youtube_router.remote_auto_youtube(
            _payload(),
            _request(method="POST", authorization=f"Bearer {REMOTE_TOKEN}"),
            idempotency_key="key-conflict",
            settings=_settings(),
            db=db,
        )
        with pytest.raises(HTTPException) as raised:
            youtube_router.remote_auto_youtube(
                _payload(OTHER_YOUTUBE_URL),
                _request(method="POST", authorization=f"Bearer {REMOTE_TOKEN}"),
                idempotency_key="key-conflict",
                settings=_settings(),
                db=db,
            )

    assert raised.value.status_code == 409
