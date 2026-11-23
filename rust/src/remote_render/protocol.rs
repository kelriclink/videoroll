use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

fn default_one() -> i32 {
    1
}
fn default_spec() -> Vec<i32> {
    vec![1]
}
fn default_object() -> Value {
    serde_json::json!({})
}
#[derive(Debug, Clone, Deserialize)]
pub struct EnrollRequest {
    pub worker_key: String,
    pub name: String,
    pub platform: String,
    #[serde(default)]
    pub architecture: Option<String>,
    #[serde(default)]
    pub version: String,
    #[serde(default = "default_one")]
    pub protocol_version: i32,
    #[serde(default = "default_spec")]
    pub render_spec_versions: Vec<i32>,
    #[serde(default = "default_object")]
    pub capabilities: Value,
    #[serde(default = "default_object")]
    pub resources: Value,
    #[serde(default = "default_object")]
    pub labels: Value,
    #[serde(default = "default_one")]
    pub max_concurrency: i32,
    pub enrollment_token: String,
}
#[derive(Debug, Clone, Deserialize)]
pub struct WorkerHeartbeat {
    pub status: String,
    pub resources: Value,
    pub capabilities: Option<Value>,
    #[serde(default)]
    pub active_execution_ids: Vec<Uuid>,
}
#[derive(Debug, Clone, Deserialize)]
pub struct ClaimRequest {
    pub available_slots: usize,
    pub accepted_transfer_modes: Vec<String>,
    pub available_encoders: Vec<String>,
}
#[derive(Debug, Clone, Serialize)]
pub struct WorkerInfo {
    pub id: Uuid,
    pub worker_key: String,
    pub name: String,
    pub platform: String,
    pub architecture: Option<String>,
    pub version: String,
    pub protocol_version: i32,
    pub render_spec_versions: Value,
    pub capabilities: Value,
    pub resources: Value,
    pub labels: Value,
    pub status: String,
    pub enabled: bool,
    pub draining: bool,
    pub max_concurrency: i32,
    pub active_jobs: i64,
    pub last_seen_at: DateTime<Utc>,
}
#[derive(Debug, Clone, Serialize)]
pub struct EnrollmentInfo {
    pub id: Uuid,
    pub label: String,
    pub expires_at: DateTime<Utc>,
    pub server_url: String,
    pub token: String,
}
#[derive(Debug, Deserialize)]
pub struct EnrollmentCreate {
    #[serde(default)]
    pub label: String,
    #[serde(default = "ttl")]
    pub ttl_minutes: i64,
}
fn ttl() -> i64 {
    30
}
#[derive(Debug, Serialize)]
pub struct WorkerResponse {
    pub worker: WorkerInfo,
    pub credential: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ArtifactSpec {
    pub role: String,
    pub storage_key: String,
    pub size_bytes: Option<u64>,
    pub sha256: Option<String>,
    pub download_url: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct RenderSpec {
    pub schema_version: u32,
    pub render_job_id: Uuid,
    pub task_id: Uuid,
    pub mode: String,
    pub request: Value,
    pub artifacts: Vec<ArtifactSpec>,
}
#[derive(Debug, Serialize)]
pub struct ExecutionInfo {
    pub id: Uuid,
    pub render_job_id: Uuid,
    pub worker_id: Uuid,
    pub attempt: i32,
    pub fence_token: String,
    pub state: String,
    pub transfer_mode: String,
    pub progress: i32,
    pub lease_until: Option<DateTime<Utc>>,
    pub render_spec: Value,
}
#[derive(Debug, Serialize)]
pub struct ClaimResponse {
    pub execution: Option<ExecutionInfo>,
    pub render_spec: Option<RenderSpec>,
    pub retry_after_seconds: u32,
}
#[derive(Debug, Deserialize)]
pub struct ExecutionHeartbeat {
    pub fence_token: String,
    pub progress: Option<i32>,
    #[serde(default)]
    pub metrics: Value,
    pub stage: Option<String>,
}
#[derive(Debug, Deserialize)]
pub struct ExecutionLog {
    pub fence_token: String,
    pub text: String,
}
#[derive(Debug, Deserialize)]
pub struct ExecutionComplete {
    pub fence_token: String,
    pub output_asset_id: Option<Uuid>,
    #[serde(default)]
    pub output: Value,
}
#[derive(Debug, Deserialize)]
pub struct ExecutionFail {
    pub fence_token: String,
    pub error: String,
    #[serde(default = "yes")]
    pub retryable: bool,
}
fn yes() -> bool {
    true
}
#[derive(Debug, Serialize)]
pub struct CancelState {
    pub cancel_requested: bool,
    pub reason: Option<String>,
}
#[derive(Debug, Deserialize)]
pub struct OutputProbe {
    pub fence_token: String,
}
