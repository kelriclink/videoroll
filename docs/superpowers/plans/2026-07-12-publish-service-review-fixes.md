# Publish Service Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the reviewed publish lifecycle races, retry dead ends, legacy Bilibili idempotency gap, cleanup delivery loss, and platform coupling.

**Architecture:** Keep `PublishService` as the orchestration boundary. Make batch replacement atomic under the task row lock, treat target snapshots as reusable only while compatible, validate batch ownership at publisher APIs, and keep cleanup delivery retryable until cleanup actually runs.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy, Celery, pytest, React/Vitest

## Global Constraints

- Preserve current public API shapes unless validation must reject an invalid batch.
- Keep publisher calls idempotent across legacy and current database rows.
- Add a failing regression test before each production change.
- Do not modify vendored publishing repositories.

---

### Task 1: Make batch replacement atomic and recoverable

**Files:**
- Modify: `tests/test_publish_service.py`
- Modify: `src/videoroll/apps/publish_service.py`

- [ ] Add tests proving a completed batch is replaced before releasing the task lock.
- [ ] Add tests proving failed batches are reused only when their target snapshot still matches.
- [ ] Add tests proving a changed account can start a replacement batch after terminal failure.
- [ ] Implement locked batch creation and target compatibility checks.
- [ ] Run `python -m pytest tests/test_publish_service.py -q`.

### Task 2: Preserve cleanup delivery until cleanup succeeds

**Files:**
- Create: `tests/test_publish_cleanup.py`
- Modify: `src/videoroll/apps/subtitle_service/worker.py`

- [ ] Add a test proving queued subtitle/render work clears the delivery marker and retries.
- [ ] Reuse the existing Celery retry path for all in-flight work.
- [ ] Run `python -m pytest tests/test_publish_cleanup.py tests/test_publish_lifecycle.py -q`.

### Task 3: Restore Bilibili legacy idempotency

**Files:**
- Modify: `tests/test_bilibili_publish_idempotency.py`
- Modify: `src/videoroll/apps/bilibili_publisher/main.py`
- Modify: `src/videoroll/apps/bilibili_publisher/worker.py`
- Modify: `src/videoroll/db/auto_migrate.py`

- [ ] Add tests requiring `bili_account_id` fallback in API and worker lookups.
- [ ] Add a migration backfill from `bili_account_id` to `account_id` for Bilibili rows.
- [ ] Run Bilibili idempotency and migration tests.

### Task 4: Remove Bilibili coupling and validate batch ownership

**Files:**
- Modify: `tests/test_publish_service.py`
- Modify: `tests/test_publish_idempotency.py`
- Modify: `src/videoroll/apps/orchestrator_api/services/publishing_service.py`
- Modify: `src/videoroll/apps/bilibili_publisher/main.py`
- Modify: `src/videoroll/apps/social_publisher/main.py`

- [ ] Add a social-only `publish_all` regression test.
- [ ] Add cross-task and stale-batch validation tests for both publisher APIs.
- [ ] Prepare Bilibili metadata only when Bilibili is enabled.
- [ ] Reject publisher requests whose batch is missing, cross-task, or inactive.
- [ ] Run the focused publish test suite.

### Task 5: Verify and commit

- [ ] Run `python -m pytest tests/ -q`.
- [ ] Run `cd src/web && npm run lint && npm run test && npm run build`.
- [ ] Run `docker compose -f docker-compose.yml --env-file .env config --quiet`.
- [ ] Review `git diff --check` and commit only the publish-service fix scope.
