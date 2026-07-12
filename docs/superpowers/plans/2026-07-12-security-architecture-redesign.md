# VideoRoll Security Architecture Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace implicit browser, service, worker, desktop, and outbound-network trust with authenticated gateway routing, durable async delivery, lease-based recovery, and regression-tested frontend behavior.

**Architecture:** The orchestrator becomes the only browser-facing API. Internal services accept a dedicated service token and are reachable only on the private Compose network. PostgreSQL owns idempotency, outbox delivery, worker leases, and scoped desktop grants; Redis remains a rate-limit and throughput aid, never the correctness authority. RAG fetches move behind a bounded egress client with verified DNS/IP connections.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Redis, Celery, httpx/httpcore, Nginx, React 18, TypeScript, Vite, Vitest, pytest.

## Global Constraints

- Do not modify unrelated untracked `fromprod/` or `social-auto-upload-kelric/` files.
- Do not restore Remote API GET or query-token authentication after migration.
- Do not expose internal service ports on the host in production Compose files.
- Every production behavior change must have a focused failing test before implementation.
- Secrets, cookies, bearer tokens, and full remote response bodies must never be written to logs.
- Existing tasks, publish batches, publish jobs, assets, and settings must remain readable during rollout.
- `auto_migrate.py` may keep compatibility checks but must not create new security-critical tables.
- Use `Authorization: Bearer` for service-to-service and Remote API credentials; never put credentials in URLs.
- Every fixture/helper name shown in a test snippet is part of that task's Step 1 and must be implemented in the same test file before the test is run.

---

### Task 1: Introduce Versioned Security Schema

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_security_architecture.py`
- Modify: `pyproject.toml`
- Modify: `src/videoroll/db/models.py`
- Modify: `src/videoroll/db/session.py`
- Modify: `src/videoroll/db/auto_migrate.py`
- Create: `tests/test_security_schema_migration.py`

**Interfaces:**
- New models: `OutboxEvent`, `OperationInbox`, `RemoteAPIRequest`, `DesktopAccessGrant`, `SecurityAuditEvent`.
- New common lease fields: `lease_owner`, `lease_until`, `heartbeat_at`, `operation_key` on subtitle/render/publish job tables where the existing model permits it.
- Migration entry point: `python -m videoroll.db.migrate upgrade`.

- [ ] **Step 1: Write the failing migration/model tests**

```python
def test_security_tables_and_unique_operation_keys_exist():
    metadata = Base.metadata
    assert "outbox_events" in metadata.tables
    assert "operation_inbox" in metadata.tables
    assert "remote_api_requests" in metadata.tables
    assert "desktop_access_grants" in metadata.tables
    assert "security_audit_events" in metadata.tables
    assert any(c.name == "operation_key" for c in metadata.tables["operation_inbox"].columns)

def test_migration_does_not_depend_on_auto_migrate():
    source = Path("src/videoroll/db/auto_migrate.py").read_text(encoding="utf-8")
    assert "CREATE TABLE outbox_events" not in source
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_security_schema_migration.py -q`

Expected: FAIL because the new tables and migration command do not exist.

- [ ] **Step 3: Add migration dependency and model definitions**

Add `alembic>=1.13,<2` to `pyproject.toml`. Define UUID primary keys, UTC timestamps, JSONB payloads, bounded error fields, indexes for pending/lease queries, and unique constraints for `operation_inbox.operation_key` and `(token_hash, idempotency_key)` on `remote_api_requests`. Add optimistic `version` columns to editable settings rows that will be updated through the API.

- [ ] **Step 4: Implement the versioned migration runner**

`migrations/env.py` must read `DATABASE_URL` from the existing settings and expose offline/online Alembic modes. `videoroll.db.migrate` must run `alembic upgrade head` and return a non-zero exit code on migration failure. Keep `auto_migrate()` limited to legacy additive compatibility checks.

- [ ] **Step 5: Run migration tests and a SQLite metadata smoke test**

Run: `.venv/bin/python -m pytest tests/test_security_schema_migration.py -q`

Expected: PASS, including inspection of the generated migration SQL and model metadata.

- [ ] **Step 6: Commit**

```bash
git add alembic.ini migrations pyproject.toml src/videoroll/db/models.py src/videoroll/db/session.py src/videoroll/db/auto_migrate.py tests/test_security_schema_migration.py
git commit -m "feat: add versioned security schema"
```

### Task 2: Add Internal Service Identity And Admin Abuse Controls

**Files:**
- Create: `src/videoroll/apps/security/service_auth.py`
- Create: `src/videoroll/apps/security/rate_limits.py`
- Create: `src/videoroll/apps/security/audit.py`
- Modify: `src/videoroll/config.py`
- Modify: `src/videoroll/apps/orchestrator_api/middleware.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/auth_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/routers/auth.py`
- Modify: `src/videoroll/apps/orchestrator_api/infrastructure/lifecycle.py`
- Modify: `src/videoroll/apps/subtitle_service/main.py`
- Modify: `src/videoroll/apps/youtube_ingest/main.py`
- Modify: `src/videoroll/apps/bilibili_publisher/main.py`
- Modify: `src/videoroll/apps/social_publisher/main.py`
- Create: `tests/test_service_auth.py`
- Create: `tests/test_admin_auth_controls.py`

**Interfaces:**
- `service_token(settings) -> str` derives a token from `INTERNAL_API_SECRET` using a versioned HMAC.
- `require_internal_service(request) -> None` accepts only the internal header for non-health routes.
- `consume_bootstrap_secret(request, presented) -> None` atomically consumes the configured bootstrap secret.
- `check_login_rate_limit(redis_url, key) -> RateLimitDecision` returns allow/deny and retry-after seconds.

- [ ] **Step 1: Write failing tests for token separation, bootstrap, and throttling**

```python
def test_internal_token_does_not_change_when_s3_secret_changes():
    assert service_token_for("internal-secret", "s3-a") == service_token_for("internal-secret", "s3-b")

def test_setup_requires_bootstrap_secret_and_consumes_it_once():
    first = setup_with_bootstrap("one-time-secret", "password-123")
    second = setup_with_bootstrap("one-time-secret", "password-456")
    assert first.trusted is True
    assert second.status_code == 403

def test_login_rate_limit_returns_retry_after_after_failures():
    decision = limiter.record_failure("login:203.0.113.7")
    assert decision.allowed is False
    assert decision.retry_after > 0
```

- [ ] **Step 2: Run tests and verify expected failures**

Run: `.venv/bin/python -m pytest tests/test_service_auth.py tests/test_admin_auth_controls.py -q`

Expected: FAIL because the dedicated secret, bootstrap gate, and limiter do not exist.

- [ ] **Step 3: Implement shared service authentication**

Add `INTERNAL_API_SECRET`, `ADMIN_BOOTSTRAP_SECRET`, and explicit development-mode settings. Validate that production does not use empty or known default values. Add middleware to every internal FastAPI app, exempting only `/health`; allow the monolith parent to inject the internal header after validating an administrator session.

- [ ] **Step 4: Implement bootstrap consumption, Redis rate limits, and audit events**

Use a row lock on `admin.auth` for bootstrap consumption. Rate-limit setup and login by endpoint plus normalized client IP, store only counters and expiry in Redis, and write bounded `SecurityAuditEvent` rows for success/failure/throttle. Never include password, bearer token, or cookie values.

- [ ] **Step 5: Run focused tests and route-level auth tests**

Run: `.venv/bin/python -m pytest tests/test_service_auth.py tests/test_admin_auth_controls.py tests/test_orchestrator_architecture.py -q`

Expected: PASS; direct internal app requests without the header return 401/403, while monolith-authenticated forwarding succeeds.

- [ ] **Step 6: Commit**

```bash
git add src/videoroll/apps/security src/videoroll/config.py src/videoroll/apps/orchestrator_api src/videoroll/apps/subtitle_service/main.py src/videoroll/apps/youtube_ingest/main.py src/videoroll/apps/bilibili_publisher/main.py src/videoroll/apps/social_publisher/main.py tests/test_service_auth.py tests/test_admin_auth_controls.py
git commit -m "feat: enforce internal service identity"
```

### Task 3: Make The Orchestrator The Only Browser API

**Files:**
- Modify: `src/videoroll/apps/monolith/main.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/infrastructure/internal_http.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/subtitle_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/youtube_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/publishing_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/routers/settings.py`
- Modify: `src/videoroll/apps/orchestrator_api/routers/youtube.py`
- Modify: `src/web/src/lib/urls.ts`
- Modify: `src/web/src/pages/SettingsASRPage.tsx`
- Modify: `src/web/src/pages/SettingsTranslatePage.tsx`
- Modify: `src/web/src/pages/SettingsPublishPage.tsx`
- Modify: `src/web/src/pages/TaskNewPage.tsx`
- Modify: `tests/test_orchestrator_architecture.py`
- Create: `tests/test_internal_service_routing.py`

**Interfaces:**
- Orchestrator service clients accept `OrchestratorSettings` and inject `X-Videoroll-Internal-Token` server-side.
- Frontend exports only `ORCHESTRATOR_URL` for API calls.
- Child service settings/actions are exposed through authenticated orchestrator routes.

- [ ] **Step 1: Add failing tests proving direct browser service URLs are rejected**

```python
def test_frontend_has_no_direct_internal_service_urls():
    source = Path("src/web/src/lib/urls.ts").read_text(encoding="utf-8")
    assert "VITE_SUBTITLE_SERVICE_URL" not in source
    assert "VITE_YOUTUBE_INGEST_URL" not in source
    assert "VITE_BILIBILI_PUBLISHER_URL" not in source

def test_monolith_does_not_mount_unauthenticated_child_apps():
    source = Path("src/videoroll/apps/monolith/main.py").read_text(encoding="utf-8")
    assert ".mount(" not in source
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_internal_service_routing.py tests/test_orchestrator_architecture.py -q`

Expected: FAIL because the monolith still mounts child apps and frontend URL overrides exist.

- [ ] **Step 3: Move child-service browser operations behind orchestrator services**

For each current frontend child-service call, add or reuse an orchestrator route that performs the internal HTTP request with the service token. Preserve response shapes where possible so pages do not need a broad rewrite.

- [ ] **Step 4: Remove child mounts and direct URL variables**

Delete `app.mount(...)` calls from `monolith/main.py`, remove direct service URL exports and Vite/Compose build args, and update pages to call orchestrator routes only.

- [ ] **Step 5: Run routing and frontend type checks**

Run: `.venv/bin/python -m pytest tests/test_internal_service_routing.py tests/test_orchestrator_architecture.py -q` and `cd src/web && npm run build`.

Expected: PASS; all browser operations use `/api` and child services are internal-only.

- [ ] **Step 6: Commit**

```bash
git add src/videoroll/apps/monolith src/videoroll/apps/orchestrator_api src/web/src/lib/urls.ts src/web/src/pages tests/test_internal_service_routing.py
git commit -m "refactor: route browser operations through orchestrator"
```

### Task 4: Add Scoped noVNC Desktop Grants And Nginx Authorization

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/desktop_grants.py`
- Create: `src/videoroll/apps/orchestrator_api/routers/desktop.py`
- Modify: `src/videoroll/apps/orchestrator_api/app.py`
- Modify: `src/videoroll/apps/orchestrator_api/middleware.py`
- Modify: `src/web/nginx.conf`
- Modify: `docker/social-publisher-entrypoint.sh`
- Modify: `src/web/src/pages/SettingsPublishPage.tsx`
- Modify: `src/web/src/pages/TaskDetailPage.tsx`
- Create: `tests/test_desktop_grants.py`
- Modify: `tests/test_social_publisher_container.py`

**Interfaces:**
- `create_desktop_grant(db, admin_session, desktop_type, resource_id) -> DesktopGrantRead`.
- `authorize_desktop_request(request) -> None` validates cookie, grant token, resource scope, expiry, and reconnect count.
- Nginx internal auth endpoint: `/internal/desktop-auth` proxied to the orchestrator authorization route.

- [ ] **Step 1: Write failing grant and container tests**

```python
def test_grant_is_scoped_and_single_use():
    grant = create_grant("session-a", "publish", "job-a", ttl_seconds=30)
    assert authorize(grant.token, "session-a", "publish", "job-a") is True
    assert authorize(grant.token, "session-b", "publish", "job-a") is False
    assert authorize(grant.token, "session-a", "login", "job-a") is False

def test_nginx_no_vnc_paths_use_auth_request_and_no_nopw():
    nginx = Path("src/web/nginx.conf").read_text(encoding="utf-8")
    entrypoint = Path("docker/social-publisher-entrypoint.sh").read_text(encoding="utf-8")
    assert "auth_request" in nginx
    assert "-nopw" not in entrypoint
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_desktop_grants.py tests/test_social_publisher_container.py -q`

Expected: FAIL because grants and Nginx authorization are absent.

- [ ] **Step 3: Implement grant storage and authorization**

Store only a hash of the random grant token. Bind grants to the administrator device cookie fingerprint, desktop type, login session or publish job, expiry, and bounded reconnect count. Consume or increment the grant atomically.

- [ ] **Step 4: Protect Nginx HTTP and WebSocket upgrades**

Add an `internal` auth location that forwards cookies and grant headers to the orchestrator. Add `auth_request` to both desktop locations before proxying. Keep WebSocket upgrade headers only after authorization succeeds.

- [ ] **Step 5: Replace noVNC passwordless startup**

Generate a random VNC password into a mode-0700 tmpfs file, start x11vnc with that password, and pass the scoped grant through the browser URL without exposing the VNC password.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/test_desktop_grants.py tests/test_social_publisher_container.py -q` and `cd src/web && npm run build`.

```bash
git add src/videoroll/apps/orchestrator_api/desktop_grants.py src/videoroll/apps/orchestrator_api/routers/desktop.py src/videoroll/apps/orchestrator_api/app.py src/videoroll/apps/orchestrator_api/middleware.py src/web/nginx.conf docker/social-publisher-entrypoint.sh src/web/src/pages tests/test_desktop_grants.py tests/test_social_publisher_container.py
git commit -m "feat: protect social automation desktops"
```

### Task 5: Replace Remote API Query Tokens With Idempotent POST Requests

**Files:**
- Modify: `src/videoroll/apps/orchestrator_api/routers/youtube.py`
- Modify: `src/videoroll/apps/orchestrator_api/schemas.py`
- Create: `src/videoroll/apps/orchestrator_api/remote_api.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/youtube_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/remote_api_settings_store.py`
- Modify: `src/web/src/pages/SettingsApiPage.tsx`
- Modify: `docs/REMOTE_API.md`
- Modify: `tests/test_auto_youtube_pipeline.py`
- Create: `tests/test_remote_api_idempotency.py`

**Interfaces:**
- `RemoteAutoYouTubeRequest` carries `url`, `license`, `proof_url`, and `auto_publish` in JSON.
- `authenticate_remote_request(request, db) -> RemotePrincipal` accepts only Bearer auth.
- `accept_remote_request(principal, idempotency_key, payload, db) -> RemoteAPIResponse` performs request hashing and durable dispatch.

- [ ] **Step 1: Write failing contract and replay tests**

```python
def test_remote_get_and_query_token_are_rejected(client):
    assert client.get("/remote/auto/youtube?token=secret&url=https://youtube.com/watch?v=x").status_code == 405

def test_same_idempotency_key_returns_one_pipeline(monkeypatch):
    first = submit_remote("key-1", {"url": YOUTUBE_URL})
    second = submit_remote("key-1", {"url": YOUTUBE_URL})
    assert first.task_id == second.task_id
    assert dispatch.call_count == 1

def test_same_key_with_different_payload_is_conflict():
    submit_remote("key-1", {"url": YOUTUBE_URL})
    assert submit_remote("key-1", {"url": OTHER_URL}).status_code == 409
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_remote_api_idempotency.py tests/test_auto_youtube_pipeline.py -q`

Expected: FAIL because the route accepts GET/query tokens and has no durable idempotency record.

- [ ] **Step 3: Implement Bearer authentication, request hashing, and quotas**

Use constant-time token verification. Store only token identity hash, request hash, response, task ID, dispatch state, and expiry. Add Redis counters for token/IP rate limits and active pipeline count.

- [ ] **Step 4: Make duplicate YouTube ingestion side-effect free**

Change `start_auto_youtube_pipeline()` so `deduped=True` returns the existing task without enqueuing a second pipeline. Add a separate authenticated restart route for deliberate reprocessing.

- [ ] **Step 5: Update docs/UI and return 410 for removed query contract**

Update generated curl examples to JSON plus Bearer. Do not print the token in the generated URL. The old GET route returns 410 with a migration detail for one release.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/test_remote_api_idempotency.py tests/test_auto_youtube_pipeline.py -q`.

```bash
git add src/videoroll/apps/orchestrator_api/routers/youtube.py src/videoroll/apps/orchestrator_api/schemas.py src/videoroll/apps/orchestrator_api/remote_api.py src/videoroll/apps/orchestrator_api/services/youtube_service.py src/videoroll/apps/orchestrator_api/remote_api_settings_store.py src/web/src/pages/SettingsApiPage.tsx docs/REMOTE_API.md tests/test_auto_youtube_pipeline.py tests/test_remote_api_idempotency.py
git commit -m "feat: make remote API idempotent and bearer-only"
```

### Task 6: Build Durable Outbox, Inbox, And Dispatcher

**Files:**
- Create: `src/videoroll/apps/outbox/__init__.py`
- Create: `src/videoroll/apps/outbox/service.py`
- Create: `src/videoroll/apps/outbox/dispatcher.py`
- Create: `src/videoroll/apps/outbox/worker_inbox.py`
- Modify: `src/videoroll/apps/subtitle_service/worker.py`
- Modify: `src/videoroll/apps/bilibili_publisher/worker.py`
- Modify: `src/videoroll/apps/social_publisher/worker.py`
- Modify: `src/videoroll/apps/publish_lifecycle.py`
- Create: `tests/test_outbox.py`
- Create: `tests/test_worker_inbox.py`

**Interfaces:**
- `create_outbox_event(db, event_type, aggregate_type, aggregate_id, task_name, args, operation_key) -> OutboxEvent`.
- `claim_outbox_events(db, owner, limit, now) -> list[OutboxEvent]`.
- `mark_outbox_dispatched(db, event_id, broker_id) -> None`.
- `claim_operation(db, operation_key, owner, lease_seconds) -> OperationClaim`.
- `finish_operation(db, operation_key, result_json) -> None`.

- [ ] **Step 1: Write failing outbox/inbox tests**

```python
def test_domain_commit_and_outbox_event_are_atomic(db):
    create_publish_operation(db, task_id, operation_key="task:batch:bilibili")
    assert db.query(OutboxEvent).filter_by(operation_key="task:batch:bilibili").one()

def test_expired_dispatch_lease_can_be_reclaimed(db):
    event = pending_event(lease_until=utcnow() - timedelta(seconds=1))
    claimed = claim_outbox_events(db, owner="new", limit=1, now=utcnow())
    assert claimed == [event]

def test_duplicate_operation_delivery_returns_stored_result(db):
    first = claim_operation(db, "op-1", "worker-a", lease_seconds=60)
    finish_operation(db, "op-1", {"external_id": "post-1"})
    second = claim_operation(db, "op-1", "worker-b", lease_seconds=60)
    assert second.result_json == {"external_id": "post-1"}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_outbox.py tests/test_worker_inbox.py -q`

Expected: FAIL because no outbox/inbox service exists.

- [ ] **Step 3: Implement transactional event creation and claiming**

Use `SELECT ... FOR UPDATE SKIP LOCKED`, bounded leases, exponential retry timestamps, and unique operation keys. Ensure broker failures leave events in `pending` or `failed` with a retryable error.

- [ ] **Step 4: Implement dispatcher and worker inbox helpers**

The dispatcher sends Celery task name plus event ID. Workers claim the inbox operation before external side effects and heartbeat long operations. A duplicate event returns the persisted result or exits while the first lease is live.

- [ ] **Step 5: Convert cleanup and selected publish dispatches**

Route publish cleanup, Bilibili publish, social publish, and after-render publish through outbox events. Keep the existing task names as consumers so queue topology stays compatible.

- [ ] **Step 6: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/test_outbox.py tests/test_worker_inbox.py tests/test_publish_cleanup.py tests/test_publish_idempotency.py -q`.

```bash
git add src/videoroll/apps/outbox src/videoroll/apps/subtitle_service/worker.py src/videoroll/apps/bilibili_publisher/worker.py src/videoroll/apps/social_publisher/worker.py src/videoroll/apps/publish_lifecycle.py tests/test_outbox.py tests/test_worker_inbox.py
git commit -m "feat: add durable outbox and worker inbox"
```

### Task 7: Replace Global Worker Recovery With Lease-Based Recovery

**Files:**
- Modify: `src/videoroll/apps/subtitle_service/worker.py`
- Modify: `src/videoroll/apps/subtitle_service/worker_concurrency.py`
- Modify: `src/videoroll/apps/bilibili_publisher/worker.py`
- Modify: `src/videoroll/apps/social_publisher/worker.py`
- Modify: `src/videoroll/apps/orchestrator_api/infrastructure/scheduler.py`
- Create: `tests/test_worker_lease_recovery.py`

**Interfaces:**
- `acquire_job_lease(db, job, owner, ttl_seconds) -> bool`.
- `heartbeat_job_lease(db, job_id, owner, ttl_seconds) -> bool`.
- `recover_expired_leases(db, now, limit) -> RecoverySummary`.

- [ ] **Step 1: Write failing recovery tests**

```python
def test_live_subtitle_lease_is_not_requeued_on_worker_start(db):
    job = running_job(lease_owner="worker-a", lease_until=utcnow() + timedelta(minutes=2))
    recover_expired_leases(db, now=utcnow(), limit=100)
    assert job.status == SubtitleJobStatus.running

def test_expired_render_lease_is_requeued_with_resume(db):
    job = running_render(lease_owner="dead-worker", lease_until=utcnow() - timedelta(seconds=1))
    recover_expired_leases(db, now=utcnow(), limit=100)
    assert job.status == RenderJobStatus.queued
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_worker_lease_recovery.py -q`

Expected: FAIL because worker startup still rewrites every `running` row.

- [ ] **Step 3: Remove worker-init global rewrites**

`worker_init` may initialize runtime and enqueue a scheduler tick, but it must not call functions that rewrite all running rows. Move recovery into the scheduler/outbox repair path.

- [ ] **Step 4: Add lease acquisition and heartbeats**

Claim the task/job row atomically before work, heartbeat before lease expiry, and release the lease on success/failure. A lost heartbeat leaves the row recoverable after expiry.

- [ ] **Step 5: Run concurrency tests and commit**

Run: `.venv/bin/python -m pytest tests/test_worker_lease_recovery.py tests/test_worker_concurrency.py -q`.

```bash
git add src/videoroll/apps/subtitle_service/worker.py src/videoroll/apps/subtitle_service/worker_concurrency.py src/videoroll/apps/bilibili_publisher/worker.py src/videoroll/apps/social_publisher/worker.py src/videoroll/apps/orchestrator_api/infrastructure/scheduler.py tests/test_worker_lease_recovery.py
git commit -m "fix: recover jobs by expired leases"
```

### Task 8: Harden Publish Lifecycle And Broker Failure Recovery

**Files:**
- Modify: `src/videoroll/apps/publish_service.py`
- Modify: `src/videoroll/apps/publish_lifecycle.py`
- Modify: `src/videoroll/apps/bilibili_publisher/main.py`
- Modify: `src/videoroll/apps/bilibili_publisher/worker.py`
- Modify: `src/videoroll/apps/social_publisher/main.py`
- Modify: `src/videoroll/apps/social_publisher/worker.py`
- Modify: `src/videoroll/db/models.py`
- Create: `tests/test_publish_dispatch_recovery.py`
- Modify: `tests/test_publish_lifecycle.py`

**Interfaces:**
- `create_or_reuse_publish_batch(db, task_id, targets, payload) -> PublishBatch` executes under one task-row lock and rechecks the active pointer before creation.
- `repair_stale_publish_jobs(db, now, limit) -> RepairSummary` redispatches jobs with no worker start heartbeat and marks externally-started stale jobs unknown.
- `publish_operation_key(task_id, batch_id, platform, account_id) -> str` is unique and stable.

- [ ] **Step 1: Write failing race and broker failure tests**

```python
def test_two_concurrent_completed_batch_retries_create_one_replacement(db):
    first, second = run_concurrent_publish_selection(db, task_id)
    assert first.id == second.id

def test_broker_failure_does_not_leave_unrecoverable_submitting_job(db):
    job = create_submitting_job(db, started_at=None)
    repair_stale_publish_jobs(db, now=job.created_at + timedelta(minutes=10), limit=10)
    assert job.state in {PublishState.failed, PublishState.submitting}
    assert job.operation_key is not None
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_publish_dispatch_recovery.py tests/test_publish_lifecycle.py -q`

Expected: FAIL because batch replacement and stale submitting repair are not atomic/durable.

- [ ] **Step 3: Make batch replacement one transaction**

Keep the existing active pointer but perform selection, compatibility check, and replacement creation while holding the task row lock. Add a unique partial index or equivalent transaction guard for one active batch per task.

- [ ] **Step 4: Add publish outbox/inbox and stale job repair**

Commit the job and dispatch event together. On broker outage, the outbox remains pending. A job with `started_at is None` is safe to redispatch; a job with a prior heartbeat becomes `unknown` and requires explicit operator retry.

- [ ] **Step 5: Make Redis locks non-authoritative**

Redis lock failure must not cause duplicate work or silently disable protection. Use database operation leases for correctness and Redis only for stage throttling.

- [ ] **Step 6: Run publish suite and commit**

Run: `.venv/bin/python -m pytest tests/test_publish_dispatch_recovery.py tests/test_publish_lifecycle.py tests/test_publish_service.py tests/test_bilibili_publish_idempotency.py tests/test_social_publisher_worker.py -q`.

```bash
git add src/videoroll/apps/publish_service.py src/videoroll/apps/publish_lifecycle.py src/videoroll/apps/bilibili_publisher src/videoroll/apps/social_publisher src/videoroll/db/models.py tests/test_publish_dispatch_recovery.py tests/test_publish_lifecycle.py
git commit -m "fix: make publishing dispatch recoverable"
```

### Task 9: Add Egress Gateway And DNS-Rebinding Defense

**Files:**
- Create: `src/videoroll/apps/egress_gateway/__init__.py`
- Create: `src/videoroll/apps/egress_gateway/client.py`
- Create: `src/videoroll/apps/egress_gateway/main.py`
- Modify: `src/videoroll/apps/subtitle_service/rag.py`
- Modify: `compose.yml`
- Modify: `docker-compose.yml`
- Create: `tests/test_egress_gateway.py`
- Modify: `tests/test_translation_rag.py`

**Interfaces:**
- `resolve_public_endpoint(url) -> ResolvedEndpoint` returns hostname, port, verified IP, and SNI name.
- `fetch_public(url, timeout, max_bytes, redirects) -> EgressResponse` performs the request through the fixed endpoint.
- RAG calls `fetch_public()` and never calls `httpx.Client.get()` directly for arbitrary URLs.

- [ ] **Step 1: Write failing DNS, redirect, and size-limit tests**

```python
def test_dns_resolution_failure_is_denied(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    with pytest.raises(EgressDenied):
        resolve_public_endpoint("https://example.test/page")

def test_private_redirect_is_denied(fake_resolver):
    with pytest.raises(EgressDenied):
        fetch_public("https://public.test/redirect-to-private")

def test_response_body_is_bounded(public_server):
    response = fetch_public(public_server.url, max_bytes=1024)
    assert response.truncated is True
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_egress_gateway.py tests/test_translation_rag.py -q`

Expected: FAIL because RAG currently resolves and connects independently.

- [ ] **Step 3: Implement fixed-endpoint transport**

Resolve once, reject any non-global address, and use a custom httpcore transport that connects to the verified IP while preserving the original Host header and TLS SNI. Verify the final socket peer before reading the body. Reject DNS errors, userinfo, unsupported schemes, private ports/ranges, and unsafe redirects.

- [ ] **Step 4: Add gateway process and private networking**

Run the egress service on the private Compose network. Remove direct arbitrary outbound access from the subtitle service container where deployment permits. Expose only a bounded internal endpoint authenticated with the service token.

- [ ] **Step 5: Migrate RAG and run tests**

Run: `.venv/bin/python -m pytest tests/test_egress_gateway.py tests/test_translation_rag.py -q`.

```bash
git add src/videoroll/apps/egress_gateway src/videoroll/apps/subtitle_service/rag.py compose.yml docker-compose.yml tests/test_egress_gateway.py tests/test_translation_rag.py
git commit -m "feat: route rag fetches through verified egress"
```

### Task 10: Harden Upload And Asset Streaming Boundaries

**Files:**
- Create: `src/videoroll/apps/orchestrator_api/services/image_validation.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/asset_service.py`
- Modify: `src/videoroll/apps/orchestrator_api/routers/assets.py`
- Modify: `pyproject.toml`
- Create: `tests/test_upload_content_validation.py`
- Modify: `tests/test_orchestrator_asset_service.py`

**Interfaces:**
- `validate_and_reencode_cover(file_obj) -> ValidatedImage` accepts JPEG/PNG/WebP only.
- `safe_asset_headers(asset, content_type, inline) -> dict[str, str]` adds `nosniff` and safe disposition.

- [ ] **Step 1: Write failing SVG and header tests**

```python
def test_svg_cover_is_rejected():
    with pytest.raises(HTTPException):
        validate_and_reencode_cover(io.BytesIO(b"<svg/onload=alert(1)>") )

def test_inline_asset_has_nosniff_and_safe_type():
    headers = safe_asset_headers(cover_asset, "image/svg+xml", inline=True)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Disposition"].startswith("attachment")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py -q`

Expected: FAIL because MIME prefix checks permit SVG and stream headers trust stored content type.

- [ ] **Step 3: Add trusted image decoding and canonical storage**

Add Pillow with an upper bound compatible with Python 3.12. Decode only JPEG, PNG, or WebP, strip metadata, re-encode to the canonical format, and store the canonical content type and extension. Reject oversized dimensions and decompression bombs.

- [ ] **Step 4: Harden download/stream headers**

Add `X-Content-Type-Options: nosniff`; use attachment disposition for images and text unless the asset kind is explicitly safe-listed for inline playback. Never return arbitrary stored content types for active content.

- [ ] **Step 5: Run upload tests and commit**

Run: `.venv/bin/python -m pytest tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py tests/test_upload_guardrails.py -q`.

```bash
git add src/videoroll/apps/orchestrator_api/services/image_validation.py src/videoroll/apps/orchestrator_api/services/asset_service.py src/videoroll/apps/orchestrator_api/routers/assets.py pyproject.toml tests/test_upload_content_validation.py tests/test_orchestrator_asset_service.py tests/test_upload_guardrails.py
git commit -m "fix: validate uploaded images and asset headers"
```

### Task 11: Update Frontend State And API Contracts

**Files:**
- Modify: `src/web/src/lib/urls.ts`
- Modify: `src/web/src/lib/http.ts`
- Modify: `src/web/src/lib/types.ts`
- Modify: `src/web/src/pages/taskDetailPage.helpers.ts`
- Modify: `src/web/src/pages/TaskDetailPage.tsx`
- Modify: `src/web/src/pages/SettingsPublishPage.tsx`
- Modify: `src/web/src/pages/SettingsApiPage.tsx`
- Modify: `src/web/src/pages/KnowledgeBasePage.tsx`
- Modify: `src/web/src/pages/DashboardPage.tsx`
- Modify: `src/web/src/pages/TaskNewPage.tsx`
- Modify: `src/web/src/App.tsx`
- Modify: `src/web/src/pages/taskDetailPage.helpers.test.ts`
- Modify: `src/web/src/pages/settingsPublishPage.helpers.test.ts`
- Create: `src/web/src/lib/requestState.ts`
- Create: `src/web/src/lib/requestState.test.ts`

**Interfaces:**
- `loadSlices(coreLoader, optionalLoaders) -> { core, optional, errors }` updates slices independently.
- `DirtyFieldState<T>` tracks server version, local value, dirty status, and conflict response.
- All publish payloads include `force_retry` consistently.

- [ ] **Step 1: Write failing frontend tests**

```typescript
it("includes force_retry for bilibili retries", () => {
  expect(buildPublishActionPayload({ ...args, platform: "bilibili", forceRetry: true })).toMatchObject({ force_retry: true });
});

it("keeps core task data when optional publishing load fails", async () => {
  const state = await loadSlices(loadCore, [rejectingPublisherSlice]);
  expect(state.core.task.id).toBe("task-1");
  expect(state.errors.publisher).toBeTruthy();
});

it("does not replace dirty metadata from a background refresh", () => {
  const state = applyServerValue({ value: "local", dirty: true }, "server");
  expect(state.value).toBe("local");
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd src/web && npm run test -- src/lib/requestState.test.ts src/pages/taskDetailPage.helpers.test.ts`

Expected: FAIL because Bilibili payloads, slice loading, and dirty-state helpers do not implement these behaviors.

- [ ] **Step 3: Implement request slices and dirty-state helpers**

Split TaskDetail refresh into core and optional loaders. Preserve last successful optional values and show per-slice errors. Add server version/conflict handling to publish settings. Add an explicit fallback route for unknown URLs.

- [ ] **Step 4: Update Remote API UI and deep links**

Generate POST JSON plus Bearer examples without putting tokens in URLs. Make Dashboard knowledge-item links consume `item` query state. Add request generation or `AbortController` protection to KnowledgeBase filtering and pagination.

- [ ] **Step 5: Run frontend tests, lint, and build**

Run: `cd src/web && npm run test && npm run lint && npm run build`.

- [ ] **Step 6: Commit**

```bash
git add src/web/src
git commit -m "fix: harden frontend api and editing state"
```

### Task 12: Compose Process Separation And Deployment Guardrails

**Files:**
- Modify: `compose.yml`
- Modify: `docker-compose.yml`
- Modify: `Dockerfile`
- Modify: `docker/entrypoint.sh`
- Modify: `docker/social-publisher.Dockerfile`
- Modify: `docker/social-publisher-entrypoint.sh`
- Modify: `.env.example`
- Modify: `scripts/dev_up.sh`
- Modify: `src/web/nginx.conf`
- Modify: `pyproject.toml`
- Create: `tests/test_deployment_security.py`

**Interfaces:**
- Separate Compose services for `orchestrator`, `outbox-dispatcher`, `subtitle-worker`, `publish-worker`, and `egress-gateway`.
- No host `ports:` entries for internal APIs, workers, Redis, or MinIO API.
- Development startup generates non-default local secrets and writes them only to `.env`.

- [ ] **Step 1: Write failing deployment tests**

```python
def test_production_compose_has_no_internal_service_host_ports():
    compose = yaml.safe_load(Path("compose.yml").read_text())
    for name in ("app", "social-publisher-api", "social-publisher-worker", "redis"):
        assert not compose["services"][name].get("ports")

def test_default_secrets_are_not_accepted_outside_dev_mode():
    assert validate_deployment_secrets({"S3_SECRET_ACCESS_KEY": "videorollsecret", "INTERNAL_API_SECRET": ""}, production=True) is False
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_deployment_security.py -q`

Expected: FAIL because Compose and startup currently permit weak defaults and combined process roles.

- [ ] **Step 3: Split process roles and add health checks**

Run Uvicorn, outbox dispatcher, subtitle worker, and publisher worker as independent services with explicit health checks and restart policies. Keep shared environment and private network aliases stable.

Add `PyYAML>=6,<7` to the development/test dependency set so `tests/test_deployment_security.py` parses Compose files structurally rather than matching strings.

- [ ] **Step 4: Remove weak production defaults**

Require explicit `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `INTERNAL_API_SECRET` in production. Make `scripts/dev_up.sh` generate random development values when `.env` is absent. Bind MinIO console and Web only to the configured publish address; never publish the MinIO API port.

- [ ] **Step 5: Apply container least privilege**

Run application and social services as their existing non-root users, make secret and cookie directories mode 0700, and keep VNC password files in tmpfs.

- [ ] **Step 6: Run deployment tests and commit**

Run: `.venv/bin/python -m pytest tests/test_deployment_security.py tests/test_social_publisher_container.py -q`.

```bash
git add compose.yml docker-compose.yml Dockerfile docker/entrypoint.sh docker/social-publisher.Dockerfile docker/social-publisher-entrypoint.sh .env.example scripts/dev_up.sh src/web/nginx.conf pyproject.toml tests/test_deployment_security.py
git commit -m "chore: harden deployment process boundaries"
```

### Task 13: Migration, Compatibility, And End-to-End Verification

**Files:**
- Modify: `docs/REMOTE_API.md`
- Modify: `docs/DEVELOPER_GUIDE.md`
- Modify: `docs/SECURITY_AUDIT.md`
- Create: `tests/test_security_rollout.py`
- Create: `scripts/security_smoke.sh`

**Interfaces:**
- `security_smoke.sh` runs authenticated gateway, service-token, Remote API, desktop grant, outbox retry, and egress denial checks against a local Compose stack.
- `tests/test_security_rollout.py` verifies old data migration, feature flags, and removal of unsafe routes.

- [ ] **Step 1: Write failing rollout tests**

```python
def test_legacy_publish_rows_are_backfilled_without_duplicate_batches(migrated_db):
    assert count_active_batches(migrated_db, task_id) <= 1

def test_removed_remote_query_contract_returns_410(client):
    assert client.get("/remote/auto/youtube?token=x").status_code == 410
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_security_rollout.py -q`

Expected: FAIL until migrations, compatibility responses, and feature flags are implemented.

- [ ] **Step 3: Implement staged migration and rollback flags**

Deploy schema first, run report-only service-auth checks, then enforce service auth, outbox dispatch, egress routing, desktop grants, and Remote API contract in that order. Keep additive columns and tables during rollback; never restore query-token or unauthenticated desktop behavior.

- [ ] **Step 4: Add security smoke script and documentation**

Document environment variables, private network requirements, outbox repair commands, Remote API migration, desktop grant flow, and rollback limits. The smoke script must fail on any unauthenticated internal endpoint, exposed service port, URL token, or private egress response.

- [ ] **Step 5: Run the complete verification matrix**

Run:

```bash
.venv/bin/python -m pytest tests/ -q
cd src/web && npm run lint && npm run test && npm run build
cd ../..
./scripts/security_smoke.sh
git diff --check
```

Expected: all tests pass; smoke checks report no exposed internal service, no unauthenticated desktop, no query credentials, no duplicate outbox delivery, and no private egress access.

- [ ] **Step 6: Commit final rollout documentation**

```bash
git add docs/REMOTE_API.md docs/DEVELOPER_GUIDE.md docs/SECURITY_AUDIT.md tests/test_security_rollout.py scripts/security_smoke.sh
git commit -m "docs: document security rollout and verification"
```

## Plan Self-Review

- Schema work precedes any code path that reads new outbox, inbox, grant, or lease fields.
- Service authentication precedes removal of monolith mounts and direct frontend URLs.
- Outbox/inbox primitives precede worker recovery and publisher dispatch conversion.
- Egress tests cover DNS failure, private addresses, redirects, fixed peer connections, and response limits.
- Frontend tests cover the previously observed Bilibili retry, optional request failure, dirty editor overwrite, deep-link, and request ordering failures.
- Deployment tests cover noVNC passwordless startup, host port exposure, weak secrets, and process separation.
- No task requires a placeholder or an undefined function from a later task.
- Each task has a focused test command and a commit boundary.
