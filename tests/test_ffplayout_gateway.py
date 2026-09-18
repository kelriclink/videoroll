from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NGINX_CONF = ROOT / "src" / "web" / "nginx.conf"
FFPLAYOUT_PATHS = ROOT / "services" / "ffplayout" / "backend" / "app" / "src" / "api" / "path.rs"
FFPLAYOUT_SSE_PATHS = ROOT / "services" / "ffplayout" / "backend" / "app" / "src" / "sse" / "routes.rs"


def _ffplayout_api_paths() -> list[str]:
    source = FFPLAYOUT_PATHS.read_text(encoding="utf-8")
    start = source.index('.nest(\n            "/api",')
    end = source.index('.nest("/data"', start)
    api_block = source[start:end]
    paths = [f"/api{path}" for path in re.findall(r'\.route\("([^"]+)"', api_block)]

    sse_source = FFPLAYOUT_SSE_PATHS.read_text(encoding="utf-8")
    api_routes_start = sse_source.index("pub fn api_routes()")
    data_routes_start = sse_source.index("pub fn data_routes()", api_routes_start)
    sse_api_block = sse_source[api_routes_start:data_routes_start]
    paths.extend(f"/api{path}" for path in re.findall(r'\.route\("([^"]+)"', sse_api_block))
    return paths


def _sample_route(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_ffplayout_api_routes_are_covered_by_nginx_compatibility_gateway() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    match = re.search(r"location ~ (\^/api/\(\?:[^\n]+\)) \{", nginx)
    assert match, "ffplayout /api compatibility location is missing from nginx.conf"
    gateway_pattern = re.compile(match.group(1))

    uncovered = [path for path in _ffplayout_api_paths() if not gateway_pattern.match(_sample_route(path))]
    assert not uncovered, f"ffplayout API routes missing from nginx compatibility gateway: {uncovered}"


def test_ffplayout_gateway_does_not_capture_videoroll_api_routes() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    match = re.search(r"location ~ (\^/api/\(\?:[^\n]+\)) \{", nginx)
    assert match
    gateway_pattern = re.compile(match.group(1))

    assert not gateway_pattern.match("/api/ws/events")
    assert not gateway_pattern.match("/api/system/resources")
    assert not gateway_pattern.match("/api/auth/status")


def test_ffplayout_is_mounted_on_same_origin_path() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    assert "location = /playout" in nginx
    assert "return 308 $videoroll_forwarded_proto://$http_host/playout/;" in nginx
    assert "location /playout/" in nginx
    assert "resolver 127.0.0.11" in nginx
    assert "set $ffplayout_upstream http://ffplayout:8787;" in nginx
    assert "rewrite ^/playout/(.*)$ /$1 break;" in nginx
    assert "proxy_pass $ffplayout_upstream;" in nginx
    assert "listen 81" not in nginx
