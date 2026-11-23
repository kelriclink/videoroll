//! Original V1 publish-batch receipts and removed legacy YouDub controls.
//! Batch data is read from the *original* publish_batches/publish_jobs tables.
//! Never synthesize batches from unrelated vr2_jobs or social intents.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::{Row, postgres::PgRow};
use uuid::Uuid;

#[derive(Debug, Deserialize)]
struct BatchQuery {
    limit: Option<i64>,
}
fn job_error(response: Option<&Value>) -> Option<String> {
    let obj = response.and_then(Value::as_object)?;
    let text = ["error", "message", "detail"]
        .iter()
        .filter_map(|key| obj.get(*key))
        .filter_map(|v| v.as_str())
        .find(|v| !v.trim().is_empty())?
        .trim();
    let extras = ["code", "status_code", "v_voucher"]
        .iter()
        .filter_map(|key| {
            let value = obj.get(*key)?;
            let text = match value {
                Value::String(v) => v.trim().to_owned(),
                Value::Number(v) => v.to_string(),
                _ => String::new(),
            };
            if text.is_empty() || text == "0" {
                None
            } else {
                Some(format!("{key}={text}"))
            }
        })
        .collect::<Vec<_>>();
    let summary = if extras.is_empty() {
        text.to_owned()
    } else {
        format!("{} ({})", text, extras.join(", "))
    };
    if summary.chars().count() > 500 {
        Some(format!(
            "{}…",
            summary.chars().take(499).collect::<String>()
        ))
    } else {
        Some(summary)
    }
}
fn latest_key(platform: &str, account_id: Option<Uuid>) -> String {
    format!(
        "{platform}:{}",
        account_id
            .map(|a| a.to_string())
            .unwrap_or_else(|| "default".into())
    )
}
fn merge_outcomes(base: &Value, jobs: impl Iterator<Item = (String, Value)>) -> Value {
    let mut results = base.as_object().cloned().unwrap_or_else(Map::new);
    for (key, value) in jobs {
        results.insert(key, value);
    }
    Value::Object(results)
}
fn receipt(row: &PgRow, outcomes: Value) -> AppResult<Value> {
    Ok(json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "task_id":row.try_get::<Uuid,_>("task_id")?,
        "state":row.try_get::<String,_>("state")?,
        "expected_targets":row.try_get::<Value,_>("expected_targets")?,
        "outcomes":outcomes,
        "cleanup_enqueued_at":row.try_get::<Option<DateTime<Utc>>,_>("cleanup_enqueued_at")?,
        "finished_at":row.try_get::<Option<DateTime<Utc>>,_>("finished_at")?,
        "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
        "updated_at":row.try_get::<DateTime<Utc>,_>("updated_at")?
    }))
}
async fn publish_batches(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Query(query): Query<BatchQuery>,
) -> AppResult<Json<Vec<Value>>> {
    let limit = query.limit.unwrap_or(20);
    if !(1..=100).contains(&limit) {
        return Err(AppError::Invalid("limit must be between 1 and 100".into()));
    }
    crate::legacy_contracts::read_task(&state.db, task_id).await?;
    let rows = sqlx::query(
        "SELECT id,task_id,state,expected_targets,outcomes_json,
          cleanup_enqueued_at,finished_at,created_at,updated_at
         FROM publish_batches WHERE task_id=$1
         ORDER BY created_at DESC LIMIT $2",
    )
    .bind(task_id)
    .bind(limit)
    .fetch_all(&state.db.pool)
    .await?;
    if rows.is_empty() {
        return Ok(Json(Vec::new()));
    }
    let ids = rows
        .iter()
        .map(|r| r.get::<Uuid, _>("id"))
        .collect::<Vec<_>>();
    let jobs = sqlx::query(
        "SELECT id,batch_id,platform::text AS platform,account_id,
          state::text AS state,response_json,created_at,updated_at
         FROM publish_jobs WHERE batch_id=ANY($1)
         ORDER BY updated_at DESC,created_at DESC",
    )
    .bind(&ids)
    .fetch_all(&state.db.pool)
    .await?;
    let mut by_batch = std::collections::HashMap::<Uuid, Map<String, Value>>::new();
    for job in jobs {
        let batch_id = job.try_get::<Uuid, _>("batch_id")?;
        let platform = job.try_get::<String, _>("platform")?;
        let account_id = job.try_get::<Option<Uuid>, _>("account_id")?;
        let key = latest_key(&platform, account_id);
        let outcomes = by_batch.entry(batch_id).or_default();
        if outcomes.contains_key(&key) {
            continue;
        }
        let response: Option<Value> = job.try_get("response_json")?;
        outcomes.insert(
            key,
            json!({
                "state":job.try_get::<String,_>("state")?,
                "detail":job_error(response.as_ref()),
                "job_id":job.try_get::<Uuid,_>("id")?
            }),
        );
    }
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let id = row.try_get::<Uuid, _>("id")?;
        let base: Value = row.try_get("outcomes_json")?;
        let merged = merge_outcomes(&base, by_batch.remove(&id).unwrap_or_default().into_iter());
        result.push(receipt(&row, merged)?);
    }
    Ok(Json(result))
}
async fn retired(Path((_task_id, _job_id)): Path<(Uuid, Uuid)>) -> (StatusCode, Json<Value>) {
    (
        StatusCode::GONE,
        Json(json!({
            "detail":"Legacy previews and sentence rewriting have been removed. Create a new native YouDub job."
        })),
    )
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{task_id}/publish_batches", get(publish_batches))
        .route(
            "/tasks/{task_id}/dubbing_jobs/{job_id}/confirm",
            post(retired),
        )
        .route(
            "/tasks/{task_id}/dubbing_jobs/{job_id}/rerun",
            post(retired),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn outcomes_prefer_newest_actual_job_without_fabricating_platform() {
        let target = latest_key("bilibili", None);
        assert_eq!(target, "bilibili:default");
        let v = merge_outcomes(
            &json!({"bilibili:default":{"state":"queued"},"legacy":{}}),
            std::iter::once((target, json!({"state":"unknown","job_id":"test"}))),
        );
        assert_eq!(v["bilibili:default"]["state"], "unknown");
        assert_eq!(v["legacy"], json!({}));
    }
    #[test]
    fn error_receipt_keeps_reason() {
        let item = json!({"message":"Upload timed out","status_code":503});
        assert_eq!(
            job_error(Some(&item)).as_deref(),
            Some("Upload timed out (status_code=503)")
        );
    }
}
