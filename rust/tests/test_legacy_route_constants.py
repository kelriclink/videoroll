"""Regression: imported FastAPI route constants must not become None paths."""
import importlib.util
from pathlib import Path


def test_remote_auto_youtube_paths_are_resolved():
    root = Path(__file__).resolve().parents[2]
    script = root / "rust/scripts/audit_legacy_routes.py"
    spec = importlib.util.spec_from_file_location("audit_legacy_routes", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    routes = module.route_catalog(root)
    remote = {(route["method"], route["path"]) for route in routes
              if route["handler"] in {"remote_auto_youtube", "remote_auto_youtube_legacy"}}
    assert remote == {("GET", "/remote/auto/youtube"), ("POST", "/remote/auto/youtube")}
    assert all(route["path"] is not None for route in routes)
