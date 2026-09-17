from __future__ import annotations

import ipaddress
import socket
import time
from functools import lru_cache

from starlette.requests import Request


ProxyAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_DNS_CACHE_SECONDS = 60


@lru_cache(maxsize=128)
def _resolve_proxy_host(hostname: str, time_window: int) -> frozenset[ProxyAddress]:
    # Include the window in the cache key so container replacement or a failed
    # lookup cannot leave a stale trust decision for the process lifetime.
    del time_window
    try:
        answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return frozenset()
    addresses: set[ProxyAddress] = set()
    for answer in answers[:64]:
        try:
            address = ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError, ValueError):
            continue
        if not address.is_unspecified and not address.is_multicast:
            addresses.add(address)
    return frozenset(addresses)


def trusted_proxy_addresses(configured_hosts: str) -> frozenset[ProxyAddress]:
    """Resolve explicitly configured proxy names, never names from a request."""
    time_window = int(time.monotonic() // _DNS_CACHE_SECONDS)
    addresses: set[ProxyAddress] = set()
    for raw_host in str(configured_hosts or "").split(",")[:32]:
        hostname = raw_host.strip().lower().rstrip(".")
        if not hostname or len(hostname) > 253:
            continue
        if any(not (char.isalnum() or char in "-_.") for char in hostname):
            continue
        addresses.update(_resolve_proxy_host(hostname, time_window))
    return frozenset(addresses)


def request_source_ip(request: Request) -> str:
    """Return the client IP after removing only explicitly trusted proxies."""
    peer_text = str(getattr(request.client, "host", "") or "").strip()
    try:
        peer = ipaddress.ip_address(peer_text)
    except ValueError:
        return "unknown"

    app_state = getattr(getattr(request, "app", None), "state", None)
    raw_cidrs = str(getattr(app_state, "trusted_proxy_cidrs", "") or "")
    proxy_addresses = trusted_proxy_addresses(str(getattr(app_state, "trusted_proxy_hosts", "") or ""))
    trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_cidr in raw_cidrs.split(",")[:32]:
        cidr = raw_cidr.strip()
        if not cidr:
            continue
        try:
            trusted_networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue

    def is_trusted(address: ProxyAddress) -> bool:
        return address in proxy_addresses or any(
            address.version == network.version and address in network for network in trusted_networks
        )

    if not is_trusted(peer):
        return peer.compressed
    forwarded_values = [part.strip() for part in str(request.headers.get("x-forwarded-for") or "").split(",")]
    if not forwarded_values or not all(forwarded_values):
        return peer.compressed
    try:
        forwarded = [ipaddress.ip_address(value) for value in forwarded_values]
    except ValueError:
        return peer.compressed
    candidate = peer
    for hop in reversed(forwarded):
        if not is_trusted(candidate):
            break
        candidate = hop
    return candidate.compressed
