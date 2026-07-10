# Social Publisher Integration Design

Date: 2026-07-10

## Summary

VideoRoll will integrate `social-auto-upload` (SAU) as an isolated browser-automation publisher. The main VideoRoll application remains the orchestration and public API boundary. A dedicated social-publisher image runs a private FastAPI service and a Celery worker that invoke the installed `sau` CLI for Douyin, Xiaohongshu, and Kuaishou video publishing.

The first release supports server-side headless publishing, encrypted import of locally generated Playwright/Patchright `storage_state` JSON account files, account validation, per-account execution locks, and conservative publish states that prevent accidental duplicate submissions.

## Goals

- Deploy the SAU integration together with VideoRoll through Docker Compose.
- Keep Chromium, Patchright, platform selectors, and SAU dependencies outside the main VideoRoll image and processes.
- Support real headless video publishing to Douyin, Xiaohongshu, and Kuaishou.
- Let users generate account state locally and import it through the VideoRoll web UI.
- Encrypt account state at rest and never store it in S3 or expose it through APIs or logs.
- Preserve a unified VideoRoll publish action while keeping platform-specific behavior in publisher drivers.
- Prevent automatic retries after a browser submission may have occurred.
- Keep all automated tests offline by mocking browsers, SAU, platform networks, Redis, and S3 where appropriate.

## Non-goals

- Image/note publishing.
- Server-side QR-code login in the first release.
- Product links, shopping metadata, or multi-ratio Douyin covers.
- Direct imports from SAU internal Python uploader modules.
- Automatic confirmation that a submitted work is publicly visible.
- Automatic retry of ambiguous browser publishing failures.
- Dynamic third-party publisher plugins loaded at runtime.

## Selected Architecture

The implementation uses one dedicated social-publisher image with two Compose services:

- `social-publisher-api`: private FastAPI service for account management and publish-job admission.
- `social-publisher-worker`: Celery worker for account validation and browser publishing.

Both services use the same image. Only the worker executes SAU commands. Neither service publishes a host port; the VideoRoll orchestrator reaches the API through the Compose network.

```text
Web
 └─ VideoRoll Orchestrator (public API)
     ├─ bilibili-publisher (existing backend)
     └─ social-publisher-api (private Compose service)
          ├─ account import and status API
          ├─ publish-job admission and idempotency
          └─ Redis queue: social_publish
               └─ social-publisher-worker
                   ├─ materialize S3 media
                   ├─ decrypt temporary account state
                   ├─ acquire platform/account lock
                   └─ execute sau <platform> upload-video
```

SAU is included as a pinned Git submodule and installed into the social-publisher image. The image uses Python 3.12 because SAU currently declares Python `>=3.10,<3.13`. It installs Patchright Chromium during the image build and runs as a non-root user.

## Component Boundaries

### VideoRoll Orchestrator

- Remains the only public API used for task publishing and social account settings.
- Validates task ownership/licensing, selects media assets, runs the existing publish review, and normalizes common metadata.
- Routes Bilibili to the existing publisher and social platforms to `social-publisher-api`.
- Does not execute Chromium or read decrypted social account state.
- Proxies social account import/status operations so the internal service remains private.

### Social Publisher API

- Owns social account records and social publish-job admission.
- Validates platform names and uploaded account JSON structure and size.
- Encrypts imported account JSON before committing it to the database.
- Creates platform-scoped `PublishJob` records and sends Celery tasks.
- Never invokes SAU synchronously inside an HTTP request.

### Social Publisher Worker

- Runs account checks and publishing commands.
- Downloads video and optional cover assets from S3 into a job-specific work directory.
- Decrypts account JSON into a temporary file with mode `0600` on tmpfs.
- Acquires an exclusive Redis lock for the selected platform and account.
- Builds an argument list and invokes `sau` without a shell.
- Persists sanitized results and state transitions, re-encrypts refreshed account state, and removes plaintext files.

### SAU CLI Adapter

- Is the only VideoRoll component aware of SAU command-line flags.
- Supports `douyin`, `xiaohongshu`, and `kuaishou` video commands.
- Builds argv arrays rather than command strings.
- Applies configured timeouts and bounded stdout/stderr capture.
- Does not import `uploader.*` modules from SAU.

## Account Storage and Validation

The existing `Account` model is the source of truth:

- `platform` and `name` identify an account and remain unique together.
- `secrets_encrypted` stores the canonical JSON string encrypted with the existing VideoRoll Fernet key.
- `rotated_at` records import or replacement time.
- `is_active` controls availability in publish forms.

The model will gain non-secret validation fields:

- `check_state`: `unchecked`, `queued`, `checking`, `valid`, `invalid`, or `error`.
- `last_checked_at`: last completed validation time.
- `last_check_message`: sanitized validation result without account contents.

The import endpoint accepts a multipart JSON file and account name. It rejects files larger than 1 MiB, invalid JSON, non-object roots, missing `cookies` arrays, unsupported platforms, and account names outside a conservative ASCII identifier pattern. It canonicalizes the JSON before encryption.

After import, the API sets `check_state=queued` and enqueues an account-check task. SAU resolves accounts from its fixed `BASE_DIR/cookies/{platform}_{name}.json` convention, so the worker materializes that exact validated filename inside a tmpfs-mounted SAU `cookies` directory, runs `sau <platform> check --account <name>`, updates the validation fields, and removes the plaintext file. The web UI polls account status.

The UI explicitly states that the file must be an SAU-generated Playwright/Patchright `storage_state` JSON. A raw browser Cookie header is not accepted because it may omit origin storage needed by a platform.

## Publish Request Contract

The public task action remains:

```text
POST /tasks/{task_id}/actions/publish
```

Its generic request contains:

- `platform`
- `account_id`
- `video_key`
- `cover_key`
- `meta`
- `platform_options`
- `skip_review`

Common normalized metadata contains:

- `title`
- `desc`
- `tags`
- `schedule`

The orchestrator keeps Bilibili-specific fields out of social validation. Bilibili continues to use its complete metadata model. Social platform validation requires a non-empty title, normalizes description and tags, and applies platform constraints such as Xiaohongshu's maximum of ten tags.

Per-platform metadata is stored separately:

```text
meta/{task_id}/publish/bilibili.json
meta/{task_id}/publish/douyin.json
meta/{task_id}/publish/xiaohongshu.json
meta/{task_id}/publish/kuaishou.json
```

The existing Bilibili metadata key remains readable during migration for backward compatibility.

The first-release SAU mapping is:

```text
sau <platform> upload-video
  --account <account-name>
  --file <local-video>
  --title <title>
  --desc <description>
  --tags <comma-separated-tags>
  [--thumbnail <local-cover>]
  [--schedule "YYYY-MM-DD HH:MM"]
  --headless
```

## Job Admission and Idempotency

Idempotency is scoped to `(task_id, platform, account_id)`, not only `task_id`. This permits one processed video to be submitted to multiple platforms while preventing accidental duplicate submissions to the same account.

Admission behavior:

- An existing `submitting`, `submitted`, or `unknown` job for the same scope blocks a normal new submission.
- A `submitted` or `unknown` job requires an explicit user-confirmed retry action after the user checks the platform backend.
- A confirmed pre-execution `failed` job may be retried.
- Published jobs remain terminal for the same scope.

The existing `PublishJob` model continues to hold the normalized request in `meta_json`, account relation in `account_id`, sanitized execution details in `response_json`, and platform identifiers in `external_id`/`external_url` when they become available.

It will gain:

- `started_at`: set immediately before the SAU process starts.
- `finished_at`: set when execution reaches a terminal or waiting-for-confirmation state.

These timestamps allow a watchdog to distinguish a task that never started from a task whose browser process disappeared.

## State Model

The real SAU CLI reports successful submission but does not provide a stable external work ID. VideoRoll therefore uses conservative states:

- `submitting`: admitted, queued, preparing, or executing.
- `submitted`: SAU exited successfully after executing the submission flow; platform publication is not independently confirmed.
- `unknown`: the SAU process started but timed out, exited abnormally, the worker disappeared, or the outcome otherwise cannot be proven.
- `failed`: a deterministic error occurred before SAU execution, such as invalid input, inactive account, missing media, or an S3 materialization failure.
- `published`: reserved for future confirmation that obtains a platform work ID or otherwise proves publication.

Rules:

- No automatic Celery retry is allowed after `started_at` is set.
- Any timeout or non-zero SAU result after process start becomes `unknown`, not `failed`.
- A successful SAU exit becomes `submitted`, not `published`.
- A watchdog changes overdue jobs with `started_at` set to `unknown` without requeueing them.
- A stale queued job with no `started_at` may become `failed` because no browser submission began.
- The task-level status remains an overall pipeline indicator; per-platform truth comes from `PublishJob` records.

## Locking and Concurrency

The worker acquires a Redis lock keyed by platform and account ID before materializing credentials or starting Chromium:

```text
videoroll:social-publish:{platform}:{account_id}
```

The lock TTL exceeds the configured upload timeout plus a cleanup margin. Failure to acquire the lock leaves the job queued for a bounded safe retry because no browser process has started.

The default social worker concurrency is one. The design still supports higher future concurrency across distinct accounts while preserving one active browser flow per account.

## Error Handling and Logging

Execution is divided into two safety phases:

1. Pre-execution: schema validation, account lookup, lock acquisition, S3 download, path preparation, and argv construction. Failures are deterministic and may become `failed` or receive a bounded safe retry.
2. Browser execution: begins when `started_at` is written immediately before process creation. Any ambiguous failure becomes `unknown` and is never retried automatically.

Captured output is truncated to a configured maximum. Logs and responses must not include account JSON, decrypted cookies, environment secrets, S3 credentials, or raw request bodies. SAU requires the validated account name in its temporary filename; no other user-controlled path component is accepted.

Health checks verify the API process, database/Redis reachability, presence of the `sau` executable, and Chromium installation without contacting social platforms.

## Docker and Runtime Layout

The Compose deployment adds:

- `social-publisher-api`, running Uvicorn.
- `social-publisher-worker`, running Celery on the `social_publish` queue.

Both use a dedicated Dockerfile and image. Runtime configuration includes:

- internal API URL
- publish mode (`mock` or `sau`)
- SAU executable and repository/runtime directory
- account-check timeout
- upload timeout
- stdout/stderr size limit
- worker concurrency
- work directory

Video and cover files use a persistent or bind-mounted work directory suitable for large media. The worker mounts tmpfs at the installed SAU runtime's `cookies` directory, where decrypted account files are created only for the duration of a check or publish command and deleted in `finally` cleanup. No plaintext credential directory is persisted or exported with production images.

The API and worker both mount the same existing `./data/secrets:/secrets` volume so encryption and decryption use the same Fernet key as the main VideoRoll deployment. Losing or replacing this key makes imported account records unreadable, matching the repository's existing secret-storage behavior.

## Web Interface

`SettingsPublishPage` contains platform cards for Bilibili, Douyin, Xiaohongshu, and Kuaishou. Each social platform card provides:

- account list and active state
- account-name input
- JSON file import/replace control
- validation state and last validation time
- manual recheck and delete actions
- local SAU login command and expected file path
- a warning that raw Cookie strings are not supported
- a statement that imported content is encrypted and never displayed again

`TaskDetailPage` adds enabled buttons for the three social platforms. Selecting a social platform shows:

- account selector containing only active accounts for that platform
- video and optional cover selection
- title, description, and tags
- optional scheduled publish time
- a warning explaining `submitted` and `unknown`

Bilibili type/category controls appear only when Bilibili is selected. Social publishing does not require `typeid`.

Publish-job rows display platform, account, state, external identifier when available, sanitized error detail, and timestamps. Retry controls enforce the idempotency rules and require explicit confirmation for `submitted` or `unknown` jobs.

## Testing Strategy

All automated tests remain offline.

Backend unit tests cover:

- platform normalization and backend routing
- separation of Bilibili and social metadata validation
- SAU argv construction with no shell invocation
- tag, schedule, cover, and account mappings
- account JSON validation, encryption, replacement, and non-disclosure
- platform/account idempotency
- Redis lock keys and contention behavior
- state transitions for pre-execution errors, success, non-zero exit, timeout, and stale workers
- bounded log capture and secret redaction

Service tests mock S3, Redis, Celery submission, subprocess execution, and platform networks. They verify that plaintext account files are removed in success and failure paths.

Frontend tests cover platform selection, conditional Bilibili controls, account-file guidance, account status rendering, social request payloads, and retry warnings.

Container smoke verification checks:

- `sau --help`
- each supported platform help command
- Chromium executable discovery and a local blank-page launch
- social-publisher API health

No automated verification performs a real social-media login or upload.

## Rollout Sequence

1. Extract and test platform-neutral publish metadata and routing without changing Bilibili behavior.
2. Add account schema fields, encryption helpers, internal account APIs, and mocked validation tasks.
3. Add the SAU command adapter and worker state machine with mocked subprocess tests.
4. Add the dedicated image and Compose services in mock mode.
5. Add web account management and social task controls.
6. Run backend, frontend, and container smoke tests.
7. Import a non-production account state and run `check` in the deployed container.
8. Enable real SAU mode for one platform/account and perform a manually supervised acceptance upload.
9. Enable the remaining platforms after separate acceptance uploads.

## Acceptance Criteria

- Compose starts the main application, social API, and social worker independently.
- The main VideoRoll container has no Patchright or Chromium requirement introduced by this feature.
- A user can import an SAU-generated account JSON through the web UI and see asynchronous validation status.
- The stored credential is encrypted and never returned by any read endpoint.
- A task can submit video jobs independently to Douyin, Xiaohongshu, and Kuaishou accounts.
- The worker downloads S3 media, invokes the correct argv command, and removes plaintext account files.
- A successful SAU command produces `submitted`.
- A timeout or post-start failure produces `unknown` and is not automatically retried.
- The same platform/account cannot run two browser publishing jobs concurrently.
- Existing Bilibili publishing and tests continue to pass.
- Backend, frontend, and Docker smoke tests complete without real platform network calls.
