# Compatibility fallback and failure alerts

## Task-level visibility

- Render Worker records structured `compatibility_warnings` in RenderExecution
  metrics, with stable `code`, descriptive `message` and `reason`.
- Render compatibility conditions include: hardware VAAPI decode unavailable,
  `overlay_vaapi` unavailable, event cache generation failures, event overlay
  failures, full CPU libass compatibility fallback, NVIDIA/QSV CPU ASS,
  CPU x264/SVT-AV1 encoding, and unavoidable 10-bit -> 8-bit H.264 VAAPI.
- The operations alert scanner (normally every 60 seconds) reviews recent
  recorded executions and detects terminal render execution errors, failed
  subtitle/TTS/task states and uncertain Bilibili submissions. It exposes
  alerts in **Operations > Alert Center** and prominently at **Task Detail**.
- Alerts contain `task_id` plus render job/execution ID when relevant,
  selected `pipeline`, original failure reason and warning code. Task detail
  refreshes every 30 seconds.
- Each execution-and-reason has one persistent fingerprint. Periodic scans
  do not inflate the `occurrence_count`, and acknowledging/resolving an old
  alert is respected. Alerts automatically age out of the active list after
  their 48-hour lookback (but remain in alert history). Historic incidents
  are not treated as new work or automatically retried.
- All alerts are advisory; the fallback, retry and publishing state machines
  are unchanged. `unknown` submissions must be manually checked on Bilibili.
- Animated or complex ASS using the normal live GPU path is NOT treated as
  a fallback solely because event caching is ineligible.

## Boundaries

This implementation captures the current render, subtitle, dubbing and
publishing failure state machines and the explicit render compatibility
branches above. Subsystems that do not persist a structured failure or
compatibility state cannot be assumed to generate a task-level alert merely
because a log line contains the word "fallback". Future adapters should
persist structured warning codes in their own execution metadata rather than
relying on arbitrary log string matching.
