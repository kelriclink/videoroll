# VideoRoll Security Architecture Redesign

## Status

Approved direction: complete security and reliability redesign rather than isolated patches.

This design replaces implicit trust between the browser, monolith-mounted apps, standalone services, Celery workers, browser automation desktops, and outbound fetchers with explicit authenticated boundaries and durable state transitions.

## Goals

- Expose exactly one browser-facing HTTP boundary.
- Require authenticated service identity for every internal API request.
- Protect noVNC login and publishing desktops with short-lived, scoped access grants.
- Make Remote API requests authenticated, rate-limited, idempotent, and free of URL credentials.
- Make all external side effects dispatch through durable outbox records.
- Recover worker jobs by lease ownership and expiry, never by global status rewrites.
- Prevent RAG fetches from reaching private networks, including DNS rebinding cases.
- Preserve user edits and core task workflows when optional publishing dependencies fail.
- Provide migration and rollback paths for existing tasks, jobs, batches, and deployments.

## Non-Goals

- Replacing PostgreSQL, Redis, MinIO, Celery, FastAPI, React, or Nginx.
- Adding multi-user roles or tenant isolation.
- Rewriting platform publisher implementations or the subtitle pipeline.
- Supporting public direct access to subtitle, ingest, Bilibili, or social publisher services.
- Preserving the unsafe Remote API GET and query-token contract.

## Trust Model

### Browser boundary

The browser communicates only with the Web Nginx service. Nginx serves the React application and proxies `/api/` to the orchestrator. Frontend build variables for direct subtitle, ingest, and Bilibili service URLs are removed. Browser code uses orchestrator routes exclusively.

The administrator device cookie remains the browser session credential. All browser-visible API, download, stream, noVNC, and WebSocket requests must pass the orchestrator session check.

### Internal service boundary

Subtitle, YouTube ingest, Bilibili publisher, and social publisher APIs reject all non-health requests unless they carry a valid internal service token. Internal tokens are configured through a dedicated `INTERNAL_API_SECRET`, not derived from S3 credentials.

The orchestrator validates the administrator session, then performs server-side calls to internal services with the internal token. It does not expose the token to the browser. The monolith no longer relies on mounting unauthenticated child FastAPI apps for browser access. Compatibility mounts are removed after the orchestrator has proxy routes for every required operation.

Internal service containers remain on a private Compose network and publish no host ports. Startup validation rejects an empty or known default internal secret outside explicit development mode.

### Automation desktop boundary

Nginx does not proxy noVNC paths directly. The orchestrator creates a short-lived desktop access grant bound to:

- administrator session fingerprint;
- desktop type (`login` or `publish`);
- login session or publish job identifier;
- random one-time token;
- expiry timestamp;
- single-use or bounded reconnect count.

The browser requests a grant from an authenticated orchestrator endpoint. Nginx uses `auth_request` against an orchestrator authorization endpoint before proxying noVNC HTTP or WebSocket traffic. The authorization endpoint validates both the administrator cookie and the scoped grant. x11vnc also uses a generated per-container password stored in tmpfs rather than `-nopw`.

## Authentication And Abuse Controls

### Administrator bootstrap

`/auth/setup` is disabled by default. Initial setup requires a one-time bootstrap secret supplied through `ADMIN_BOOTSTRAP_SECRET` or a generated local bootstrap file readable only on the host. Successful setup atomically consumes the secret and records completion. Concurrent setup attempts serialize on the `admin.auth` settings row.

### Login throttling

Login and setup attempts use Redis-backed rate limits keyed by normalized client IP and endpoint. Limits use a short burst window plus exponential lockout after repeated failures. Nginx applies a coarse request limit as defense in depth. Audit events record success, failure category, source IP, and user agent without recording passwords or tokens.

### Remote API

The Remote API contract becomes:

```text
POST /api/remote/auto/youtube
Authorization: Bearer <token>
Idempotency-Key: <caller-generated key>
Content-Type: application/json
```

Query tokens and GET side effects are removed. The request body carries URL, license, proof URL, and auto-publish options. A database idempotency record stores the token identity hash, idempotency key, request hash, task ID, pipeline dispatch state, response, and expiry.

Reusing a key with the same request returns the stored response. Reusing it with different content returns HTTP 409. Repeating the same YouTube URL with a different key returns the existing task and does not enqueue another active pipeline unless an explicit authenticated restart endpoint is used.

Remote API limits apply per token identity and source IP. Limits cover requests per minute, active pipelines, and daily accepted tasks.

## Durable Async Side Effects

### Outbox model

All Celery dispatches that represent durable application state use a PostgreSQL outbox table. An outbox row contains:

- unique event ID and event type;
- aggregate type and aggregate ID;
- queue and task name;
- JSON arguments;
- idempotency key;
- state (`pending`, `dispatching`, `dispatched`, `failed`);
- attempt count, next attempt time, lease owner, and lease expiry;
- timestamps and bounded error detail.

Business transactions create or update the domain row and insert the outbox event in the same database transaction. A dispatcher process claims pending rows with `FOR UPDATE SKIP LOCKED`, sends Celery tasks, and records delivery. Broker errors leave rows retryable. A periodic repair pass releases expired dispatcher leases.

### Worker inbox and leases

Worker tasks carry the outbox event ID. Before external side effects, the worker claims an inbox/operation lease using a unique operation key. Duplicate Celery deliveries return the stored result or observe the active lease instead of repeating work.

Long-running subtitle, render, Bilibili, and social publish jobs maintain database lease owner, lease expiry, and heartbeat timestamps. Recovery considers only expired leases. Worker startup schedules recovery but never changes every `running` row globally.

### Publish lifecycle

`Task.active_publish_batch_id` remains the authority for the current publishing attempt. Batch selection and replacement occur under one task-row transaction. A database invariant prevents two active batches for one task.

Bilibili and social publishing use operation keys derived from task, batch, platform, and account. Publisher APIs validate batch ownership and insert the publish job plus outbox event atomically. A job with no worker start heartbeat remains safely redispatchable. A job whose worker lease expired after an external request started becomes `unknown`, requiring explicit administrator confirmation before retry.

Redis locks remain optional throughput controls, not correctness controls. Redis failure cannot cause duplicate side effects or permanently disable recovery.

Cleanup dispatch also uses the shared outbox. Cleanup completion, not message acceptance, records the terminal cleanup marker.

## Outbound Network Security

### Egress gateway

RAG search and page fetches move behind a dedicated egress client service or sidecar. Application containers cannot directly reach arbitrary external addresses. The egress component enforces:

- HTTP and HTTPS only;
- no URL userinfo;
- normalized host and port rules;
- DNS resolution through a controlled resolver;
- rejection if any answer is private, loopback, link-local, multicast, reserved, or otherwise non-global;
- connection to the exact validated IP while preserving the original Host header and TLS SNI;
- final socket peer verification;
- redirect validation on every hop;
- response size, content type, redirect count, and timeout limits;
- denial of cloud metadata ranges and configured private networks;
- structured audit logs without response-body secrets.

DNS resolution failure is deny-by-default. The application receives a bounded fetch result and never performs a second independent DNS lookup.

### Upload and inline content

Cover uploads accept only decoded raster JPEG, PNG, or WebP images. The server verifies magic bytes and image structure, rewrites metadata through a trusted image decoder, assigns a canonical content type, and rejects SVG or ambiguous content.

Asset streaming adds `X-Content-Type-Options: nosniff`. Non-video and non-audio assets use attachment disposition unless explicitly safe-listed.

## Frontend State Model

### API boundary

The frontend uses orchestrator-only URLs. Internal service URL build variables and direct-service fetches are removed. Orchestrator endpoints provide the settings and actions currently read from mounted child services.

### Task detail loading

Task, assets, and subtitle jobs form the core task snapshot. Publishing batches, jobs, review state, accounts, and platform settings are optional slices. Core and optional slices load independently, preserve their last successful values, and expose separate errors. A publishing outage cannot block upload, subtitle, or asset workflows.

### Publish retry

All platform payload builders include `force_retry` consistently. Unknown publish states require the existing explicit confirmation dialog. Failed states may retry directly within the active batch.

### Editable settings

Settings editors track server version, local dirty state, and save state. Background account polling updates account status only. It never replaces dirty metadata text. Saving with a stale server version returns a conflict and lets the user reload or overwrite explicitly.

## Data Model Changes

New tables:

- `outbox_events` for durable dispatch;
- `operation_inbox` for worker-side idempotency and results;
- `remote_api_requests` for Remote API idempotency and quotas;
- `desktop_access_grants` for scoped noVNC access;
- `security_audit_events` for bounded authentication and security events.

New or revised columns:

- job lease owner, lease expiry, heartbeat, and operation key;
- publish batch active invariant support;
- settings row version for optimistic frontend updates;
- cleanup completion timestamp distinct from dispatch timestamps.

Schema changes use explicit versioned migrations. `auto_migrate.py` remains only for legacy compatibility checks and must not create the new security-critical schema.

## Service Layout

- `orchestrator_api`: browser API, administrator authentication, Remote API, internal service clients, desktop grants, and API aggregation.
- `subtitle_service`: internal API and workers only.
- `youtube_ingest`: internal API only.
- `bilibili_publisher`: internal API and worker only.
- `social_publisher`: internal API, login manager, and worker only.
- `outbox_dispatcher`: claims and dispatches durable events.
- `egress_gateway`: performs validated public fetches.
- `web`: static UI, authenticated reverse proxy, and noVNC authorization gate.

The existing single app container may continue to package Python code, but process roles are started separately so a failure in Uvicorn, a dispatcher, or a worker does not terminate unrelated roles.

## Error Handling And Observability

- API errors return stable codes suitable for frontend branching.
- Internal service errors do not expose cookies, tokens, proxy credentials, or full remote bodies.
- Outbox, inbox, lease, Remote API, desktop grant, and egress decisions emit structured logs with correlation IDs.
- Metrics cover pending outbox age, lease expirations, duplicate deliveries, Remote API throttles, authentication failures, denied egress destinations, and active desktop grants.
- Security audit retention is bounded and configurable.

## Migration And Rollout

1. Add versioned schema migrations and deploy them before new code paths.
2. Add internal service authentication in report-only mode and verify all orchestrator calls carry tokens.
3. Route frontend child-service operations through orchestrator endpoints, then remove direct-service build variables.
4. Enable enforced internal authentication and private network-only service exposure.
5. Deploy outbox dispatcher and dual-write selected Celery dispatches while retaining current dispatch as a monitored fallback.
6. Move subtitle/render recovery and publisher dispatches to outbox/inbox leases, then remove global startup recovery.
7. Deploy egress gateway and disable direct RAG internet access.
8. Enable authenticated noVNC grants and remove unauthenticated proxy paths.
9. Switch Remote API to POST Bearer plus idempotency. Return HTTP 410 for the removed GET contract during one release.
10. Remove compatibility paths and legacy auto-migration after operational verification.

Rollback keeps schema additions and disables new routing through feature flags. It never re-enables query-token authentication, unauthenticated noVNC, or direct public service ports.

## Testing Strategy

### Unit tests

- token verification, bootstrap consumption, rate-limit decisions;
- outbox claiming, retries, lease expiry, inbox dedupe;
- batch replacement and publish operation keys;
- Remote API request hashing and idempotency conflicts;
- desktop grant scope, expiry, and reuse limits;
- egress DNS, IP, redirect, peer, and size guardrails;
- frontend payload builders, slice reducers, and dirty-state conflict handling.

### Integration tests

- browser cookie to orchestrator to internal-token service flow;
- Nginx `auth_request` behavior for noVNC HTTP and WebSocket paths;
- domain transaction plus broker outage plus outbox recovery;
- duplicate Celery delivery with one external side effect;
- worker crash followed by expired-lease recovery;
- Remote API replay and quota enforcement;
- malicious DNS and redirect fixtures against the egress gateway;
- old database migration and rollout feature flags.

### End-to-end verification

- local upload, subtitle, render, multi-platform publish, cleanup;
- social login and headed Douyin publish through authenticated desktop grants;
- Remote API create, replay, conflict, throttle, and explicit restart;
- frontend degraded behavior while publisher services are unavailable.

## Acceptance Criteria

- No browser request can directly call an internal service.
- No unauthenticated user can open or control a noVNC desktop.
- No secret is accepted in a URL query parameter.
- Repeating an accepted Remote API request cannot enqueue duplicate active pipelines.
- Broker outages cannot lose durable dispatch intent.
- Duplicate worker deliveries cannot repeat a protected external side effect.
- Starting or restarting a worker cannot requeue a job with a live lease.
- RAG fetches cannot connect to private or rebinding destinations.
- A publishing dependency failure does not prevent core task details from loading.
- Background polling cannot overwrite unsaved settings edits.
- Backend, frontend, migration, integration, and security regression suites pass.
