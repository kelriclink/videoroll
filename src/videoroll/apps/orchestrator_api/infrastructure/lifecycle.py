from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from videoroll.apps.orchestrator_api.admin_auth_store import get_password_hash
from videoroll.apps.orchestrator_api.infrastructure.scheduler import OrchestratorScheduler
from videoroll.apps.orchestrator_api.realtime import RealtimeHub
from videoroll.apps.orchestrator_api.services.system_service import collect_system_resources
from videoroll.apps.orchestrator_api.services.live_service import recover_interrupted_live_stream
from videoroll.apps.security.service_auth import (
    admin_cookie_secret,
    ensure_bootstrap_state,
    service_token,
    validate_bootstrap_secret,
)
from videoroll.config import get_orchestrator_settings
from videoroll.db.auto_migrate import auto_migrate
from videoroll.db.base import Base
from videoroll.db.session import get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store


def initialize_runtime(app: FastAPI) -> OrchestratorScheduler:
    settings = get_orchestrator_settings()
    validate_bootstrap_secret(settings)
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    auto_migrate(settings.database_url)
    S3Store(settings).ensure_bucket()
    Path(settings.work_dir).mkdir(parents=True, exist_ok=True)

    app.state.database_url = settings.database_url
    app.state.redis_url = settings.redis_url
    app.state.trusted_proxy_cidrs = settings.trusted_proxy_cidrs
    app.state.internal_header_token = service_token(settings)
    app.state.internal_service_token = service_token(settings)
    app.state.admin_cookie_secret = admin_cookie_secret(settings)
    app.state.admin_bootstrap_secret = settings.admin_bootstrap_secret
    session_local = get_sessionmaker(settings.database_url)
    db = session_local()
    try:
        ensure_bootstrap_state(db)
        recover_interrupted_live_stream(db)
        app.state.admin_password_hash = get_password_hash(db)
    finally:
        db.close()

    scheduler = OrchestratorScheduler(settings)
    app.state.orchestrator_scheduler = scheduler

    async def sample_resources() -> dict:
        def collect() -> dict:
            resource_db = session_local()
            try:
                return collect_system_resources(resource_db).model_dump(mode="json")
            finally:
                resource_db.close()

        return await asyncio.to_thread(collect)

    app.state.realtime_hub = RealtimeHub(settings.redis_url, resource_sampler=sample_resources)
    return scheduler


@asynccontextmanager
async def orchestrator_lifespan(app: FastAPI):
    scheduler = initialize_runtime(app)
    realtime_hub: RealtimeHub = app.state.realtime_hub
    scheduler.start()
    await realtime_hub.start()
    try:
        yield
    finally:
        await realtime_hub.stop()
        scheduler.stop()
