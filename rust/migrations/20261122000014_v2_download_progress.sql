-- Persist only bounded numeric progress, never URLs, cookies, or source logs.
CREATE TABLE IF NOT EXISTS vr2_download_progress (
    task_id UUID PRIMARY KEY REFERENCES vr2_managed_tasks(task_id) ON DELETE RESTRICT,
    job_id UUID NOT NULL REFERENCES vr2_jobs(id) ON DELETE RESTRICT,
    downloaded_bytes BIGINT NOT NULL DEFAULT 0 CHECK(downloaded_bytes>=0),
    total_bytes BIGINT CHECK(total_bytes>=0),
    speed_bytes_per_second DOUBLE PRECISION CHECK(speed_bytes_per_second>=0),
    eta_seconds INTEGER CHECK(eta_seconds>=0),
    percent SMALLINT NOT NULL DEFAULT 0 CHECK(percent BETWEEN 0 AND 99),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
