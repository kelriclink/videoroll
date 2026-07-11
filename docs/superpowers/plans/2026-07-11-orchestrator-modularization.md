# Orchestrator Modularization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `orchestrator_api/main.py` with a small compatibility entry point backed by an application factory, domain routers, application services, and isolated lifecycle/scheduler infrastructure while preserving the complete API and current behavior.

**Architecture:** Build the new structure in vertical slices. First lock the current route surface and import boundaries with tests, then introduce shared dependencies and `create_app()`, move each endpoint group with its helpers into a router/service pair, isolate lifecycle threads behind a scheduler object, and finally reduce `main.py` to a compatibility facade. Existing deployment remains a modular monolith.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, httpx, Celery, boto3/S3, unittest/pytest.

## Global Constraints

- Preserve `videoroll.apps.orchestrator_api.main:app` as the production import path.
- Preserve every existing HTTP method, path, response model, auth exemption, and status code.
- Preserve all current uncommitted social-publisher and publish-platform behavior.
- Do not change database schema, `auto_migrate`, Docker topology, frontend URLs, or scheduler behavior in this plan.
- Do not introduce new runtime dependencies.
- Router modules must not import `orchestrator_api.main`.
- Service modules must not import routers, FastAPI `app`, or middleware.
- Importing application modules must not start threads or perform DB, S3, network, or Celery work.
- Use test-first red/green cycles for every production-code extraction.
- Stage and commit only files belonging to the current task; preserve unrelated user changes.

---

### Task 1: Lock the API Surface and Architecture Boundaries

**Files:**
- Create: `tests/test_orchestrator_architecture.py`
- Read: `src/videoroll/apps/orchestrator_api/main.py`

**Interfaces:**
- Consumes: current `videoroll.apps.orchestrator_api.main.app`
- Produces: `EXPECTED_ORCHESTRATOR_ROUTES`, route-manifest regression tests, and source-boundary tests used by every later task

- [ ] **Step 1: Write the route-manifest baseline test**

```python
from pathlib import Path
import unittest

from videoroll.apps.orchestrator_api.main import app


DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


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
```

- [ ] **Step 2: Run the baseline test and confirm it passes**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: PASS, proving the current app can be imported and has no duplicate method/path pairs.

- [ ] **Step 3: Add failing modularity tests**

```python
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
```

- [ ] **Step 4: Run the modularity test and verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture.OrchestratorArchitectureTests.test_main_contains_no_route_decorators -v`

Expected: FAIL because `main.py` still contains route decorators.

- [ ] **Step 5: Record the exact current route manifest**

Add a literal `EXPECTED_ORCHESTRATOR_ROUTES: set[tuple[str, str]]` generated from `route_manifest(app)`, then assert equality:

```python
    def test_route_manifest_is_preserved(self) -> None:
        self.assertEqual(route_manifest(app), EXPECTED_ORCHESTRATOR_ROUTES)
```

Run: `python -m unittest tests.test_orchestrator_architecture.OrchestratorArchitectureTests.test_route_manifest_is_preserved -v`

Expected: PASS against the pre-refactor application.

- [ ] **Step 6: Commit the regression guard**

```bash
git add tests/test_orchestrator_architecture.py
git commit -m "test: lock orchestrator API surface"
```

### Task 2: Add Shared Dependencies and the Application Factory

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/dependencies.py`
- Create: `src/videoroll/apps/orchestrator_api/app.py`
- Create: `src/videoroll/apps/orchestrator_api/routers/__init__.py`
- Create: `src/videoroll/apps/orchestrator_api/services/__init__.py`
- Create: `src/videoroll/apps/orchestrator_api/infrastructure/__init__.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Modify: `tests/test_orchestrator_architecture.py`

**Interfaces:**
- Produces: `get_settings() -> OrchestratorSettings`, `get_db(...) -> Generator[Session, None, None]`, `get_s3(...) -> S3Store`, and `create_app(*, install_lifecycle: bool = True) -> FastAPI`

- [ ] **Step 1: Write a failing application-factory test**

```python
    def test_application_factory_creates_distinct_apps_without_routes_missing(self) -> None:
        from videoroll.apps.orchestrator_api.app import create_app

        first = create_app(install_lifecycle=False)
        second = create_app(install_lifecycle=False)
        self.assertIsNot(first, second)
        self.assertEqual(route_manifest(first), EXPECTED_ORCHESTRATOR_ROUTES)
        self.assertEqual(route_manifest(second), EXPECTED_ORCHESTRATOR_ROUTES)
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture.OrchestratorArchitectureTests.test_application_factory_creates_distinct_apps_without_routes_missing -v`

Expected: ERROR because `orchestrator_api.app` does not exist.

- [ ] **Step 3: Implement shared dependencies**

```python
from collections.abc import Generator
from fastapi import Depends
from sqlalchemy.orm import Session

from videoroll.config import OrchestratorSettings, get_orchestrator_settings
from videoroll.db.session import db_session
from videoroll.storage.s3 import S3Store


def get_settings() -> OrchestratorSettings:
    return get_orchestrator_settings()


def get_db(settings: OrchestratorSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


def get_s3(settings: OrchestratorSettings = Depends(get_settings)) -> S3Store:
    return S3Store(settings)
```

- [ ] **Step 4: Implement the factory shell**

Create `create_app()` with the same title/version, middleware, exception handler, and a temporary call to `register_legacy_routes(app)` exported from `main.py`. The temporary bridge keeps the application importable until all routers move.

```python
def create_app(*, install_lifecycle: bool = True) -> FastAPI:
    application = FastAPI(title="videoroll-orchestrator", version="0.1.0")
    register_middleware(application)
    register_legacy_routes(application)
    register_exception_handlers(application)
    if install_lifecycle:
        register_legacy_lifecycle(application)
    return application
```

The bridge must register functions explicitly and must not import `main.app`, preventing recursive application construction.

- [ ] **Step 5: Run factory and existing route tests**

Run: `python -m unittest tests.test_orchestrator_architecture tests.test_publish_platform_settings -v`

Expected: PASS.

- [ ] **Step 6: Commit the application shell**

```bash
git add src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/dependencies.py src/videoroll/apps/orchestrator_api/routers src/videoroll/apps/orchestrator_api/services src/videoroll/apps/orchestrator_api/infrastructure src/videoroll/apps/orchestrator_api/main.py tests/test_orchestrator_architecture.py
git commit -m "refactor: add orchestrator application factory"
```

### Task 3: Extract Authentication and System Routers

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/middleware.py`
- Create: `src/videoroll/apps/orchestrator_api/routers/auth.py`
- Create: `src/videoroll/apps/orchestrator_api/routers/system.py`
- Create: `src/videoroll/apps/orchestrator_api/services/auth_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Modify: `tests/test_orchestrator_architecture.py`

**Interfaces:**
- Produces: `auth_router`, `system_router`, `AdminAuthMiddleware`, `register_middleware(app)`, and auth service functions that accept `Request`/`Session` only where app-state access is required

- [ ] **Step 1: Add failing ownership tests**

```python
    def test_auth_and_system_routes_are_owned_by_domain_routers(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in app.routes if hasattr(route, "endpoint")}
        self.assertEqual(owners["/auth/login"], "videoroll.apps.orchestrator_api.routers.auth")
        self.assertEqual(owners["/system/resources"], "videoroll.apps.orchestrator_api.routers.system")
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture.OrchestratorArchitectureTests.test_auth_and_system_routes_are_owned_by_domain_routers -v`

Expected: FAIL because both endpoints are still owned by `main`.

- [ ] **Step 3: Move middleware, helpers, and endpoints**

Move the existing auth middleware unchanged into `middleware.py`; move cookie/password workflow into `auth_service.py`; expose endpoints from routers using `APIRouter`. Move `_memory_read`, health, and resource reporting into `system.py`. Register routers in `app.py`.

- [ ] **Step 4: Run targeted tests**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: ownership and route-manifest tests PASS; the final no-decorator test remains RED until Task 10.

- [ ] **Step 5: Commit the auth/system slice**

```bash
git add src/videoroll/apps/orchestrator_api/middleware.py src/videoroll/apps/orchestrator_api/routers/auth.py src/videoroll/apps/orchestrator_api/routers/system.py src/videoroll/apps/orchestrator_api/services/auth_service.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_orchestrator_architecture.py
git commit -m "refactor: extract orchestrator auth and system routes"
```

### Task 4: Extract Settings and Maintenance Routers

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/routers/settings.py`
- Create: `src/videoroll/apps/orchestrator_api/routers/maintenance.py`
- Create: `src/videoroll/apps/orchestrator_api/services/maintenance_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Test: `tests/test_orchestrator_architecture.py`
- Test: `tests/test_workdir_maintenance.py`

**Interfaces:**
- Produces: `settings_router`, `maintenance_router`, `scan_workdir_state(...)`, `run_workdir_cleanup_once(...)`

- [ ] **Step 1: Add failing route ownership assertions**

```python
        self.assertEqual(owners["/settings/storage"], "videoroll.apps.orchestrator_api.routers.settings")
        self.assertEqual(owners["/maintenance/workdir"], "videoroll.apps.orchestrator_api.routers.maintenance")
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: the new ownership assertions FAIL.

- [ ] **Step 3: Move settings CRUD and maintenance workflows**

Move storage, remote API, review, and YouTube settings CRUD into `settings.py`. Move workdir lock, scan, response conversion, and cleanup workflows into `maintenance_service.py`; keep only HTTP conversion in `maintenance.py`.

- [ ] **Step 4: Verify targeted tests**

Run: `python -m unittest tests.test_orchestrator_architecture tests.test_workdir_maintenance -v`

Expected: PASS except the intentionally pending final no-decorator assertion.

- [ ] **Step 5: Commit**

```bash
git add src/videoroll/apps/orchestrator_api/routers/settings.py src/videoroll/apps/orchestrator_api/routers/maintenance.py src/videoroll/apps/orchestrator_api/services/maintenance_service.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_orchestrator_architecture.py tests/test_workdir_maintenance.py
git commit -m "refactor: extract orchestrator settings and maintenance routes"
```

### Task 5: Extract Asset Router and Service

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/routers/assets.py`
- Create: `src/videoroll/apps/orchestrator_api/services/asset_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Test: `tests/test_upload_guardrails.py`
- Test: `tests/test_orchestrator_architecture.py`

**Interfaces:**
- Produces: `assets_router`, `store_uploaded_task_asset(...)`, `parse_range_header(...)`, `content_disposition(...)`, and S3 JSON/text helpers used by later services

- [ ] **Step 1: Add failing asset ownership and helper import tests**

```python
    def test_asset_routes_are_owned_by_asset_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in app.routes if hasattr(route, "endpoint")}
        self.assertEqual(owners["/tasks/{task_id}/upload/video"], "videoroll.apps.orchestrator_api.routers.assets")

    def test_range_parser_is_owned_by_asset_service(self) -> None:
        from videoroll.apps.orchestrator_api.services.asset_service import parse_range_header
        self.assertEqual(parse_range_header("bytes=2-5", 10), (2, 5))
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: ERROR because the asset service does not exist.

- [ ] **Step 3: Move asset helpers and routes**

Move upload limits, temporary-file streaming, hashing, S3 rollback, filename/content-disposition handling, range parsing, download/stream/delete behavior, and task asset lookup into the asset slice. Preserve `run_in_threadpool` for blocking upload work.

- [ ] **Step 4: Verify asset behavior**

Run: `python -m unittest tests.test_upload_guardrails tests.test_orchestrator_architecture -v`

Expected: PASS except the final no-decorator assertion.

- [ ] **Step 5: Commit**

```bash
git add src/videoroll/apps/orchestrator_api/routers/assets.py src/videoroll/apps/orchestrator_api/services/asset_service.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_upload_guardrails.py tests/test_orchestrator_architecture.py
git commit -m "refactor: extract orchestrator asset routes"
```

### Task 6: Extract YouTube Router and Service

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/routers/youtube.py`
- Create: `src/videoroll/apps/orchestrator_api/services/youtube_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Test: `tests/test_auto_youtube_pipeline.py`
- Test: `tests/test_youtube_downloader.py`
- Test: `tests/test_youtube_home_feed.py`
- Test: `tests/test_orchestrator_architecture.py`

**Interfaces:**
- Produces: `youtube_router`, `ingest_youtube_source(...)`, `start_auto_youtube_pipeline(...)`, `effective_youtube_settings(...)`, `run_youtube_home_scan(...)`, and YouTube metadata/download workflows

- [ ] **Step 1: Add failing YouTube route ownership test**

```python
    def test_youtube_routes_are_owned_by_youtube_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in app.routes if hasattr(route, "endpoint")}
        self.assertEqual(owners["/auto/youtube"], "videoroll.apps.orchestrator_api.routers.youtube")
        self.assertEqual(owners["/tasks/{task_id}/actions/youtube_download"], "videoroll.apps.orchestrator_api.routers.youtube")
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: FAIL because endpoints remain in `main`.

- [ ] **Step 3: Move YouTube workflows and routes**

Move remote-token validation, ingest HTTP calls, auto-pipeline dispatch, home scan, source scan trigger helpers, metadata fetch, download diagnostics, and proxy testing into the YouTube slice. Update tests to patch `services.youtube_service` or `routers.youtube` according to ownership.

- [ ] **Step 4: Verify YouTube behavior**

Run: `python -m unittest tests.test_auto_youtube_pipeline tests.test_youtube_downloader tests.test_youtube_home_feed tests.test_orchestrator_architecture -v`

Expected: PASS except the final no-decorator assertion.

- [ ] **Step 5: Commit**

```bash
git add src/videoroll/apps/orchestrator_api/routers/youtube.py src/videoroll/apps/orchestrator_api/services/youtube_service.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_auto_youtube_pipeline.py tests/test_youtube_downloader.py tests/test_youtube_home_feed.py tests/test_orchestrator_architecture.py
git commit -m "refactor: extract orchestrator youtube routes"
```

### Task 7: Extract Publishing Router and Service

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/routers/publishing.py`
- Create: `src/videoroll/apps/orchestrator_api/services/publishing_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Modify: `tests/test_publish_gateway.py`
- Modify: `tests/test_publish_platform_settings.py`
- Test: `tests/test_publish_review.py`
- Test: `tests/test_orchestrator_architecture.py`

**Interfaces:**
- Produces: `publishing_router`, `build_publish_gateway_request(...)`, `prepare_publish_meta(...)`, `run_task_publish_review(...)`, and internal social-publisher client operations

- [ ] **Step 1: Add failing publishing ownership test and migrate helper import**

```python
    def test_publishing_routes_are_owned_by_publishing_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in app.routes if hasattr(route, "endpoint")}
        self.assertEqual(owners["/tasks/{task_id}/actions/publish"], "videoroll.apps.orchestrator_api.routers.publishing")
        self.assertEqual(owners["/settings/publish/social/accounts"], "videoroll.apps.orchestrator_api.routers.publishing")
```

Change `tests/test_publish_gateway.py` to import:

```python
from videoroll.apps.orchestrator_api.services.publishing_service import build_publish_gateway_request
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: ownership assertions FAIL and the service import errors.

- [ ] **Step 3: Move publishing and social proxy workflows**

Move publish metadata CRUD/draft/review, platform settings, publish histories, gateway request construction, social account/login proxy operations, and publish submission into the publishing slice. Preserve current platform-scoped S3 metadata keys, force-retry behavior, account validation, and response enrichment.

- [ ] **Step 4: Update patches to owning modules**

Replace patches such as:

```python
patch("videoroll.apps.orchestrator_api.main.is_publish_platform_enabled", ...)
```

with:

```python
patch("videoroll.apps.orchestrator_api.routers.publishing.is_publish_platform_enabled", ...)
```

or patch the service function when the rule is owned by `publishing_service.py`.

- [ ] **Step 5: Verify publishing behavior**

Run: `python -m unittest tests.test_publish_gateway tests.test_publish_platform_settings tests.test_publish_review tests.test_orchestrator_architecture -v`

Expected: PASS except the final no-decorator assertion.

- [ ] **Step 6: Commit**

```bash
git add src/videoroll/apps/orchestrator_api/routers/publishing.py src/videoroll/apps/orchestrator_api/services/publishing_service.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_publish_gateway.py tests/test_publish_platform_settings.py tests/test_publish_review.py tests/test_orchestrator_architecture.py
git commit -m "refactor: extract orchestrator publishing routes"
```

### Task 8: Extract Task and Subtitle Workflow Routers and Services

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/routers/tasks.py`
- Create: `src/videoroll/apps/orchestrator_api/services/task_service.py`
- Create: `src/videoroll/apps/orchestrator_api/services/subtitle_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Test: `tests/test_resume_failed_recent.py`
- Test: `tests/test_translate_resume.py`
- Test: `tests/test_orchestrator_architecture.py`

**Interfaces:**
- Produces: `tasks_router`, task state reconciliation/read helpers, `enqueue_subtitle_service_job(...)`, `build_resume_subtitle_request(...)`, and recent-failure resume workflow

- [ ] **Step 1: Add failing task ownership test**

```python
    def test_task_routes_are_owned_by_task_router(self) -> None:
        owners = {route.path: route.endpoint.__module__ for route in app.routes if hasattr(route, "endpoint")}
        self.assertEqual(owners["/tasks"], "videoroll.apps.orchestrator_api.routers.tasks")
        self.assertEqual(owners["/tasks/{task_id}/actions/subtitle"], "videoroll.apps.orchestrator_api.routers.tasks")
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: FAIL because task endpoints remain in `main`.

- [ ] **Step 3: Move remaining task and subtitle workflows**

Move task creation/list/detail, converted-video listing, display-title hydration, status reconciliation, subtitle job histories, subtitle enqueue/resume, and recent failed resume into the task slice. Put internal subtitle-service request building and HTTP calls in `subtitle_service.py`.

- [ ] **Step 4: Verify task behavior**

Run: `python -m unittest tests.test_resume_failed_recent tests.test_translate_resume tests.test_orchestrator_architecture -v`

Expected: PASS except the final no-decorator assertion.

- [ ] **Step 5: Commit**

```bash
git add src/videoroll/apps/orchestrator_api/routers/tasks.py src/videoroll/apps/orchestrator_api/services/task_service.py src/videoroll/apps/orchestrator_api/services/subtitle_service.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_resume_failed_recent.py tests/test_translate_resume.py tests/test_orchestrator_architecture.py
git commit -m "refactor: extract orchestrator task routes"
```

### Task 9: Isolate Lifecycle and Scheduler Infrastructure

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/infrastructure/scheduler.py`
- Create: `src/videoroll/apps/orchestrator_api/infrastructure/lifecycle.py`
- Create: `src/videoroll/apps/orchestrator_api/infrastructure/internal_http.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Test: `tests/test_orchestrator_architecture.py`
- Test: `tests/test_workdir_maintenance.py`
- Test: `tests/test_youtube_source_service.py`

**Interfaces:**
- Produces: `OrchestratorScheduler.start()`, `OrchestratorScheduler.stop()`, `orchestrator_lifespan(app)`, `internal_http_headers(settings)`, and typed upstream-error translation

- [ ] **Step 1: Write failing scheduler import-safety test**

```python
    def test_factory_without_lifecycle_does_not_start_scheduler(self) -> None:
        from unittest.mock import patch
        from videoroll.apps.orchestrator_api.app import create_app

        with patch("videoroll.apps.orchestrator_api.infrastructure.scheduler.OrchestratorScheduler.start") as start:
            create_app(install_lifecycle=False)
        start.assert_not_called()
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_orchestrator_architecture.OrchestratorArchitectureTests.test_factory_without_lifecycle_does_not_start_scheduler -v`

Expected: ERROR because scheduler infrastructure does not exist.

- [ ] **Step 3: Implement scheduler and lifespan**

Move the existing stop events, thread references, intervals, worker IDs, loop bodies, and thread start/stop code into one `OrchestratorScheduler` instance created during lifespan. Move DB schema initialization, compatibility migration, S3 bucket initialization, workdir creation, and `app.state` initialization into `orchestrator_lifespan`.

Use the FastAPI lifespan protocol:

```python
@asynccontextmanager
async def orchestrator_lifespan(app: FastAPI):
    runtime = initialize_runtime(app)
    runtime.scheduler.start()
    try:
        yield
    finally:
        runtime.scheduler.stop()
```

- [ ] **Step 4: Verify lifecycle behavior**

Run: `python -m unittest tests.test_orchestrator_architecture tests.test_workdir_maintenance tests.test_youtube_source_service -v`

Expected: PASS except the final no-decorator assertion.

- [ ] **Step 5: Commit**

```bash
git add src/videoroll/apps/orchestrator_api/infrastructure/scheduler.py src/videoroll/apps/orchestrator_api/infrastructure/lifecycle.py src/videoroll/apps/orchestrator_api/infrastructure/internal_http.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/main.py tests/test_orchestrator_architecture.py tests/test_workdir_maintenance.py tests/test_youtube_source_service.py
git commit -m "refactor: isolate orchestrator lifecycle"
```

### Task 10: Reduce `main.py` to the Compatibility Facade and Verify Everything

**Files:**
- Modify: `src/videoroll/apps/orchestrator_api/main.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `tests/test_orchestrator_architecture.py`
- Modify: tests that still import private `main.py` symbols

**Interfaces:**
- Produces: final `app = create_app()` compatibility entry point and no legacy route bridge

- [ ] **Step 1: Remove the legacy bridge and all remaining route code**

Final `main.py` must be structurally equivalent to:

```python
from videoroll.apps.orchestrator_api.app import create_app


app = create_app()
```

Only deliberately documented compatibility re-exports may remain, and no production module may call them.

- [ ] **Step 2: Run the previously failing architecture test and verify GREEN**

Run: `python -m unittest tests.test_orchestrator_architecture -v`

Expected: all architecture, ownership, route-manifest, import-boundary, and factory tests PASS.

- [ ] **Step 3: Run backend verification**

Run: `python -m pytest tests/`

Expected: all backend tests PASS. If the host lacks pytest, run the same command in the project test container or install development-only test tooling without changing production dependencies.

- [ ] **Step 4: Run frontend verification**

Run: `cd src/web && npm run lint && npm run test && npm run build`

Expected: ESLint exits 0, all Vitest tests pass, and Vite production build exits 0.

- [ ] **Step 5: Inspect the final diff and route surface**

Run:

```bash
git diff --check
git status --short
python -m unittest tests.test_orchestrator_architecture -v
```

Expected: no whitespace errors; unrelated pre-existing user changes remain present; route manifest exactly matches the baseline.

- [ ] **Step 6: Commit the facade cleanup**

```bash
git add src/videoroll/apps/orchestrator_api tests/test_orchestrator_architecture.py tests/test_auto_youtube_pipeline.py tests/test_publish_gateway.py tests/test_publish_platform_settings.py
git commit -m "refactor: modularize orchestrator application"
```

