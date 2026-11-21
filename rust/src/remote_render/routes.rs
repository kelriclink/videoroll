use super::{
    protocol::{
        ArtifactSpec, CancelState, ClaimRequest, ClaimResponse, EnrollRequest, EnrollmentCreate,
        EnrollmentInfo, ExecutionComplete, ExecutionFail, ExecutionHeartbeat, ExecutionInfo,
        ExecutionLog, OutputProbe, RenderSpec, WorkerHeartbeat, WorkerInfo, WorkerResponse,
    },
    service::{
        check_worker_label, is_fence, log_message_tail, output_key, receipt, strong_token,
        token_hash, validate_lease,
    },
};
use crate::{
    AppError, AppResult,
    api::AppState,
    media::ffmpeg::{latest_asset_key, probe_video},
};
use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, Multipart, Path, Query, Request, State},
    http::{HeaderMap, StatusCode, header},
    response::Response,
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
};
use tokio_util::io::ReaderStream;
use uuid::Uuid;

const LEASE_SECONDS: i64 = 240;
const MAX_VIDEO_BYTES: u64 = 256 * 1024 * 1024 * 1024;

fn bearer(headers: &HeaderMap) -> AppResult<&str> {
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let token = auth.strip_prefix("Bearer ").unwrap_or("");
    if token.len() != 64 || !token.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(AppError::Unauthorized);
    }
    Ok(token)
}
async fn authenticate(state: &AppState, headers: &HeaderMap) -> AppResult<Uuid> {
    let hash = token_hash(bearer(headers)?);
    let found: Option<Uuid> = sqlx::query_scalar(
        "SELECT id FROM vr2_render_workers WHERE credential_hash=$1 AND enabled=true",
    )
    .bind(hash)
    .fetch_optional(&state.db.pool)
    .await?;
    found.ok_or(AppError::Unauthorized)
}
async fn enrolled_worker(state: &AppState, headers: &HeaderMap, worker_id: Uuid) -> AppResult<()> {
    if authenticate(state, headers).await? != worker_id {
        return Err(AppError::Unauthorized);
    }
    Ok(())
}
async fn enrolled_execution(
    state: &AppState,
    headers: &HeaderMap,
    execution_id: Uuid,
) -> AppResult<sqlx::postgres::PgRow> {
    let worker_id = authenticate(state, headers).await?;
    sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1 AND worker_id=$2")
        .bind(execution_id)
        .bind(worker_id)
        .fetch_optional(&state.db.pool)
        .await?
        .ok_or(AppError::Unauthorized)
}
async fn read_worker(state: &AppState, id: Uuid) -> AppResult<WorkerInfo> {
    let row=sqlx::query(
        "SELECT w.*, (SELECT count(*) FROM vr2_render_executions e WHERE e.worker_id=w.id AND e.state IN ('running','uploading') AND e.lease_until>now())::bigint AS active_jobs FROM vr2_render_workers w WHERE w.id=$1"
    ).bind(id).fetch_optional(&state.db.pool).await?
        .ok_or_else(||AppError::NotFound("render worker not found".into()))?;
    Ok(WorkerInfo {
        id: row.try_get("id")?,
        worker_key: row.try_get("worker_key")?,
        name: row.try_get("name")?,
        platform: row.try_get("platform")?,
        architecture: row.try_get("architecture")?,
        version: row.try_get("version")?,
        protocol_version: row.try_get("protocol_version")?,
        render_spec_versions: row.try_get("render_spec_versions")?,
        capabilities: row.try_get("capabilities")?,
        resources: row.try_get("resources")?,
        labels: row.try_get("labels")?,
        status: row.try_get("status")?,
        enabled: row.try_get("enabled")?,
        draining: row.try_get("draining")?,
        max_concurrency: row.try_get("max_concurrency")?,
        active_jobs: row.try_get("active_jobs")?,
        last_seen_at: row.try_get("last_seen_at")?,
    })
}
fn execution_info(row: &sqlx::postgres::PgRow) -> AppResult<ExecutionInfo> {
    Ok(ExecutionInfo {
        id: row.try_get("id")?,
        render_job_id: row.try_get("job_id")?,
        worker_id: row.try_get("worker_id")?,
        attempt: row.try_get("attempt")?,
        fence_token: row
            .try_get::<Option<String>, _>("fence_token")?
            .unwrap_or_default(),
        state: row.try_get("state")?,
        transfer_mode: "http".into(),
        progress: row.try_get("progress")?,
        lease_until: row.try_get("lease_until")?,
        render_spec: row.try_get("render_spec")?,
    })
}
async fn create_enrollment(
    State(s): State<AppState>,
    Json(payload): Json<EnrollmentCreate>,
) -> AppResult<(StatusCode, Json<EnrollmentInfo>)> {
    if !(5..=1440).contains(&payload.ttl_minutes) || payload.label.len() > 128 {
        return Err(AppError::Invalid("enrollment TTL or label invalid".into()));
    }
    let token = strong_token();
    let id = Uuid::new_v4();
    let expires_at = Utc::now() + chrono::Duration::minutes(payload.ttl_minutes);
    sqlx::query(
        "INSERT INTO vr2_render_enrollments(id,token_hash,label,expires_at) VALUES($1,$2,$3,$4)",
    )
    .bind(id)
    .bind(token_hash(&token))
    .bind(payload.label.trim())
    .bind(expires_at)
    .execute(&s.db.pool)
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(EnrollmentInfo {
            id,
            label: payload.label,
            token,
            expires_at,
            server_url: "/api".into(),
        }),
    ))
}
async fn admin_workers(State(s): State<AppState>) -> AppResult<Json<Vec<WorkerInfo>>> {
    let ids = sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM vr2_render_workers ORDER BY last_seen_at DESC LIMIT 100",
    )
    .fetch_all(&s.db.pool)
    .await?;
    let mut out = Vec::with_capacity(ids.len());
    for id in ids {
        out.push(read_worker(&s, id).await?);
    }
    Ok(Json(out))
}
async fn protocol() -> Json<Value> {
    Json(json!({
        "protocol_version":1,"render_spec_versions":[1],"transfer_modes":["http"],
        "features":[
            "worker_registration","capability_report","pull_claim","lease_heartbeat",
            "progress","log_tail","range_download","output_upload","fencing",
            "retryable_failure","cancel_poll"
        ],"lease_seconds":LEASE_SECONDS
    }))
}
async fn enroll(
    State(s): State<AppState>,
    Json(payload): Json<EnrollRequest>,
) -> AppResult<Json<WorkerResponse>> {
    if payload.enrollment_token.len() != 64
        || !payload.render_spec_versions.contains(&1)
        || payload.protocol_version != 1
        || !(1..=32).contains(&payload.max_concurrency)
    {
        return Err(AppError::Invalid(
            "unsupported render worker enrollment capabilities".into(),
        ));
    }
    for text in [&payload.worker_key, &payload.name, &payload.platform] {
        check_worker_label(text)?;
    }
    let mut tx = s.db.pool.begin().await?;
    let enrollment=sqlx::query(
        "SELECT id FROM vr2_render_enrollments WHERE token_hash=$1 AND consumed_at IS NULL AND expires_at>now() FOR UPDATE"
    ).bind(token_hash(&payload.enrollment_token)).fetch_optional(&mut *tx).await?
        .ok_or(AppError::Unauthorized)?;
    let enrollment_id: Uuid = enrollment.try_get("id")?;
    let id = Uuid::new_v4();
    let credential = strong_token();
    let updated=sqlx::query(
        "INSERT INTO vr2_render_workers(id,worker_key,name,platform,architecture,version,protocol_version,render_spec_versions,capabilities,resources,labels,credential_hash,max_concurrency,status) VALUES($1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,$12,'online') ON CONFLICT(worker_key) DO UPDATE SET name=$3,platform=$4,architecture=$5,version=$6,render_spec_versions=$7,capabilities=$8,resources=$9,labels=$10,credential_hash=$11,max_concurrency=$12,enabled=true,draining=false,status='online',last_seen_at=now(),updated_at=now() RETURNING id"
    ).bind(id).bind(&payload.worker_key).bind(&payload.name)
        .bind(&payload.platform).bind(&payload.architecture).bind(&payload.version)
        .bind(serde_json::to_value(&payload.render_spec_versions)
            .map_err(|_|AppError::Invalid("invalid render spec versions".into()))?)
        .bind(&payload.capabilities).bind(&payload.resources).bind(&payload.labels)
        .bind(token_hash(&credential)).bind(payload.max_concurrency)
        .fetch_one(&mut *tx).await?;
    let assigned_id: Uuid = updated.try_get("id")?;
    sqlx::query("UPDATE vr2_render_enrollments SET consumed_at=now(),worker_id=$1 WHERE id=$2")
        .bind(assigned_id)
        .bind(enrollment_id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(WorkerResponse {
        worker: read_worker(&s, assigned_id).await?,
        credential,
    }))
}
async fn heartbeat(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Json(payload): Json<WorkerHeartbeat>,
) -> AppResult<Json<WorkerInfo>> {
    enrolled_worker(&s, &headers, id).await?;
    if !matches!(
        payload.status.as_str(),
        "online" | "busy" | "paused" | "draining"
    ) || payload.active_execution_ids.len() > 64
    {
        return Err(AppError::Invalid("invalid render worker heartbeat".into()));
    }
    sqlx::query(
        "UPDATE vr2_render_workers SET status=$1,resources=$2,capabilities=COALESCE($3,capabilities),last_seen_at=now(),updated_at=now() WHERE id=$4"
    ).bind(payload.status).bind(payload.resources).bind(payload.capabilities).bind(id)
        .execute(&s.db.pool).await?;
    Ok(Json(read_worker(&s, id).await?))
}
async fn build_spec(s: &AppState, exec: &ExecutionInfo) -> AppResult<RenderSpec> {
    let row =
        sqlx::query("SELECT j.workflow_id,j.task_id,j.id AS job_id FROM vr2_jobs j WHERE j.id=$1")
            .bind(exec.render_job_id)
            .fetch_one(&s.db.pool)
            .await?;
    let task_id: Uuid = row.try_get("task_id")?;
    let flow_id: Uuid = row.try_get("workflow_id")?;
    let job = crate::domain::WorkItem {
        id: exec.render_job_id,
        workflow_id: flow_id,
        task_id,
        stage: crate::domain::Stage::Render,
        generation: 1,
        operation_key: String::new(),
        lease_token: Uuid::nil(),
        attempts: 0,
        max_attempts: 5,
        payload: Value::Null,
    };
    let profile = crate::media::ffmpeg::profile(&s.db, &job).await?;
    let source = latest_asset_key(&s.db, task_id, "video_raw")
        .await?
        .ok_or_else(|| AppError::NotFound("remote render input video_raw missing".into()))?;
    let ass = latest_asset_key(&s.db, task_id, "subtitle_ass").await?;
    let srt = latest_asset_key(&s.db, task_id, "subtitle_srt").await?;
    let mode = if profile.burn_in && ass.is_some() {
        "burn_in"
    } else if profile.soft_sub && srt.is_some() {
        "soft_sub"
    } else {
        "noop"
    };
    let mut artifacts = vec![];
    for (role, key) in [
        ("input", Some(source.clone())),
        ("ass", if mode == "burn_in" { ass.clone() } else { None }),
        (
            "srt",
            if mode == "soft_sub" {
                srt.clone()
            } else {
                None
            },
        ),
    ] {
        if let Some(key) = key {
            let path = s.storage.resolve_existing(&key).await?;
            let metadata = fs::metadata(&path).await?;
            let sha=sqlx::query_scalar::<_,Option<String>>(
                "SELECT sha256 FROM assets WHERE task_id=$1 AND storage_key=$2 ORDER BY created_at DESC LIMIT 1"
            ).bind(task_id).bind(&key).fetch_optional(&s.db.pool).await?.flatten();
            let sha = match sha {
                Some(value) if value.len() == 64 => value,
                _ => s.storage.sha256(&key).await?.0,
            };
            artifacts.push(ArtifactSpec {
                role: role.into(),
                storage_key: key,
                size_bytes: Some(metadata.len()),
                sha256: Some(sha),
                download_url: format!("/render-workers/v1/executions/{}/artifacts/{role}", exec.id),
            });
        }
    }
    let config: Value = serde_json::to_value(&profile)
        .map_err(|_| AppError::Invalid("render profile invalid".into()))?;
    Ok(RenderSpec {
        schema_version: 1,
        render_job_id: job.id,
        task_id,
        mode: mode.into(),
        request: json!({
            "runtime_profile":true,"burn_in":profile.burn_in,
            "soft_sub":profile.soft_sub,
            "input_key":source,"srt_key":srt,"ass_key":ass,
            "render":config,
        }),
        artifacts,
    })
}
async fn claim(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Json(payload): Json<ClaimRequest>,
) -> AppResult<Json<ClaimResponse>> {
    enrolled_worker(&s, &headers, id).await?;
    let empty = || {
        Json(ClaimResponse {
            execution: None,
            render_spec: None,
            retry_after_seconds: 5,
        })
    };
    if payload.available_encoders.len() > 128
        || payload
            .available_encoders
            .iter()
            .any(|v| v.len() > 80 || v.chars().any(char::is_control))
    {
        return Err(AppError::Invalid("invalid renderer encoder list".into()));
    }
    if payload.available_slots == 0 || !payload.accepted_transfer_modes.iter().any(|v| v == "http")
    {
        return Ok(empty());
    }
    let mut tx = s.db.pool.begin().await?;
    let worker=sqlx::query(
        "SELECT enabled,draining,max_concurrency,status,last_seen_at FROM vr2_render_workers WHERE id=$1 FOR UPDATE"
    ).bind(id).fetch_one(&mut *tx).await?;
    let enabled: bool = worker.try_get("enabled")?;
    let draining: bool = worker.try_get("draining")?;
    let status: String = worker.try_get("status")?;
    let max: i32 = worker.try_get("max_concurrency")?;
    let last_seen: DateTime<Utc> = worker.try_get("last_seen_at")?;
    if !enabled
        || draining
        || status == "paused"
        || status == "draining"
        || last_seen < Utc::now() - chrono::Duration::minutes(5)
    {
        return Ok(empty());
    }
    let active:i64=sqlx::query_scalar(
        "SELECT count(*) FROM vr2_render_executions WHERE worker_id=$1 AND state IN ('running','uploading') AND lease_until>now()"
    ).bind(id).fetch_one(&mut *tx).await?;
    if active >= max as i64 {
        return Ok(empty());
    }
    let row = sqlx::query(
        r#"WITH ready AS(
          SELECT r.id FROM vr2_render_executions r
          JOIN vr2_jobs j ON j.id=r.job_id
          JOIN vr2_workflows w ON w.id=r.workflow_id
          JOIN tasks t ON t.id=r.task_id
          WHERE w.state='running' AND j.state='awaiting'
            AND j.external_ticket=r.id::text
            AND t.status::text NOT IN ('CANCELED','FAILED')
            AND (
              r.state='queued'
              OR (r.state IN ('running','uploading') AND r.lease_until<now())
            )
          ORDER BY t.priority DESC,r.created_at
          FOR UPDATE OF r SKIP LOCKED LIMIT 1
        )
        UPDATE vr2_render_executions r SET
          worker_id=$1,fence_token=$2,attempt=r.attempt+1,state='running',
          progress=0,lease_until=now()+($3*interval '1 second'),
          updated_at=now()
        FROM ready WHERE r.id=ready.id RETURNING r.*"#,
    )
    .bind(id)
    .bind(strong_token())
    .bind(LEASE_SECONDS)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    let Some(row) = row else { return Ok(empty()) };
    let mut info = execution_info(&row)?;
    let spec = build_spec(&s, &info).await?;
    info.render_spec = serde_json::to_value(&spec)
        .map_err(|_| AppError::Invalid("render spec serialization failed".into()))?;
    sqlx::query(
        "UPDATE vr2_render_executions SET mode=$1,render_spec=$2,updated_at=now() WHERE id=$3 AND worker_id=$4 AND fence_token=$5 AND state='running'"
    ).bind(&spec.mode).bind(&info.render_spec).bind(info.id).bind(id)
        .bind(&info.fence_token).execute(&s.db.pool).await?;
    Ok(Json(ClaimResponse {
        execution: Some(info),
        render_spec: Some(spec),
        retry_after_seconds: 5,
    }))
}
async fn get_execution_with_fence(
    s: &AppState,
    execution_id: Uuid,
    worker_id: Uuid,
    fence: &str,
) -> AppResult<sqlx::postgres::PgRow> {
    let row = sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1 AND worker_id=$2")
        .bind(execution_id)
        .bind(worker_id)
        .fetch_optional(&s.db.pool)
        .await?
        .ok_or(AppError::Unauthorized)?;
    validate_lease(&row, fence)?;
    Ok(row)
}
async fn execution_heartbeat(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Json(payload): Json<ExecutionHeartbeat>,
) -> AppResult<Json<ExecutionInfo>> {
    let worker_id = authenticate(&s, &headers).await?;
    if payload.progress.is_some_and(|n| !(0..=100).contains(&n)) {
        return Err(AppError::Invalid("invalid progress percentage".into()));
    }
    if let Some(stage) = payload.stage.as_deref()
        && (stage.len() > 64 || stage.chars().any(char::is_control))
    {
        return Err(AppError::Invalid("invalid execution stage".into()));
    }
    let mut tx = s.db.pool.begin().await?;
    let row =
        sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1 AND worker_id=$2 FOR UPDATE")
            .bind(id)
            .bind(worker_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(AppError::Unauthorized)?;
    validate_lease(&row, &payload.fence_token)?;
    let progress = payload.progress.unwrap_or(row.try_get("progress")?);
    sqlx::query(
        "UPDATE vr2_render_executions SET lease_until=now()+($1*interval '1 second'),progress=$2,metrics=$3,updated_at=now() WHERE id=$4"
    ).bind(LEASE_SECONDS).bind(progress).bind(payload.metrics).bind(id)
        .execute(&mut *tx).await?;
    let row = sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1")
        .bind(id)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(execution_info(&row)?))
}
async fn execution_log(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Json(payload): Json<ExecutionLog>,
) -> AppResult<Json<ExecutionInfo>> {
    let worker_id = authenticate(&s, &headers).await?;
    if payload.text.len() > 65536 {
        return Err(AppError::Invalid("render worker log too long".into()));
    }
    let mut tx = s.db.pool.begin().await?;
    let row =
        sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1 AND worker_id=$2 FOR UPDATE")
            .bind(id)
            .bind(worker_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(AppError::Unauthorized)?;
    validate_lease(&row, &payload.fence_token)?;
    let prior: String = row.try_get("log_tail")?;
    let tail = log_message_tail(&prior, &payload.text)?;
    sqlx::query("UPDATE vr2_render_executions SET log_tail=$1,updated_at=now() WHERE id=$2")
        .bind(tail)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    let row = sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1")
        .bind(id)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(execution_info(&row)?))
}
async fn execution_cancel(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
) -> AppResult<Json<CancelState>> {
    let row = enrolled_execution(&s, &headers, id).await?;
    let task_id: Uuid = row.try_get("task_id")?;
    let workflow_id: Uuid = row.try_get("workflow_id")?;
    let current:Option<(String,String)>=sqlx::query_as(
        "SELECT t.status::text,w.state FROM vr2_workflows w JOIN tasks t ON t.id=w.task_id WHERE w.id=$1 AND t.id=$2"
    ).bind(workflow_id).bind(task_id).fetch_optional(&s.db.pool).await?;
    let state = match current {
        Some((status, flow)) if status == "CANCELED" || flow != "running" => {
            Some(format!("render canceled: task={status},workflow={flow}"))
        }
        None => Some("render workflow disappeared".into()),
        _ => None,
    };
    Ok(Json(CancelState {
        cancel_requested: state.is_some(),
        reason: state,
    }))
}
fn parse_range(requested: Option<&str>, size: u64) -> AppResult<(u64, u64, StatusCode)> {
    if size == 0 {
        return Err(AppError::NotFound("render input file is empty".into()));
    }
    match requested {
        Some(raw) => {
            let (start, end) = crate::assets::parse_range(raw, size)
                .ok_or_else(|| AppError::Invalid("invalid Range header".into()))?;
            Ok((start, end, StatusCode::PARTIAL_CONTENT))
        }
        None => Ok((0, size - 1, StatusCode::OK)),
    }
}
async fn execution_artifact(
    State(s): State<AppState>,
    Path((id, role)): Path<(Uuid, String)>,
    headers: HeaderMap,
    request: Request,
) -> AppResult<Response> {
    // Auth must be checked before exposing which artifact roles exist.
    let execution = enrolled_execution(&s, &headers, id).await?;
    let status: String = execution.try_get("state")?;
    let until: Option<DateTime<Utc>> = execution.try_get("lease_until")?;
    if !matches!(status.as_str(), "running" | "uploading") || !until.is_some_and(|v| v > Utc::now())
    {
        return Err(AppError::Conflict("render lease expired".into()));
    }
    let spec_value: Value = execution.try_get("render_spec")?;
    let spec: RenderSpec = serde_json::from_value(spec_value)
        .map_err(|_| AppError::Conflict("remote render spec has not been committed".into()))?;
    let artifact = spec
        .artifacts
        .iter()
        .find(|v| v.role == role)
        .ok_or_else(|| AppError::NotFound("render artifact role missing".into()))?;
    let path = s.storage.resolve_existing(&artifact.storage_key).await?;
    let mut file = fs::File::open(path).await?;
    let size = file.metadata().await?.len();
    let range = request
        .headers()
        .get(header::RANGE)
        .and_then(|v| v.to_str().ok());
    let (start, end, code) = parse_range(range, size)?;
    file.seek(std::io::SeekFrom::Start(start)).await?;
    let stream = ReaderStream::new(file.take(end - start + 1));
    let mut response = Response::new(Body::from_stream(stream));
    *response.status_mut() = code;
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        "application/octet-stream".parse().unwrap(),
    );
    headers.insert(header::ACCEPT_RANGES, "bytes".parse().unwrap());
    headers.insert(
        header::CONTENT_LENGTH,
        (end - start + 1).to_string().parse().unwrap(),
    );
    headers.insert("x-content-type-options", "nosniff".parse().unwrap());
    if code == StatusCode::PARTIAL_CONTENT {
        headers.insert(
            header::CONTENT_RANGE,
            format!("bytes {start}-{end}/{size}").parse().unwrap(),
        );
    }
    Ok(response)
}
async fn execution_output_probe(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Query(q): Query<OutputProbe>,
) -> AppResult<Json<Value>> {
    let row = enrolled_execution(&s, &headers, id).await?;
    let fence: String = row
        .try_get::<Option<String>, _>("fence_token")?
        .unwrap_or_default();
    if !is_fence(&fence, &q.fence_token) {
        return Err(AppError::Conflict("render execution fenced".into()));
    }
    let asset: Option<Uuid> = row.try_get("uploaded_asset_id")?;
    let Some(asset) = asset else {
        return Err(AppError::NotFound(
            "execution output not yet uploaded".into(),
        ));
    };
    let item = s.db.asset(asset).await?;
    Ok(Json(serde_json::to_value(item).map_err(|_| {
        AppError::Invalid("render asset serialization failed".into())
    })?))
}
async fn execution_output_upload(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let worker_id = authenticate(&s, &headers).await?;
    let initial = get_execution_with_fence(&s, id, worker_id, &{
        // The fence is supplied by multipart. Verify the ownership now;
        // compare fence after parsing the header and before committing.
        let row = enrolled_execution(&s, &headers, id).await?;
        row.try_get::<Option<String>, _>("fence_token")?
            .unwrap_or_default()
    })
    .await?;
    let task_id: Uuid = initial.try_get("task_id")?;
    let workflow_id: Uuid = initial.try_get("workflow_id")?;
    let stage_job: Uuid = initial.try_get("job_id")?;
    let file_key = output_key(task_id, id);
    let dest = s.storage.resolve_existing(&file_key).await?;
    if let Some(dir) = dest.parent() {
        fs::create_dir_all(dir).await?;
    }
    let temp = dest.with_extension("uploading");
    let task=async {
        let mut fence=None::<String>;
        let mut size=0u64;
        let mut digest=Sha256::new();
        let mut received_file=false;
        let mut disk=None::<fs::File>;
        while let Some(mut field)=multipart.next_field().await
            .map_err(|e|AppError::Invalid(format!("invalid multipart: {e}")))?{
            let Some(name)=field.name() else{continue};
            match name {
                "fence_token"=>{
                    let value=field.text().await
                        .map_err(|_|AppError::Invalid("invalid upload fence field".into()))?;
                    if value.len()!=64 || fence.is_some(){
                        return Err(AppError::Invalid("invalid or duplicate fence".into()));
                    }
                    fence=Some(value);
                },
                "file"=>{
                    if received_file{return Err(AppError::Invalid("multiple render output files".into()));}
                    received_file=true;
                    let mut file=fs::OpenOptions::new().create_new(true).write(true)
                        .open(&temp).await?;
                    while let Some(chunk)=field.chunk().await
                        .map_err(|e|AppError::Invalid(format!("invalid media stream: {e}")))?{
                        size=size.checked_add(chunk.len() as u64)
                            .ok_or_else(||AppError::Invalid("output size overflow".into()))?;
                        if size>MAX_VIDEO_BYTES {
                            return Err(AppError::Invalid("remote render output exceeds 256GiB".into()));
                        }
                        digest.update(&chunk);
                        file.write_all(&chunk).await?;
                    }
                    file.sync_all().await?;
                    disk=Some(file);
                },
                _=>return Err(AppError::Invalid("unexpected render upload field".into())),
            }
        }
        let Some(fence)=fence else{return Err(AppError::Invalid("render upload fence absent".into()));};
        let Some(file)=disk else{return Err(AppError::Invalid("render output file absent".into()));};
        if size==0{return Err(AppError::Invalid("zero-byte render output invalid".into()));}
        drop(file);
        let bytes_digest=hex::encode(digest.finalize());
        // Validate expensive media outside any row lock.
        let _=probe_video(&s.config.ffprobe_binary,&temp).await?;
        let mut tx=s.db.pool.begin().await?;
        // Acquire locks in the same order as the Scheduler: Task -> Workflow -> Job -> Execution.
        let status:String=sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id).fetch_one(&mut *tx).await?;
        let flow:String=sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
            .bind(workflow_id).fetch_one(&mut *tx).await?;
        let job_state:String=sqlx::query_scalar("SELECT state FROM vr2_jobs WHERE id=$1 FOR UPDATE")
            .bind(stage_job).fetch_one(&mut *tx).await?;
        let row=sqlx::query(
            "SELECT * FROM vr2_render_executions WHERE id=$1 AND worker_id=$2 FOR UPDATE"
        ).bind(id).bind(worker_id).fetch_one(&mut *tx).await?;
        validate_lease(&row,&fence)?;
        if status=="CANCELED" || flow!="running" || job_state!="awaiting" {
            return Err(AppError::Conflict("render upload rejected after parent stopped".into()));
        }
        if row.try_get::<Option<Uuid>,_>("uploaded_asset_id")?.is_some(){
            return Err(AppError::Conflict("render output already registered".into()));
        }
        // Atomic rename before SQL commit; on failure file becomes an orphan
        // under the v2 workdir, eligible for safe terminal cleanup.
        fs::rename(&temp,&dest).await?;
        let asset_id=Uuid::new_v4();
        sqlx::query(
            "INSERT INTO vr2_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days') ON CONFLICT(storage_key) DO UPDATE SET checksum_sha256=$2,size_bytes=$3,retain_until=GREATEST(vr2_objects.retain_until,now()+interval '2 days') WHERE vr2_objects.state='live'"
        ).bind(&file_key).bind(&bytes_digest).bind(size as i64)
            .execute(&mut *tx).await?;
        sqlx::query(
            "INSERT INTO vr2_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'workflow',$2,'video_final') ON CONFLICT DO NOTHING"
        ).bind(&file_key).bind(workflow_id).execute(&mut *tx).await?;
        sqlx::query(
            "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES($1,$2,'video_final'::asset_kind,$3,$4,$5)"
        ).bind(asset_id).bind(task_id).bind(&file_key).bind(&bytes_digest).bind(size as i64)
            .execute(&mut *tx).await?;
        sqlx::query(
            "UPDATE vr2_render_executions SET uploaded_asset_id=$1,state='uploading',updated_at=now() WHERE id=$2"
        ).bind(asset_id).bind(id).execute(&mut *tx).await?;
        tx.commit().await?;
        let asset=s.db.asset(asset_id).await?;
        serde_json::to_value(asset)
            .map_err(|_|AppError::Invalid("cannot serialize render asset".into()))
    }.await;
    if task.is_err() {
        let _ = fs::remove_file(&temp).await;
    }
    Ok(Json(task?))
}
async fn execution_completed(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Json(payload): Json<ExecutionComplete>,
) -> AppResult<Json<ExecutionInfo>> {
    if !payload.output.is_object()
        || serde_json::to_vec(&payload.output).map_or(true, |bytes| bytes.len() > 128 * 1024)
    {
        return Err(AppError::Invalid(
            "render completion metrics must be a bounded object".into(),
        ));
    }
    let worker_id = authenticate(&s, &headers).await?;
    let row = enrolled_execution(&s, &headers, id).await?;
    let state: String = row.try_get("state")?;
    let job_id: Uuid = row.try_get("job_id")?;
    let task_id: Uuid = row.try_get("task_id")?;
    let flow_id: Uuid = row.try_get("workflow_id")?;
    let mode: String = row.try_get("mode")?;
    if state != "completed" {
        validate_lease(&row, &payload.fence_token)?;
    } else {
        let fence: String = row
            .try_get::<Option<String>, _>("fence_token")?
            .unwrap_or_default();
        if !is_fence(&fence, &payload.fence_token) {
            return Err(AppError::Conflict(
                "completed render execution has another fence".into(),
            ));
        }
    }
    let asset_id: Option<Uuid> = row.try_get("uploaded_asset_id")?;
    if payload.output_asset_id.is_some() && payload.output_asset_id != asset_id {
        return Err(AppError::Conflict(
            "uploaded render asset does not match completion".into(),
        ));
    }
    let (asset_id, key) = if mode == "noop" {
        let key = latest_asset_key(&s.db, task_id, "video_raw")
            .await?
            .ok_or_else(|| AppError::NotFound("noop render has no source video".into()))?;
        let job = crate::domain::WorkItem {
            id: job_id,
            workflow_id: flow_id,
            task_id,
            stage: crate::domain::Stage::Render,
            generation: 1,
            operation_key: String::new(),
            lease_token: Uuid::nil(),
            attempts: 0,
            max_attempts: 5,
            payload: Value::Null,
        };
        crate::media::ffmpeg::ensure_task_asset(
            &s.db,
            &s.storage,
            task_id,
            &job,
            "video_final",
            &key,
        )
        .await?;
        (None, Some(key))
    } else {
        let asset = asset_id.ok_or_else(|| {
            AppError::Conflict("render output must be uploaded before completion".into())
        })?;
        let record = s.db.asset(asset).await?;
        (Some(asset), Some(record.storage_key))
    };
    let result = receipt(id, asset_id, key.as_deref(), &mode);
    // A state update can commit even if the subsequent stage completion call
    // loses network; repeated calls are idempotent via the exact ticket+result.
    if state != "completed" {
        let row=sqlx::query(
            "UPDATE vr2_render_executions SET state='completed',progress=100,finished_at=now(),updated_at=now() WHERE id=$1 AND worker_id=$2 AND fence_token=$3 AND state IN ('running','uploading') AND lease_until>now() RETURNING *"
        ).bind(id).bind(worker_id).bind(&payload.fence_token)
            .fetch_optional(&s.db.pool).await?
            .ok_or_else(||AppError::Conflict("render fence expired before completion".into()))?;
        let _ = row;
    }
    let scheduler = crate::scheduler::Scheduler::new(s.db.clone(), s.config.job_lease);
    scheduler
        .complete_external(job_id, &id.to_string(), result)
        .await?;
    let row = sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1")
        .bind(id)
        .fetch_one(&s.db.pool)
        .await?;
    Ok(Json(execution_info(&row)?))
}
async fn execution_failed(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    headers: HeaderMap,
    Json(payload): Json<ExecutionFail>,
) -> AppResult<Json<ExecutionInfo>> {
    let worker_id = authenticate(&s, &headers).await?;
    if payload.error.trim().is_empty() || payload.error.len() > 8192 {
        return Err(AppError::Invalid("render failure detail invalid".into()));
    }
    let mut tx = s.db.pool.begin().await?;
    let row =
        sqlx::query("SELECT * FROM vr2_render_executions WHERE id=$1 AND worker_id=$2 FOR UPDATE")
            .bind(id)
            .bind(worker_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(AppError::Unauthorized)?;
    validate_lease(&row, &payload.fence_token)?;
    let attempt: i32 = row.try_get("attempt")?;
    let job_id: Uuid = row.try_get("job_id")?;
    let will_retry = payload.retryable && attempt < 5;
    let next = if will_retry { "queued" } else { "failed" };
    let row=sqlx::query(
        "UPDATE vr2_render_executions SET state=$1,worker_id=CASE WHEN $2 THEN NULL ELSE worker_id END,fence_token=CASE WHEN $2 THEN NULL ELSE fence_token END,lease_until=NULL,last_error=$3,updated_at=now(),finished_at=CASE WHEN $2 THEN NULL ELSE now() END WHERE id=$4 RETURNING *"
    ).bind(next).bind(will_retry).bind(payload.error.chars().take(1000).collect::<String>())
        .bind(id).fetch_one(&mut *tx).await?;
    tx.commit().await?;
    if !will_retry {
        let scheduler = crate::scheduler::Scheduler::new(s.db.clone(), s.config.job_lease);
        scheduler
            .fail_external(job_id, &id.to_string(), &payload.error)
            .await?;
    }
    // A retry reply represents a durable queued execution, not an active lease.
    Ok(Json(execution_info(&row)?))
}
pub fn worker_router() -> Router<AppState> {
    let routes = || {
        Router::new()
            .route("/protocol", get(protocol))
            .route("/enroll", post(enroll))
            .route("/{worker_id}/heartbeat", post(heartbeat))
            .route("/{worker_id}/claim", post(claim))
            .route("/executions/{id}/heartbeat", post(execution_heartbeat))
            .route("/executions/{id}/progress", post(execution_heartbeat))
            .route("/executions/{id}/logs", post(execution_log))
            .route("/executions/{id}/cancel", get(execution_cancel))
            .route("/executions/{id}/artifacts/{role}", get(execution_artifact))
            .route(
                "/executions/{id}/output",
                post(execution_output_upload)
                    .layer(DefaultBodyLimit::disable())
                    .get(execution_output_probe),
            )
            .route("/executions/{id}/complete", post(execution_completed))
            .route("/executions/{id}/fail", post(execution_failed))
    };
    Router::new()
        .nest("/render-workers/v1", routes())
        .nest("/api/render-workers/v1", routes())
}
pub fn admin_router() -> Router<AppState> {
    Router::new()
        .route("/render/enrollments", post(create_enrollment))
        .route("/render/workers", get(admin_workers))
}
