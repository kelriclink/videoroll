---
name: videoroll
description: Collect YouTube video URLs on an external machine and submit them to VideoRoll through the remote auto-ingest API. Use when OpenClaw needs to find canonical YouTube watch links, deduplicate them, and push each URL to VideoRoll with the configured token so the project starts auto mode immediately.
---

# OpenClaw -> VideoRoll

## Overview

Use this skill only for one job: get valid YouTube video links and send them to VideoRoll's remote API.

## Required config

- Before using this skill, check whether `skills/videoroll/local.env` exists.
- If it exists, load it first and use the values from that file.
- If it does not exist yet, copy `skills/videoroll/local.env.example` to `skills/videoroll/local.env`, then fill the real values.
- Fill these values in `local.env`:

```bash
VIDEOROLL_REMOTE_URL="<<填写你的 VideoRoll 远程接口地址>>"
VIDEOROLL_REMOTE_TOKEN="<<填写 Settings · API 里配置的 token>>"
VIDEOROLL_SOURCE_LICENSE="authorized"
VIDEOROLL_PROOF_URL=""
VIDEOROLL_AUTO_PUBLISH="true"
```

- What you need to fill:
  - `VIDEOROLL_REMOTE_URL`
    Fill your real remote API endpoint, for example:
    `https://your-host/api/remote/auto/youtube`
  - `VIDEOROLL_REMOTE_TOKEN`
    Fill the token you configured in VideoRoll `Settings · API`
  - `VIDEOROLL_SOURCE_LICENSE`
    Usually keep `authorized`
  - `VIDEOROLL_PROOF_URL`
    Optional. Fill only if you want every pushed task to carry the same proof link
  - `VIDEOROLL_AUTO_PUBLISH`
    Optional. `true` / `false`. If set, override VideoRoll current Auto Profile `auto_publish`

- Local setup:

```bash
cp ~/.codex/skills/videoroll/local.env.example ~/.codex/skills/videoroll/local.env
vi ~/.codex/skills/videoroll/local.env
set -a
source ~/.codex/skills/videoroll/local.env
set +a
```

- `VIDEOROLL_REMOTE_URL`
  Full remote endpoint URL. Usually:
  `https://your-host/api/remote/auto/youtube`
- `VIDEOROLL_REMOTE_TOKEN`
  Token configured in VideoRoll's `Settings · API`
- Optional: `VIDEOROLL_SOURCE_LICENSE`
  Default `authorized`
- Optional: `VIDEOROLL_PROOF_URL`
  Authorization proof link if needed
- Optional: `VIDEOROLL_AUTO_PUBLISH`
  Override whether this submission should auto-publish after render. If omitted, VideoRoll uses its current Auto Profile setting

## Workflow

1. Collect candidate YouTube links from the requested source.
2. Normalize every link to a canonical watch URL:
   `https://www.youtube.com/watch?v=<video_id>`
3. Drop non-video links, duplicates, shorts landing pages without a resolvable video id, and obviously broken URLs.
4. For each final URL, call VideoRoll remote API once.
5. Return the API result for each URL, especially:
   `task_id`, `pipeline_job_id`, `deduped`, `source_id`

## API call

Send query parameters:

- `token`
- `url`
- Optional `license`
- Optional `proof_url`
- Optional `auto_publish`

Example:

```bash
curl -G "$VIDEOROLL_REMOTE_URL" \
  --data-urlencode "token=$VIDEOROLL_REMOTE_TOKEN" \
  --data-urlencode "url=https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --data-urlencode "license=${VIDEOROLL_SOURCE_LICENSE:-authorized}" \
  --data-urlencode "auto_publish=${VIDEOROLL_AUTO_PUBLISH:-true}"
```

Success response shape:

```json
{
  "task_id": "...",
  "pipeline_job_id": "...",
  "deduped": false,
  "source_id": "..."
}
```

## Rules

- Only submit actual YouTube video URLs.
- Preserve the original video id exactly.
- Do not invent metadata that the API does not need.
- If the API returns `deduped=true`, report it instead of retrying.
- Remote auto-publish depends on VideoRoll Auto Profile unless `auto_publish` is explicitly provided.
- If token/auth fails, stop and surface the exact error.
- Do not attempt to bypass paywalls, private videos, or platform restrictions.
- Never commit the real `local.env` file or real token back into git.

## Output format

When reporting back, list each processed URL with:
- normalized URL
- API status
- `task_id` if created
- `pipeline_job_id` if started
- whether it was deduped
