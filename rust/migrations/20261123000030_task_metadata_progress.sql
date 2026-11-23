-- Fold purely task-scoped V2 metadata, moderation and download progress
-- into the original Python tasks business row. Rust V2 is not deployed and
-- has no production history. Reject nonempty shadow tables rather than
-- silently discarding records in an unexpected environment.
DO $check$
BEGIN
 IF EXISTS(SELECT 1 FROM vr2_publish_meta)
 OR EXISTS(SELECT 1 FROM vr2_publish_reviews)
 OR EXISTS(SELECT 1 FROM vr2_download_progress) THEN
   RAISE EXCEPTION 'legacy vr2 metadata/review/download tables unexpectedly contain data; reconcile before migrating';
 END IF;
END $check$;

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_meta_json JSONB;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_meta_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_meta_sha256 CHAR(64);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_meta_updated_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_review_fingerprint CHAR(64);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_review_approved BOOLEAN;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_review_result JSONB;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS publish_review_checked_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_job_id UUID;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_total_bytes BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_speed DOUBLE PRECISION;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_eta_seconds INTEGER;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_percent SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS download_stage_updated_at TIMESTAMPTZ;
ALTER TABLE tasks ADD CONSTRAINT fk_task_download_stage_job
 FOREIGN KEY(download_stage_job_id) REFERENCES operation_inbox(id) ON DELETE RESTRICT;
ALTER TABLE tasks ADD CONSTRAINT ck_task_download_telemetry
 CHECK(download_stage_bytes>=0 AND (download_stage_total_bytes IS NULL OR download_stage_total_bytes>=0)
       AND (download_stage_speed IS NULL OR download_stage_speed>=0)
       AND (download_stage_eta_seconds IS NULL OR download_stage_eta_seconds>=0)
       AND download_stage_percent BETWEEN 0 AND 99);

DROP TABLE vr2_publish_meta;
DROP TABLE vr2_publish_reviews;
DROP TABLE vr2_download_progress;

CREATE VIEW vr2_publish_meta AS
 SELECT id AS task_id,publish_meta_json AS value_json,
        publish_meta_revision AS revision,publish_meta_sha256 AS sha256,
        publish_meta_updated_at AS updated_at
 FROM tasks WHERE publish_meta_json IS NOT NULL;
CREATE VIEW vr2_publish_reviews AS
 SELECT id AS task_id,publish_review_fingerprint AS fingerprint,
        publish_review_approved AS approved,publish_review_result AS result,
        publish_review_checked_at AS checked_at
 FROM tasks WHERE publish_review_fingerprint IS NOT NULL;
CREATE VIEW vr2_download_progress AS
 SELECT id AS task_id,download_stage_job_id AS job_id,
        download_stage_bytes AS downloaded_bytes,
        download_stage_total_bytes AS total_bytes,
        download_stage_speed AS speed_bytes_per_second,
        download_stage_eta_seconds AS eta_seconds,
        download_stage_percent AS percent,download_stage_updated_at AS updated_at
 FROM tasks WHERE download_stage_job_id IS NOT NULL;
COMMENT ON VIEW vr2_publish_meta IS 'Read-only transitional projection of original tasks metadata';
COMMENT ON VIEW vr2_publish_reviews IS 'Read-only transitional projection of original tasks review decision';
COMMENT ON VIEW vr2_download_progress IS 'Read-only transitional projection of original tasks download telemetry';
