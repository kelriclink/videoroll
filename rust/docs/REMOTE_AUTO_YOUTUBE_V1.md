# Rust Remote YouTube Intake V1 (2026-11-24)

Migrated the ORIGINAL Python public routes:
- GET /remote/auto/youtube returns HTTP 410 explaining that old query-token
  requests were removed.
- POST /remote/auto/youtube accepts JSON with url, license (default authorized),
  proof_url and auto_publish (nullable) and requires Authorization: Bearer
  and Idempotency-Key. This is a public endpoint with its OWN authentication:
  internal API tokens, admin sessions and query-token strings are NOT accepted.

Security and persistence:
- Reuses the original app_settings['remote.api'].token_hash, validates the PBKDF2
  SHA256 encoded hash with the exact Python-compatible verifier, and permits
  token rotation/revocation using existing GET/PUT /settings/api. Never persists
  a plaintext bearer. PBKDF2 runs in a bounded blocking pool, not the async
  HTTP reactor. Authentication happens before parsing the JSON body and
  unrecognized/malformed request bodies cannot bypass Bearer validation.
  The body is limited to 32 KiB.
- Uses the original Python remote_api_requests table, its
  (token_hash,idempotency_key) UNIQUE constraint and original JSON schema.
  Stores only SHA256(token), SHA256(canonical JSON payload) and an empty
  request_json. The plaintext payload/URLs are never stored in the request
  ledger; created tasks retain the approved source URL.
- Python Pydantic model_dump(sort_keys=True) matches Rust canonical JSON
  hashing. Completed Python requests replay identically. Pending or failed
  requests return 409; an identical key with a different payload returns 409.
  Uncertain pending requests are NEVER silently re-dispatched after their
  lease expires. This intentionally tightens Python's 24h reissue behavior.
- The durable pending reservation is committed BEFORE creating a workflow,
  and the response is committed afterward; ambiguous completion leaves a
  pending record requiring reconciliation, not an automated second dispatch.
- Same original ingested_videos unique key protects the underlying YouTube
  video even across different Idempotency-Key values. Source proof is inserted
  into the original Task row in the SAME transaction as Task+Workflow+Ingest.
  Existing Task source proofs are never replaced by a replay.
- 60/min token quota and at most four live reservations are checked using
  original PostgreSQL records. A second transaction-scoped per-token advisory
  lock prevents concurrent different keys from bypassing this quota. No Redis
  is required for correct idempotency.
  Unlike the Python Redis best-effort quota, this does not implement the
  separate 120/min IP quota or Retry-After header. Full parity is therefore
  NOT claimed. Additional network-edge rate controls remain recommended.
- Auto-publish is conservatively false in this native adapter. The original
  Python intake ignored the request field and used a runtime-resolved setting;
  the full runtime auto-publish equivalence must be checked before production.

Verification:
The disposable PostgreSQL fixture adds EXACTLY the original Python
remote_api_requests schema; it is not a V2 shadow table or production migration.
Run the focused test:
VIDEOROLL_V2_TEST_REMOTE_AUTO_ONLY=true bash rust/scripts/smoke-isolated.sh

Tests compare original Python PBKDF2 and canonical SHA256 hashing; invalid
authentication, missing/invalid keys, source URL/license/proof validation,
Python-created completed/pending/failed ledger rows, completed replay,
different-payload conflict, repeated video dedupe, four concurrent HTTP
calls, token rotation/revocation, quota and proof-field transactional
consistency. No real YouTube or production platform posts are executed.

This is a PARTIAL source-compatible mapping. Real remote clients, edge rate
limits, provider/auto-publish behavior and full restart fault injection still
need production-like qualification.
