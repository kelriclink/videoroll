//! Optional compatibility adapter to the existing VideoRoll publisher service.
//! The live platform operation is never retried on an ambiguous network failure.
//! Remove only after a Rust native Bilibili/social publisher passes parity tests.
use crate::{AppError, AppResult, Config, Database, domain::WorkItem};
use reqwest::{Client, Url};
use serde_json::{Value, json};
use sqlx::Row;
use std::time::Duration;
use tokio::time::sleep;
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct PublishSelection {
    pub enabled: bool,
    pub platforms: Vec<String>,
}
pub fn selection(saved: &Value, overrides: &Value) -> PublishSelection {
    let enabled = overrides
        .get("auto_publish")
        .and_then(Value::as_bool)
        .or_else(|| saved.get("auto_publish").and_then(Value::as_bool))
        .unwrap_or(true);
    let choices = overrides
        .get("auto_publish_platforms")
        .or_else(|| saved.get("auto_publish_platforms"))
        .and_then(Value::as_array);
    let platforms = choices
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|s| matches!(*s, "bilibili" | "douyin" | "xiaohongshu" | "kuaishou"))
        .map(str::to_string)
        .collect::<Vec<_>>();
    PublishSelection { enabled, platforms }
}
pub async fn read_selection(db: &Database, job: &WorkItem) -> AppResult<PublishSelection> {
    let saved: Option<Value> = Some(crate::settings::effective(db, "subtitle.auto_profile").await?);
    let overrides: Option<Value> =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
            .bind(job.workflow_id)
            .fetch_optional(&db.pool)
            .await?;
    Ok(selection(
        &saved.unwrap_or(json!({})),
        &overrides.unwrap_or(json!({})),
    ))
}
#[derive(Debug, Clone)]
struct PublishedReceipt {
    platform: String,
    state: String,
    identity: String,
    bvid: Option<String>,
}
fn trusted_api(base: &str) -> AppResult<String> {
    let url = Url::parse(base)
        .map_err(|_| AppError::Invalid("invalid legacy publisher base URL".into()))?;
    let allowed = url.scheme() == "https"
        || (url.scheme() == "http"
            && matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1")));
    if !allowed || url.username() != "" || url.password().is_some() {
        return Err(AppError::Invalid(
            "legacy publisher endpoint must use HTTPS or loopback".into(),
        ));
    }
    Ok(base.trim_end_matches('/').to_owned())
}
async fn batch_status(db: &Database, batch_id: Uuid, task_id: Uuid) -> AppResult<String> {
    sqlx::query_scalar::<_, String>("SELECT state FROM publish_batches WHERE id=$1 AND task_id=$2")
        .bind(batch_id)
        .bind(task_id)
        .fetch_optional(&db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("legacy publish batch disappeared".into()))
}
async fn confirmed_receipts(db: &Database, batch_id: Uuid, task_id: Uuid) -> AppResult<Value> {
    let batch =
        sqlx::query("SELECT expected_targets FROM publish_batches WHERE id=$1 AND task_id=$2")
            .bind(batch_id)
            .bind(task_id)
            .fetch_one(&db.pool)
            .await?;
    let expected: Value = batch.try_get("expected_targets")?;
    let target_count = expected.as_array().map(Vec::len).unwrap_or(0);
    if target_count == 0 {
        return Err(AppError::Unavailable(
            "publisher did not commit any targets".into(),
        ));
    }
    let rows=sqlx::query(
        "SELECT platform::text AS platform,state::text AS state,external_id,bvid,aid FROM publish_jobs WHERE batch_id=$1 ORDER BY created_at"
    ).bind(batch_id).fetch_all(&db.pool).await?;
    if rows.len() < target_count {
        return Err(AppError::Unavailable(
            "publish batch has incomplete target rows".into(),
        ));
    }
    let mut output = Vec::new();
    let mut bvid = None::<String>;
    let mut external_id = None::<String>;
    for row in rows {
        let state: String = row.try_get("state")?;
        let platform: String = row.try_get("platform")?;
        if !matches!(state.as_str(), "published" | "submitted") {
            return Err(AppError::Unavailable(
                "publish batch contains an unresolved target".into(),
            ));
        }
        let id: Option<String> = row.try_get("external_id")?;
        let bv: Option<String> = row.try_get("bvid")?;
        let aid: Option<String> = row.try_get("aid")?;
        let receipt = if platform == "bilibili" {
            bv.as_deref()
                .filter(|s| !s.is_empty())
                .or(aid.as_deref().filter(|s| !s.is_empty()))
        } else {
            id.as_deref().filter(|s| !s.is_empty())
        };
        let receipt = receipt
            .ok_or_else(|| {
                AppError::Unavailable(format!(
                    "{platform} publisher confirmed state without external identifier"
                ))
            })?
            .to_string();
        if external_id.is_none() {
            external_id = Some(receipt.clone());
        }
        if bvid.is_none() && platform == "bilibili" {
            bvid = Some(receipt.clone());
        }
        output.push(PublishedReceipt {
            platform,
            state,
            identity: receipt,
            bvid: bv,
        });
    }
    Ok(json!({
        "confirmed":true,"external_id":external_id,"bvid":bvid,
        "batch_id":batch_id,"receipts":output.iter().map(|r|json!({
            "platform":r.platform,"state":r.state,"id":r.identity,"bvid":r.bvid
        })).collect::<Vec<_>>()
    }))
}
pub async fn publish(config: &Config, db: &Database, job: &WorkItem) -> AppResult<Value> {
    if !config.legacy_publish_allowed {
        return Err(AppError::Unavailable(
            "legacy publisher is not explicitly enabled in v2".into(),
        ));
    }
    let base =
        trusted_api(config.legacy_orchestrator_url.as_deref().ok_or_else(|| {
            AppError::Unavailable("legacy orchestrator URL not configured".into())
        })?)?;
    let token = config
        .legacy_internal_token
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("legacy orchestrator token not configured".into()))?;
    // v2's separate YouDub P2 is not a v1 DubbingJob, and may not be silently
    // downgraded to single-P. The direct Rust publisher must support this first.
    let v2_p2:Option<Uuid>=sqlx::query_scalar(
        "SELECT job_id FROM vr2_dubbing_runs WHERE workflow_id=$1 AND model_status='succeeded' ORDER BY created_at DESC LIMIT 1"
    ).bind(job.workflow_id).fetch_optional(&db.pool).await?;
    if v2_p2.is_some() {
        return Err(AppError::Unavailable(
            "P2 belongs to v2 YouDub; v1 publishing adapter cannot prove matching pair".into(),
        ));
    }
    let url = format!("{base}/tasks/{}/actions/auto_publish", job.task_id);
    let client = Client::builder()
        .timeout(Duration::from_secs(120))
        .build()?;
    let response = client
        .post(url)
        .header("x-videoroll-internal-token", token)
        .header("x-videoroll-v2-operation-key", &job.operation_key)
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(AppError::Unavailable(format!(
            "orchestrator refused publish request HTTP {}",
            response.status()
        )));
    }
    if response.content_length().is_some_and(|n| n > 1024 * 1024) {
        return Err(AppError::Unavailable(
            "publish handoff response too large".into(),
        ));
    }
    let raw = response.bytes().await?;
    if raw.len() > 1024 * 1024 {
        return Err(AppError::Unavailable(
            "publish handoff response too large".into(),
        ));
    }
    let payload: Value = serde_json::from_slice(&raw)
        .map_err(|_| AppError::Unavailable("invalid publish handoff response".into()))?;
    let status = payload.get("status").and_then(Value::as_str).unwrap_or("");
    if matches!(status, "skipped" | "disabled") {
        return Ok(json!({"skipped":status}));
    }
    if status == "error" {
        return Err(AppError::Unavailable(
            "publisher declined the requested targets".into(),
        ));
    }
    let batch_id = payload
        .get("publish_batch_id")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::Unavailable("publisher did not return a durable batch ID".into())
        })?;
    let batch_id = Uuid::parse_str(batch_id)
        .map_err(|_| AppError::Unavailable("publisher batch ID malformed".into()))?;
    // Only a matching persisted batch from the user's task is trusted.
    for _ in 0..480 {
        match batch_status(db, batch_id, job.task_id).await?.as_str() {
            "succeeded" => return confirmed_receipts(db, batch_id, job.task_id).await,
            "partial_failed" | "failed" => {
                return Err(AppError::Unavailable(
                    "one or more publish targets failed or are uncertain; manual reconciliation required".into()
                ));
            }
            "active" => sleep(Duration::from_secs(15)).await,
            state => {
                return Err(AppError::Unavailable(format!(
                    "publisher returned unrecognized batch state: {state}"
                )));
            }
        }
    }
    Err(AppError::Unavailable(
        "publisher outcome not confirmed within reconciliation window".into(),
    ))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn no_targets_means_no_auto_publish() {
        let p = selection(
            &json!({"auto_publish":true,"auto_publish_platforms":[]}),
            &json!({}),
        );
        assert!(p.enabled);
        assert!(p.platforms.is_empty());
        let p = selection(
            &json!({"auto_publish":false,"auto_publish_platforms":[]}),
            &json!({"auto_publish":true,"auto_publish_platforms":["bilibili","unknown"]}),
        );
        assert!(p.enabled);
        assert_eq!(p.platforms, vec!["bilibili"]);
    }
    #[test]
    fn legacy_request_never_leaks_internal_auth_over_plain_remote_http() {
        assert!(trusted_api("http://8.8.8.8:8000/api").is_err());
        assert!(trusted_api("http://127.0.0.1:8000/api").is_ok());
        assert!(trusted_api("https://private.example.com/api").is_ok());
    }
}
