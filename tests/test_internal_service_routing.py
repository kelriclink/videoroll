from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from videoroll.apps.orchestrator_api.infrastructure import internal_http
from videoroll.apps.orchestrator_api.services import subtitle_service
from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER, service_token


WEB_ROOT = Path("src/web/src")
DIRECT_BROWSER_SERVICE_URLS = (
    "SUBTITLE_SERVICE_URL",
    "YOUTUBE_INGEST_URL",
    "BILIBILI_PUBLISHER_URL",
    "SOCIAL_PUBLISHER_URL",
)
DIRECT_BROWSER_SERVICE_ENV_VARS = (
    "VITE_SUBTITLE_SERVICE_URL",
    "VITE_YOUTUBE_INGEST_URL",
    "VITE_BILIBILI_PUBLISHER_URL",
    "VITE_SOCIAL_PUBLISHER_URL",
)


def test_frontend_uses_only_the_orchestrator_api_base() -> None:
    sources = {
        path: path.read_text(encoding="utf-8")
        for pattern in ("*.ts", "*.tsx")
        for path in WEB_ROOT.rglob(pattern)
    }

    for forbidden in (*DIRECT_BROWSER_SERVICE_URLS, *DIRECT_BROWSER_SERVICE_ENV_VARS):
        offenders = [str(path) for path, source in sources.items() if forbidden in source]
        assert not offenders, f"{forbidden} remains in browser source: {offenders}"

    urls_source = (WEB_ROOT / "lib" / "urls.ts").read_text(encoding="utf-8")
    assert "export const ORCHESTRATOR_URL" in urls_source
    assert urls_source.count("export const ") == 1


def test_frontend_image_build_has_no_child_service_url_arguments() -> None:
    for path in (Path("src/web/Dockerfile"), Path("compose.yml"), Path("docker-compose.yml"), Path("scripts/build_export_prod.sh")):
        source = path.read_text(encoding="utf-8")
        for forbidden in DIRECT_BROWSER_SERVICE_ENV_VARS:
            assert forbidden not in source, f"{forbidden} remains in {path}"


def test_orchestrator_compose_settings_target_internal_service_dns() -> None:
    for path in (Path("compose.yml"), Path("docker-compose.yml")):
        source = path.read_text(encoding="utf-8")
        assert "http://localhost:8000/subtitle-service" not in source
        assert "http://localhost:8000/youtube-ingest" not in source
        assert "http://localhost:8000/bilibili-publisher" not in source


def test_monolith_does_not_mount_internal_child_apps() -> None:
    source = Path("src/videoroll/apps/monolith/main.py").read_text(encoding="utf-8")

    assert ".mount(" not in source
    assert "subtitle_service.main" not in source
    assert "youtube_ingest.main" not in source
    assert "bilibili_publisher.main" not in source


class _RecordingAsyncClient:
    def __init__(self, **kwargs) -> None:
        self.headers = dict(kwargs["headers"])
        self.requests: list[tuple[str, str, bytes]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def request(self, method: str, url: str, *, content: bytes) -> httpx.Response:
        self.requests.append((method, url, content))
        return httpx.Response(201, content=b'{"ok":true}', headers={"content-type": "application/json"})


@pytest.mark.anyio
async def test_subtitle_browser_proxy_uses_server_derived_service_token(monkeypatch) -> None:
    clients: list[_RecordingAsyncClient] = []

    def create_client(**kwargs) -> _RecordingAsyncClient:
        client = _RecordingAsyncClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(internal_http.httpx, "AsyncClient", create_client)
    settings = SimpleNamespace(
        subtitle_service_url="http://subtitle-service:8001",
        internal_api_secret="internal-secret",
        development_mode=False,
    )

    response = await subtitle_service.proxy_browser_request(
        settings,
        service_path="subtitle/models/download",
        method="POST",
        query_string="name=large-v3",
        body=b'{"proxy":"http://proxy"}',
        content_type="application/json",
    )

    assert response.status_code == 201
    assert response.content == b'{"ok":true}'
    assert response.headers["content-type"] == "application/json"
    assert len(clients) == 1
    client = clients[0]
    assert client.headers[INTERNAL_TOKEN_HEADER] == service_token(settings)
    assert client.headers["content-type"] == "application/json"
    assert client.requests == [
        ("POST", "http://subtitle-service:8001/subtitle/models/download?name=large-v3", b'{"proxy":"http://proxy"}')
    ]


@pytest.mark.parametrize(
    ("method", "service_path"),
    [
        ("GET", "subtitle/agents/runs/5d7db7cf-1a7d-4d77-9335-cf39bbbcb9a4"),
        ("DELETE", "subtitle/models/model-name"),
        ("PUT", "subtitle/dictionaries/sources/source-id"),
        ("DELETE", "subtitle/dictionaries/entries/entry-id"),
        ("DELETE", "subtitle/knowledge/items/item-id"),
    ],
)
def test_subtitle_proxy_allows_each_dynamic_browser_operation(method: str, service_path: str) -> None:
    assert subtitle_service._is_browser_proxy_path_allowed(method, service_path)


def test_subtitle_proxy_rejects_invalid_agent_run_id() -> None:
    assert not subtitle_service._is_browser_proxy_path_allowed("GET", "subtitle/agents/runs/not-a-uuid")
