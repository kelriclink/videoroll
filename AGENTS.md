# Repository Guidelines

## Project Structure & Module Organization
`src/videoroll/` is the Python 3.12 backend. `apps/orchestrator_api` exposes the browser-facing `/api`; `apps/subtitle_service` owns ASR, translation, render queues, and the RAG Agent runtime; `apps/youtube_ingest`, `apps/bilibili_publisher`, and `apps/social_publisher` handle ingest and publishing. Shared config, DB, filesystem storage, AI, and utilities live in `config.py`, `db/`, `storage/`, `ai/`, and `utils/`. `services/ffplayout/` is the integrated playout engine, while `social-auto-upload` is the external source submodule required for social publishing. `src/web/` is React 18 + Vite + Tailwind. Backend tests are `tests/test_*.py`; frontend tests are colocated under `src/web/src/`. Historical/reference upstream projects are documented in `docs/REFERENCES.md` rather than copied into the main source tree.

## Build, Test, and Development Commands
Compose starts the Web gateway, Orchestrator, isolated internal APIs/workers, Redis, egress gateway, social publisher services, and ffplayout. PostgreSQL 16+ is external; configure `DATABASE_URL`. Media and artifacts use the shared filesystem rooted at `STORAGE_HOST_ROOT`/`STORAGE_ROOT`.

- `./scripts/dev_up.sh`: create `.env` if missing, build, and start locally.
- `./scripts/dev_down.sh`, `./scripts/dev_logs.sh`, `./scripts/dev_health.sh`: stop, inspect logs, or check health.
- `./scripts/dev_web.sh`: run only Vite on port `3000`.
- `python3 -m pytest tests/` (or `python -m pytest tests/` in an activated venv): run backend tests.
- `cd src/web && npm run lint && npm run test && npm run build`: lint, test, and build the frontend.
- `./scripts/smoke_local.sh [video.mp4]`: run an upload/subtitle smoke flow.
- `./scripts/build_export_prod.sh`: build and export Docker images.

## Coding Style & Naming Conventions
Follow the existing style; do not introduce a new formatter. Python uses 4-space indentation, type hints, `snake_case` for modules/functions, and `PascalCase` for classes and Pydantic models. Keep service code in the matching `apps/*` package and prefer structured DB/API helpers. Frontend components use `PascalCase`, helpers use names like `videosPage.helpers.ts`, and tests use `*.test.ts`. Run ESLint for UI changes.

## Testing Guidelines
Backend tests use `pytest`, with `unittest.TestCase` and plain pytest functions. Add coverage in `tests/test_<feature>.py`, especially for queues, RAG retrieval/agent budgets, publishing retries, and parsing. Mock network, `yt-dlp`, LLMs, and external APIs. Keep frontend tests colocated. Targeted examples: `python -m pytest tests/test_translation_rag.py` or `cd src/web && npm run test`.

## Commit & Pull Request Guidelines
Recent history favors short imperative subjects with Conventional Commit prefixes, for example `feat: add tool-driven RAG agents` or `fix: harden publish retry and proxy checks`. Keep commits focused. PRs should include a summary, linked issue when applicable, config/schema notes, UI screenshots, and verification commands.

## Security & Configuration Tips
Start from `.env.example`. Never commit real cookies, API keys, generated model data, or `data/secrets/fernet.key`; losing that key makes encrypted DB settings unreadable. RAG vector search may require pgvector. Treat downloader, auth, publishing, LLM tools, URL fetching, and secret storage as security-sensitive. Document new env vars and keep tests offline with bounded tool/fetch budgets.
