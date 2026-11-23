//! Legacy Python RenderWorker wire compatibility.
//! Dispatch is selected solely by the original vrw_ credential prefix;
//! native 64-hex V2 credentials retain their existing vr2_* tables.
//! Legacy write endpoints require the same explicit opt-in as V1 render
//! administration, preventing a preview from taking over production leases.
use crate::{AppError, AppResult, api::AppState};
use axum::http::{HeaderMap, header};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{Row, postgres::PgRow};
use subtle::ConstantTimeEq;
use uuid::Uuid;

const LEASE_SECONDS: i64 = 180;
const ACTIVE: &[&str] = &["claimed", "running", "uploading"];

fn can_write() -> AppResult<()> {
    if matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        Ok(())
    } else {
        Err(AppError::Conflict(
            "legacy RenderWorker lease writes require explicit V1 render-control opt-in".into(),
        ))
    }
}
fn token(headers: &HeaderMap) -> Option<&str> {
    let raw = headers.get(header::AUTHORIZATION)?.to_str().ok()?;
    let (scheme, t) = raw.split_once(' ')?;
    if !scheme.eq_ignore_ascii_case("bearer")
        || !t.starts_with("vrw_")
        || t.len() > 256
        || t.len() < 16
    {
        return None;
    }
    Some(t)
}
pub(crate) fn is_legacy(headers: &HeaderMap) -> bool {
    token(headers).is_some()
}
pub(crate) async fn authenticate(s: &AppState, headers: &HeaderMap) -> AppResult<Uuid> {
    let t = token(headers).ok_or(AppError::Unauthorized)?;
    let hash = hex::encode(Sha256::digest(t.as_bytes()));
    let result = sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM render_workers WHERE credential_hash=$1 AND runtime_engine='python'
         AND credential_revoked_at IS NULL AND enabled=true",
    )
    .bind(hash)
    .fetch_optional(&s.db.pool)
    .await?;
    result.ok_or(AppError::Unauthorized)
}
fn worker_read(row: &PgRow) -> AppResult<Value> {
    Ok(json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "worker_key":row.try_get::<String,_>("worker_key")?,
        "name":row.try_get::<String,_>("name")?,
        "platform":row.try_get::<String,_>("platform")?,
        "architecture":row.try_get::<Option<String>,_>("architecture")?,
        "version":row.try_get::<String,_>("version")?,
        "protocol_version":row.try_get::<i32,_>("protocol_version")?,
        "render_spec_versions":row.try_get::<Value,_>("render_spec_versions")?,
        "capabilities":row.try_get::<Value,_>("capabilities")?,
        "resources":row.try_get::<Value,_>("resources")?,
        "labels":row.try_get::<Value,_>("labels")?,
        "status":row.try_get::<String,_>("status")?,
        "enabled":row.try_get::<bool,_>("enabled")?,
        "draining":row.try_get::<bool,_>("draining")?,
        "max_concurrency":row.try_get::<i32,_>("max_concurrency")?,
        "active_jobs":row.try_get::<i32,_>("active_jobs")?,
        "last_seen_at":row.try_get::<DateTime<Utc>,_>("last_seen_at")?
    }))
}
pub(crate) async fn worker_heartbeat(
    s: &AppState,
    headers: &HeaderMap,
    id: Uuid,
    status: &str,
    resources: &Value,
    capabilities: Option<&Value>,
    active_count: usize,
) -> AppResult<Value> {
    can_write()?;
    let authorized = authenticate(s, headers).await?;
    if authorized != id {
        return Err(AppError::Unauthorized);
    }
    if !matches!(status, "online" | "busy" | "paused" | "draining") || active_count > 64 {
        return Err(AppError::Invalid(
            "invalid legacy render worker heartbeat".into(),
        ));
    }
    if !resources.is_object() || capabilities.is_some_and(|v| !v.is_object()) {
        return Err(AppError::Invalid(
            "resources and capabilities must be JSON objects".into(),
        ));
    }
    // Locking the worker serializes status changes with admin control.
    let mut tx = s.db.pool.begin().await?;
    let row = sqlx::query(
        "UPDATE render_workers w SET status=$2,draining=(w.draining OR $2='draining'),
         resources=$3,capabilities=COALESCE($4,w.capabilities),
         active_jobs=(SELECT count(*)::int FROM render_executions e
                      WHERE e.worker_id=w.id AND e.state IN ('claimed','running','uploading')),
         last_seen_at=now(),updated_at=now()
         WHERE w.id=$1 RETURNING w.*",
    )
    .bind(id)
    .bind(status)
    .bind(resources)
    .bind(capabilities)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("render worker not found".into()))?;
    tx.commit().await?;
    worker_read(&row)
}
fn execution_view(row: &PgRow) -> AppResult<Value> {
    Ok(json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "render_job_id":row.try_get::<Uuid,_>("render_job_id")?,
        "worker_id":row.try_get::<Uuid,_>("worker_id")?,
        "attempt":row.try_get::<i32,_>("attempt")?,
        "fence_token":row.try_get::<String,_>("fence_token")?,
        "state":row.try_get::<String,_>("state")?,
        "transfer_mode":row.try_get::<String,_>("transfer_mode")?,
        "progress":row.try_get::<i32,_>("progress")?,
        "lease_until":row.try_get::<Option<DateTime<Utc>>,_>("lease_until")?,
        "render_spec":row.try_get::<Value,_>("render_spec")?
    }))
}
fn valid_fence(row: &PgRow, fence: &str, expired: bool) -> AppResult<()> {
    if !(16..=64).contains(&fence.len()) {
        return Err(AppError::Invalid(
            "invalid render execution fence token".into(),
        ));
    }
    let saved: String = row.try_get("fence_token")?;
    if !bool::from(saved.as_bytes().ct_eq(fence.as_bytes())) {
        return Err(AppError::Conflict(
            "stale render execution fence token".into(),
        ));
    }
    let state: String = row.try_get("state")?;
    if !ACTIVE.contains(&state.as_str()) {
        return Err(AppError::Conflict(format!("render execution is {state}")));
    }
    let until: Option<DateTime<Utc>> = row.try_get("lease_until")?;
    if expired && until.is_some_and(|t| t <= Utc::now()) {
        return Err(AppError::Conflict("render execution lease expired".into()));
    }
    Ok(())
}
async fn owned_execution(s: &AppState, headers: &HeaderMap, id: Uuid) -> AppResult<PgRow> {
    let owner = authenticate(s, headers).await?;
    let row = sqlx::query("SELECT * FROM render_executions WHERE id=$1")
        .bind(id)
        .fetch_optional(&s.db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("render execution not found".into()))?;
    let assigned: Uuid = row.try_get("worker_id")?;
    if assigned != owner {
        return Err(AppError::Unauthorized);
    }
    Ok(row)
}
pub(crate) async fn heartbeat_execution(
    s: &AppState,
    headers: &HeaderMap,
    id: Uuid,
    fence: &str,
    progress: Option<i32>,
    metrics: &Value,
    stage: Option<&str>,
) -> AppResult<Value> {
    can_write()?;
    let authorized = authenticate(s, headers).await?;
    if progress.is_some_and(|v| !(0..=100).contains(&v))
        || !metrics.is_object()
        || stage.is_some_and(|v| v.len() > 64)
    {
        return Err(AppError::Invalid(
            "invalid render execution heartbeat".into(),
        ));
    }
    let mut tx = s.db.pool.begin().await?;
    let row = sqlx::query("SELECT * FROM render_executions WHERE id=$1 FOR UPDATE")
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("render execution not found".into()))?;
    if row.try_get::<Uuid, _>("worker_id")? != authorized {
        return Err(AppError::Unauthorized);
    }
    valid_fence(&row, fence, true)?;
    let job: Uuid = row.try_get("render_job_id")?;
    let owner = format!("render-worker:{id}");
    let current_owner: Option<String> =
        sqlx::query_scalar("SELECT COALESCE(lease_owner,'') FROM render_jobs WHERE id=$1")
            .bind(job)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::Conflict("render job ownership lost".into()))?;
    if current_owner.as_deref() != Some(owner.as_str()) {
        return Err(AppError::Conflict("render job ownership lost".into()));
    }
    let result = sqlx::query(
        "UPDATE render_executions SET state='running',heartbeat_at=now(),
         lease_until=now()+($2*interval '1 second'),metrics=$3,
         progress=COALESCE($4,progress),updated_at=now()
         WHERE id=$1 RETURNING *",
    )
    .bind(id)
    .bind(LEASE_SECONDS)
    .bind(metrics)
    .bind(progress)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    // The execution lease is durable before touching the business job row.
    // An unrelated job lock cannot prevent lease renewal.
    if let Some(progress) = progress {
        let changed = sqlx::query(
            "UPDATE render_jobs SET heartbeat_at=$3,lease_until=$4,progress=$5,
             updated_at=now() WHERE id=$1 AND lease_owner=$2",
        )
        .bind(job)
        .bind(&owner)
        .bind(result.try_get::<Option<DateTime<Utc>>, _>("heartbeat_at")?)
        .bind(result.try_get::<Option<DateTime<Utc>>, _>("lease_until")?)
        .bind(progress)
        .execute(&s.db.pool)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(AppError::Conflict("render job ownership lost".into()));
        }
    }
    execution_view(&result)
}
pub(crate) async fn log_execution(
    s: &AppState,
    headers: &HeaderMap,
    id: Uuid,
    fence: &str,
    text: &str,
) -> AppResult<Value> {
    can_write()?;
    if text.chars().count() > 65536 {
        return Err(AppError::Invalid("render worker log too long".into()));
    }
    let worker = authenticate(s, headers).await?;
    let mut tx = s.db.pool.begin().await?;
    let row = sqlx::query("SELECT * FROM render_executions WHERE id=$1 FOR UPDATE")
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("render execution not found".into()))?;
    if row.try_get::<Uuid, _>("worker_id")? != worker {
        return Err(AppError::Unauthorized);
    }
    valid_fence(&row, fence, false)?;
    let result = sqlx::query(
        "UPDATE render_executions SET log_tail=right(COALESCE(log_tail,'')||$2,262144),
         updated_at=now() WHERE id=$1 RETURNING *",
    )
    .bind(id)
    .bind(text)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    execution_view(&result)
}
pub(crate) async fn cancellation(s: &AppState, headers: &HeaderMap, id: Uuid) -> AppResult<Value> {
    let row = owned_execution(s, headers, id).await?;
    let state: String = row.try_get("state")?;
    if !ACTIVE.contains(&state.as_str()) {
        return Ok(json!({"cancel_requested":true,"reason":format!("execution is {state}")}));
    }
    let until: Option<DateTime<Utc>> = row.try_get("lease_until")?;
    if until.is_some_and(|t| t <= Utc::now()) {
        return Ok(json!({"cancel_requested":true,"reason":"render execution lease expired"}));
    }
    let job: Uuid = row.try_get("render_job_id")?;
    let j = sqlx::query(
        "SELECT task_id,status::text AS status,request_json FROM render_jobs WHERE id=$1",
    )
    .bind(job)
    .fetch_optional(&s.db.pool)
    .await?;
    let Some(j) = j else {
        return Ok(json!({"cancel_requested":true,"reason":"render job canceled"}));
    };
    if j.try_get::<String, _>("status")? == "canceled" {
        return Ok(json!({"cancel_requested":true,"reason":"render job canceled"}));
    }
    let task_id: Uuid = j.try_get("task_id")?;
    let status: Option<String> = sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1")
        .bind(task_id)
        .fetch_optional(&s.db.pool)
        .await?;
    let dubbed = j
        .try_get::<Value, _>("request_json")?
        .get("dubbing_job_id")
        .is_some_and(|v| !v.is_null() && !v.as_str().unwrap_or("").is_empty());
    if status.is_none()
        || matches!(status.as_deref(), Some("canceled"))
        || (status.as_deref() == Some("published") && !dubbed)
    {
        return Ok(json!({"cancel_requested":true,"reason":"task stopped or terminal"}));
    }
    Ok(json!({"cancel_requested":false,"reason":null}))
}
pub(crate) async fn probe_output(
    s: &AppState,
    headers: &HeaderMap,
    id: Uuid,
    fence: &str,
) -> AppResult<Value> {
    let row = owned_execution(s, headers, id).await?;
    valid_fence(&row, fence, false)?;
    let record: Value = row.try_get("output_json")?;
    let id = record
        .get("uploaded_output_asset_id")
        .and_then(Value::as_str)
        .and_then(|id| Uuid::parse_str(id).ok())
        .ok_or_else(|| AppError::NotFound("render output not registered".into()))?;
    let asset = sqlx::query(
        "SELECT id,task_id,kind::text AS kind,storage_key,sha256,size_bytes,duration_ms,created_at
         FROM assets WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(&s.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("render output asset missing".into()))?;
    Ok(json!({
        "id":asset.try_get::<Uuid,_>("id")?,
        "kind":asset.try_get::<String,_>("kind")?,
        "storage_key":asset.try_get::<String,_>("storage_key")?,
        "sha256":asset.try_get::<Option<String>,_>("sha256")?,
        "size_bytes":asset.try_get::<Option<i64>,_>("size_bytes")?,
        "duration_ms":asset.try_get::<Option<i64>,_>("duration_ms")?,
        "created_at":asset.try_get::<DateTime<Utc>,_>("created_at")?,
    }))
}

pub(crate) async fn enroll(
    s: &AppState,
    p: &crate::remote_render::EnrollRequest,
) -> AppResult<Value> {
    can_write()?;
    if !p.enrollment_token.starts_with("vre_")
        || !(32..=256).contains(&p.enrollment_token.len())
        || !(1..=128).contains(&p.worker_key.len())
        || !(1..=128).contains(&p.name.len())
        || !(1..=32).contains(&p.platform.len())
        || p.architecture.as_deref().is_some_and(|a| a.len() > 32)
        || p.version.len() > 64
        || !(1..=100).contains(&p.protocol_version)
        || p.render_spec_versions.is_empty()
        || p.render_spec_versions.len() > 16
        || !(1..=32).contains(&p.max_concurrency)
        || !p.capabilities.is_object()
        || !p.resources.is_object()
        || !p.labels.is_object()
    {
        return Err(AppError::Invalid(
            "invalid legacy render worker enrollment".into(),
        ));
    }
    let hashed = hex::encode(Sha256::digest(p.enrollment_token.as_bytes()));
    let mut tx = s.db.pool.begin().await?;
    let enrollment = sqlx::query(
        "SELECT id FROM render_worker_enrollments
         WHERE token_hash=$1 AND status='active' AND expires_at>now()
         FOR UPDATE",
    )
    .bind(hashed)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(AppError::Unauthorized)?;
    let enrollment_id: Uuid = enrollment.try_get("id")?;
    let previous = sqlx::query(
        "SELECT id,max_concurrency,labels,runtime_engine FROM render_workers WHERE worker_key=$1 FOR UPDATE",
    )
    .bind(&p.worker_key)
    .fetch_optional(&mut *tx)
    .await?;
    if previous
        .as_ref()
        .is_some_and(|r| r.try_get::<String, _>("runtime_engine").ok().as_deref() != Some("python"))
    {
        return Err(AppError::Conflict(
            "worker_key belongs to a Rust-native worker; cannot replace its credential".into(),
        ));
    }
    let id = previous
        .as_ref()
        .map(|r| r.get::<Uuid, _>("id"))
        .unwrap_or_else(Uuid::new_v4);
    let limit = previous
        .as_ref()
        .map(|r| r.get::<i32, _>("max_concurrency"))
        .unwrap_or(p.max_concurrency);
    let mut labels = p.labels.as_object().cloned().unwrap_or_default();
    if let Some(ref old) = previous {
        let previous_labels: Value = old.try_get("labels")?;
        if let Some(devices) = previous_labels.get("disabled_render_device_ids") {
            labels.insert("disabled_render_device_ids".into(), devices.clone());
        }
    }
    use base64::Engine as _;
    let entropy = [Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4()]
        .into_iter()
        .flat_map(|id| id.into_bytes())
        .collect::<Vec<_>>();
    let credential = format!(
        "vrw_{}",
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(entropy)
    );
    let password = hex::encode(Sha256::digest(credential.as_bytes()));
    if previous.is_some() {
        sqlx::query(
            "UPDATE render_workers SET name=$2,platform=$3,architecture=$4,
             version=$5,protocol_version=$6,render_spec_versions=$7,
             capabilities=$8,resources=$9,labels=$10,max_concurrency=$11,
             credential_hash=$12,credential_issued_at=now(),
             credential_revoked_at=NULL,status='online',last_seen_at=now(),
             updated_at=now() WHERE id=$1",
        )
        .bind(id)
        .bind(&p.name)
        .bind(&p.platform)
        .bind(&p.architecture)
        .bind(&p.version)
        .bind(p.protocol_version)
        .bind(json!(p.render_spec_versions))
        .bind(&p.capabilities)
        .bind(&p.resources)
        .bind(Value::Object(labels))
        .bind(limit)
        .bind(&password)
        .execute(&mut *tx)
        .await?;
    } else {
        sqlx::query(
            "INSERT INTO render_workers(
             id,worker_key,name,platform,architecture,version,protocol_version,
             render_spec_versions,capabilities,resources,labels,max_concurrency,
             credential_hash,credential_issued_at,credential_revoked_at,status,last_seen_at)
             VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),NULL,'online',now())",
        )
        .bind(id)
        .bind(&p.worker_key)
        .bind(&p.name)
        .bind(&p.platform)
        .bind(&p.architecture)
        .bind(&p.version)
        .bind(p.protocol_version)
        .bind(json!(p.render_spec_versions))
        .bind(&p.capabilities)
        .bind(&p.resources)
        .bind(Value::Object(labels))
        .bind(limit)
        .bind(&password)
        .execute(&mut *tx)
        .await?;
    }
    sqlx::query(
        "UPDATE render_worker_enrollments
         SET status='consumed',consumed_at=now(),worker_id=$2 WHERE id=$1",
    )
    .bind(enrollment_id)
    .bind(id)
    .execute(&mut *tx)
    .await?;
    let worker = sqlx::query("SELECT * FROM render_workers WHERE id=$1")
        .bind(id)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(json!({"worker":worker_read(&worker)?,"credential":credential}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn credential_dispatch_never_accepts_native_hex_tokens() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::AUTHORIZATION,
            "Bearer 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                .parse()
                .unwrap(),
        );
        assert!(!is_legacy(&headers));
        headers.insert(
            header::AUTHORIZATION,
            "Bearer vrw_original-worker-token-material".parse().unwrap(),
        );
        assert!(is_legacy(&headers));
    }
    #[test]
    fn rejects_short_or_wrong_prefix() {
        let mut headers = HeaderMap::new();
        headers.insert(header::AUTHORIZATION, "Bearer vrw_x".parse().unwrap());
        assert!(!is_legacy(&headers));
    }
}
