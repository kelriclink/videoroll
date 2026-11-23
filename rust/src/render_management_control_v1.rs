//! Source-compatible administrative control of legacy RenderWorker rows.
//! Deliberate opt-in is required: production Python tables must not be
//! mutated by an unreviewed Rust preview deployment.
//! Row-lock and lease ownership checks prevent duplicate cancellation/requeue.
use crate::{
    AppError, AppResult,
    api::AppState,
    render_management_read_v1::{EXEC_SQL, execution_payload, worker_payload},
};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::HeaderMap,
    routing::{patch, post},
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use std::collections::BTreeSet;
use uuid::Uuid;

fn can_write() -> AppResult<()> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        return Err(AppError::Conflict("V1 RenderWorker controls disabled until explicitly enabled on an isolated or audited deployment".into()));
    }
    Ok(())
}
async fn worker_lock(tx: &mut Transaction<'_, Postgres>, id: Uuid) -> AppResult<PgRow> {
    sqlx::query("SELECT * FROM render_workers WHERE id=$1 AND runtime_engine='python' FOR UPDATE")
        .bind(id)
        .fetch_optional(&mut **tx)
        .await?
        .ok_or_else(|| AppError::NotFound("render worker not found".into()))
}
async fn worker_read(state: &AppState, id: Uuid) -> AppResult<Json<Value>> {
    let row = sqlx::query("SELECT * FROM render_workers WHERE id=$1 AND runtime_engine='python'")
        .bind(id)
        .fetch_one(&state.db.pool)
        .await?;
    Ok(Json(worker_payload(&row)?))
}
#[derive(Debug, Deserialize)]
struct WorkerPatch {
    enabled: Option<bool>,
    draining: Option<bool>,
    max_concurrency: Option<i32>,
    worker_key: Option<String>,
}
async fn worker_update(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(p): Json<WorkerPatch>,
) -> AppResult<Json<Value>> {
    can_write()?;
    if p.max_concurrency.is_some_and(|v| !(1..=32).contains(&v)) {
        return Err(AppError::Invalid(
            "max_concurrency must be between 1 and 32".into(),
        ));
    }
    if p.worker_key
        .as_deref()
        .is_some_and(|s| s.trim().is_empty() || s.trim().len() > 128)
    {
        return Err(AppError::Invalid(
            "worker_key must be 1..128 characters".into(),
        ));
    }
    let mut tx = state.db.pool.begin().await?;
    let current = worker_lock(&mut tx, id).await?;
    let enabled = p.enabled.unwrap_or(current.try_get("enabled")?);
    let draining = p.draining.unwrap_or(current.try_get("draining")?);
    let concurrency = p
        .max_concurrency
        .unwrap_or(current.try_get("max_concurrency")?);
    let key = p
        .worker_key
        .map(|s| s.trim().to_string())
        .unwrap_or(current.try_get("worker_key")?);
    let result = sqlx::query(
        "UPDATE render_workers SET enabled=$1,draining=$2,max_concurrency=$3,
         worker_key=$4,updated_at=now() WHERE id=$5",
    )
    .bind(enabled)
    .bind(draining)
    .bind(concurrency)
    .bind(key)
    .bind(id)
    .execute(&mut *tx)
    .await;
    if let Err(error) = result {
        if error
            .as_database_error()
            .is_some_and(|e| e.code().as_deref() == Some("23505"))
        {
            return Err(AppError::Conflict(
                "render worker key is already in use".into(),
            ));
        }
        return Err(error.into());
    }
    tx.commit().await?;
    worker_read(&state, id).await
}
#[derive(Debug, Deserialize)]
struct DevicePatch {
    enabled: bool,
}
async fn device_update(
    State(state): State<AppState>,
    Path((id, device)): Path<(Uuid, String)>,
    Json(p): Json<DevicePatch>,
) -> AppResult<Json<Value>> {
    can_write()?;
    if device.is_empty() || device.len() > 256 {
        return Err(AppError::Invalid("render device ID is invalid".into()));
    }
    let mut tx = state.db.pool.begin().await?;
    let row = worker_lock(&mut tx, id).await?;
    let resources: Value = row.try_get("resources")?;
    let capabilities: Value = row.try_get("capabilities")?;
    let advertised = resources
        .get("devices")
        .and_then(Value::as_array)
        .or_else(|| capabilities.get("devices").and_then(Value::as_array));
    let known = advertised
        .into_iter()
        .flatten()
        .any(|item| item.get("id").and_then(Value::as_str) == Some(device.as_str()));
    if !known {
        return Err(AppError::NotFound(
            "render device not found on worker".into(),
        ));
    }
    let labels: Value = row.try_get("labels")?;
    let mut labels = labels.as_object().cloned().unwrap_or_default();
    let mut disabled: BTreeSet<String> = labels
        .get("disabled_render_device_ids")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    if p.enabled {
        disabled.remove(&device);
    } else {
        disabled.insert(device);
    }
    labels.insert("disabled_render_device_ids".into(), json!(disabled));
    sqlx::query("UPDATE render_workers SET labels=$1,updated_at=now() WHERE id=$2")
        .bind(json!(labels))
        .bind(id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    worker_read(&state, id).await
}
async fn credential_revoke(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    can_write()?;
    let mut tx = state.db.pool.begin().await?;
    worker_lock(&mut tx, id).await?;
    sqlx::query(
        "UPDATE render_workers SET credential_hash=NULL,credential_revoked_at=now(),
        status='offline',enabled=FALSE,updated_at=now() WHERE id=$1",
    )
    .bind(id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    worker_read(&state, id).await
}
#[derive(Debug, Deserialize)]
struct EnrollmentCreate {
    label: Option<String>,
    ttl_minutes: Option<i64>,
}
fn enrollment_json(row: &PgRow) -> AppResult<Value> {
    Ok(json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "label":row.try_get::<String,_>("label")?,
        "status":row.try_get::<String,_>("status")?,
        "expires_at":row.try_get::<DateTime<Utc>,_>("expires_at")?,
        "consumed_at":row.try_get::<Option<DateTime<Utc>>,_>("consumed_at")?,
        "worker_id":row.try_get::<Option<Uuid>,_>("worker_id")?,
        "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?
    }))
}
async fn enrollment_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(p): Json<EnrollmentCreate>,
) -> AppResult<Json<Value>> {
    can_write()?;
    let label = p.label.unwrap_or_default();
    let ttl = p.ttl_minutes.unwrap_or(30);
    if label.len() > 128 || !(5..=1440).contains(&ttl) {
        return Err(AppError::Invalid(
            "label must be <=128 bytes, ttl_minutes 5..1440".into(),
        ));
    }
    let token = format!("vre_{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let hash = hex::encode(Sha256::digest(token.as_bytes()));
    let id = Uuid::new_v4();
    let expiry: DateTime<Utc> = sqlx::query_scalar(
        "INSERT INTO render_worker_enrollments(id,token_hash,label,status,expires_at)
         VALUES ($1,$2,$3,'active',now()+($4*interval '1 minute'))
         RETURNING expires_at",
    )
    .bind(id)
    .bind(hash)
    .bind(label)
    .bind(ttl)
    .fetch_one(&state.db.pool)
    .await?;
    let origin = crate::management_v1::render_server_url(&state.db, &headers).await?;
    Ok(Json(json!({"id":id,"token":token,"expires_at":expiry,
        "server_url":origin})))
}
async fn enrollment_revoke(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    can_write()?;
    let row = sqlx::query(
        "UPDATE render_worker_enrollments SET status=CASE WHEN status='active'
         THEN 'revoked' ELSE status END WHERE id=$1 AND runtime_engine='python'
       RETURNING id,label,status,expires_at,consumed_at,worker_id,created_at",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("render worker enrollment not found".into()))?;
    Ok(Json(enrollment_json(&row)?))
}
#[derive(Debug, Deserialize)]
struct ExecutionAction {
    reason: Option<String>,
}
fn reason(raw: Option<String>) -> AppResult<String> {
    let result = raw.unwrap_or_else(|| "canceled by coordinator".into());
    if result.trim().is_empty() || result.len() > 1024 {
        return Err(AppError::Invalid(
            "reason must be 1..1024 characters".into(),
        ));
    }
    Ok(result)
}
fn active(state: &str) -> bool {
    matches!(state, "claimed" | "running" | "uploading")
}
async fn execution_read(state: &AppState, id: Uuid) -> AppResult<Json<Value>> {
    let sql = format!("{EXEC_SQL} WHERE e.id=$1 AND e.runtime_engine='python'");
    let row = sqlx::query(&sql).bind(id).fetch_one(&state.db.pool).await?;
    Ok(Json(execution_payload(&row)?))
}
async fn execution_change(
    state: &AppState,
    id: Uuid,
    reason: &str,
    requeue: bool,
) -> AppResult<Json<Value>> {
    can_write()?;
    let mut tx = state.db.pool.begin().await?;
    let execution = sqlx::query(
        "SELECT * FROM render_executions WHERE id=$1 AND runtime_engine='python' FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("render execution not found".into()))?;
    let state_before: String = execution.try_get("state")?;
    let job_id: Uuid = execution.try_get("render_job_id")?;
    let worker_id: Uuid = execution.try_get("worker_id")?;
    let job = sqlx::query("SELECT * FROM render_jobs WHERE id=$1 FOR UPDATE")
        .bind(job_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("render job not found".into()))?;
    // Original Python cancel is idempotent for terminal execution rows.
    // In particular, do not reject a previously canceled P2 render simply
    // because the later dubbing event bridge is not migrated yet.
    if !requeue && !active(&state_before) {
        tx.commit().await?;
        return execution_read(state, id).await;
    }
    let request: Value = job.try_get("request_json")?;
    // P2 uses the *same* source-compatible DubbingJob + durable Python outbox
    // contract, in one PostgreSQL transaction with execution and job state.
    let p2_id = request
        .get("dubbing_job_id")
        .and_then(Value::as_str)
        .map(|raw| {
            Uuid::parse_str(raw).map_err(|_| AppError::Conflict("invalid P2 dubbing job ID".into()))
        })
        .transpose()?;
    let owner = format!("render-worker:{id}");
    let lease_owner: Option<String> = job.try_get("lease_owner")?;
    if (requeue || active(&state_before)) && lease_owner.as_deref().is_some_and(|o| o != owner) {
        return Err(AppError::Conflict(
            "render job is owned by another execution".into(),
        ));
    }
    let dubbing = if let Some(dub_id) = p2_id {
        let row = sqlx::query(
            "SELECT id,task_id,status,output_json FROM dubbing_jobs WHERE id=$1 FOR UPDATE",
        )
        .bind(dub_id)
        .fetch_optional(&mut *tx)
        .await?;
        if requeue {
            let row = row
                .ok_or_else(|| AppError::Conflict("P2 dubbing state cannot be requeued".into()))?;
            let task: Uuid = row.try_get("task_id")?;
            let job_task: Uuid = job.try_get("task_id")?;
            let status: String = row.try_get("status")?;
            if task != job_task || matches!(status.as_str(), "canceled" | "succeeded") {
                return Err(AppError::Conflict(
                    "P2 dubbing state cannot be requeued".into(),
                ));
            }
            Some(row)
        } else {
            row
        }
    } else {
        None
    };

    if requeue {
        let status: String = job.try_get("status")?;
        if state_before == "canceled" && status == "queued" {
            tx.commit().await?;
            return execution_read(state, id).await;
        }
        if let Some(row) = &dubbing {
            let dub_id: Uuid = row.try_get("id")?;
            let status: String = row.try_get("status")?;
            if matches!(status.as_str(), "paused" | "failed") {
                let output: Value = row.try_get("output_json")?;
                let mut output = output.as_object().cloned().unwrap_or_default();
                let mut youdub = output
                    .get("youdub")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                youdub.insert("stage".into(), json!("rendering_p2"));
                output.insert("youdub".into(), json!(youdub));
                sqlx::query(
                    "UPDATE dubbing_jobs SET status='rendering',error_message=NULL,
                     output_json=$1,updated_at=now() WHERE id=$2",
                )
                .bind(json!(output))
                .bind(dub_id)
                .execute(&mut *tx)
                .await?;
            }
        }
        sqlx::query(
            "UPDATE render_executions SET state='canceled',
             finished_at=COALESCE(finished_at,now()),error_message=$1,updated_at=now()
             WHERE id=$2 AND state IN ('claimed','running','uploading')",
        )
        .bind(reason)
        .bind(id)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "UPDATE render_jobs SET status='queued',progress=0,started_at=NULL,
              finished_at=NULL,error_message=$1,retry_count=retry_count+1,
              lease_owner=NULL,lease_until=NULL,heartbeat_at=now(),
              updated_at=now() WHERE id=$2",
        )
        .bind(reason)
        .bind(job_id)
        .execute(&mut *tx)
        .await?;
    } else if active(&state_before) {
        sqlx::query(
            "UPDATE render_executions SET state='canceled',error_message=$1,
             finished_at=now(),updated_at=now() WHERE id=$2",
        )
        .bind(reason)
        .bind(id)
        .execute(&mut *tx)
        .await?;
        if lease_owner.as_deref() == Some(owner.as_str()) {
            sqlx::query(
                "UPDATE render_jobs SET status='canceled',error_message=$1,
                 finished_at=now(),lease_owner=NULL,lease_until=NULL,
                 heartbeat_at=now(),updated_at=now() WHERE id=$2",
            )
            .bind(reason)
            .bind(job_id)
            .execute(&mut *tx)
            .await?;
        }
    }
    if active(&state_before) {
        // Cancellation must publish a durable failure notification for an
        // in-flight P2 dubbing job. Locking the DubbingJob above prevents another
        // cancel/callback from creating an inconsistent second outcome.
        if !requeue && active(&state_before) {
            if let Some(row) = &dubbing {
                let status: String = row.try_get("status")?;
                if status == "rendering" {
                    let dub_id: Uuid = row.try_get("id")?;
                    let message = format!("P2 render canceled: {reason}");
                    let message = message.chars().take(2000).collect::<String>();
                    sqlx::query(
                        "UPDATE dubbing_jobs SET status='failed',error_message=$1,
                     updated_at=now() WHERE id=$2",
                    )
                    .bind(&message)
                    .bind(dub_id)
                    .execute(&mut *tx)
                    .await?;
                    let key = format!("workflow-dubbing-finished:{dub_id}:failed");
                    let old: Option<Uuid> = sqlx::query_scalar(
                        "SELECT id FROM outbox_events WHERE operation_key=$1
                     ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                    )
                    .bind(&key)
                    .fetch_optional(&mut *tx)
                    .await?;
                    if old.is_none() {
                        sqlx::query(
                            "INSERT INTO outbox_events(id,event_type,aggregate_type,
                        aggregate_id,task_name,args_json,operation_key,status)
                        VALUES($1,'workflow.dubbing.finished','dubbing_job',$2,
                        'subtitle_service.push_dubbing_workflow_event',$3,$4,'pending')",
                        )
                        .bind(Uuid::new_v4())
                        .bind(dub_id.to_string())
                        .bind(json!({"args":[dub_id.to_string(),"failed",message],
                          "queue":"subtitle-control"}))
                        .bind(key)
                        .execute(&mut *tx)
                        .await?;
                    }
                }
            }
        }

        sqlx::query(
            "UPDATE render_workers SET active_jobs=GREATEST(0,active_jobs-1),
              status='online',updated_at=now() WHERE id=$1",
        )
        .bind(worker_id)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    execution_read(state, id).await
}
async fn execution_cancel(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(p): Json<ExecutionAction>,
) -> AppResult<Json<Value>> {
    let reason = reason(p.reason)?;
    execution_change(&state, id, &reason, false).await
}
async fn execution_requeue(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(p): Json<ExecutionAction>,
) -> AppResult<Json<Value>> {
    let reason = reason(p.reason)?;
    execution_change(&state, id, &reason, true).await
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/render-management/enrollments", post(enrollment_create))
        .route(
            "/render-management/enrollments/{enrollment_id}",
            axum::routing::delete(enrollment_revoke),
        )
        .route(
            "/render-management/workers/{worker_id}",
            patch(worker_update),
        )
        .route(
            "/render-management/workers/{worker_id}/devices/{*device_id}",
            patch(device_update),
        )
        .route(
            "/render-management/workers/{worker_id}/revoke-credential",
            post(credential_revoke),
        )
        .route(
            "/render-management/executions/{execution_id}/cancel",
            post(execution_cancel),
        )
        .route(
            "/render-management/executions/{execution_id}/requeue",
            post(execution_requeue),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn action_guards() {
        assert!(active("running"));
        assert!(!active("succeeded"));
        assert!(reason(Some("".into())).is_err());
    }
}
