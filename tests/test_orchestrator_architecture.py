from __future__ import annotations

from pathlib import Path
import unittest

from videoroll.apps.orchestrator_api.main import app


DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def application_routes(application):
    """Flatten FastAPI 0.139+ included routers while supporting older releases."""
    pending = list(application.routes)
    while pending:
        route = pending.pop(0)
        if hasattr(route, "path"):
            yield route
            continue
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(getattr(original_router, "routes", []))

EXPECTED_ORCHESTRATOR_ROUTES: set[tuple[str, str]] = {
    ("DELETE", "/render-management/enrollments/{enrollment_id}"),
    ("DELETE", "/settings/publish/social/accounts/{account_id}"),
    ("DELETE", "/settings/publish/social/login-sessions/{session_id}"),
    ("DELETE", "/tasks/{task_id}/assets/{asset_id}"),
    ("GET", "/auth/status"),
    ("GET", "/bilibili/{service_path:path}"),
    ("GET", "/health"),
    ("GET", "/maintenance/workdir"),
    ("GET", "/operations/ai-usage"),
    ("GET", "/operations/ai-usage/pricing"),
    ("GET", "/operations/alerts"),
    ("GET", "/render-management/connection"),
    ("GET", "/render-management/enrollments"),
    ("GET", "/render-management/executions"),
    ("GET", "/render-management/executions/{execution_id}"),
    ("GET", "/render-management/workers"),
    ("GET", "/render-workers/v1/executions/{execution_id}/artifacts/{role}"),
    ("GET", "/render-workers/v1/executions/{execution_id}/cancel"),
    ("GET", "/render-workers/v1/protocol"),
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
    ("GET", "/tasks/{task_id}/playout-assets"),
    ("GET", "/tasks/{task_id}/publish_batches"),
    ("GET", "/tasks/{task_id}/publish_jobs"),
    ("GET", "/tasks/{task_id}/publish_meta"),
    ("GET", "/tasks/{task_id}/publish_meta/draft"),
    ("GET", "/tasks/{task_id}/publish_review"),
    ("GET", "/tasks/{task_id}/subtitle_jobs"),
    ("GET", "/tasks/{task_id}/youtube_meta"),
    ("GET", "/tasks/{task_id}/youtube_download_progress"),
    ("GET", "/videos/converted"),
    ("POST", "/auth/login"),
    ("POST", "/desktop/grants"),
    ("POST", "/bilibili/{service_path:path}"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/setup"),
    ("POST", "/auto/youtube"),
    ("POST", "/maintenance/workdir/cleanup"),
    ("POST", "/maintenance/storage/cleanup-terminal"),
    ("POST", "/operations/alerts/scan"),
    ("POST", "/operations/alerts/report"),
    ("POST", "/operations/alerts/{alert_id}/ack"),
    ("POST", "/operations/alerts/{alert_id}/resolve"),
    ("POST", "/render-management/enrollments"),
    ("POST", "/render-management/executions/{execution_id}/cancel"),
    ("POST", "/render-management/executions/{execution_id}/requeue"),
    ("POST", "/render-management/workers/{worker_id}/revoke-credential"),
    ("POST", "/render-workers/v1/enroll"),
    ("POST", "/render-workers/v1/local-enroll"),
    ("POST", "/render-workers/v1/executions/{execution_id}/complete"),
    ("POST", "/render-workers/v1/executions/{execution_id}/fail"),
    ("POST", "/render-workers/v1/executions/{execution_id}/heartbeat"),
    ("POST", "/render-workers/v1/executions/{execution_id}/logs"),
    ("POST", "/render-workers/v1/executions/{execution_id}/output"),
    ("POST", "/render-workers/v1/executions/{execution_id}/progress"),
    ("POST", "/render-workers/v1/{worker_id}/claim"),
    ("POST", "/render-workers/v1/{worker_id}/heartbeat"),
    ("POST", "/remote/auto/youtube"),
    ("POST", "/settings/publish/social/accounts/{account_id}/check"),
    ("POST", "/settings/publish/social/accounts/{platform}"),
    ("POST", "/settings/publish/social/login-sessions/{platform}"),
    ("POST", "/settings/youtube/home_scan/run"),
    ("POST", "/settings/youtube/test"),
    ("POST", "/tasks"),
    ("POST", "/tasks/actions/resume_stopped"),
    ("POST", "/tasks/actions/resume_failed_recent"),
    ("POST", "/tasks/actions/stop_all"),
    ("POST", "/tasks/{task_id}/actions/auto_youtube_start"),
    ("POST", "/tasks/{task_id}/actions/publish"),
    ("POST", "/tasks/{task_id}/actions/publish_all"),
    ("POST", "/tasks/{task_id}/actions/publish_review"),
    ("POST", "/tasks/{task_id}/actions/subtitle"),
    ("POST", "/tasks/{task_id}/actions/subtitle_resume"),
    ("POST", "/tasks/{task_id}/actions/stop"),
    ("POST", "/tasks/{task_id}/actions/resume"),
    ("POST", "/tasks/{task_id}/actions/youtube_download"),
    ("POST", "/tasks/{task_id}/actions/youtube_meta"),
    ("POST", "/tasks/{task_id}/publish_meta/draft"),
    ("POST", "/tasks/{task_id}/upload/cover"),
    ("POST", "/tasks/{task_id}/upload/video"),
    ("POST", "/tasks/{task_id}/assets/{asset_id}/playout"),
    ("PUT", "/settings/api"),
    ("PUT", "/operations/ai-usage/pricing"),
    ("PUT", "/render-management/connection"),
    ("PUT", "/bilibili/{service_path:path}"),
    ("PUT", "/settings/publish/platforms/{platform}"),
    ("PUT", "/settings/review"),
    ("PUT", "/settings/storage"),
    ("PUT", "/settings/youtube"),
    ("PUT", "/tasks/{task_id}/publish_meta"),
    ("PATCH", "/youtube/{service_path:path}"),
    ("PATCH", "/render-management/workers/{worker_id}"),
    ("DELETE", "/subtitle/{service_path:path}"),
    ("GET", "/subtitle/{service_path:path}"),
    ("POST", "/subtitle/{service_path:path}"),
    ("PUT", "/subtitle/{service_path:path}"),
    ("DELETE", "/youtube/{service_path:path}"),
    ("GET", "/youtube/{service_path:path}"),
    ("POST", "/youtube/{service_path:path}"),
}


def route_manifest(application) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in application_routes(application)
        if route.path not in DOC_PATHS and getattr(route, "include_in_schema", True)
        for method in sorted(getattr(route, "methods", set()) or set())
        if method not in {"HEAD", "OPTIONS"}
    }


class OrchestratorArchitectureTests(unittest.TestCase):
    def test_route_manifest_has_no_duplicate_method_path_pairs(self) -> None:
        pairs = [
            (method, route.path)
            for route in application_routes(app)
            if route.path not in DOC_PATHS and getattr(route, "include_in_schema", True)
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
        subtitle_source = (root / "services" / "subtitle_service.py").read_text(encoding="utf-8")
        self.assertNotIn("publishing_service.internal_http_headers", subtitle_source)

    def test_application_factory_creates_distinct_apps_without_routes_missing(self) -> None:
        from videoroll.apps.orchestrator_api.app import create_app

        first = create_app(install_lifecycle=False)
        second = create_app(install_lifecycle=False)

        self.assertIsNot(first, second)
        self.assertEqual(route_manifest(first), EXPECTED_ORCHESTRATOR_ROUTES)
        self.assertEqual(route_manifest(second), EXPECTED_ORCHESTRATOR_ROUTES)

    def test_auth_and_system_routes_are_owned_by_domain_routers(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        self.assertEqual(owners["/auth/login"], "videoroll.apps.orchestrator_api.routers.auth")
        self.assertEqual(owners["/system/resources"], "videoroll.apps.orchestrator_api.routers.system")
        self.assertEqual(owners["/operations/alerts"], "videoroll.apps.orchestrator_api.routers.operations")
        self.assertEqual(owners["/operations/ai-usage"], "videoroll.apps.orchestrator_api.routers.operations")

    def test_settings_and_maintenance_routes_are_owned_by_domain_routers(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        self.assertEqual(owners["/settings/storage"], "videoroll.apps.orchestrator_api.routers.settings")
        self.assertEqual(owners["/maintenance/workdir"], "videoroll.apps.orchestrator_api.routers.maintenance")
        self.assertEqual(owners["/maintenance/storage/cleanup-terminal"], "videoroll.apps.orchestrator_api.routers.maintenance")

    def test_legacy_live_routes_are_removed(self) -> None:
        live_routes = {
            (method, path)
            for method, path in route_manifest(app)
            if path == "/live" or path.startswith("/live/")
        }
        self.assertEqual(live_routes, set())
        self.assertFalse(Path("src/videoroll/apps/orchestrator_api/routers/live.py").exists())
        self.assertFalse(Path("src/videoroll/apps/orchestrator_api/services/live_service.py").exists())

    def test_asset_routes_are_owned_by_asset_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        self.assertEqual(owners["/tasks/{task_id}/upload/video"], "videoroll.apps.orchestrator_api.routers.assets")
        self.assertEqual(owners["/tasks/{task_id}/assets/{asset_id}/playout"], "videoroll.apps.orchestrator_api.routers.assets")

    def test_publishing_routes_are_owned_by_publishing_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        self.assertEqual(
            owners["/tasks/{task_id}/actions/publish"],
            "videoroll.apps.orchestrator_api.routers.publishing",
        )
        self.assertEqual(
            owners["/settings/publish/platforms"],
            "videoroll.apps.orchestrator_api.routers.publishing",
        )

    def test_youtube_routes_are_owned_by_youtube_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        for path in (
            "/auto/youtube",
            "/remote/auto/youtube",
            "/settings/youtube/home_scan/run",
            "/settings/youtube/test",
            "/tasks/{task_id}/youtube_meta",
            "/tasks/{task_id}/actions/auto_youtube_start",
            "/tasks/{task_id}/actions/youtube_meta",
            "/tasks/{task_id}/actions/youtube_download",
            "/youtube/{service_path:path}",
        ):
            self.assertEqual(owners[path], "videoroll.apps.orchestrator_api.routers.youtube")

    def test_internal_service_proxy_routes_are_owned_by_their_domains(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        self.assertEqual(owners["/subtitle/{service_path:path}"], "videoroll.apps.orchestrator_api.routers.settings")
        self.assertEqual(owners["/bilibili/{service_path:path}"], "videoroll.apps.orchestrator_api.routers.publishing")

    def test_task_and_subtitle_routes_are_owned_by_tasks_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in application_routes(app) if hasattr(route, "endpoint")}

        for path in (
            "/tasks",
            "/tasks/{task_id}",
            "/videos/converted",
            "/tasks/{task_id}/subtitle_jobs",
            "/tasks/{task_id}/actions/subtitle",
            "/tasks/{task_id}/actions/subtitle_resume",
            "/tasks/{task_id}/actions/stop",
            "/tasks/{task_id}/actions/resume",
            "/tasks/actions/stop_all",
            "/tasks/actions/resume_stopped",
            "/tasks/actions/resume_failed_recent",
        ):
            self.assertEqual(owners[path], "videoroll.apps.orchestrator_api.routers.tasks")

    def test_range_parser_is_owned_by_asset_service(self) -> None:
        from videoroll.apps.orchestrator_api.services.asset_service import parse_range_header

        self.assertEqual(parse_range_header("bytes=2-5", 10), (2, 5))

    def test_asset_router_delegates_storage_and_database_work_to_service(self) -> None:
        source = Path("src/videoroll/apps/orchestrator_api/routers/assets.py").read_text(encoding="utf-8")

        self.assertNotIn("db.query(", source)
        self.assertNotIn("db.get(", source)
        self.assertNotIn("s3.get_object(", source)
        self.assertNotIn("s3.head_object(", source)


if __name__ == "__main__":
    unittest.main()
