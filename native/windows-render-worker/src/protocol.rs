use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize)]
pub struct EnrollRequest {
    pub worker_key: String,
    pub name: String,
    pub platform: String,
    pub architecture: Option<String>,
    pub version: String,
    pub protocol_version: u32,
    pub render_spec_versions: Vec<u32>,
    pub capabilities: Value,
    pub resources: Value,
    pub labels: Value,
    pub max_concurrency: usize,
    pub enrollment_token: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WorkerRead {
    pub id: Uuid,
    pub worker_key: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct EnrollResponse {
    pub worker: WorkerRead,
    pub credential: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkerHeartbeat {
    pub status: String,
    pub resources: Value,
    pub capabilities: Value,
    pub active_execution_ids: Vec<Uuid>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ClaimRequest {
    pub available_slots: usize,
    pub accepted_transfer_modes: Vec<String>,
    pub available_encoders: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ClaimResponse {
    pub execution: Option<ExecutionRead>,
    pub render_spec: Option<RenderSpec>,
    #[serde(default = "default_retry")]
    pub retry_after_seconds: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ExecutionRead {
    pub id: Uuid,
    pub fence_token: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RenderSpec {
    pub schema_version: u32,
    pub render_job_id: Uuid,
    pub task_id: Uuid,
    pub mode: String,
    #[serde(default)]
    pub request: Value,
    #[serde(default)]
    pub artifacts: Vec<ArtifactSpec>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ArtifactSpec {
    pub role: String,
    pub storage_key: String,
    pub size_bytes: Option<u64>,
    pub sha256: Option<String>,
    pub download_url: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CancelState {
    pub cancel_requested: bool,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AssetRead {
    pub id: Uuid,
}

fn default_retry() -> u64 {
    5
}
