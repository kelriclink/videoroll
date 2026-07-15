from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from videoroll.apps.orchestrator_api.infrastructure.lifecycle import orchestrator_lifespan
from videoroll.apps.orchestrator_api.middleware import AdminAuthMiddleware
from videoroll.apps.orchestrator_api.realtime import router as realtime_router
from videoroll.apps.orchestrator_api.routers.assets import router as assets_router
from videoroll.apps.orchestrator_api.routers.auth import router as auth_router
from videoroll.apps.orchestrator_api.routers.desktop import router as desktop_router
from videoroll.apps.orchestrator_api.routers.maintenance import router as maintenance_router
from videoroll.apps.orchestrator_api.routers.publishing import router as publishing_router
from videoroll.apps.orchestrator_api.routers.settings import router as settings_router
from videoroll.apps.orchestrator_api.routers.system import router as system_router
from videoroll.apps.orchestrator_api.routers.tasks import router as tasks_router
from videoroll.apps.orchestrator_api.routers.youtube import router as youtube_router


def _value_error_handler(_request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def create_app(*, install_lifecycle: bool = True) -> FastAPI:
    application = FastAPI(
        title="videoroll-orchestrator",
        version="0.1.0",
        lifespan=orchestrator_lifespan if install_lifecycle else None,
    )

    application.add_middleware(AdminAuthMiddleware)
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ALLOW_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.state.cors_origins = tuple(cors_origins)

    application.include_router(auth_router)
    application.include_router(realtime_router)
    application.include_router(desktop_router)
    application.include_router(system_router)
    application.include_router(settings_router)
    application.include_router(maintenance_router)
    application.include_router(assets_router)
    application.include_router(youtube_router)
    application.include_router(publishing_router)
    application.include_router(tasks_router)
    application.add_exception_handler(ValueError, _value_error_handler)
    return application
