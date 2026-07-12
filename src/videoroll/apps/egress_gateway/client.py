from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpcore


_ALLOWED_SCHEMES = {"http": 80, "https": 443}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_REDIRECTS = 5
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


def resolve_public_endpoint(url: str) -> ResolvedEndpoint:
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
        answers = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
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


class _FixedEndpointBackend(httpcore.NetworkBackend):
    def __init__(self, endpoint: ResolvedEndpoint, delegate: httpcore.NetworkBackend) -> None:
        self.endpoint = endpoint
        self.delegate = delegate

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
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        peer = stream.get_extra_info("server_addr")
        try:
            peer_ip = str(ipaddress.ip_address(str(peer[0]).split("%", 1)[0]))
        except (IndexError, TypeError, ValueError) as exc:
            stream.close()
            raise EgressDenied("egress socket peer could not be verified") from exc
        if peer_ip != self.endpoint.verified_ip:
            stream.close()
            raise EgressDenied("egress socket peer does not match the verified IP")
        return stream

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise EgressDenied("egress gateway does not allow unix socket destinations")

    def sleep(self, seconds: float) -> None:
        self.delegate.sleep(seconds)


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
        return True
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

    for redirect_number in range(redirect_limit + 1):
        endpoint = resolve_public_endpoint(current_url)
        delegate = _network_backend_factory(endpoint)
        backend = _FixedEndpointBackend(endpoint, delegate)
        timeout_extensions = {
            "timeout": {
                "connect": request_timeout,
                "read": request_timeout,
                "write": request_timeout,
                "pool": request_timeout,
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
        except EgressDenied:
            raise
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise RuntimeError(f"egress request failed: {type(exc).__name__}") from exc

    raise EgressDenied("egress redirect limit exceeded")
