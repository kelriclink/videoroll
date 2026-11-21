-- Link P2 to the same succeeded native YouDub job and corresponding SRT.
CREATE TABLE IF NOT EXISTS vr2_dubbing_runs (
    job_id UUID PRIMARY KEY,
    workflow_id UUID NOT NULL REFERENCES vr2_workflows(id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    source_video_key TEXT NOT NULL,
    source_srt_key TEXT NOT NULL,
    output_video_key TEXT NOT NULL,
    model_job_id UUID NOT NULL,
    model_status TEXT NOT NULL CHECK(model_status IN ('succeeded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workflow_id, output_video_key)
);
CREATE INDEX IF NOT EXISTS ix_vr2_dubbing_task ON vr2_dubbing_runs(task_id, created_at);
