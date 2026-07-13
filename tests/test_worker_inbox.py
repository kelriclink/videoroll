from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.outbox.worker_inbox import claim_operation, finish_operation, heartbeat_operation
from videoroll.db.models import OperationInbox


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type: JSONB, _compiler: object, **_kwargs: object) -> str:
    return "JSON"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    OperationInbox.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        OperationInbox.__table__.drop(engine)


def test_duplicate_operation_delivery_returns_stored_result(db: Session) -> None:
    first = claim_operation(db, "op-1", "worker-a", lease_seconds=60)
    assert first.acquired is True
    finish_operation(db, "op-1", {"external_id": "post-1"})

    second = claim_operation(db, "op-1", "worker-b", lease_seconds=60)

    assert second.acquired is False
    assert second.result_json == {"external_id": "post-1"}


def test_active_operation_is_not_reclaimed_before_lease_expiry(db: Session) -> None:
    first = claim_operation(db, "op-1", "worker-a", lease_seconds=60)

    second = claim_operation(db, "op-1", "worker-b", lease_seconds=60)

    assert first.acquired is True
    assert second.acquired is False
    assert second.is_active is True


def test_expired_operation_lease_is_reclaimed_and_heartbeated(db: Session) -> None:
    first = claim_operation(db, "op-1", "worker-a", lease_seconds=1)
    first.operation.lease_until = first.operation.lease_until - timedelta(seconds=2)
    db.flush()

    second = claim_operation(db, "op-1", "worker-b", lease_seconds=60)

    assert second.acquired is True
    assert heartbeat_operation(db, "op-1", "worker-b", lease_seconds=60) is True
    assert second.operation.lease_owner == "worker-b"
