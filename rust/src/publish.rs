//! Publish side-effects have strictly different retry semantics than rendering.
use crate::{AppError, AppResult};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublishIntent {
    pub task_id: Uuid,
    pub batch_id: Uuid,
    pub platform: String,
    pub account_id: Uuid,
    pub operation_key: String,
    pub parts: Vec<VideoPart>,
    pub metadata: Value,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VideoPart {
    pub storage_key: String,
    pub title: String,
}
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubmissionBoundary {
    Prepared,
    Uploading,
    Submitting,
    Confirmed,
    Unknown,
}
impl SubmissionBoundary {
    pub fn automatic_retry_is_safe(self) -> bool {
        matches!(self, Self::Prepared | Self::Uploading)
    }
}
impl PublishIntent {
    pub fn validate(&self) -> AppResult<()> {
        if self.operation_key.trim().is_empty() || self.platform.trim().is_empty() {
            return Err(AppError::Invalid(
                "publish intent requires platform and operation_key".into(),
            ));
        }
        if self.parts.is_empty() || self.parts.len() > 2 {
            return Err(AppError::Invalid(
                "Bilibili publish must contain 1 or 2 video parts".into(),
            ));
        }
        for part in &self.parts {
            crate::storage::AssetRegistry::checked_key(&part.storage_key)?;
            if part.title.trim().is_empty() {
                return Err(AppError::Invalid("publish video title is empty".into()));
            }
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn submission_might_have_happened() {
        assert!(SubmissionBoundary::Prepared.automatic_retry_is_safe());
        assert!(SubmissionBoundary::Uploading.automatic_retry_is_safe());
        assert!(!SubmissionBoundary::Submitting.automatic_retry_is_safe());
        assert!(!SubmissionBoundary::Unknown.automatic_retry_is_safe());
        assert!(!SubmissionBoundary::Confirmed.automatic_retry_is_safe());
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishJobState {
    Draft,
    Submitting,
    Submitted,
    Published,
    Unknown,
    Failed,
}
impl PublishJobState {
    pub fn is_success(self) -> bool {
        matches!(self, Self::Submitted | Self::Published)
    }
    pub fn is_failure(self) -> bool {
        matches!(self, Self::Unknown | Self::Failed)
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BatchState {
    Active,
    Succeeded,
    PartialFailed,
    Failed,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct BatchDecision {
    pub batch_state: BatchState,
    pub task_status: &'static str,
    pub cleanup_ready: bool,
}
pub fn evaluate_batch(states: &[Option<PublishJobState>]) -> BatchDecision {
    let mut success = false;
    let mut failure = false;
    let mut pending = false;
    for state in states {
        match state {
            Some(value) if value.is_success() => success = true,
            Some(value) if value.is_failure() => failure = true,
            _ => pending = true,
        }
    }
    if !states.is_empty() && !pending && !failure {
        BatchDecision {
            batch_state: BatchState::Succeeded,
            task_status: "PUBLISHED",
            cleanup_ready: true,
        }
    } else if pending {
        BatchDecision {
            batch_state: BatchState::Active,
            task_status: "PUBLISHING",
            cleanup_ready: false,
        }
    } else if success {
        BatchDecision {
            batch_state: BatchState::PartialFailed,
            task_status: "PUBLISHING",
            cleanup_ready: false,
        }
    } else {
        BatchDecision {
            batch_state: BatchState::Failed,
            task_status: "FAILED",
            cleanup_ready: false,
        }
    }
}
#[cfg(test)]
mod publish_lifecycle_tests {
    use super::*;
    #[test]
    fn matches_legacy_batch_status_semantics() {
        use PublishJobState::*;
        assert_eq!(
            evaluate_batch(&[Some(Published)]).batch_state,
            BatchState::Succeeded
        );
        assert_eq!(
            evaluate_batch(&[Some(Submitted), Some(Published)]).batch_state,
            BatchState::Succeeded
        );
        assert_eq!(
            evaluate_batch(&[Some(Published), Some(Failed)]).batch_state,
            BatchState::PartialFailed
        );
        assert_eq!(
            evaluate_batch(&[Some(Published), Some(Unknown)]).batch_state,
            BatchState::PartialFailed
        );
        assert_eq!(
            evaluate_batch(&[Some(Failed)]).batch_state,
            BatchState::Failed
        );
        assert_eq!(
            evaluate_batch(&[Some(Unknown)]).batch_state,
            BatchState::Failed
        );
        assert_eq!(
            evaluate_batch(&[Some(Draft), Some(Published)]).batch_state,
            BatchState::Active
        );
        assert_eq!(evaluate_batch(&[]).batch_state, BatchState::Failed);
        assert!(evaluate_batch(&[Some(Published)]).cleanup_ready);
        assert!(!evaluate_batch(&[Some(Submitted), Some(Unknown)]).cleanup_ready);
    }
}

/// Never turn an upstream HTTP 200 into a claimed publication without a verifiable receipt.
/// 'confirmed' alone is not proof; require a platform result identifier or signed receipt.
pub fn has_confirmed_result(result: &Value) -> bool {
    if result.get("confirmed").and_then(Value::as_bool) != Some(true) {
        return false;
    }
    result
        .get("bvid")
        .or_else(|| result.get("external_id"))
        .and_then(Value::as_str)
        .is_some_and(|s| (6..=200).contains(&s.len()))
}
#[cfg(test)]
mod receipt_tests {
    use super::*;
    #[test]
    fn success_http_status_alone_is_not_publish_proof() {
        assert!(!has_confirmed_result(&serde_json::json!({"status":"ok"})));
        assert!(!has_confirmed_result(
            &serde_json::json!({"confirmed":true})
        ));
        assert!(has_confirmed_result(
            &serde_json::json!({"confirmed":true,"bvid":"BV0123456789"})
        ));
    }
}
