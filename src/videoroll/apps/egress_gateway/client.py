from __future__ import annotations

import base64
import ipaddress
import json
import queue
import socket
import time
from dataclasses import dataclass
from threading import BoundedSemaphore, Thread
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER


_ALLOWED_SCHEMES = {"http": 80, "https": 443}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_REDIRECTS = 5
_RESOLVER_SLOTS = BoundedSemaphore(16)
_DEFAULT_GATEWAY_HOSTNAMES = frozenset({"egress-gateway"})
_DEFAULT_HEADERS = {
    "accept": "application/json, text/html;q=0.9, text/plain;q=0.8, */*;q=0.1",
    "user-agent": "VideoRoll-Egress-Gateway/1.0",
}
_FORBIDDEN_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "proxy-authorization",
    "proxy-connection",
}


class EgressDenied(RuntimeError):
    pass


class EgressGatewayError(RuntimeError):
    pass


class EgressTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class ResolvedEndpoint:
    scheme: str
    hostname: str
    port: int
    verified_ip: str
    sni_name: str


@dataclass(frozen=True)
class EgressResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str
    truncated: bool

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            name, separator, value = part.strip().partition("=")
            if separator and name.lower() == "charset" and value.strip():
                charset = value.strip().strip('"')
                break
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"egress request returned HTTP {self.status_code}")


class EgressGatewayClient:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        *,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(str(gateway_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise EgressGatewayError("egress gateway URL must be HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise EgressGatewayError("egress gateway URL is invalid")
        hostname = str(parsed.hostname).strip().rstrip(".").lower()
        if hostname not in _DEFAULT_GATEWAY_HOSTNAMES:
            raise EgressGatewayError("egress gateway URL must use an explicitly allowed hostname")
        clean_token = str(token or "").strip()
        if not clean_token:
            raise EgressGatewayError("egress gateway service token is unavailable")
        base_path = parsed.path.rstrip("/")
        self.fetch_url = parsed._replace(path=f"{base_path}/fetch", query="", fragment="").geturl()
        self.client = httpx.Client(
            timeout=httpx.Timeout(max(1.0, min(65.0, float(timeout) + 5.0))),
            follow_redirects=False,
            trust_env=False,
            headers={INTERNAL_TOKEN_HEADER: clean_token},
            transport=transport,
        )

    def __enter__(self) -> EgressGatewayClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.client.close()

    def fetch(
        self,
        url: str,
        *,
        timeout: float,
        max_bytes: int,
        redirects: int,
    ) -> EgressResponse:
        try:
            response = self.client.post(
                self.fetch_url,
                json={
                    "url": str(url or "").strip(),
                    "timeout": float(timeout),
                    "max_bytes": int(max_bytes),
                    "redirects": int(redirects),
                },
            )
        except httpx.HTTPError as exc:
            raise EgressGatewayError(f"egress gateway request failed: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise EgressGatewayError("egress gateway returned invalid JSON") from exc
        if response.status_code >= 400:
            detail = (
                str(payload.get("detail") or "egress gateway rejected the request")
                if isinstance(payload, dict)
                else "egress gateway rejected the request"
            )
            if response.status_code in {401, 403}:
                raise EgressDenied(detail[:300])
            raise EgressGatewayError(detail[:300])
        if not isinstance(payload, dict):
            raise EgressGatewayError("egress gateway response must be an object")
        try:
            content = base64.b64decode(str(payload.get("body_base64") or ""), validate=True)
            status_code = int(payload["status_code"])
            response_url = str(payload["url"])
            response_headers = payload["headers"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EgressGatewayError("egress gateway response is malformed") from exc
        if len(content) > max(1, int(max_bytes)):
            raise EgressGatewayError("egress gateway response exceeded the requested byte limit")
        if not isinstance(response_headers, dict):
            raise EgressGatewayError("egress gateway response headers are malformed")
        return EgressResponse(
            status_code=status_code,
            headers={str(name).lower(): str(value) for name, value in response_headers.items()},
            content=content,
            url=response_url,
            truncated=bool(payload.get("truncated")),
        )


def _normalized_hostname(raw_hostname: str) -> str:
    hostname = str(raw_hostname or "").strip().rstrip(".")
    if not hostname or "%" in hostname:
        raise EgressDenied("egress URL has an invalid hostname")
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise EgressDenied("egress URL hostname is not valid IDNA") from exc


def _is_global_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _getaddrinfo_with_deadline(hostname: str, port: int, deadline: float | None) -> list[tuple[Any, ...]]:
    if deadline is None:
        return socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )

    if not _RESOLVER_SLOTS.acquire(timeout=_remaining_time(deadline)):
        raise EgressTimeout("egress total deadline exceeded during DNS resolution")
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            value = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            results.put((True, value))
        except Exception as exc:
            results.put((False, exc))
        finally:
            _RESOLVER_SLOTS.release()

    Thread(target=resolve, name="videoroll-egress-dns", daemon=True).start()
    try:
        ok, value = results.get(timeout=_remaining_time(deadline))
    except queue.Empty as exc:
        raise EgressTimeout("egress total deadline exceeded during DNS resolution") from exc
    if not ok:
        raise value
    return value


def resolve_public_endpoint(url: str, *, deadline: float | None = None) -> ResolvedEndpoint:
    raw_url = str(url or "").strip()
    try:
        parsed = urlsplit(raw_url)
        explicit_port = parsed.port
    except ValueError as exc:
        raise EgressDenied("egress URL has an invalid port") from exc

    scheme = parsed.scheme.lower()
    expected_port = _ALLOWED_SCHEMES.get(scheme)
    if expected_port is None:
        raise EgressDenied("egress URL scheme must be HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise EgressDenied("egress URL userinfo is not allowed")
    if not parsed.hostname:
        raise EgressDenied("egress URL hostname is required")

    hostname = _normalized_hostname(parsed.hostname)
    port = explicit_port or expected_port
    if port != expected_port:
        raise EgressDenied(f"egress URL port {port} is not allowed for {scheme}")

    try:
        answers = _getaddrinfo_with_deadline(hostname, port, deadline)
    except EgressTimeout:
        raise
    except (OSError, socket.gaierror) as exc:
        raise EgressDenied(f"DNS resolution failed for {hostname}") from exc
    if not answers:
        raise EgressDenied(f"DNS resolution returned no addresses for {hostname}")

    addresses: list[str] = []
    for answer in answers:
        try:
            address = str(answer[4][0]).split("%", 1)[0]
            normalized = str(ipaddress.ip_address(address))
        except (IndexError, TypeError, ValueError) as exc:
            raise EgressDenied(f"DNS resolution returned an invalid address for {hostname}") from exc
        if normalized not in addresses:
            addresses.append(normalized)

    denied = [address for address in addresses if not _is_global_address(address)]
    if denied:
        raise EgressDenied(f"DNS resolution returned a non-global address for {hostname}")

    return ResolvedEndpoint(
        scheme=scheme,
        hostname=hostname,
        port=port,
        verified_ip=addresses[0],
        sni_name=hostname,
    )


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EgressTimeout("egress total deadline exceeded")
    return remaining


def _operation_timeout(timeout: float | None, deadline: float) -> float:
    remaining = _remaining_time(deadline)
    return remaining if timeout is None else min(float(timeout), remaining)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk = self.stream.read(max_bytes, timeout=_operation_timeout(timeout, self.deadline))
        _remaining_time(self.deadline)
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.stream.write(buffer, timeout=_operation_timeout(timeout, self.deadline))
        _remaining_time(self.deadline)

    def close(self) -> None:
        self.stream.close()

    def start_tls(
        self,
        ssl_context,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self.stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=_operation_timeout(timeout, self.deadline),
        )
        try:
            _remaining_time(self.deadline)
        except EgressTimeout:
            stream.close()
            raise
        return _DeadlineStream(stream, self.deadline)

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


class _FixedEndpointBackend(httpcore.NetworkBackend):
    def __init__(
        self,
        endpoint: ResolvedEndpoint,
        delegate: httpcore.NetworkBackend,
        deadline: float,
    ) -> None:
        self.endpoint = endpoint
        self.delegate = delegate
        self.deadline = deadline

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        stream = self.delegate.connect_tcp(
            host=self.endpoint.verified_ip,
            port=self.endpoint.port,
            timeout=_operation_timeout(timeout, self.deadline),
            local_address=local_address,
            socket_options=socket_options,
        )
        try:
            _remaining_time(self.deadline)
        except EgressTimeout:
            stream.close()
            raise
        peer = stream.get_extra_info("server_addr")
        try:
            peer_ip = str(ipaddress.ip_address(str(peer[0]).split("%", 1)[0]))
        except (IndexError, TypeError, ValueError) as exc:
            stream.close()
            raise EgressDenied("egress socket peer could not be verified") from exc
        if peer_ip != self.endpoint.verified_ip:
            stream.close()
            raise EgressDenied("egress socket peer does not match the verified IP")
        return _DeadlineStream(stream, self.deadline)

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise EgressDenied("egress gateway does not allow unix socket destinations")

    def sleep(self, seconds: float) -> None:
        self.delegate.sleep(min(float(seconds), _remaining_time(self.deadline)))
        _remaining_time(self.deadline)


def _network_backend_factory(endpoint: ResolvedEndpoint) -> httpcore.NetworkBackend:
    return httpcore.SyncBackend()


def _request_headers(headers: Mapping[str, str] | None) -> list[tuple[bytes, bytes]]:
    values = dict(_DEFAULT_HEADERS)
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name or "").strip().lower()
        value = str(raw_value or "").strip()
        if not name or not value or name in _FORBIDDEN_REQUEST_HEADERS:
            continue
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise EgressDenied("egress request contains an invalid header")
        values[name] = value[:1024]
    return [(name.encode("ascii"), value.encode("latin-1")) for name, value in values.items()]


def _response_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("ascii", errors="ignore").lower()
        value = raw_value.decode("latin-1", errors="replace")
        if name in headers:
            headers[name] = f"{headers[name]}, {value}"
        else:
            headers[name] = value
    return headers


def _content_type_allowed(headers: Mapping[str, str]) -> bool:
    content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not content_type:
        return False
    return bool(
        content_type.startswith("text/")
        or content_type == "application/json"
        or content_type.endswith("+json")
        or content_type in {"application/xml", "application/xhtml+xml"}
        or content_type.endswith("+xml")
    )


def _bounded_body(response: httpcore.Response, max_bytes: int) -> tuple[bytes, bool]:
    body = bytearray()
    truncated = False
    for chunk in response.iter_stream():
        remaining = max_bytes - len(body)
        if len(chunk) > remaining:
            body.extend(chunk[:remaining])
            truncated = True
            break
        body.extend(chunk)
        if len(body) == max_bytes:
            continue
    return bytes(body), truncated


def fetch_public(
    url: str,
    timeout: float = 20.0,
    max_bytes: int = 500_000,
    redirects: int = 5,
    *,
    headers: Mapping[str, str] | None = None,
) -> EgressResponse:
    request_timeout = max(0.1, min(_MAX_TIMEOUT_SECONDS, float(timeout)))
    response_limit = max(1, min(_MAX_RESPONSE_BYTES, int(max_bytes)))
    redirect_limit = max(0, min(_MAX_REDIRECTS, int(redirects)))
    current_url = str(url or "").strip()
    deadline = time.monotonic() + request_timeout

    for redirect_number in range(redirect_limit + 1):
        _remaining_time(deadline)
        endpoint = resolve_public_endpoint(current_url, deadline=deadline)
        remaining = _remaining_time(deadline)
        delegate = _network_backend_factory(endpoint)
        backend = _FixedEndpointBackend(endpoint, delegate, deadline)
        timeout_extensions = {
            "timeout": {
                "connect": remaining,
                "read": remaining,
                "write": remaining,
                "pool": remaining,
            },
            "sni_hostname": endpoint.sni_name,
        }
        try:
            with httpcore.ConnectionPool(
                network_backend=backend,
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
            ) as pool:
                with pool.stream(
                    "GET",
                    current_url,
                    headers=_request_headers(headers),
                    extensions=timeout_extensions,
                ) as response:
                    response_headers = _response_headers(response.headers)
                    status_code = int(response.status)
                    if status_code in _REDIRECT_STATUSES:
                        location = str(response_headers.get("location") or "").strip()
                        if not location:
                            return EgressResponse(
                                status_code=status_code,
                                headers=response_headers,
                                content=b"",
                                url=current_url,
                                truncated=False,
                            )
                        if redirect_number >= redirect_limit:
                            raise EgressDenied("egress redirect limit exceeded")
                        current_url = urljoin(current_url, location)
                        continue
                    if not _content_type_allowed(response_headers):
                        raise EgressDenied("egress response content type is not allowed")
                    content, truncated = _bounded_body(response, response_limit)
                    return EgressResponse(
                        status_code=status_code,
                        headers=response_headers,
                        content=content,
                        url=current_url,
                        truncated=truncated,
                    )
        except (EgressDenied, EgressTimeout):
            raise
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise RuntimeError(f"egress request failed: {type(exc).__name__}") from exc

    raise EgressDenied("egress redirect limit exceeded")
