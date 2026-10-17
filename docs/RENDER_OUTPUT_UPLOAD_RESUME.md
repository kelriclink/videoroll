# 4K Render Worker upload HTTP 500 / PostgreSQL transaction issue

## Failure observed

Large 4K videos completed encoding but failed at `POST /render-workers/v1/executions/{id}/output` with HTTP 500, sometimes repeatedly at 90% (e.g. `MGQcrX7DMYw`, `RXxABp79Wh8`). API logs showed `psycopg.errors.IdleInTransactionSessionTimeout` and repeated Redis realtime publish timeouts; Worker treated each failed upload as an entire render failure and deleted its execution directory. The next attempt repeated hours of FFmpeg work.

## Transaction fix

`upload_output()` read an ORM `RenderJob` and then committed the preflight state. With SQLAlchemy default `expire_on_commit=True`, subsequent `job.request_json` access performed a fresh SELECT, reopening a transaction **before** the large async upload. `store_uploaded_task_asset()` accessed `task.id` from an expired ORM instance after the upload, with the same risk. PostgreSQL terminates sessions that remain idle in transaction while the video moves over HTTP/disk.

Capture `is_dubbed` and `task_id` before committing the preflight and pass the captured UUID to the asset service. No ORM-derived values are accessed during the remote upload. The atomic post-upload ownership/fence/cancellation check still runs inside a new, short DB transaction just before registration.

## Reuse rendered output instead of re-encoding

- On FFmpeg success, atomically move the completed file to a per-job cache under the Render Worker work directory. A manifest tracks SHA-256 and bytes; reattempts verify both before reuse. Match the render mode, full configuration and source artifact keys/digests (not execution ID or URL).
- New attempt with matching validated cache skips downloading source media and FFmpeg; it proceeds directly to output upload with a new execution fence. Truncated/incomplete output never qualifies.
- Within one execution retry transient HTTP 500/502/503/504/network errors at most four upload attempts, with backoff and unchanged file. A read-only authenticated GET probe checks whether the server already registered that exact execution output, preventing duplicate asset registration on ambiguous 500/response loss. Nontransient HTTP failures (400/403/409 etc.) still fail immediately.
- Only clear the cached file after the coordinator confirms execution complete. Preserve it across temporary upload failures and worker restarts. Automatically prune cache entries older than 72h on worker startup. Old execution directories are not swept by this mechanism.
- Keep the original exact fence, task-ownership, cancellation and upload registration semantics. Never automatically repost to Bilibili.

## Verification

Tests simulate idle transaction hazard during file storage, concurrent DB transaction boundaries, exact output reference fencing, corrupted cache, changed config and source SHA, cache survival across worker restarts, 500 retry and ambiguous 500-after-commit, and cache retention bounds. See `tests/test_render_worker_server.py` and `tests/test_render_worker_runtime.py`.
