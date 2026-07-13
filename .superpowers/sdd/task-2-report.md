# Task 2 Report: Internal Service Identity And Admin Abuse Controls

## Status

Complete. Implementation commit: `c2834d7` (`feat: enforce internal service identity`).

## RED / GREEN

- RED 1: `/mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_service_auth.py tests/test_admin_auth_controls.py -q`
  - Collection failed with `ModuleNotFoundError: videoroll.apps.security`, confirming the new service identity, bootstrap, limiter, and audit modules did not exist.
- GREEN 1: the focused Task 2 suite passed after the initial implementation (`7 passed`).
- RED 2: review regressions reproduced insecure implicit development mode, CORS preflight bypass, stale bootstrap ORM state, spoofable forwarded IPs, and missing threshold `429`/throttle audit semantics (`5 failed`).
- GREEN 2: all review regressions passed after the scoped fixes.

## Implementation

- Added versioned HMAC service tokens derived only from `INTERNAL_API_SECRET`.
- Added fail-closed runtime validation with explicit `DEVELOPMENT_MODE`; production defaults reject known internal/bootstrap secrets at service startup.
- Added internal authentication middleware to subtitle, YouTube ingest, Bilibili publisher, and social publisher. Only exact `/health` is exempt, and auth is outermost relative to CORS.
- Added administrator-session-to-internal-header injection in orchestrator middleware.
- Added one-time bootstrap-secret consumption under a refreshed `FOR UPDATE` lock on `admin.auth`, committed atomically with the password hash.
- Added Redis failure counters with expiry, endpoint plus normalized-IP keys, `429` and `Retry-After` responses, and explicit trusted-proxy opt-in.
- Added bounded, sensitive-field-filtered `SecurityAuditEvent` writes for setup/login success, failure, and throttle outcomes.

## Verification

Final command:

```text
/mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest \
  tests/test_service_auth.py \
  tests/test_admin_auth_controls.py \
  tests/test_orchestrator_architecture.py \
  tests/test_openvino_asr.py -q
```

Result: `29 passed, 6 warnings` (existing Pydantic v2 deprecation warnings).

Additional checks:

- Python `compileall` for all changed production modules: exit 0.
- Scoped `git diff --check`: exit 0.

## Concerns / Integration Dependencies

- Orchestrator outbound header generation must use `service_token(settings)`. The pre-Task-2 `infrastructure/internal_http.py` implementation derived from S3 credentials; that file is outside Task 2 and was explicitly reserved for the parallel integration task. The parent and Task 9 agent were notified.
- Existing Compose/development configuration must provide non-default `INTERNAL_API_SECRET` and `ADMIN_BOOTSTRAP_SECRET`, or explicitly set `DEVELOPMENT_MODE=true`. Current fail-closed behavior intentionally prevents startup with omitted production secrets; deployment configuration is owned by the later deployment-hardening task.
- `TRUSTED_PROXY_CIDRS` must explicitly enumerate the trusted Web/Nginx proxy networks before forwarded client IPs are used for rate-limit identity.
- The temporary worktree has no `.venv`; verification used the shared main-workspace virtual environment. Its Python 3.14 AnyIO threadpool hangs on sync TestClient fixtures, so Task 2 ASGI tests use async endpoints and `httpx.ASGITransport`.

## Review-Gap Closure (2026-07-13)

Review-fix commit: `874e198` (`fix: close internal auth review gaps`).

### Findings verified and fixed

- Trusted proxy handling now accepts forwarded addresses only when the immediate peer belongs to an explicit `TRUSTED_PROXY_CIDRS` network, then walks the chain from right to left and stops at the first untrusted hop. Public peers cannot spoof the rate-limit identity.
- Missing or non-boolean `development_mode` is fail-closed. Only the literal boolean `True` permits known development secrets.
- The bootstrap form requires a secret and sends it only in `X-Videoroll-Admin-Bootstrap`; it is not serialized into the JSON body.
- Authentication failures use a 60-second burst window followed by bounded exponential lockouts. Redis keys with TTL `-1` or another non-positive TTL are repaired with a finite expiry.
- Internal health exemption is normalized for mounted apps but remains exact: `/health` is exempt and `/health/` is authenticated.
- Orchestrator internal HTTP calls and subtitle worker callbacks derive their header from `INTERNAL_API_SECRET` via `service_token(settings)`, never from S3 credentials.
- Bootstrap consumption is exercised with two real SQLAlchemy sessions against SQLite, including a stale identity-map session, in addition to the row-lock refresh unit test.
- Four real internal FastAPI applications are exercised while mounted to prove their `/health` route remains reachable.

### Additional RED / GREEN

- RED: `PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest tests/test_service_auth.py -q`
  - `2 failed, 12 passed`: string `development_mode="false"` enabled insecure defaults, and `/health/` bypassed internal authentication with a `307` redirect.
- GREEN: the same command after the minimal fixes passed with `14 passed`.

### Final verification commands

```text
PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest \
  tests/test_service_auth.py \
  tests/test_admin_auth_controls.py \
  tests/test_orchestrator_architecture.py \
  tests/test_openvino_asr.py -q
```

Result: `43 passed, 14 warnings in 11.37s`. Warnings are existing Pydantic v2 and FastAPI `on_event` deprecations.

```text
cd src/web && npm run test -- --run src/components/AuthGate.helpers.test.ts
```

Result: `1 test passed`.

```text
cd src/web && npm run test
```

Result: `4 files passed, 11 tests passed`.

```text
cd src/web && npm run lint
```

Result: exit `0`.

```text
cd src/web && npm run build
```

Result: exit `0`; Vite built 62 modules successfully.

```text
PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m compileall -q \
  src/videoroll/apps/security \
  src/videoroll/apps/orchestrator_api/infrastructure/internal_http.py \
  src/videoroll/apps/orchestrator_api/infrastructure/lifecycle.py \
  src/videoroll/apps/orchestrator_api/services/auth_service.py \
  src/videoroll/apps/subtitle_service/worker.py \
  src/videoroll/config.py
```

Result: exit `0`.

```text
git diff --cached --check
```

Result: exit `0`.

### Self-review

- Staging contains only Task 2 production/tests/UI files. The `config.py` staging was split so Task 9's `EGRESS_GATEWAY_URL` hunk remains unstaged.
- No Compose, egress-gateway, RAG, translation-RAG, upload-hardening, or other parallel-task files are staged.
- No passwords, bootstrap values, cookies, bearer tokens, or S3 secrets are written to audit payloads or request bodies.
- Remaining deployment concerns are intentionally deferred: Compose/environment templates must supply secrets and trusted proxy CIDRs, and network/process separation remains part of later deployment tasks.

## Security Audit Follow-up (2026-07-13)

### TDD

- RED:

  ```text
  PYTHONPATH=src /mnt/d/kelric_soft/videoroll/.venv/bin/python -m pytest \
    tests/test_admin_auth_controls.py -q -k \
    'security_audit_event_keeps_only_allowlisted_counter_payload or security_audit_storage_drops_attacker_controlled_credentials or admin_auth_only_exempts_normalized_root_health_path'
  ```

  Result: `3 failed`. The persisted audit row retained attacker-provided bearer/password/cookie/token/secret values, arbitrary string payload data was accepted, and `/admin/private/health` bypassed administrator authentication.

- GREEN: the same focused command returned `3 passed` after the minimal fix.

### Closure

- Audit payloads now allow only integer `attempts` and `retry_after` counters; arbitrary payload strings are discarded rather than filtered by their key name.
- Client-controlled request IDs, user agents, and exception text are not persisted in `SecurityAuditEvent`; structured `error_code` and canonical source IP remain available for diagnosis.
- `AdminAuthMiddleware` normalizes the mount root and exempts only exact `/health`; a nested route ending in `/health` remains authenticated.
- The regression writes an actual SQLite `SecurityAuditEvent` row and asserts that all injected credential values are absent from its stored JSON/text fields.

### Verification scheduling

- The user requested that complete cross-task verification be consolidated at the end of the work. No additional broad suite was started after that instruction.
- A focused Task 2/orchestrator suite had already begun when the instruction arrived and completed with `43 passed, 14 warnings`; the warnings are existing Pydantic and FastAPI deprecations. Full repository/security validation remains deferred to the unified stage.
