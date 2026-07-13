from __future__ import annotations

import socket
import time
from threading import Event
from dataclasses import dataclass, field

import httpx
import pytest


PUBLIC_IP = "93.184.216.34"


def _addrinfo(ip: str, port: int) -> tuple[object, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)


@dataclass
class _FakeStream:
    response: bytes
    peer_ip: str = PUBLIC_IP
    writes: list[bytes] = field(default_factory=list)
    server_hostname: str | None = None
    max_chunk_bytes: int | None = None
    on_read: object | None = None
    _offset: int = 0

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if callable(self.on_read):
            self.on_read()
        chunk_limit = min(max_bytes, self.max_chunk_bytes or max_bytes)
        chunk = self.response[self._offset : self._offset + chunk_limit]
        self._offset += len(chunk)
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    def close(self) -> None:
        return None

    def start_tls(self, ssl_context, server_hostname: str | None = None, timeout: float | None = None):
        self.server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str):
        if info == "server_addr":
            return (self.peer_ip, 443)
        if info == "ssl_object":
            return None
        if info == "is_readable":
            return True
        return None


@dataclass
class _FakeBackend:
    stream: _FakeStream
    connections: list[tuple[str, int]] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> _FakeStream:
        self.connections.append((host, port))
        self.timeouts.append(timeout)
        return self.stream

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise AssertionError("egress must not use unix sockets")

    def sleep(self, seconds: float) -> None:
        return None


def _http_response(
    body: bytes = b"ok",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content_type: str | None = "text/plain; charset=utf-8",
) -> bytes:
    reason = "OK" if status == 200 else "Found"
    response_headers = {
        "Content-Length": str(len(body)),
        "Connection": "close",
        **(headers or {}),
    }
    if content_type is not None:
        response_headers["Content-Type"] = content_type
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{name}: {value}" for name, value in response_headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def test_dns_resolution_failure_is_denied(monkeypatch) -> None:
    from videoroll.apps.egress_gateway.client import EgressDenied, resolve_public_endpoint

    def fail_dns(*args: object, **kwargs: object) -> list[object]:
        raise socket.gaierror("resolver unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)

    with pytest.raises(EgressDenied, match="DNS"):
        resolve_public_endpoint("https://example.test/page")


def test_dns_resolution_obeys_total_deadline(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    release = Event()

    def blocked_dns(*args: object, **kwargs: object) -> list[object]:
        release.wait(1.0)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    started = time.perf_counter()
    try:
        with pytest.raises(client_module.EgressTimeout, match="deadline"):
            client_module.fetch_public("https://blocked-dns.test/page", timeout=0.05)
    finally:
        release.set()

    assert time.perf_counter() - started < 0.2


def test_private_dns_answer_is_denied(monkeypatch) -> None:
    from videoroll.apps.egress_gateway.client import EgressDenied, resolve_public_endpoint

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo("10.0.0.7", port)],
    )

    with pytest.raises(EgressDenied, match="non-global"):
        resolve_public_endpoint("https://private.test/page")


def test_mixed_public_and_private_dns_answers_are_denied(monkeypatch) -> None:
    from videoroll.apps.egress_gateway.client import EgressDenied, resolve_public_endpoint

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            _addrinfo(PUBLIC_IP, port),
            _addrinfo("127.0.0.1", port),
        ],
    )

    with pytest.raises(EgressDenied, match="non-global"):
        resolve_public_endpoint("https://mixed.test/page")


def test_fixed_endpoint_preserves_host_and_tls_sni(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo(PUBLIC_IP, port)],
    )
    stream = _FakeStream(_http_response())
    backend = _FakeBackend(stream)
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: backend)

    response = client_module.fetch_public("https://public.test/page")

    assert response.content == b"ok"
    assert backend.connections == [(PUBLIC_IP, 443)]
    assert stream.server_hostname == "public.test"
    request = b"".join(stream.writes)
    assert b"GET /page HTTP/1.1\r\n" in request
    assert b"Host: public.test\r\n" in request


def test_socket_peer_must_match_verified_ip(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo(PUBLIC_IP, port)],
    )
    backend = _FakeBackend(_FakeStream(_http_response(), peer_ip="93.184.216.35"))
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: backend)

    with pytest.raises(client_module.EgressDenied, match="peer"):
        client_module.fetch_public("https://public.test/page")


def test_private_redirect_is_denied(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    def resolve(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        ip = PUBLIC_IP if host == "public.test" else "10.0.0.8"
        return [_addrinfo(ip, port)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    redirect = _FakeBackend(
        _FakeStream(
            _http_response(
                b"",
                status=302,
                headers={"Location": "http://private.test/admin"},
            )
        )
    )
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: redirect)

    with pytest.raises(client_module.EgressDenied, match="non-global"):
        client_module.fetch_public("https://public.test/start")

    assert redirect.connections == [(PUBLIC_IP, 443)]


def test_response_body_is_bounded(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo(PUBLIC_IP, port)],
    )
    backend = _FakeBackend(_FakeStream(_http_response(b"x" * 4096)))
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: backend)

    response = client_module.fetch_public("http://public.test/large", max_bytes=1024)

    assert response.content == b"x" * 1024
    assert response.truncated is True


def test_missing_content_type_is_denied(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo(PUBLIC_IP, port)],
    )
    backend = _FakeBackend(_FakeStream(_http_response(content_type=None)))
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: backend)

    with pytest.raises(client_module.EgressDenied, match="content type"):
        client_module.fetch_public("https://public.test/no-content-type")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_redirects_share_one_total_deadline(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo(PUBLIC_IP, port)],
    )
    first = _FakeBackend(
        _FakeStream(
            _http_response(b"", status=302, headers={"Location": "https://public.test/final"}),
            on_read=lambda: clock.advance(0.3),
        )
    )
    second = _FakeBackend(
        _FakeStream(
            _http_response(b"done"),
            on_read=lambda: clock.advance(0.3),
        )
    )
    backends = iter([first, second])
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: next(backends))

    with pytest.raises(client_module.EgressTimeout, match="deadline"):
        client_module.fetch_public("https://public.test/start", timeout=0.5)


def test_slow_trickle_cannot_extend_total_deadline(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import client as client_module

    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_addrinfo(PUBLIC_IP, port)],
    )
    stream = _FakeStream(
        _http_response(b"x" * 256),
        max_chunk_bytes=32,
        on_read=lambda: clock.advance(0.2),
    )
    backend = _FakeBackend(stream)
    monkeypatch.setattr(client_module, "_network_backend_factory", lambda endpoint: backend)

    with pytest.raises(client_module.EgressTimeout, match="deadline"):
        client_module.fetch_public("https://public.test/trickle", timeout=0.5)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://public.test/file",
        "https://user:secret@public.test/page",
        "https://public.test:8443/page",
    ],
)
def test_unsafe_url_forms_are_denied(url: str) -> None:
    from videoroll.apps.egress_gateway.client import EgressDenied, resolve_public_endpoint

    with pytest.raises(EgressDenied):
        resolve_public_endpoint(url)


@pytest.mark.anyio
async def test_gateway_endpoint_requires_internal_service_token(monkeypatch) -> None:
    from videoroll.apps.egress_gateway import main as gateway_main
    from videoroll.apps.egress_gateway.client import EgressResponse
    from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER

    gateway_main.app.state.internal_service_token = "service-token"
    async def fake_fetch_public(payload: object) -> EgressResponse:
        return EgressResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            content=b"ok",
            url="https://public.test/page",
            truncated=False,
        )

    monkeypatch.setattr(gateway_main, "fetch_public_async", fake_fetch_public)
    transport = httpx.ASGITransport(app=gateway_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        assert (await client.post("/fetch", json={"url": "https://public.test/page"})).status_code == 401
        assert (
            await client.post(
                "/fetch",
                json={"url": "https://public.test/page"},
                headers={INTERNAL_TOKEN_HEADER: "wrong"},
            )
        ).status_code == 403
        response = await client.post(
            "/fetch",
            json={"url": "https://public.test/page"},
            headers={INTERNAL_TOKEN_HEADER: "service-token"},
        )
    assert response.status_code == 200
    assert response.json()["body_base64"] == "b2s="


def test_gateway_token_is_derived_from_internal_api_secret() -> None:
    from videoroll.apps.egress_gateway import main as gateway_main
    from videoroll.apps.security.service_auth import service_token

    settings = gateway_main.EgressGatewaySettings(INTERNAL_API_SECRET="internal-secret")

    assert gateway_main.internal_service_token(settings) == service_token(settings)


def test_gateway_rejects_known_default_secret_outside_development() -> None:
    from videoroll.apps.egress_gateway import main as gateway_main

    settings = gateway_main.EgressGatewaySettings(
        INTERNAL_API_SECRET="videoroll-development-internal-secret",
        DEVELOPMENT_MODE=False,
    )

    with pytest.raises(ValueError, match="non-default"):
        gateway_main.internal_service_token(settings)


def test_internal_gateway_client_rejects_public_gateway_hostname() -> None:
    from videoroll.apps.egress_gateway.client import EgressGatewayClient, EgressGatewayError

    with pytest.raises(EgressGatewayError, match="allowed hostname"):
        EgressGatewayClient("https://gateway.example.com", "service-token")


def test_internal_gateway_client_rejects_unexpected_dotless_hostname() -> None:
    from videoroll.apps.egress_gateway.client import EgressGatewayClient, EgressGatewayError

    with pytest.raises(EgressGatewayError, match="allowed hostname"):
        EgressGatewayClient("http://attacker:8020", "service-token")


@pytest.mark.parametrize(
    "gateway_url",
    [
        "http://10.0.0.8:8020",
        "http://127.0.0.1:8020",
        "http://localhost:8020",
    ],
)
def test_internal_gateway_client_rejects_hosts_outside_exact_allowlist(gateway_url: str) -> None:
    from videoroll.apps.egress_gateway.client import EgressGatewayClient, EgressGatewayError

    with pytest.raises(EgressGatewayError, match="allowed hostname"):
        EgressGatewayClient(gateway_url, "service-token")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
