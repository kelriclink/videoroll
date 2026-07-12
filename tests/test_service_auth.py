from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
