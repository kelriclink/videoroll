from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, request_response

from videoroll.apps.security.service_auth import (
    INTERNAL_TOKEN_HEADER,
    InternalServiceAuthMiddleware,
    service_token,
)
from videoroll.config import OrchestratorSettings


def _settings(*, internal_secret: str, s3_secret: str = "s3-secret") -> SimpleNamespace:
    return SimpleNamespace(
        internal_api_secret=internal_secret,
        s3_secret_access_key=s3_secret,
    )


def test_internal_token_does_not_change_when_s3_secret_changes() -> None:
    first = service_token(_settings(internal_secret="internal-secret", s3_secret="s3-a"))
    second = service_token(_settings(internal_secret="internal-secret", s3_secret="s3-b"))

    assert first == second


def test_internal_token_changes_when_internal_secret_changes() -> None:
    first = service_token(_settings(internal_secret="internal-a"))
    second = service_token(_settings(internal_secret="internal-b"))

    assert first != second


def test_production_mode_rejects_known_default_security_secrets() -> None:
    settings = OrchestratorSettings(
        DATABASE_URL="postgresql+psycopg://localhost/videoroll",
        REDIS_URL="redis://localhost:6379/0",
        S3_ENDPOINT_URL="http://localhost:9000",
        S3_ACCESS_KEY_ID="minio",
        S3_SECRET_ACCESS_KEY="minio-secret",
        S3_BUCKET="videoroll",
        DEVELOPMENT_MODE=False,
    )

    with pytest.raises(ValueError):
        service_token(settings)


def test_development_mode_must_be_explicit_for_default_security_secrets() -> None:
    settings = OrchestratorSettings(
        DATABASE_URL="postgresql+psycopg://localhost/videoroll",
        REDIS_URL="redis://localhost:6379/0",
        S3_ENDPOINT_URL="http://localhost:9000",
        S3_ACCESS_KEY_ID="minio",
        S3_SECRET_ACCESS_KEY="minio-secret",
        S3_BUCKET="videoroll",
    )

    with pytest.raises(ValueError):
        service_token(settings)


def test_missing_development_mode_is_not_treated_as_development() -> None:
    with pytest.raises(ValueError):
        service_token(SimpleNamespace(internal_api_secret="videoroll-development-internal-secret"))


def test_string_development_mode_does_not_enable_insecure_defaults() -> None:
    with pytest.raises(ValueError):
        service_token(
            SimpleNamespace(
                internal_api_secret="videoroll-development-internal-secret",
                development_mode="false",
            )
        )


@pytest.mark.anyio
async def test_internal_service_middleware_exempts_only_health() -> None:
    app = FastAPI()
    app.state.internal_service_token = service_token(_settings(internal_secret="internal-secret"))
    app.add_middleware(InternalServiceAuthMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/private")
    async def private() -> dict[str, str]:
        return {"status": "ok"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health/", follow_redirects=False)).status_code == 401
        assert (await client.get("/private")).status_code == 401
        assert (
            await client.get("/private", headers={INTERNAL_TOKEN_HEADER: "wrong"})
        ).status_code == 403
        assert (
            await client.get(
            "/private",
            headers={INTERNAL_TOKEN_HEADER: app.state.internal_service_token},
            )
        ).status_code == 200


def test_internal_auth_is_outermost_middleware_on_service_apps() -> None:
    paths = (
        "src/videoroll/apps/subtitle_service/main.py",
        "src/videoroll/apps/youtube_ingest/main.py",
        "src/videoroll/apps/bilibili_publisher/main.py",
        "src/videoroll/apps/social_publisher/main.py",
    )
    for path in paths:
        source = Path(path).read_text(encoding="utf-8")
        assert source.index("app.add_middleware(\n    CORSMiddleware") < source.index(
            "install_internal_service_auth(app"
        )


@pytest.mark.anyio
async def test_mounted_health_path_is_exempted_after_scope_normalization() -> None:
    child = FastAPI()
    child.state.internal_service_token = service_token(_settings(internal_secret="internal-secret"))
    child.add_middleware(InternalServiceAuthMiddleware)

    @child.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    root = Starlette(routes=[Mount("/internal", app=child)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root),
        base_url="http://test",
    ) as client:
        response = await client.get("/internal/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "module_name",
    [
        "videoroll.apps.subtitle_service.main",
        "videoroll.apps.youtube_ingest.main",
        "videoroll.apps.bilibili_publisher.main",
        "videoroll.apps.social_publisher.main",
    ],
)
async def test_real_internal_apps_allow_mounted_health(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from videoroll import config

    monkeypatch.setenv("DEVELOPMENT_MODE", "true")
    setting_getters = (
        config.get_subtitle_settings,
        config.get_youtube_ingest_settings,
        config.get_bilibili_publisher_settings,
        config.get_social_publisher_settings,
    )
    for getter in setting_getters:
        getter.cache_clear()

    async def async_health(_request: object) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    try:
        app = importlib.import_module(module_name).app
        app.state.internal_service_token = "internal-token"
        health_route = next(route for route in app.routes if getattr(route, "path", None) == "/health")
        original_route_app = health_route.app
        health_route.app = request_response(async_health)
        try:
            root = Starlette(routes=[Mount("/internal", app=app)])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=root),
                base_url="http://test",
            ) as client:
                response = await client.get("/internal/health")
        finally:
            health_route.app = original_route_app
    finally:
        for getter in setting_getters:
            getter.cache_clear()

    assert response.status_code == 200


def test_subtitle_worker_callback_uses_dedicated_service_token() -> None:
    source = Path("src/videoroll/apps/subtitle_service/worker.py").read_text(encoding="utf-8")
    assert "service_token(settings)" in source
    assert "internal_api_token(settings.s3_secret_access_key)" not in source


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
