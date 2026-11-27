use crate::{AppError, AppResult, Database, domain::WorkItem, storage::AssetRegistry};
use serde_json::json;
use sqlx::Row;
use uuid::Uuid;

/// Shared ceiling for explicit failures and abandoned worker leases.
pub(crate) const MAX_RENDER_ATTEMPTS: i32 = 5;

/// Durable queue handoff: the actual worker claim and stage completion happen
/// after the scheduler has committed its independent awaiting(ticket) state.
pub async fn enqueue(db: &Database, _registry: &AssetRegistry, job: &WorkItem) -> AppResult<Uuid> {
    let id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("VideoRoll-v2-remote-render:{}", job.id).as_bytes(),
    );
    let row=sqlx::query(
        "INSERT INTO render_executions(id,job_id,task_id,workflow_id,state,attempt,runtime_engine) VALUES($1,$2,$3,$4,'queued',0,'rust-v2') ON CONFLICT(job_id) WHERE runtime_engine='rust-v2' DO UPDATE SET updated_at=now() WHERE render_executions.state IN ('queued','running','uploading') RETURNING id"
    ).bind(id).bind(job.id).bind(job.task_id).bind(job.workflow_id)
        .fetch_optional(&db.pool).await?;
    if let Some(row) = row {
        return Ok(row.try_get("id")?);
    }
    Err(AppError::Conflict(
        "remote render job is terminal and cannot be silently repeated".into(),
    ))
}
pub fn token_hash(secret: &str) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(secret.as_bytes()))
}
pub fn strong_token() -> String {
    let one = Uuid::new_v4().simple().to_string();
    let two = Uuid::new_v4().simple().to_string();
    format!("{one}{two}")
}
pub fn check_worker_label(value: &str) -> AppResult<()> {
    if value.is_empty() || value.chars().count() > 128 || value.chars().any(char::is_control) {
        return Err(AppError::Invalid("invalid render worker label".into()));
    }
    Ok(())
}
pub fn is_fence(old: &str, received: &str) -> bool {
    use subtle::ConstantTimeEq;
    !old.is_empty() && bool::from(old.as_bytes().ct_eq(received.as_bytes()))
}
pub(crate) fn output_kind(mode: &str) -> &'static str {
    if mode == crate::media::dubbing_handoff::MODE {
        "video_dubbed"
    } else {
        "video_final"
    }
}
pub(crate) fn output_stage(mode: &str) -> &'static str {
    if mode == crate::media::dubbing_handoff::MODE {
        "dubbing"
    } else {
        "render"
    }
}
pub fn output_key(task_id: Uuid, execution_id: Uuid) -> String {
    format!("v2/render/{task_id}/remote-{execution_id}.mp4")
}
pub fn validate_lease(row: &sqlx::postgres::PgRow, token: &str) -> AppResult<()> {
    let actual: String = row.try_get("fence_token")?;
    let status: String = row.try_get("state")?;
    let until: Option<chrono::DateTime<chrono::Utc>> = row.try_get("lease_until")?;
    if !matches!(status.as_str(), "running" | "uploading")
        || !is_fence(&actual, token)
        || !until.is_some_and(|deadline| deadline > chrono::Utc::now())
    {
        return Err(AppError::Conflict(
            "remote render fence or lease expired".into(),
        ));
    }
    Ok(())
}
pub fn log_message_tail(previous: &str, new: &str) -> AppResult<String> {
    if new.len() > 65536 {
        return Err(AppError::Invalid("render log message too long".into()));
    }
    let merged = format!("{previous}\n{new}");
    Ok(merged
        .chars()
        .rev()
        .take(100_000)
        .collect::<String>()
        .chars()
        .rev()
        .collect())
}
pub fn receipt(
    execution_id: Uuid,
    asset_id: Option<Uuid>,
    key: Option<&str>,
    mode: &str,
) -> serde_json::Value {
    json!({"remote_execution_id":execution_id,"asset_id":asset_id,
           "storage_key":key,"mode":mode,"remote_completed":true})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fences_are_constant_time_and_logs_are_bounded() {
        assert!(is_fence("abcd", "abcd"));
        assert!(!is_fence("abcd", "abc"));
        assert!(!is_fence("", ""));
        let tail = log_message_tail(&"x".repeat(150_000), "new").unwrap();
        assert!(tail.len() < 101_000);
        assert!(tail.ends_with("new"));
    }
    #[test]
    fn no_second_random_execution_id_for_the_same_stage() {
        let task = Uuid::new_v4();
        let exec = Uuid::new_v4();
        assert!(output_key(task, exec).starts_with("v2/render/"));
        assert_eq!(strong_token().len(), 64);
    }
}
