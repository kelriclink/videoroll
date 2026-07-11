from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from videoroll.config import OrchestratorSettings, get_orchestrator_settings
from videoroll.db.session import db_session
from videoroll.storage.s3 import S3Store


def get_settings() -> OrchestratorSettings:
    return get_orchestrator_settings()


def get_db(settings: OrchestratorSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


def get_s3(settings: OrchestratorSettings = Depends(get_settings)) -> S3Store:
    return S3Store(settings)
