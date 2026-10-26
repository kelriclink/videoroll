# YouDub SQLite contention: native status polling

## Incident

On October 17, multiple automated dubbing jobs failed with
`sqlite3.OperationalError: database is locked` at `youdub_bridge.py:status ->
load_upstream -> database.init_db -> conn.executescript`.

Although `--status` should be a read-only query, the native bridge executed
schema DDL and seeded settings every 5-second poll while another YouDub
PipelineRunner was updating `data/youdub.sqlite` in SQLite DELETE journal mode.
VideoRoll treated the resulting bridge error as fatal in its polling loop and
terminated the **healthy native worker**, marking the stage `Interrupted by
VideoRoll controller`. In the 2026-10-17 incident, native tasks `cd5fe8c4`,
`31605df8`, `19c067a4`, and later `9a39458a` hit this failure.

## Corrective action

- `load_upstream(initialize_db=False)` by default: status, resume,
  interrupt and native run **never** invoke schema initialization in polling.
  Creating a task retains the original schema init for fresh installations.
- `status` uses bounded, read-only SQLite-busy retries (4 attempts) to tolerate
  an active writer, without changing another task's progress/metadata.
- VideoRoll polling treats only `database is locked` as transient while the
  original native subprocess continues. The controller still validates its
  cancel/lease heartbeat. All other failures continue to propagate and use
  existing cleanup semantics.
- No changes to YouDub tables, checkpoints, stage outputs, media, model, or
  the native GPU lock. No journal-mode change during an active pipeline.

## Verification

- Regression: `--status` is usable while a second SQLite connection holds
  `BEGIN IMMEDIATE`, proving it does not attempt schema DDL on reads.
- Regression: transient SQLite poll errors do not kill the native process;
  unrelated errors still propagate.
- Real-time observation: fixed bridge `--status` successfully read a current
  task on link while its native Demucs task was active.
- Full VideoRoll backend suite, including native YouDub bridge checks.

**Rollout:** Use `videorollctl update --bundle ... --apply` only after the
existing active subtitle/render/dub queues have drained. Never delete SQLite
`-journal` / `-wal` files or terminate a working Demucs/TTS process to force
this application-only fix.
