from __future__ import annotations

from pathlib import Path
import unittest

from videoroll.apps.orchestrator_api.main import app


DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

EXPECTED_ORCHESTRATOR_ROUTES: set[tuple[str, str]] = {
    ("DELETE", "/settings/publish/social/accounts/{account_id}"),
    ("DELETE", "/settings/publish/social/login-sessions/{session_id}"),
    ("DELETE", "/tasks/{task_id}/assets/{asset_id}"),
    ("GET", "/auth/status"),
    ("GET", "/health"),
    ("GET", "/maintenance/workdir"),
    ("GET", "/remote/auto/youtube"),
    ("GET", "/settings/api"),
    ("GET", "/settings/publish/platforms"),
    ("GET", "/settings/publish/social/accounts"),
    ("GET", "/settings/publish/social/login-sessions/{session_id}"),
    ("GET", "/settings/review"),
    ("GET", "/settings/storage"),
    ("GET", "/settings/youtube"),
    ("GET", "/system/resources"),
    ("GET", "/tasks"),
    ("GET", "/tasks/{task_id}"),
    ("GET", "/tasks/{task_id}/assets"),
    ("GET", "/tasks/{task_id}/assets/{asset_id}/download"),
    ("GET", "/tasks/{task_id}/assets/{asset_id}/stream"),
    ("GET", "/tasks/{task_id}/publish_jobs"),
    ("GET", "/tasks/{task_id}/publish_meta"),
    ("GET", "/tasks/{task_id}/publish_meta/draft"),
    ("GET", "/tasks/{task_id}/publish_review"),
    ("GET", "/tasks/{task_id}/subtitle_jobs"),
    ("GET", "/tasks/{task_id}/youtube_meta"),
    ("GET", "/videos/converted"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/setup"),
    ("POST", "/auto/youtube"),
    ("POST", "/maintenance/workdir/cleanup"),
    ("POST", "/remote/auto/youtube"),
    ("POST", "/settings/publish/social/accounts/{account_id}/check"),
    ("POST", "/settings/publish/social/accounts/{platform}"),
    ("POST", "/settings/publish/social/login-sessions/{platform}"),
    ("POST", "/settings/youtube/home_scan/run"),
    ("POST", "/settings/youtube/test"),
    ("POST", "/tasks"),
    ("POST", "/tasks/actions/resume_failed_recent"),
    ("POST", "/tasks/{task_id}/actions/auto_youtube_start"),
    ("POST", "/tasks/{task_id}/actions/publish"),
    ("POST", "/tasks/{task_id}/actions/publish_review"),
    ("POST", "/tasks/{task_id}/actions/subtitle"),
    ("POST", "/tasks/{task_id}/actions/subtitle_resume"),
    ("POST", "/tasks/{task_id}/actions/youtube_download"),
    ("POST", "/tasks/{task_id}/actions/youtube_meta"),
    ("POST", "/tasks/{task_id}/publish_meta/draft"),
    ("POST", "/tasks/{task_id}/upload/cover"),
    ("POST", "/tasks/{task_id}/upload/video"),
    ("PUT", "/settings/api"),
    ("PUT", "/settings/publish/platforms/{platform}"),
    ("PUT", "/settings/review"),
    ("PUT", "/settings/storage"),
    ("PUT", "/settings/youtube"),
    ("PUT", "/tasks/{task_id}/publish_meta"),
}


def route_manifest(application) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in application.routes
        if route.path not in DOC_PATHS
        for method in sorted(getattr(route, "methods", set()) or set())
        if method not in {"HEAD", "OPTIONS"}
    }


class OrchestratorArchitectureTests(unittest.TestCase):
    def test_route_manifest_has_no_duplicate_method_path_pairs(self) -> None:
        pairs = [
            (method, route.path)
            for route in app.routes
            if route.path not in DOC_PATHS
            for method in sorted(getattr(route, "methods", set()) or set())
            if method not in {"HEAD", "OPTIONS"}
        ]

        self.assertEqual(len(pairs), len(set(pairs)))

    def test_route_manifest_is_preserved(self) -> None:
        self.assertEqual(route_manifest(app), EXPECTED_ORCHESTRATOR_ROUTES)

    def test_main_contains_no_route_decorators(self) -> None:
        source = Path("src/videoroll/apps/orchestrator_api/main.py").read_text(encoding="utf-8")

        for decorator in ("@app.get", "@app.post", "@app.put", "@app.patch", "@app.delete"):
            self.assertNotIn(decorator, source)

    def test_router_and_service_import_boundaries(self) -> None:
        root = Path("src/videoroll/apps/orchestrator_api")
        for path in (root / "routers").glob("*.py"):
            self.assertNotIn("orchestrator_api.main", path.read_text(encoding="utf-8"), path.as_posix())
        for path in (root / "services").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("orchestrator_api.main", source, path.as_posix())
            self.assertNotIn("orchestrator_api.routers", source, path.as_posix())

    def test_application_factory_creates_distinct_apps_without_routes_missing(self) -> None:
        from videoroll.apps.orchestrator_api.app import create_app

        first = create_app(install_lifecycle=False)
        second = create_app(install_lifecycle=False)

        self.assertIsNot(first, second)
        self.assertEqual(route_manifest(first), EXPECTED_ORCHESTRATOR_ROUTES)
        self.assertEqual(route_manifest(second), EXPECTED_ORCHESTRATOR_ROUTES)


if __name__ == "__main__":
    unittest.main()
