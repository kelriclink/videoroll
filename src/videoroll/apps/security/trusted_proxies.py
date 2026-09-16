from __future__ import annotations

import ipaddress
import socket
import time
from functools import lru_cache


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
