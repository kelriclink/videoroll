# VideoPipelineV1 parent workflow audit reconciliation

## What was wrong

`PipelineLauncher` wrote `PipelineRun.state=submitted` on Hatchet acceptance, but
`VideoPipelineV1` did not close its parent audit on terminal completion. For the
100-second `vuvckBQ1bME` test, Hatchet's authoritative `runs.get_status` was
`COMPLETED` and the Bilibili batch was `succeeded`, while the database parent
was still `submitted`, preventing truly idle deploys.

## Fixed behavior

- Register native Hatchet `on_success_task` and `on_failure_task` callbacks.
  Both resolve the **exact attempt** by task ID and durable `dispatch_key`, then
  write `succeeded` or `failed`, with `finished_at`, to the parent audit.
- Add an authenticated **read-only** `GET /runs/{run_id}/status` endpoint on
  the workflow service. It returns the actual Hatchet run status. It does not
  launch, cancel or mutate any workflow or publication.
- Add a dedicated orchestrator background reconciliation loop, every 30 seconds
  (configurable `WORKFLOW_AUDIT_RECONCILE_SECONDS`), with a bounded batch of 25
  unresolved `VideoPipelineV1` records. Reconciliation reads Hatchet without
  holding a DB transaction, then row-locks and confirms the unchanged external
  ID before updating. Mappings: `COMPLETED→succeeded`, `FAILED→failed`,
  `CANCELLED→canceled`, `RUNNING→running`; unknown, queued and unreachable
  states are left intact. The loop repairs missed callbacks and historical
  unfinished audits. Callback and polling both leave terminal states immutable.
- Protect the launch-response handler from overwriting a callback that finished
  before HTTP launch acknowledgement was committed.

**Task business state, PublishBatch, PublishJob, assets, and BV URLs are not
changed.** An upload success is *not* enough to mark a still-running Hatchet
run completed. A cancelled workflow must not be considered published by the
reconciler just because old P1 assets exist. Stale status reconciliation never
triggers a repeat upload, TTS or render.

Tests cover exact-attempt updates, complete/fail/cancel/running mapping,
transient Hatchet unavailability, idempotence, callback-vs-launch race,
authenticated status API, and Hatchet SDK workflow callback registration.
