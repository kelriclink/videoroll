//! Original Python RenderWorker management read models. V1 tables are
//! read-only; no synthetic worker heartbeats or GPU / encoder state.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::get,
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::{Row, postgres::PgRow};
use uuid::Uuid;

// The compatibility preview keeps its historical Python-only scope. The
// broker-free deployment uses the same browser endpoints for native workers.
pub(crate) fn native_management() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_RENDER_MANAGEMENT_NATIVE")
            .ok()
            .as_deref(),
        Some("true" | "1")
    )
}
pub(crate) fn management_engine() -> &'static str {
    if native_management() {
        "rust-v2"
    } else {
        "python"
    }
}
pub(crate) const WORKER_SQL: &str = r#"SELECT w.*,
 CASE WHEN w.runtime_engine='rust-v2' THEN
   (SELECT count(*)::int FROM render_executions e WHERE e.worker_id=w.id
     AND e.runtime_engine='rust-v2' AND e.state IN ('running','uploading')
     AND e.lease_until>now())
 ELSE w.active_jobs END AS live_active_jobs FROM render_workers w"#;

pub(crate) fn worker_payload(row: &PgRow) -> AppResult<Value> {
    let last_seen: DateTime<Utc> = row.try_get("last_seen_at")?;
    let seconds = Utc::now()
        .signed_duration_since(last_seen)
        .num_seconds()
        .max(0);
    let resources: Value = row.try_get("resources")?;
    let labels: Value = row.try_get("labels")?;
    let disabled = labels
        .get("disabled_render_device_ids")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut presented = Vec::new();
    for device in resources
        .get("devices")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(fields) = device.as_object() else {
            continue;
        };
        let mut fields = fields.clone();
        let is_disabled = fields.get("id").is_some_and(|id| disabled.contains(id));
        fields.insert("enabled".into(), json!(!is_disabled));
        fields.insert(
            "status".into(),
            if is_disabled {
                json!("disabled")
            } else {
                fields.get("status").cloned().unwrap_or(json!("idle"))
            },
        );
        presented.push(Value::Object(fields));
    }
    let mut output_resources = resources.as_object().cloned().unwrap_or_else(Map::new);
    output_resources.insert("devices".into(), json!(presented));
    let limit: i32 = row.try_get("max_concurrency")?;
    let active: i32 = row
        .try_get("live_active_jobs")
        .or_else(|_| row.try_get("active_jobs"))?;
    let credentials: Option<String> = row.try_get("credential_hash")?;
    let revoked: Option<DateTime<Utc>> = row.try_get("credential_revoked_at")?;
    let mut result = json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "worker_key":row.try_get::<String,_>("worker_key")?,
        "name":row.try_get::<String,_>("name")?,
        "platform":row.try_get::<String,_>("platform")?,
        "architecture":row.try_get::<Option<String>,_>("architecture")?,
        "version":row.try_get::<String,_>("version")?,
        "protocol_version":row.try_get::<i32,_>("protocol_version")?,
        "render_spec_versions":row.try_get::<Value,_>("render_spec_versions")?,
        "capabilities":row.try_get::<Value,_>("capabilities")?,
        "resources":output_resources,
        "labels":labels,
        "status":row.try_get::<String,_>("status")?,
        "enabled":row.try_get::<bool,_>("enabled")?,
        "draining":row.try_get::<bool,_>("draining")?,
        "max_concurrency":limit,
        "active_jobs":active,
        "device_count":presented.len(),
        "detected_capacity":presented.len(),
        "effective_capacity":limit,
        "available_slots":(limit-active).max(0),
        "last_seen_at":last_seen,
        "stale":seconds>if native_management(){300}else{180},
        "seconds_since_heartbeat":seconds,
        "credential_active":credentials.as_deref().is_some_and(|h|!h.is_empty()) && revoked.is_none()
    });
    if native_management() {
        result["runtime_engine"] = json!("rust-v2");
        result["device_control_supported"] = json!(false);
    }
    Ok(result)
}
async fn workers(State(state): State<AppState>) -> AppResult<Json<Vec<Value>>> {
    let query = format!("{WORKER_SQL} WHERE w.runtime_engine=$1 ORDER BY w.last_seen_at DESC");
    let rows = sqlx::query(&query)
        .bind(management_engine())
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(
        rows.iter()
            .map(worker_payload)
            .collect::<AppResult<Vec<_>>>()?,
    ))
}
async fn enrollments(State(state): State<AppState>) -> AppResult<Json<Vec<Value>>> {
    let rows = sqlx::query(
        "SELECT id,label,status,expires_at,consumed_at,worker_id,created_at
         FROM render_worker_enrollments WHERE runtime_engine=$1 ORDER BY created_at DESC LIMIT 100",
    )
    .bind(management_engine())
    .fetch_all(&state.db.pool)
    .await?;
    let mut results = Vec::with_capacity(rows.len());
    for row in rows {
        results.push(json!({
            "id":row.try_get::<Uuid,_>("id")?,
            "label":row.try_get::<String,_>("label")?,
            "status":row.try_get::<String,_>("status")?,
            "expires_at":row.try_get::<DateTime<Utc>,_>("expires_at")?,
            "consumed_at":row.try_get::<Option<DateTime<Utc>>,_>("consumed_at")?,
            "worker_id":row.try_get::<Option<Uuid>,_>("worker_id")?,
            "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?
        }));
    }
    Ok(Json(results))
}
pub(crate) fn execution_payload(row: &PgRow) -> AppResult<Value> {
    let mut result = json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "render_job_id":row.try_get::<Uuid,_>("render_job_id")?,
        "worker_id":row.try_get::<Option<Uuid>,_>("worker_id")?,
        "attempt":row.try_get::<i32,_>("attempt")?,
        "state":row.try_get::<String,_>("state")?,
        "transfer_mode":row.try_get::<String,_>("transfer_mode")?,
        "progress":row.try_get::<i32,_>("progress")?,
        "lease_until":row.try_get::<Option<DateTime<Utc>>,_>("lease_until")?,
        "render_spec":row.try_get::<Value,_>("render_spec")?,
        "worker_name":row.try_get::<Option<String>,_>("worker_name")?,
        "task_id":row.try_get::<Option<Uuid>,_>("task_id")?,
        "job_status":row.try_get::<Option<String>,_>("job_status")?,
        "metrics":row.try_get::<Value,_>("metrics")?,
        "log_tail":row.try_get::<Option<String>,_>("log_tail")?,
        "error_message":row.try_get::<Option<String>,_>("error_message")?,
        "heartbeat_at":row.try_get::<Option<DateTime<Utc>>,_>("heartbeat_at")?,
        "started_at":row.try_get::<DateTime<Utc>,_>("started_at")?,
        "finished_at":row.try_get::<Option<DateTime<Utc>>,_>("finished_at")?
    });
    if native_management() {
        result["runtime_engine"] = json!(row.try_get::<String, _>("runtime_engine")?);
        // Native administrative cancellation/requeue requires scheduler
        // fencing and receipt reconciliation, not a legacy SQL mutation.
        result["control_supported"] = json!(false);
    }
    Ok(result)
}
pub(crate) const EXEC_SQL: &str = r#"
    SELECT e.id,COALESCE(e.render_job_id,e.job_id) AS render_job_id,e.worker_id,e.attempt,
           CASE WHEN e.runtime_engine='rust-v2' AND e.state='completed' THEN 'succeeded' ELSE e.state END AS state,
           e.transfer_mode,e.progress,e.lease_until,e.render_spec,
           e.metrics,e.log_tail,e.error_message,
           CASE WHEN e.runtime_engine='rust-v2' AND e.state IN ('running','uploading') THEN e.updated_at ELSE e.heartbeat_at END AS heartbeat_at,
           e.started_at,e.finished_at,e.created_at,e.runtime_engine,
           w.name AS worker_name,COALESCE(j.task_id,e.task_id) AS task_id,COALESCE(j.status::text,o.status) AS job_status
    FROM render_executions e
    LEFT JOIN render_workers w ON w.id=e.worker_id
    LEFT JOIN render_jobs j ON j.id=e.render_job_id
    LEFT JOIN operation_inbox o ON o.id=e.job_id AND o.runtime_engine='rust-v2'
"#;
#[derive(Deserialize)]
struct ExecutionFilter {
    worker_id: Option<Uuid>,
    task_id: Option<Uuid>,
    state: Option<String>,
    limit: Option<i64>,
}
async fn executions(
    State(state): State<AppState>,
    Query(params): Query<ExecutionFilter>,
) -> AppResult<Json<Vec<Value>>> {
    let query = format!(
        "{EXEC_SQL} WHERE (e.runtime_engine='python' OR ($5 AND e.runtime_engine='rust-v2')) AND ($1::uuid IS NULL OR e.worker_id=$1)
        AND ($2::uuid IS NULL OR COALESCE(j.task_id,e.task_id)=$2)
        AND ($3::text IS NULL OR CASE WHEN e.runtime_engine='rust-v2' AND e.state='completed' THEN 'succeeded' ELSE e.state END=$3)
        ORDER BY e.created_at DESC LIMIT $4"
    );
    let rows = sqlx::query(&query)
        .bind(params.worker_id)
        .bind(params.task_id)
        .bind(params.state.filter(|s| !s.is_empty()))
        .bind(params.limit.unwrap_or(100).clamp(1, 500))
        .bind(native_management())
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(
        rows.iter()
            .map(execution_payload)
            .collect::<AppResult<Vec<_>>>()?,
    ))
}
async fn execution(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    let query = format!(
        "{EXEC_SQL} WHERE e.id=$1 AND (e.runtime_engine='python' OR ($2 AND e.runtime_engine='rust-v2'))"
    );
    let row = sqlx::query(&query)
        .bind(id)
        .bind(native_management())
        .fetch_optional(&state.db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("render execution not found".into()))?;
    Ok(Json(execution_payload(&row)?))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/render-management/workers", get(workers))
        .route("/render-management/enrollments", get(enrollments))
        .route("/render-management/executions", get(executions))
        .route(
            "/render-management/executions/{execution_id}",
            get(execution),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn constants_are_public_contract() {
        assert!(EXEC_SQL.contains("FROM render_executions"));
    }
}
