//! Source-compatible administrative control of legacy RenderWorker rows.
//! Deliberate opt-in is required: production Python tables must not be
//! mutated by an unreviewed Rust preview deployment.
//! Row-lock and lease ownership checks prevent duplicate cancellation/requeue.
use crate::{
    AppError, AppResult,
    api::AppState,
    render_management_read_v1::{
        EXEC_SQL, WORKER_SQL, execution_payload, management_engine, native_management,
        worker_payload,
    },
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
    if native_management() {
        return Ok(());
    }
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
    sqlx::query("SELECT * FROM render_workers WHERE id=$1 AND runtime_engine=$2 FOR UPDATE")
        .bind(id)
        .bind(management_engine())
        .fetch_optional(&mut **tx)
        .await?
        .ok_or_else(|| AppError::NotFound("render worker not found".into()))
}
async fn worker_read(state: &AppState, id: Uuid) -> AppResult<Json<Value>> {
    let query = format!("{WORKER_SQL} WHERE w.id=$1 AND w.runtime_engine=$2");
    let row = sqlx::query(&query)
        .bind(id)
        .bind(management_engine())
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
    if native_management() {
        return Err(AppError::Conflict(
            "native render device scheduling is not yet supported; drain the entire worker instead"
                .into(),
        ));
    }
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
    let raw = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let token = if native_management() {
        raw
    } else {
        format!("vre_{raw}")
    };
    let hash = hex::encode(Sha256::digest(token.as_bytes()));
    let id = Uuid::new_v4();
    let expiry: DateTime<Utc> = sqlx::query_scalar(
        "INSERT INTO render_worker_enrollments(id,token_hash,label,status,expires_at,runtime_engine)
         VALUES ($1,$2,$3,'active',now()+($4*interval '1 minute'),$5)
         RETURNING expires_at",
    )
    .bind(id)
    .bind(hash)
    .bind(label)
    .bind(ttl)
    .bind(management_engine())
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
         THEN 'revoked' ELSE status END WHERE id=$1 AND runtime_engine=$2
       RETURNING id,label,status,expires_at,consumed_at,worker_id,created_at",
    )
    .bind(id)
    .bind(management_engine())
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
    let sql = format!(
        "{EXEC_SQL} WHERE e.id=$1 AND (e.runtime_engine=$2 OR ($2='rust-v2' AND e.runtime_engine='python'))"
    );
    let row = sqlx::query(&sql)
        .bind(id)
        .bind(management_engine())
        .fetch_one(&state.db.pool)
        .await?;
    Ok(Json(execution_payload(&row)?))
}
// Only operate on an exact native awaiting render stage. Cancel fences a live
// worker; requeue never resets it. Committed outputs and legacy rows are protected.
async fn native_execution_change(
    state: &AppState,
    id: Uuid,
    reason: &str,
    requeue: bool,
) -> AppResult<Json<Value>> {
    let mut tx = state.db.pool.begin().await?;
    let peek = sqlx::query(
        "SELECT task_id,workflow_id,job_id,runtime_engine FROM render_executions WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("render execution not found".into()))?;
    if peek.try_get::<String, _>("runtime_engine")? == "python" && requeue {
        tx.rollback().await?;
        crate::historical_p2_render::enqueue(&state.db, id, reason).await?;
        return execution_read(state, id).await;
    }
    if peek.try_get::<String, _>("runtime_engine")? != "rust-v2" {
        return Err(AppError::Conflict(
            "historical render executions are read-only".into(),
        ));
    }
    let task: Uuid = peek.try_get("task_id")?;
    let flow: Uuid = peek.try_get("workflow_id")?;
    let job: Uuid = peek.try_get("job_id")?;
    // Same lock order as scheduler and upload: task -> workflow -> job -> execution.
    let task_row = sqlx::query(
        "SELECT runtime_owner,status::text AS status FROM tasks WHERE id=$1 FOR UPDATE",
    )
    .bind(task)
    .fetch_one(&mut *tx)
    .await?;
    let flow_row = sqlx::query("SELECT state,current_stage FROM pipeline_runs WHERE id=$1 AND task_id=$2 AND engine='rust-v2' FOR UPDATE")
        .bind(flow).bind(task).fetch_optional(&mut *tx).await?
        .ok_or_else(|| AppError::Conflict("render workflow identity mismatch".into()))?;
    let job_row = sqlx::query("SELECT status,external_ticket,stage FROM operation_inbox WHERE id=$1 AND task_id=$2 AND workflow_id=$3 AND stage IN ('render','dubbing') AND runtime_engine='rust-v2' FOR UPDATE")
        .bind(job).bind(task).bind(flow).fetch_optional(&mut *tx).await?
        .ok_or_else(|| AppError::Conflict("render job identity mismatch".into()))?;
    let row = sqlx::query("SELECT state,attempt,uploaded_asset_id,mode,COALESCE(lease_until<=now(),false) AS expired FROM render_executions WHERE id=$1 AND task_id=$2 AND workflow_id=$3 AND job_id=$4 AND runtime_engine='rust-v2' FOR UPDATE")
        .bind(id).bind(task).bind(flow).bind(job).fetch_optional(&mut *tx).await?
        .ok_or_else(|| AppError::Conflict("render execution identity mismatch".into()))?;
    let mode: String = row.try_get("mode")?;
    let stage = crate::remote_render::output_stage(&mode);
    if job_row.try_get::<String, _>("stage")? != stage {
        return Err(AppError::Conflict("render mode/stage mismatch".into()));
    }
    let execution_state: String = row.try_get("state")?;
    let flow_state: String = flow_row.try_get("state")?;
    let job_state: String = job_row.try_get("status")?;
    if task_row.try_get::<String, _>("runtime_owner")? != "rust"
        || job_row
            .try_get::<Option<String>, _>("external_ticket")?
            .as_deref()
            != Some(id.to_string().as_str())
    {
        return Err(AppError::Conflict(
            "render ownership or ticket mismatch".into(),
        ));
    }
    if !requeue
        && execution_state == "canceled"
        && flow_state == "canceled"
        && job_state == "canceled"
    {
        tx.commit().await?;
        return execution_read(state, id).await;
    }
    let task_status: String = task_row.try_get("status")?;
    let safe_state = execution_state == "queued"
        || (matches!(execution_state.as_str(), "running" | "uploading")
            && (!requeue || row.try_get::<bool, _>("expired")?));
    if !safe_state
        || row
            .try_get::<Option<Uuid>, _>("uploaded_asset_id")?
            .is_some()
        || flow_state != "running"
        || flow_row
            .try_get::<Option<String>, _>("current_stage")?
            .as_deref()
            != Some(stage)
        || job_state != "awaiting"
        || matches!(
            task_status.as_str(),
            "canceled" | "failed" | "published" | "publishing"
        )
    {
        return Err(AppError::Conflict("render control requires an eligible execution without uploaded output in a running render stage".into()));
    }
    let other_active: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE workflow_id=$1 AND runtime_engine='rust-v2' AND id<>$2 AND status IN ('pending','running','awaiting','unknown'))")
        .bind(flow).bind(job).fetch_one(&mut *tx).await?;
    if other_active {
        return Err(AppError::Conflict(
            "another workflow stage is active or uncertain".into(),
        ));
    }
    if requeue {
        if row.try_get::<i32, _>("attempt")? >= crate::remote_render::MAX_RENDER_ATTEMPTS {
            return Err(AppError::Conflict(
                "render retry budget exhausted; restart the task workflow".into(),
            ));
        }
        if execution_state == "queued" {
            tx.commit().await?;
            return execution_read(state, id).await;
        }
        sqlx::query("UPDATE render_executions SET state='queued',worker_id=NULL,fence_token=NULL,lease_until=NULL,progress=0,finished_at=NULL,error_message=$2,updated_at=now() WHERE id=$1")
            .bind(id).bind(reason).execute(&mut *tx).await?;
    } else {
        sqlx::query("UPDATE render_executions SET state='canceled',lease_until=CASE WHEN state IN ('running','uploading') AND lease_until>now() THEN lease_until ELSE NULL END,finished_at=now(),error_message=$2,updated_at=now() WHERE id=$1")
            .bind(id).bind(reason).execute(&mut *tx).await?;
        sqlx::query("UPDATE vr2_jobs SET state='canceled',lease_token=NULL,lease_until=NULL,last_error=$2,finished_at=now(),updated_at=now() WHERE id=$1")
            .bind(job).bind(reason).execute(&mut *tx).await?;
        sqlx::query(
            "UPDATE vr2_workflows SET state='canceled',last_error=$2,updated_at=now() WHERE id=$1",
        )
        .bind(flow)
        .bind(reason)
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE tasks SET status='canceled'::task_status,updated_at=now() WHERE id=$1")
            .bind(task)
            .execute(&mut *tx)
            .await?;
    }
    sqlx::query(
        "INSERT INTO workflow_events(id,workflow_id,job_id,kind,details) VALUES($1,$2,$3,$4,$5)",
    )
    .bind(Uuid::new_v4())
    .bind(flow)
    .bind(job)
    .bind(if requeue {
        "render.admin_requeued"
    } else {
        "workflow.canceled"
    })
    .bind(json!({"execution_id":id,"reason":reason}))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    execution_read(state, id).await
}
async fn execution_change(
    state: &AppState,
    id: Uuid,
    reason: &str,
    requeue: bool,
) -> AppResult<Json<Value>> {
    can_write()?;
    if native_management() {
        return native_execution_change(state, id, reason, requeue).await;
    }
    let mut tx = state.db.pool.begin().await?;
    let task_id:Uuid=sqlx::query_scalar("SELECT j.task_id FROM render_executions e JOIN render_jobs j ON j.id=e.render_job_id WHERE e.id=$1 AND e.runtime_engine='python'")
        .bind(id).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::NotFound("render execution not found".into()))?;
    let task_owner: String =
        sqlx::query_scalar("SELECT runtime_owner FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
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
        // A Rust-owned historical render has no native execution lease.
        // Requeueing this legacy row would require the retired Python
        // consumer. Use a new native workflow generation instead.
        if task_owner == "rust" {
            return Err(AppError::Conflict("historical render cannot be requeued by Rust; restart the native render or dubbing stage".into()));
        }
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
        if !requeue && active(&state_before) && lease_owner.as_deref() == Some(owner.as_str()) {
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
                    if row.try_get::<Uuid, _>("task_id")? != task_id
                        || row
                            .try_get::<Value, _>("output_json")?
                            .get("render_job_id")
                            .and_then(Value::as_str)
                            != Some(job_id.to_string().as_str())
                    {
                        return Err(AppError::Conflict(
                            "P2 render does not own dubbing job".into(),
                        ));
                    }
                    // Legacy Hatchet runs cannot be completed by a fabricated
                    // native notification. Require an audited ownership cutover.
                    let old_flow:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND engine<>'rust-v2' AND state NOT IN ('succeeded','failed','canceled','completed'))")
                        .bind(task_id).fetch_one(&mut *tx).await?;
                    if task_owner != "rust" || old_flow {
                        return Err(AppError::Conflict("P2 render belongs to historical Python workflow; drain or migrate ownership before Rust cancellation".into()));
                    }
                    crate::render_events::record_p2_failure(
                        &mut tx, task_id, dub_id, job_id, id, &message,
                    )
                    .await?;
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
