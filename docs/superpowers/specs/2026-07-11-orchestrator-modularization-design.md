# Orchestrator Modularization Design

## Goal

Replace the 3,400-line `orchestrator_api/main.py` God File with a modular FastAPI application whose routers only handle HTTP concerns, whose services own business workflows and transactions, and whose infrastructure modules own application lifecycle and scheduling. The refactor must preserve every existing HTTP path, method, response model, authentication rule, task state transition, and current uncommitted social-publisher feature.

This is the first sub-project in the broader VideoRoll optimization roadmap. Moving recurring work to Celery Beat, splitting the RAG module, introducing Alembic, and splitting the frontend task detail page remain separate follow-up projects so each change can be verified and reviewed independently.

## Constraints

- Keep the current modular-monolith deployment model. Do not create new network services.
- Preserve `videoroll.apps.orchestrator_api.main:app` as the production import path.
- Preserve all public API paths and request/response schemas.
- Do not change database schema or migration behavior in this sub-project.
- Do not move the existing background loops to Celery yet; isolate them behind infrastructure boundaries so the next project can replace them safely.
- Do not overwrite or discard the current uncommitted social-publisher, publish-platform, Docker, or frontend changes.
- Avoid adding runtime dependencies.
- Keep tests offline and mock external HTTP, S3, YouTube, LLM, and publishing services.

## Approaches Considered

### 1. Router-only extraction

Move decorated endpoints into `APIRouter` modules while leaving helpers, state, transactions, and scheduling in `main.py`.

This has the smallest diff, but routers would need to import private functions from `main.py`, creating circular dependencies and leaving the God File largely intact. It does not establish a durable architecture.

### 2. Domain routers plus application services and infrastructure modules

Create an application factory, shared dependency module, domain routers, domain services, and lifecycle infrastructure. Move code in vertical slices while keeping compatibility imports during the transition.

This is the selected approach. It reduces coupling without changing deployment or API behavior, and it creates the boundary required for later Celery Beat migration.

### 3. Immediate service decomposition into independently deployed microservices

Split orchestrator domains into separate FastAPI deployments and communicate over HTTP or queues.

This would increase deployment, authentication, observability, and distributed transaction complexity without a current scaling requirement. It is outside this project's scope.

## Target Structure

```text
src/videoroll/apps/orchestrator_api/
├── main.py
├── app.py
├── dependencies.py
├── middleware.py
├── schemas.py
├── routers/
│   ├── __init__.py
│   ├── auth.py
│   ├── system.py
│   ├── tasks.py
│   ├── assets.py
│   ├── youtube.py
│   ├── publishing.py
│   ├── settings.py
│   └── maintenance.py
├── services/
│   ├── __init__.py
│   ├── task_service.py
│   ├── asset_service.py
│   ├── youtube_service.py
│   ├── publishing_service.py
│   └── subtitle_service.py
└── infrastructure/
    ├── __init__.py
    ├── lifecycle.py
    ├── scheduler.py
    └── internal_http.py
```

Existing specialized modules such as `admin_auth_store.py`, `remote_api_settings_store.py`, `storage_retention_store.py`, `youtube_downloader.py`, and `youtube_home_feed.py` remain in place. They are already focused components and will be consumed by the new services.

## Module Responsibilities

### `main.py`

Remain a compatibility entry point only:

```python
from videoroll.apps.orchestrator_api.app import create_app

app = create_app()
```

Temporary re-exports may be kept only for internal symbols currently imported by repository tests. New code must import from the owning service module.

### `app.py`

Own `create_app()`. It creates FastAPI, registers middleware, exception handlers, lifespan behavior, and routers in a deterministic order. Importing router modules must not perform database, S3, network, thread, or Celery operations.

### `dependencies.py`

Own FastAPI dependencies for `OrchestratorSettings`, SQLAlchemy sessions, and `S3Store`. This prevents every router from importing `main.py` and gives tests one stable dependency-override surface.

### `middleware.py`

Own the admin authentication middleware and path exemptions. Authentication continues to read initialized secrets and password state from `request.app.state`. Cookie behavior and internal-token behavior remain unchanged.

### Routers

Routers translate HTTP inputs into service calls and service results into response models. A router may perform lightweight request parsing, but it must not contain long database workflows, external client orchestration, filesystem pipelines, or task state machines.

- `auth.py`: setup, login, logout, and auth status.
- `system.py`: health and resource reporting.
- `tasks.py`: task creation, listing, detail, converted videos, subtitle/publish job histories, resume actions, and pipeline actions not owned by a more specific router.
- `assets.py`: upload, list, stream, download, and delete task assets.
- `youtube.py`: automatic YouTube entry points, metadata fetching, downloading, proxy test, home-scan trigger, and YouTube task metadata.
- `publishing.py`: publish metadata, publish review, platform settings, social accounts/login proxy, and publish actions.
- `settings.py`: storage, remote API, review, and YouTube configuration endpoints. A YouTube action such as running a scan remains in `youtube.py`; configuration CRUD remains here.
- `maintenance.py`: work-directory inspection and cleanup endpoints.

### Services

Services own business rules and transaction boundaries. They accept explicit dependencies such as `Session`, `S3Store`, and settings instead of resolving FastAPI dependencies themselves.

- `task_service.py`: task reads, status reconciliation, display-title hydration, recent-failure resume selection, and cross-stage task transitions.
- `asset_service.py`: upload guardrails, temporary file streaming, hashing, S3 persistence, range streaming metadata, and rollback cleanup.
- `youtube_service.py`: ingest calls, effective YouTube settings, metadata/download workflows, diagnostics, and auto-pipeline dispatch.
- `publishing_service.py`: publish request construction, metadata persistence, review orchestration, platform routing, social-publisher proxy calls, and publish state reconciliation.
- `subtitle_service.py`: subtitle enqueue/resume request construction and subtitle-service HTTP calls.

Small pure helpers belong in the service that owns their concept. A generic `utils.py` dumping ground will not be introduced.

### Infrastructure

- `internal_http.py`: internal authentication headers and shared internal-service HTTP error translation. Domain services still decide URLs and payloads.
- `scheduler.py`: current storage cleanup, YouTube home scan, YouTube source scan, and work-directory startup cleanup loops. The public interface is a lifecycle-managed scheduler object with `start()` and `stop()` methods. This preserves current behavior while isolating it for later Celery replacement.
- `lifecycle.py`: initialize DB schema, current compatibility migrations, S3 bucket, work directory, app state, and scheduler. It exposes a FastAPI lifespan context manager.

## Application and Data Flow

For a typical request:

```text
HTTP request
  -> authentication middleware
  -> domain router
  -> FastAPI dependency resolution
  -> application service
  -> database / S3 / internal HTTP / Celery
  -> typed service result
  -> response model
```

Services must not depend on routers or the FastAPI application object. Routers may depend on services. Infrastructure may depend on services needed by scheduled jobs, but services must not depend on infrastructure scheduling objects.

## Transaction Rules

- A service that mutates database state owns its commit and rollback behavior unless the caller explicitly provides a transaction scope.
- S3 plus database operations preserve the current compensation pattern: if database persistence fails after an S3 upload, the uploaded object is deleted when safe.
- External HTTP requests must not occur while holding database row locks longer than required.
- Task status changes continue to clear or set error fields consistently with current behavior.
- Existing database-based distributed locks remain active for cleanup and scanning.

## Error Handling

- Routers raise `HTTPException` only for HTTP-facing validation or to translate a typed service exception.
- Services raise domain-specific exceptions carrying a stable category, message, retryability, and optional upstream status.
- `app.py` registers handlers that preserve current response status codes and `{"detail": ...}` bodies.
- External HTTP errors identify the upstream service without exposing secrets or full credential-bearing payloads.
- Cleanup-only failures are logged with task/job/storage identifiers. Failures that make the requested operation incomplete are not silently swallowed.
- The existing `ValueError` handler remains for compatibility, but new service code should use explicit exception types.

## Compatibility Strategy

Before moving code, capture a route manifest containing every method and path currently registered on the application. The refactored app must match it exactly, excluding FastAPI-generated documentation routes when appropriate.

Repository tests currently import or patch private symbols in `main.py`, including the publish request builder and remote API verification helpers. Tests will be migrated to patch the owning module. Temporary re-exports from `main.py` may remain for one release where they prevent unnecessary breakage, but internal implementation must not call through the compatibility facade.

`videoroll.apps.monolith.main` continues mounting the same `app` object, so Docker, Uvicorn, nginx, and frontend URLs do not change.

## Testing Strategy

The refactor follows test-driven development.

1. Add an architecture test that fails while route decorators and business workflows remain in `main.py`.
2. Add a route-manifest regression test covering all current non-documentation methods and paths.
3. Add application-factory tests proving multiple app instances can be created without starting threads or touching external systems during import.
4. Add router tests with dependency overrides for representative auth, asset, YouTube, publishing, settings, maintenance, and task endpoints.
5. Move existing unit tests to patch service owners rather than `main.py` internals.
6. Run targeted tests after each vertical slice.
7. Run the complete backend test suite in the project container or an environment containing the declared dependencies.
8. Run frontend lint, unit tests, and production build to verify the unchanged API surface remains consumable.

The architecture test will enforce these invariants:

- `main.py` contains no `@app.get`, `@app.post`, `@app.put`, `@app.patch`, or `@app.delete` decorators.
- Router modules do not import `orchestrator_api.main`.
- Service modules do not import routers or the FastAPI application.
- Importing `main.app` does not start scheduler threads.

## Implementation Sequence

1. Capture route manifest and architecture constraints.
2. Introduce shared dependencies, internal HTTP helpers, application factory, and lifespan shell.
3. Extract auth and system routers.
4. Extract settings and maintenance routers.
5. Extract asset router and asset service.
6. Extract YouTube router and YouTube service.
7. Extract publishing router and publishing service, including current social-publisher changes.
8. Extract task router, task service, and subtitle service.
9. Move background loops into scheduler infrastructure without changing their runtime behavior.
10. Reduce `main.py` to the compatibility facade, remove dead imports, and update tests.
11. Run full verification and compare the final route manifest with the captured baseline.

Each sequence step must leave an importable application and passing targeted tests. Large all-at-once file replacement is explicitly avoided even though the final result is a complete split.

## Success Criteria

- `main.py` is a small compatibility entry point with no route implementations.
- Every current API method/path remains registered exactly once.
- Authentication and internal-service bypass rules behave unchanged.
- Startup initializes the same DB, migration, S3, work-directory, secret, and scheduler state.
- Shutdown reliably stops current scheduler loops.
- Domain routers contain HTTP adaptation rather than business workflows.
- Services can be tested without constructing a FastAPI application.
- Existing social-publisher functionality and platform settings remain intact.
- Targeted backend tests and the complete available verification suite pass.
- No unrelated user changes are reverted or included in refactor-only commits.

## Follow-up Projects

After this design is implemented and verified:

1. Replace scheduler threads with Celery Beat tasks and split API/workers into separate Compose services.
2. Split `subtitle_service/rag.py` into models, settings, repository, retrieval, agent, verification, and tool modules.
3. Introduce Alembic and migrate away from startup-time schema mutation.
4. Split `TaskDetailPage.tsx`, generate or validate API types, and add consistent request cancellation and error handling.

