use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Download,
    Subtitle,
    Render,
    Dubbing,
    Publish,
    Cleanup,
}
impl Stage {
    pub const ORDER: [Stage; 6] = [
        Stage::Download,
        Stage::Subtitle,
        Stage::Render,
        Stage::Dubbing,
        Stage::Publish,
        Stage::Cleanup,
    ];
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Download => "download",
            Self::Subtitle => "subtitle",
            Self::Render => "render",
            Self::Dubbing => "dubbing",
            Self::Publish => "publish",
            Self::Cleanup => "cleanup",
        }
    }
    pub fn after(self) -> Option<Self> {
        let pos = Self::ORDER.iter().position(|v| *v == self)?;
        Self::ORDER.get(pos + 1).copied()
    }
    pub fn from_name(name: &str) -> Option<Self> {
        Self::ORDER.into_iter().find(|stage| stage.as_str() == name)
    }
    pub fn has_irreversible_external_effect(self) -> bool {
        matches!(self, Self::Publish)
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkState {
    Pending,
    Running,
    Awaiting,
    Succeeded,
    Failed,
    Unknown,
    Canceled,
}
impl WorkState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Running => "running",
            Self::Awaiting => "awaiting",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Unknown => "unknown",
            Self::Canceled => "canceled",
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct TaskInfo {
    pub id: Uuid,
    pub source_type: String,
    pub source_url: Option<String>,
    pub status: String,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub priority: i32,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct AssetInfo {
    pub id: Uuid,
    pub task_id: Uuid,
    pub kind: String,
    pub storage_key: String,
    pub sha256: Option<String>,
    pub size_bytes: Option<i64>,
    pub duration_ms: Option<i64>,
    pub created_at: DateTime<Utc>,
}
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct WorkflowInfo {
    pub id: Uuid,
    pub task_id: Uuid,
    pub generation: i32,
    pub state: String,
    pub current_stage: String,
    pub configuration: Value,
    pub last_error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkItem {
    pub id: Uuid,
    pub workflow_id: Uuid,
    pub task_id: Uuid,
    pub stage: Stage,
    pub generation: i32,
    pub operation_key: String,
    pub lease_token: Uuid,
    pub attempts: i32,
    pub max_attempts: i32,
    pub payload: Value,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum StageOutcome {
    Completed { result: Value },
    Skipped { reason: String },
    Awaiting { ticket: String },
}
impl StageOutcome {
    pub fn result_payload(&self) -> Value {
        match self {
            Self::Completed { result } => result.clone(),
            Self::Skipped { reason } => serde_json::json!({"skipped":reason}),
            Self::Awaiting { ticket } => serde_json::json!({"ticket":ticket}),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stage_chain_never_skips_publish() {
        assert_eq!(Stage::ORDER.len(), 6);
        assert_eq!(Stage::Render.after(), Some(Stage::Dubbing));
        assert_eq!(Stage::Dubbing.after(), Some(Stage::Publish));
        assert_eq!(Stage::Cleanup.after(), None);
    }
    #[test]
    fn only_publish_is_irreversible() {
        assert!(Stage::Publish.has_irreversible_external_effect());
        for stage in Stage::ORDER {
            if stage != Stage::Publish {
                assert!(!stage.has_irreversible_external_effect());
            }
            assert_eq!(Stage::from_name(stage.as_str()), Some(stage));
        }
    }
}
