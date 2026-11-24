//! Source-verified V1 response adapters backed by the V2 runtime.
//! These are native database queries, not Python or HTTP reverse proxies.
//! Only V2-owned tasks are exposed. Unsupported legacy operations stay absent.
use crate::{AppError, AppResult, api::AppState, domain::AssetInfo};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::StatusCode,
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{FromRow, Row};
use std::collections::HashSet;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, FromRow)]
pub struct V1Task {
    pub id: Uuid,
    pub source_type: String,
    pub source_url: Option<String>,
    pub source_license: String,
    pub source_proof_url: Option<String>,
    pub status: String,
    pub stopped_status: Option<String>,
    pub priority: i32,
    pub created_by: Option<String>,
    pub display_title: Option<String>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub retry_count: i32,
    pub bilibili_upload: Option<Value>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

const TASK_VIEW: &str = r#"
SELECT t.id,t.source_type::text AS source_type,t.source_url,
  t.source_license::text AS source_license,t.source_proof_url,
  upper(t.status::text) AS status,upper(t.stopped_status::text) AS stopped_status,
  t.priority,t.created_by,
  COALESCE(
    NULLIF(trim(title.value_json->>'translated_title'),''),
    NULLIF(trim(title.value_json->>'source_title'),'')
  ) AS display_title,
  t.error_code,t.error_message,t.retry_count,NULL::jsonb AS bilibili_upload,
  t.created_at,t.updated_at
FROM tasks t
LEFT JOIN app_settings title ON title.key=('task.title.'||t.id::text)
WHERE (EXISTS(SELECT 1 FROM vr2_workflows v WHERE v.task_id=t.id)
    OR t.runtime_owner='rust')
"#;

#[derive(Debug, Deserialize)]
struct CreateTask {
    source_type: String,
    source_url: Option<String>,
    #[serde(default = "default_license")]
    source_license: String,
    source_proof_url: Option<String>,
    priority: Option<i32>,
    created_by: Option<String>,
}
fn default_license() -> String {
    "own".into()
}
#[derive(Debug, Deserialize)]
struct IngestYouTube {
    url: String,
    #[serde(default = "default_authorized")]
    license: String,
    proof_url: Option<String>,
    auto_publish: Option<bool>,
}
fn default_authorized() -> String {
    "authorized".into()
}

fn valid_license(value: &str) -> bool {
    matches!(value, "own" | "authorized" | "cc" | "unknown")
}
fn verify_proof(value: Option<&str>) -> AppResult<Option<String>> {
    let Some(proof) = value.filter(|v| !v.trim().is_empty()) else {
        return Ok(None);
    };
    if proof.len() > 2048 {
        return Err(AppError::Invalid("proof URL too long".into()));
    }
    let url =
        reqwest::Url::parse(proof).map_err(|_| AppError::Invalid("invalid proof URL".into()))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(AppError::Invalid(
            "proof URL must be HTTP(S) without credentials".into(),
        ));
    }
    Ok(Some(proof.into()))
}
async fn create_task(
    State(state): State<AppState>,
    Json(payload): Json<CreateTask>,
) -> AppResult<(StatusCode, Json<V1Task>)> {
    if !valid_license(&payload.source_license) {
        return Err(AppError::Invalid("invalid source license".into()));
    }
    let proof = verify_proof(payload.source_proof_url.as_deref())?;
    let priority = payload.priority.unwrap_or(0).clamp(-100, 100);
    if payload
        .created_by
        .as_deref()
        .is_some_and(|v| v.chars().count() > 128)
    {
        return Err(AppError::Invalid("created_by too long".into()));
    }
    match payload.source_type.as_str() {
        "youtube" => {
            let source = payload
                .source_url
                .as_deref()
                .ok_or_else(|| AppError::Invalid("YouTube source URL missing".into()))?;
            let (task, _workflow, deduped) = state
                .scheduler
                .create_youtube_source_task(
                    source,
                    &payload.source_license,
                    priority,
                    json!({}),
                    false,
                )
                .await?;
            if deduped {
                let owned: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1 AND runtime_owner='rust')",
                )
                .bind(task.id)
                .fetch_one(&state.db.pool)
                .await?;
                if !owned {
                    return Err(AppError::Conflict(
                        "source belongs to an existing V1 task".into(),
                    ));
                }
            } else if proof.is_some() {
                sqlx::query("UPDATE tasks SET source_proof_url=$1 WHERE id=$2")
                    .bind(proof)
                    .bind(task.id)
                    .execute(&state.db.pool)
                    .await?;
            }
            Ok((
                if deduped {
                    StatusCode::OK
                } else {
                    StatusCode::CREATED
                },
                Json(read_task(&state.db, task.id).await?),
            ))
        }
        "local" => {
            if payload
                .source_url
                .as_deref()
                .is_some_and(|v| !v.trim().is_empty())
            {
                return Err(AppError::Invalid(
                    "local upload source URL must be empty".into(),
                ));
            }
            let id = Uuid::new_v4();
            let mut tx = state.db.pool.begin().await?;
            sqlx::query(
                "INSERT INTO tasks(id,source_type,source_url,source_license,source_proof_url,status,priority,created_by,origin_kind)
                 VALUES($1,'local'::source_type,NULL,$2::source_license,$3,'created'::task_status,$4,$5,'v2_local_upload')"
            ).bind(id).bind(&payload.source_license).bind(proof).bind(priority)
                .bind(payload.created_by.as_deref().unwrap_or("web"))
                .execute(&mut *tx).await?;
            sqlx::query(
                "UPDATE tasks SET runtime_owner='rust',runtime_origin='local_upload' WHERE id=$1",
            )
            .bind(id)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
            Ok((StatusCode::CREATED, Json(read_task(&state.db, id).await?)))
        }
        _ => Err(AppError::Invalid(
            "native V1 TaskCreate supports only local and YouTube sources".into(),
        )),
    }
}
async fn youtube_ingest(
    State(state): State<AppState>,
    Json(req): Json<IngestYouTube>,
) -> AppResult<(StatusCode, Json<Value>)> {
    if !valid_license(&req.license) {
        return Err(AppError::Invalid("invalid YouTube license".into()));
    }
    let proof = verify_proof(req.proof_url.as_deref())?;
    let (task, _flow, deduped) = state
        .scheduler
        .create_youtube_source_task(&req.url, &req.license, 0, json!({}), false)
        .await?;
    if deduped {
        let _: V1Task = read_task(&state.db, task.id).await.map_err(|_| {
            AppError::Conflict("YouTube video was imported by the original V1 service".into())
        })?;
    } else if proof.is_some() {
        sqlx::query("UPDATE tasks SET source_proof_url=$1 WHERE id=$2")
            .bind(proof)
            .bind(task.id)
            .execute(&state.db.pool)
            .await?;
    }
    Ok((
        if deduped {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        },
        Json(json!({
            "task_id":task.id,"deduped":deduped,
            "source_id":crate::ingest::youtube_video_id(&req.url)?,
        })),
    ))
}
async fn auto_youtube(
    State(state): State<AppState>,
    Json(req): Json<IngestYouTube>,
) -> AppResult<(StatusCode, Json<Value>)> {
    if !valid_license(&req.license) {
        return Err(AppError::Invalid("invalid YouTube license".into()));
    }
    let proof = verify_proof(req.proof_url.as_deref())?;
    let config = json!({"auto_publish":req.auto_publish.unwrap_or(false),
        "auto_publish_platforms":if req.auto_publish==Some(true) {vec!["bilibili"]}else{vec![]}});
    let (task, flow, deduped) = state
        .scheduler
        .create_auto_youtube(&req.url, &req.license, 0, config)
        .await?;
    if deduped {
        read_task(&state.db, task.id).await.map_err(|_| {
            AppError::Conflict("YouTube video belongs to the original V1 workflow".into())
        })?;
    } else if proof.is_some() {
        sqlx::query("UPDATE tasks SET source_proof_url=$1 WHERE id=$2")
            .bind(proof)
            .bind(task.id)
            .execute(&state.db.pool)
            .await?;
    }
    Ok((
        if deduped {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        },
        Json(
            json!({"task_id":task.id,"pipeline_job_id":flow.map(|v|v.id.to_string()),
            "deduped":deduped,"source_id":crate::ingest::youtube_video_id(&req.url)?}),
        ),
    ))
}
async fn start_existing_youtube(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let task = read_task(&state.db, id).await?;
    if task.source_type != "youtube" {
        return Err(AppError::Conflict(
            "only owned YouTube tasks can be auto-started".into(),
        ));
    }
    let flow = state
        .scheduler
        .start(
            id,
            json!({"auto_publish":false,
        "auto_publish_platforms":[]}),
        )
        .await?;
    Ok(Json(
        json!({"task_id":id,"pipeline_job_id":flow.id.to_string()}),
    ))
}

fn valid_task_status(raw: &str) -> bool {
    matches!(
        raw,
        "CREATED"
            | "INGESTED"
            | "DOWNLOADED"
            | "AUDIO_EXTRACTED"
            | "ASR_DONE"
            | "TRANSLATED"
            | "SUBTITLE_READY"
            | "RENDERED"
            | "READY_FOR_REVIEW"
            | "APPROVED"
            | "PUBLISHING"
            | "PUBLISHED"
            | "FAILED"
            | "CANCELED"
    )
}

#[derive(Deserialize)]
pub struct ListQuery {
    pub status: Option<String>,
    pub limit: Option<i64>,
}
#[derive(Deserialize)]
struct LimitQuery {
    limit: Option<i64>,
}
pub async fn read_task(db: &crate::Database, id: Uuid) -> AppResult<V1Task> {
    let sql = format!("{TASK_VIEW} AND t.id=$1");
    sqlx::query_as::<_, V1Task>(&sql)
        .bind(id)
        .fetch_optional(&db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("V2 task not found".into()))
}
async fn stop_task(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<V1Task>> {
    state.scheduler.stop_task_compat(id).await?;
    Ok(Json(read_task(&state.db, id).await?))
}
async fn resume_task(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<V1Task>> {
    state.scheduler.resume_task_compat(id).await?;
    Ok(Json(read_task(&state.db, id).await?))
}
async fn bulk_change(state: AppState, stop: bool) -> AppResult<Json<Value>> {
    let where_expr = if stop {
        "upper(t.status::text) IN ('CREATED','INGESTED','DOWNLOADED','AUDIO_EXTRACTED',
        'ASR_DONE','TRANSLATED','SUBTITLE_READY','RENDERED','READY_FOR_REVIEW','APPROVED')"
    } else {
        "upper(t.status::text)='CANCELED' AND t.stopped_status IS NOT NULL"
    };
    let sql = format!(
        "SELECT t.id FROM tasks t JOIN tasks v ON v.id=t.id AND v.runtime_owner='rust'
         WHERE {where_expr} ORDER BY t.id LIMIT 1001"
    );
    let ids = sqlx::query_scalar::<_, Uuid>(&sql)
        .fetch_all(&state.db.pool)
        .await?;
    if ids.len() > 1000 {
        return Err(AppError::Conflict(
            "more than 1000 V2 tasks selected; split the operation".into(),
        ));
    }
    let mut changed = 0usize;
    for id in &ids {
        if stop {
            state.scheduler.stop_task_compat(*id).await?;
        } else {
            state.scheduler.resume_task_compat(*id).await?;
        }
        changed += 1;
    }
    Ok(Json(
        json!({"matched_count":ids.len(),"changed_count":changed}),
    ))
}
async fn stop_all(State(state): State<AppState>) -> AppResult<Json<Value>> {
    bulk_change(state, true).await
}
async fn resume_stopped(State(state): State<AppState>) -> AppResult<Json<Value>> {
    bulk_change(state, false).await
}
async fn list_tasks(
    State(state): State<AppState>,
    Query(q): Query<ListQuery>,
) -> AppResult<Json<Vec<V1Task>>> {
    if q.status.as_deref().is_some_and(|v| !valid_task_status(v)) {
        return Err(AppError::Invalid("invalid TaskStatus filter".into()));
    }
    let sql = format!(
        "{TASK_VIEW} AND ($1::text IS NULL OR upper(t.status::text)=$1) ORDER BY t.created_at DESC LIMIT $2"
    );
    let rows = sqlx::query_as::<_, V1Task>(&sql)
        .bind(q.status)
        .bind(q.limit.unwrap_or(50).clamp(1, 500))
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(rows))
}
async fn get_task(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<V1Task>> {
    Ok(Json(read_task(&state.db, id).await?))
}
async fn task_assets(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<AssetInfo>>> {
    read_task(&state.db, id).await?;
    let rows = sqlx::query_as::<_, AssetInfo>(
        "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at
         FROM assets WHERE task_id=$1 AND storage_key LIKE 'v2/%' ORDER BY created_at DESC",
    )
    .bind(id)
    .fetch_all(&state.db.pool)
    .await?;
    Ok(Json(rows))
}
#[derive(Serialize)]
struct ConvertedVideo {
    task: V1Task,
    final_asset: AssetInfo,
    cover_asset: Option<AssetInfo>,
    display_title: Option<String>,
}
async fn converted(
    State(state): State<AppState>,
    Query(q): Query<LimitQuery>,
) -> AppResult<Json<Vec<ConvertedVideo>>> {
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    // Match the original UI's most-recent-final-asset-per-task semantics.
    let candidates = sqlx::query_as::<_, AssetInfo>(
        "SELECT a.id,a.task_id,a.kind::text,a.storage_key,a.sha256,a.size_bytes,a.duration_ms,a.created_at
         FROM assets a WHERE a.kind::text='video_final'
         AND a.storage_key LIKE 'v2/%'
         AND EXISTS(SELECT 1 FROM vr2_workflows v WHERE v.task_id=a.task_id)
         ORDER BY a.created_at DESC,a.id DESC LIMIT $1",
    )
    .bind(limit.saturating_mul(8).min(1600))
    .fetch_all(&state.db.pool)
    .await?;
    let mut seen = HashSet::new();
    let mut results = Vec::new();
    for asset in candidates {
        if !seen.insert(asset.task_id) {
            continue;
        }
        let task = read_task(&state.db, asset.task_id).await?;
        let cover = sqlx::query_as::<_, AssetInfo>(
            "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at
             FROM assets WHERE task_id=$1 AND kind::text='cover_image'
             AND storage_key LIKE 'v2/%' ORDER BY created_at DESC,id DESC LIMIT 1",
        )
        .bind(asset.task_id)
        .fetch_optional(&state.db.pool)
        .await?;
        results.push(ConvertedVideo {
            display_title: task.display_title.clone(),
            task,
            final_asset: asset,
            cover_asset: cover,
        });
        if results.len() >= limit as usize {
            break;
        }
    }
    Ok(Json(results))
}
async fn subtitle_jobs(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Query(q): Query<LimitQuery>,
) -> AppResult<Json<Vec<Value>>> {
    read_task(&state.db, id).await?;
    let rows = sqlx::query_scalar::<_, Value>(
        r#"SELECT jsonb_build_object(
              'id',j.id,'task_id',j.task_id,
              'status', CASE WHEN j.state='pending' THEN 'queued'
                             WHEN j.state='awaiting' THEN 'processing'
                             ELSE j.state END,
              'progress', CASE WHEN j.state='succeeded' THEN 100 ELSE 0 END,
              'error_message',j.last_error,
              'created_at',j.created_at,'updated_at',j.updated_at)
           FROM vr2_jobs j WHERE j.task_id=$1 AND j.stage='subtitle'
           ORDER BY j.created_at DESC LIMIT $2"#,
    )
    .bind(id)
    .bind(q.limit.unwrap_or(50).clamp(1, 500))
    .fetch_all(&state.db.pool)
    .await?;
    Ok(Json(rows))
}
async fn dubbing_jobs(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Query(q): Query<LimitQuery>,
) -> AppResult<Json<Vec<Value>>> {
    read_task(&state.db, id).await?;
    let rows=sqlx::query(
        "SELECT j.id,j.task_id,j.workflow_id,j.state,j.payload,j.result,j.operation_key,j.last_error,
           j.created_at,j.updated_at,s.id AS subtitle_job_id,
           r.output_video_key,r.source_video_key,r.source_srt_key
         FROM vr2_jobs j LEFT JOIN vr2_jobs s
           ON s.workflow_id=j.workflow_id AND s.stage='subtitle'
         LEFT JOIN vr2_dubbing_runs r ON r.job_id=j.id
         WHERE j.task_id=$1 AND j.stage='dubbing'
         ORDER BY j.created_at DESC LIMIT $2"
    ).bind(id).bind(q.limit.unwrap_or(50).clamp(1,500)).fetch_all(&state.db.pool).await?;
    let mut output = Vec::with_capacity(rows.len());
    for row in rows {
        let status: String = row.try_get("state")?;
        let mapped = match status.as_str() {
            "pending" => "queued",
            "awaiting" => "preparing",
            "running" => "preparing",
            "succeeded" => "succeeded",
            "unknown" => "needs_review",
            value => value,
        };
        output.push(json!({
            "id":row.try_get::<Uuid,_>("id")?,
            "task_id":row.try_get::<Uuid,_>("task_id")?,
            "subtitle_job_id":row.try_get::<Option<Uuid>,_>("subtitle_job_id")?,
            "status":mapped,
            "progress": if status=="succeeded"{100}else{0},
            "operation_key":row.try_get::<String,_>("operation_key")?,
            "request":row.try_get::<Value,_>("payload")?,
            "output":{
                "video_key":row.try_get::<Option<String>,_>("output_video_key")?,
                "subtitle_video_key":row.try_get::<Option<String>,_>("source_video_key")?,
                "subtitle_srt_key":row.try_get::<Option<String>,_>("source_srt_key")?,
            },
            "error_message":row.try_get::<Option<String>,_>("last_error")?,
            "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
            "updated_at":row.try_get::<DateTime<Utc>,_>("updated_at")?,
        }));
    }
    Ok(Json(output))
}
async fn publish_jobs(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Query(q): Query<LimitQuery>,
) -> AppResult<Json<Vec<Value>>> {
    read_task(&state.db, id).await?;
    let rows = sqlx::query(
        "SELECT id,task_id,state,result,last_error,started_at,finished_at,created_at,updated_at
         FROM vr2_jobs WHERE task_id=$1 AND stage='publish'
         ORDER BY created_at DESC LIMIT $2",
    )
    .bind(id)
    .bind(q.limit.unwrap_or(50).clamp(1, 500))
    .fetch_all(&state.db.pool)
    .await?;
    let mut output = Vec::with_capacity(rows.len());
    for row in rows {
        let state: String = row.try_get("state")?;
        let result: Option<Value> = row.try_get("result")?;
        let result = result.unwrap_or(Value::Null);
        let confirmed = crate::publish::has_confirmed_result(&result);
        let platform = if result.get("native_bilibili").and_then(Value::as_bool) == Some(true) {
            "bilibili"
        } else {
            result
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
        };
        output.push(json!({
            "id":row.try_get::<Uuid,_>("id")?,
            "task_id":row.try_get::<Uuid,_>("task_id")?,
            "batch_id":null,
            "platform":platform,
            "state": if confirmed{"published"}else if state=="succeeded"{"failed"}else if state=="running"{"submitting"}else{state.as_str()},
            "aid":result.get("aid").cloned(),
            "bvid":result.get("bvid").cloned(),
            "external_id":result.get("external_id").cloned(),
            "external_url":result.get("external_url").cloned(),
            "account_id":null,
            "upload_progress":if confirmed{100}else{0},
            "upload_active":state=="running",
            "started_at":row.try_get::<Option<DateTime<Utc>>,_>("started_at")?,
            "finished_at":row.try_get::<Option<DateTime<Utc>>,_>("finished_at")?,
            "error_message":row.try_get::<Option<String>,_>("last_error")?,
            "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
            "updated_at":row.try_get::<DateTime<Utc>,_>("updated_at")?,
        }));
    }
    // SAU sidecar posts are independent durable V2 intents, not fake
    // vr2_jobs stages. Display them in the original task-history table.
    let social = sqlx::query(
        "SELECT id,platform,state,account_id,external_id,external_url,last_error,
         dispatched_at,finished_at,created_at,updated_at
         FROM vr2_social_publish_intents WHERE task_id=$1
         ORDER BY created_at DESC LIMIT $2",
    )
    .bind(id)
    .bind(q.limit.unwrap_or(50).clamp(1, 500))
    .fetch_all(&state.db.pool)
    .await?;
    for row in social {
        let status: String = row.try_get("state")?;
        let succeeded = matches!(status.as_str(), "submitted" | "published");
        let active = matches!(status.as_str(), "queued" | "dispatching" | "accepted");
        output.push(json!({
            "id":row.try_get::<Uuid,_>("id")?,"task_id":id,"batch_id":null,
            "platform":row.try_get::<String,_>("platform")?,
            "state":if active{"submitting"}else{status.as_str()},
            "aid":null,"bvid":null,
            "external_id":row.try_get::<Option<String>,_>("external_id")?,
            "external_url":row.try_get::<Option<String>,_>("external_url")?,
            "account_id":row.try_get::<Uuid,_>("account_id")?,
            "upload_progress":if succeeded{100}else{0},
            "upload_active":active,
            "started_at":row.try_get::<Option<DateTime<Utc>>,_>("dispatched_at")?,
            "finished_at":row.try_get::<Option<DateTime<Utc>>,_>("finished_at")?,
            "error_message":row.try_get::<Option<String>,_>("last_error")?,
            "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
            "updated_at":row.try_get::<DateTime<Utc>,_>("updated_at")?
        }));
    }
    Ok(Json(output))
}
async fn playout_assets(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<Value>>> {
    read_task(&state.db, id).await?;
    let rows = sqlx::query_scalar::<_, Value>(
        r#"SELECT jsonb_build_object(
          'id',p.id,'status',p.status,'task_id',p.task_id,'asset_id',p.asset_id,
          'ffplayout_channel_id',p.channel_id,
          'playout_path',p.playout_path,
          'relative_media_path',p.relative_media_path,
          'transfer_mode',p.transfer_mode,'source_checksum',p.source_checksum,
          'created_at',p.created_at,'updated_at',p.updated_at)
          FROM vr2_playout_links p WHERE p.task_id=$1
          AND p.status='ready' AND p.asset_id IS NOT NULL
          ORDER BY p.created_at DESC"#,
    )
    .bind(id)
    .fetch_all(&state.db.pool)
    .await?;
    Ok(Json(rows))
}
async fn asset_for_task(
    State(state): State<AppState>,
    Path((task_id, asset_id)): Path<(Uuid, Uuid)>,
    request: axum::extract::Request,
) -> AppResult<axum::response::Response> {
    read_task(&state.db, task_id).await?;
    let asset = state.db.asset(asset_id).await?;
    if asset.task_id != task_id || !asset.storage_key.starts_with("v2/") {
        return Err(AppError::NotFound("V2 task asset not found".into()));
    }
    crate::assets::download(State(state), Path(asset_id), request).await
}
pub(crate) async fn health() -> Json<Value> {
    Json(json!({"status":"ok"}))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .merge(crate::publish_meta::router())
        .merge(crate::legacy_reliability_v1::router())
        .merge(crate::youtube_meta::router())
        .merge(crate::youtube_sources_v1::router())
        .merge(crate::youtube_settings_v1::router())
        .merge(crate::publish_review::router())
        .merge(crate::task_manual::router())
        .merge(crate::publish_platforms::router())
        .merge(crate::social_accounts::router())
        .merge(crate::alerts::router())
        .merge(crate::bili_p2::router())
        .merge(crate::social_publish::router())
        .merge(crate::system_resources::router())
        .merge(crate::operations_v1::router())
        .merge(crate::management_v1::router())
        .merge(crate::remote_api_settings_v1::router())
        .merge(crate::bilibili_settings_v1::router())
        .merge(crate::bilibili_account_v1::router())
        .merge(crate::legacy_missing_actions_v1::router())
        .merge(crate::youdub_control_v1::router())
        .merge(crate::agent_skills_v1::router())
        .merge(crate::legacy_asset_delete_v1::router())
        .merge(crate::render_management_read_v1::router())
        .merge(crate::render_management_control_v1::router())
        .merge(crate::subtitle_management_v1::router())
        .merge(crate::subtitle_models_v1::router())
        .merge(crate::subtitle_model_download_v1::router())
        .merge(crate::subtitle_model_upload_v1::router())
        .merge(crate::subtitle_model_proxy_v1::router())
        .merge(crate::subtitle_asr_settings_v1::router())
        .merge(crate::subtitle_asr_test_v1::router())
        .merge(crate::subtitle_dictionary_v1::router())
        .merge(crate::subtitle_dictionary_import_v1::router())
        .merge(crate::subtitle_knowledge_read_v1::router())
        .merge(crate::subtitle_knowledge_import_v1::router())
        .merge(crate::subtitle_embedding_v1::router())
        .merge(crate::subtitle_translate_settings_v1::router())
        .merge(crate::subtitle_queue_v1::router())
        .merge(crate::subtitle_providers_v1::router())
        .merge(crate::subtitle_remaining_v1::router())
        .route("/tasks", get(list_tasks).post(create_task))
        .route("/youtube/ingest", post(youtube_ingest))
        .route("/auto/youtube", post(auto_youtube))
        .route(
            "/tasks/{id}/actions/auto_youtube_start",
            post(start_existing_youtube),
        )
        .route(
            "/tasks/{task_id}/upload/video",
            post(crate::assets::upload_video_form).layer(DefaultBodyLimit::disable()),
        )
        .route(
            "/tasks/{task_id}/upload/cover",
            post(crate::legacy_cover_v1::upload).layer(DefaultBodyLimit::disable()),
        )
        .route("/tasks/{id}", get(get_task))
        .route("/tasks/{id}/actions/stop", post(stop_task))
        .route("/tasks/{id}/actions/resume", post(resume_task))
        .route("/tasks/actions/stop_all", post(stop_all))
        .route("/tasks/actions/resume_stopped", post(resume_stopped))
        .route("/videos/converted", get(converted))
        .route("/tasks/{id}/assets", get(task_assets))
        .route(
            "/tasks/{id}/assets/{asset_id}/download",
            get(asset_for_task),
        )
        .route("/tasks/{id}/assets/{asset_id}/stream", get(asset_for_task))
        .route("/tasks/{id}/subtitle_jobs", get(subtitle_jobs))
        .route("/tasks/{id}/dubbing_jobs", get(dubbing_jobs))
        .route("/tasks/{id}/publish_jobs", get(publish_jobs))
        .route("/tasks/{id}/playout-assets", get(playout_assets))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_status_filter_is_explicit() {
        assert!(valid_task_status("PUBLISHED"));
        assert!(valid_task_status("CREATED"));
        assert!(!valid_task_status("PUBLISHED' OR true"));
        assert!(!valid_task_status("unknown"));
    }
}
