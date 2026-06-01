# Repository Guidelines

## Project Structure & Module Organization
`src/videoroll/` contains the Python 3.12 backend: FastAPI apps under `apps/`, shared DB models in `db/`, S3/MinIO access in `storage/`, and cross-cutting helpers in `utils/` and `ai/`. `src/web/` is the React 18 + Vite frontend. Backend tests live in `tests/` as `test_*.py`; frontend tests are colocated as `*.test.ts`. Use `scripts/` for common local workflows, `docs/` for specs, and `data/` for local state. `biliup-master/` and `bilibili-API-collect-main/` are vendored/reference trees; avoid casual edits there.

## Build, Test, and Development Commands
Use Docker Compose for the default stack:

- `./scripts/dev_up.sh`: build and start `app`, `web`, Redis, and MinIO.
- `./scripts/dev_down.sh`: stop the stack.
- `./scripts/dev_health.sh`: check published endpoints.
- `./scripts/dev_logs.sh`: tail service logs.
- `./scripts/dev_web.sh`: run only the Vite dev server on port `3000`.
- `python -m pytest tests/`: run backend tests.
- `cd src/web && npm run lint && npm run test && npm run build`: validate the frontend.

## Coding Style & Naming Conventions
Follow the existing code style rather than introducing a new formatter. Python uses 4-space indentation, explicit type hints, `snake_case` for modules/functions, and `PascalCase` for classes and Pydantic models. Keep service-specific code inside the matching `apps/*` package. In the frontend, React components use `PascalCase`, shared helpers use descriptive filenames such as `videosPage.helpers.ts`, and tests use `*.test.ts`. Run `src/web` ESLint before opening a PR.

## Testing Guidelines
Backend tests run under `pytest`, with a mix of `unittest.TestCase` and plain pytest functions. Add new backend coverage in `tests/test_<feature>.py` and mock network, `yt-dlp`, and external APIs instead of hitting live services. Keep frontend tests beside the code they cover. For targeted runs, use commands like `python -m pytest tests/test_publish_meta_draft.py` or `cd src/web && npm run test`.

## Commit & Pull Request Guidelines
Recent history favors short imperative subjects, often with Conventional Commit prefixes, for example `feat: add AI publish review workflow` and `Fix auto pipeline resume...`. Keep commits focused and explain the affected flow. PRs should include a concise summary, linked issue when applicable, config or schema changes, and screenshots for UI work. List the verification commands you ran.

## Security & Configuration Tips
Start from `.env.example`. Never commit real cookies, API keys, or `data/secrets/fernet.key`. Treat changes touching downloader, auth, or publishing code as security-sensitive and document any new environment variables.
