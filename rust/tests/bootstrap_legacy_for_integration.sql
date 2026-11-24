-- Minimal v1-compatible base schema ONLY for a disposable integration-test DB.
CREATE TYPE source_type AS ENUM ('youtube','local','url');
CREATE TYPE source_license AS ENUM ('own','authorized','cc','unknown');
CREATE TYPE task_status AS ENUM ('created','ingested','downloaded','audio_extracted','asr_done','translated','subtitle_ready','rendered','ready_for_review','approved','publishing','published','failed','canceled');
CREATE TYPE subtitle_format AS ENUM ('srt','vtt','ass');
CREATE TYPE asset_kind AS ENUM ('video_raw','metadata_json','audio_wav','segments_json','subtitle_srt','subtitle_ass','video_final','dubbing_audio','video_dubbed','cover_image','log','publish_result');
CREATE TYPE platform AS ENUM ('bilibili','youtube','douyin','xiaohongshu','kuaishou','tencent');
CREATE TABLE accounts (
  id UUID PRIMARY KEY,
  platform platform NOT NULL,
  name VARCHAR(128) NOT NULL,
  secrets_encrypted TEXT NOT NULL DEFAULT '',
  rotated_at TIMESTAMPTZ,
  is_active BOOLEAN NOT NULL DEFAULT true,
  check_state VARCHAR(16) NOT NULL DEFAULT 'unchecked',
  last_checked_at TIMESTAMPTZ,
  last_check_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(platform,name)
);

CREATE TABLE tasks (
  id UUID PRIMARY KEY,
  source_type source_type NOT NULL,
  source_url TEXT,
  source_license source_license NOT NULL,
  source_proof_url TEXT,
  status task_status NOT NULL,
  stopped_status task_status,
  priority INTEGER NOT NULL DEFAULT 0,
  origin_kind VARCHAR(32),
  origin_run_id VARCHAR(64),
  restart_generation INTEGER NOT NULL DEFAULT 0,
  created_by VARCHAR(128),
  error_code VARCHAR(64),
  error_message TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  active_publish_batch_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE ingested_videos (
  id UUID PRIMARY KEY, platform VARCHAR(32) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  task_id UUID NOT NULL REFERENCES tasks(id),
  UNIQUE(platform,source_id)
);
CREATE TABLE assets (
  id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id),
  kind asset_kind NOT NULL, storage_key TEXT NOT NULL,
  sha256 TEXT,size_bytes BIGINT, duration_ms BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE subtitles (
  id UUID PRIMARY KEY, task_id UUID NOT NULL REFERENCES tasks(id),
  version INTEGER NOT NULL DEFAULT 1,format subtitle_format NOT NULL,
  language VARCHAR(16) NOT NULL DEFAULT 'zh',
  storage_key TEXT NOT NULL
);

-- Native queue parity fixtures require Python's durable correlation and
-- download inbox tables. They are isolated test-only: no Hatchet/Redis.
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES tasks(id),
  engine VARCHAR(16) NOT NULL DEFAULT 'hatchet',
  workflow_name VARCHAR(128) NOT NULL,
  external_run_id VARCHAR(255),
  state VARCHAR(32) NOT NULL DEFAULT 'submitting',
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vr2_test_pipeline_runs_task ON pipeline_runs(task_id,created_at);
CREATE TABLE IF NOT EXISTS operation_inbox (
  id UUID PRIMARY KEY,
  operation_key VARCHAR(255) NOT NULL UNIQUE,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json JSONB,
  lease_owner VARCHAR(128),
  lease_until TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  retry_after TIMESTAMPTZ,
  recovery_count INTEGER NOT NULL DEFAULT 0,
  restart_token VARCHAR(128),
  last_error VARCHAR(1024),
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_settings (
  key VARCHAR(255) PRIMARY KEY,
  value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exact original Python AlertEvent layout, shared by both implementations.
CREATE TABLE alert_events (
    id UUID PRIMARY KEY,
    fingerprint VARCHAR(255) NOT NULL UNIQUE,
    source VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'warning',
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Source-compatible AI request ledger for local-only pricing/usage testing.
CREATE TABLE ai_usage_events (
  id UUID PRIMARY KEY,
  task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
  provider VARCHAR(64) NOT NULL DEFAULT 'openai',
  model VARCHAR(255) NOT NULL DEFAULT '',
  operation VARCHAR(96) NOT NULL DEFAULT 'chat',
  success BOOLEAN NOT NULL DEFAULT TRUE,
  status_code INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  estimated_cost_microusd BIGINT,
  error_type VARCHAR(96),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Python YouDub management summary: read-only source-compatible fixture.
CREATE TABLE dubbing_jobs (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    subtitle_job_id UUID,
    status VARCHAR(24) NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    lease_owner VARCHAR(128),
    lease_until TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    operation_key VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Python outbox event envelope used by original subtitle workflow dispatcher.
-- This disposable test database is never shared with the active broker.
CREATE TABLE outbox_events (
 id UUID PRIMARY KEY,
 event_type VARCHAR(128) NOT NULL,
 aggregate_type VARCHAR(128) NOT NULL,
 aggregate_id VARCHAR(128) NOT NULL,
 task_name VARCHAR(255) NOT NULL,
 args_json JSONB NOT NULL DEFAULT '{}'::jsonb,
 operation_key VARCHAR(255) NOT NULL,
 status VARCHAR(32) NOT NULL DEFAULT 'pending',
 available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 attempt_count INTEGER NOT NULL DEFAULT 0,
 lease_owner VARCHAR(128),
 lease_until TIMESTAMPTZ,
 heartbeat_at TIMESTAMPTZ,
 last_error VARCHAR(1024),
 delivered_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_events_operation_key ON outbox_events(operation_key);

-- Python SubtitleJob response shape (same disposable DB as Rust).
CREATE TABLE subtitle_jobs (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0,
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  logs_key TEXT,
  lease_owner VARCHAR(128),
  lease_until TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  operation_key VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Source-compatible Python RenderWorker admin data, READ-ONLY via Rust.
CREATE TABLE render_workers (
  id UUID PRIMARY KEY,
  worker_key VARCHAR(128) NOT NULL UNIQUE,
  name VARCHAR(128) NOT NULL,
  platform VARCHAR(32) NOT NULL,
  architecture VARCHAR(32),
  version VARCHAR(64) NOT NULL DEFAULT '',
  protocol_version INTEGER NOT NULL DEFAULT 1,
  render_spec_versions JSONB NOT NULL DEFAULT '[1]'::jsonb,
  capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
  resources JSONB NOT NULL DEFAULT '{}'::jsonb,
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  status VARCHAR(24) NOT NULL DEFAULT 'online',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  draining BOOLEAN NOT NULL DEFAULT FALSE,
  max_concurrency INTEGER NOT NULL DEFAULT 1,
  active_jobs INTEGER NOT NULL DEFAULT 0,
  credential_hash VARCHAR(64),
  credential_issued_at TIMESTAMPTZ,
  credential_revoked_at TIMESTAMPTZ,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE render_worker_enrollments (
  id UUID PRIMARY KEY,
  label VARCHAR(128) NOT NULL DEFAULT '',
  status VARCHAR(24) NOT NULL DEFAULT 'active',
  token_hash VARCHAR(64),
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  worker_id UUID REFERENCES render_workers(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE render_jobs (
  id UUID PRIMARY KEY,
  task_id UUID REFERENCES tasks(id),
  subtitle_job_id UUID,
  status TEXT NOT NULL DEFAULT 'queued',
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  progress INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  error_message TEXT,
  lease_owner VARCHAR(128),
  lease_until TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  operation_key VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE render_executions (
  id UUID PRIMARY KEY,
  render_job_id UUID NOT NULL REFERENCES render_jobs(id),
  worker_id UUID NOT NULL REFERENCES render_workers(id),
  attempt INTEGER NOT NULL DEFAULT 1,
  fence_token VARCHAR(64) NOT NULL DEFAULT 'isolated-fence',
  state VARCHAR(24) NOT NULL DEFAULT 'claimed',
  render_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  capability_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  transfer_mode VARCHAR(24) NOT NULL DEFAULT 'http',
  progress INTEGER NOT NULL DEFAULT 0,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  log_tail TEXT,
  error_message TEXT,
  output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  lease_until TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Original batch receipts are authoritative. V2 must never invent
-- a synthetic successful batch from unrelated job stages.
CREATE TABLE publish_batches (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    state VARCHAR(32) NOT NULL DEFAULT 'active',
    expected_targets JSONB NOT NULL DEFAULT '[]'::jsonb,
    request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcomes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    cleanup_enqueued_at TIMESTAMPTZ,
    cleanup_delivery_version INTEGER NOT NULL DEFAULT 2,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Original PublishJob columns required for Python/Rust same-DB diff,
-- plus the independent SAU account receipt used by prior fixtures.
CREATE TABLE publish_jobs (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks(id),
    batch_id UUID REFERENCES publish_batches(id),
    platform TEXT NOT NULL,
    account_id UUID,
    bili_account_id UUID,
    meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    cover_key TEXT,
    state TEXT NOT NULL,
    external_id TEXT,
    external_url TEXT,
    bvid TEXT,
    aid TEXT,
    response_json JSONB,
    upload_progress INTEGER NOT NULL DEFAULT 0,
    upload_active BOOLEAN NOT NULL DEFAULT false,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    operation_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE playout_asset_links (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES tasks(id),
  asset_id UUID NOT NULL REFERENCES assets(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TYPE youtube_source_type AS ENUM ('channel','playlist');
CREATE TYPE youtube_source_license AS ENUM ('own','authorized','cc','unknown');
CREATE TABLE youtube_sources (
  id UUID PRIMARY KEY,
  source_type youtube_source_type NOT NULL,
  source_id VARCHAR(128) NOT NULL,
  source_url TEXT,
  display_name VARCHAR(255),
  license youtube_source_license NOT NULL DEFAULT 'unknown',
  proof_url TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  scan_interval_minutes INTEGER NOT NULL DEFAULT 60,
  scan_limit INTEGER NOT NULL DEFAULT 20,
  auto_process BOOLEAN NOT NULL DEFAULT true,
  last_scan_started_at TIMESTAMPTZ,
  last_scan_finished_at TIMESTAMPTZ,
  last_scan_discovered_count INTEGER NOT NULL DEFAULT 0,
  last_scan_created_count INTEGER NOT NULL DEFAULT 0,
  last_scan_started_pipeline_count INTEGER NOT NULL DEFAULT 0,
  last_scan_skipped_duplicates INTEGER NOT NULL DEFAULT 0,
  last_scan_error TEXT,
  last_nonempty_scan_finished_at TIMESTAMPTZ,
  last_nonempty_discovered_count INTEGER NOT NULL DEFAULT 0,
  total_scan_count INTEGER NOT NULL DEFAULT 0,
  total_failed_scan_count INTEGER NOT NULL DEFAULT 0,
  total_created_count INTEGER NOT NULL DEFAULT 0,
  total_started_pipeline_count INTEGER NOT NULL DEFAULT 0,
  scan_lock_owner VARCHAR(128),
  scan_lock_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(source_type,source_id)
);

-- Source-compatible translation dictionaries for isolated API differential.
CREATE TABLE translation_dictionary_sources (
 id UUID PRIMARY KEY,
 name TEXT NOT NULL DEFAULT '',
 slug TEXT NOT NULL UNIQUE,
 description TEXT NOT NULL DEFAULT '',
 source_lang VARCHAR(16) NOT NULL DEFAULT '',
 target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
 format VARCHAR(32) NOT NULL DEFAULT 'csv',
 license TEXT NOT NULL DEFAULT '',
 license_url TEXT NOT NULL DEFAULT '',
 source_url TEXT NOT NULL DEFAULT '',
 version TEXT NOT NULL DEFAULT '',
 attribution TEXT NOT NULL DEFAULT '',
 domain TEXT NOT NULL DEFAULT '',
 priority INTEGER NOT NULL DEFAULT 0,
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
 entry_count INTEGER NOT NULL DEFAULT 0,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE translation_dictionary_import_batches (
 id UUID PRIMARY KEY,
 source_id UUID REFERENCES translation_dictionary_sources(id) ON DELETE CASCADE,
 status VARCHAR(32) NOT NULL DEFAULT 'running',
 filename TEXT NOT NULL DEFAULT '',
 archive_path TEXT NOT NULL DEFAULT '',
 file_sha256 VARCHAR(64) NOT NULL DEFAULT '',
 file_size_bytes BIGINT NOT NULL DEFAULT 0,
 format VARCHAR(32) NOT NULL DEFAULT 'csv',
 import_mode VARCHAR(32) NOT NULL DEFAULT 'upsert',
 requested_by VARCHAR(64) NOT NULL DEFAULT 'manual',
 stats JSONB NOT NULL DEFAULT '{}'::jsonb,
 error TEXT NOT NULL DEFAULT '',
 started_at TIMESTAMPTZ,
 finished_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE translation_dictionary_entries (
 id UUID PRIMARY KEY,
 source_id UUID NOT NULL REFERENCES translation_dictionary_sources(id) ON DELETE CASCADE,
 batch_id UUID REFERENCES translation_dictionary_import_batches(id) ON DELETE SET NULL,
 source_lang VARCHAR(16) NOT NULL DEFAULT '',
 target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
 term TEXT NOT NULL DEFAULT '',
 normalized_term TEXT NOT NULL DEFAULT '',
 translations JSONB NOT NULL DEFAULT '[]'::jsonb,
 translation_text TEXT NOT NULL DEFAULT '',
 pos TEXT NOT NULL DEFAULT '',
 definition TEXT NOT NULL DEFAULT '',
 domain TEXT NOT NULL DEFAULT '',
 tags JSONB NOT NULL DEFAULT '[]'::jsonb,
 aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
 examples JSONB NOT NULL DEFAULT '[]'::jsonb,
 metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
 quality DOUBLE PRECISION NOT NULL DEFAULT 0,
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 usage_count INTEGER NOT NULL DEFAULT 0,
 last_lookup_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_vr2_test_dict_entry_norm ON translation_dictionary_entries
 (source_id, source_lang,target_lang,normalized_term,domain);
-- Original RAG knowledge schema for dictionary promotion and real pgvector tests.
-- Kept separate from vr2_ tables; all data lives in the disposable test DB.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS translation_knowledge_items (
 id UUID PRIMARY KEY,
 item_type VARCHAR(32) NOT NULL DEFAULT 'document',
 term TEXT NOT NULL DEFAULT '',
 normalized_term TEXT NOT NULL DEFAULT '',
 translation TEXT NOT NULL DEFAULT '',
 target_lang VARCHAR(16) NOT NULL DEFAULT 'zh',
 domain TEXT NOT NULL DEFAULT '',
 aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
 title TEXT NOT NULL DEFAULT '',
 content TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '',
 sources JSONB NOT NULL DEFAULT '[]'::jsonb,
 confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
 status VARCHAR(32) NOT NULL DEFAULT 'approved',
 created_by VARCHAR(32) NOT NULL DEFAULT 'manual',
 usage_count INTEGER NOT NULL DEFAULT 0,
 embedding vector,
 embedding_model TEXT NOT NULL DEFAULT '',
 embedding_text_hash VARCHAR(64) NOT NULL DEFAULT '',
 last_verified_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
