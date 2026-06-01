# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is VideoRoll

VideoRoll is a modular video processing pipeline: YouTube ingest → ASR subtitles → translation → rendering (burn-in) → Bilibili publishing. It consists of a Python/FastAPI backend, Celery workers, and a React/TypeScript frontend.

## Commands

### Development environment

```bash
# Start all services (docker compose with Intel GPU passthrough)
./scripts/dev_up.sh

# Stop services
./scripts/dev_down.sh

# Health check all endpoints
./scripts/dev_health.sh

# Tail logs
./scripts/dev_logs.sh

# Frontend dev server (Vite, port 3000)
./scripts/dev_web.sh
```

### Testing

```bash
python -m pytest tests/                    # all tests
python -m pytest tests/test_publish_meta_draft.py  # single file
```

Tests use `unittest.TestCase` style. No conftest.py or shared fixtures.

### Frontend

```bash
cd src/web
npm run lint          # ESLint
npm run build         # Vite production build
```

## Architecture

### Monolith assembly

All four FastAPI apps run in a single uvicorn process (`videoroll.apps.monolith.main:app`):

- **Orchestrator** (`/api`) — task CRUD, asset management, auth, YouTube download, publish meta, settings
- **Subtitle Service** (`/api/subtitle-service`) — ASR, translation, render queue, auto profile
- **YouTube Ingest** (`/api/youtube-ingest`) — source CRUD, channel/playlist scanning
- **Bilibili Publisher** (`/api/bilibili-publisher`) — auth, publish jobs, typeid recommendation

The entrypoint (`docker/entrypoint.sh`) runs uvicorn + 2 Celery workers in parallel (subtitle queue, publish queue).

### Task state machine

`CREATED → INGESTED → DOWNLOADED → AUDIO_EXTRACTED → ASR_DONE → TRANSLATED → SUBTITLE_READY → RENDERED → READY_FOR_REVIEW → APPROVED → PUBLISHING → PUBLISHED` (+ `FAILED`, `CANCELED`)

Services communicate only through DB task state + S3 storage keys — no direct data passing.

### Celery workers

| App | Queue | Key tasks |
|---|---|---|
| `videoroll.apps.subtitle_service.worker` | `subtitle` | `task_queue_tick` (scheduler), `process_job` (ASR+translate), `process_render_job` (ffmpeg burn-in), `auto_youtube_pipeline`, `after_render_publish`, `cleanup_task` |
| `videoroll.apps.bilibili_publisher.worker` | `publish` | `process_job` (upload to Bilibili) |

The subtitle queue uses a task-level lock system (`Task.lock_owner` / `Task.lock_until`) with configurable `max_concurrency`. `task_queue_tick` is the central scheduler that picks queued work and dispatches within concurrency limits.

### Database

PostgreSQL with psycopg 3 driver. SQLAlchemy 2 ORM (`DeclarativeBase` + `Mapped` annotations). No Alembic — uses a lightweight auto-migration system (`db/auto_migrate.py`) that runs `ALTER TABLE ADD COLUMN` on startup.

### Configuration

`videoroll/config.py` — pydantic-settings, one `Settings` class per service inheriting from `CommonSettings`. All env-driven with `.env` file support, cached via `@lru_cache`.

### Key shared modules

| Path | Purpose |
|---|---|
| `videoroll/db/models.py` | All 11 ORM models (Task, Asset, Subtitle, PublishJob, etc.) |
| `videoroll/storage/s3.py` | boto3 S3/MinIO wrapper |
| `videoroll/ai/` | OpenAI client (translation, typeid recommendation, content review) |
| `videoroll/utils/crypto.py` | Fernet encryption for secrets at rest |
| `videoroll/apps/publish_meta_rules.py` | Pure functions for Bilibili-compliant metadata (CJK-aware text clamping) |
| `videoroll/apps/publish_meta_draft.py` | Builds publish meta drafts from YouTube metadata + settings |

### Frontend

React 18 + TypeScript + Vite + Tailwind CSS + react-router-dom v6. In production, nginx serves the SPA and reverse-proxies `/api/` to the backend. In dev, Vite proxies `/api` to `localhost:8000`.

### External services (from compose)

- **Redis** — Celery broker/backend
- **MinIO** — S3-compatible object storage
- **PostgreSQL 16+** — provided externally (not in compose)

## Conventions

- Python >=3.12, build with hatchling
- Source layout: `src/videoroll/`
- ASR engine is pluggable: `faster-whisper` (default), `openvino` (Intel GPU), or `mock` (testing)
- YouTube integration via `yt-dlp` with configurable proxy, cookies (Netscape format), user-agent
- Sensitive data (API keys, cookies) encrypted at rest with Fernet (`data/secrets/fernet.key`)
