//! Source-verified V1 response adapters backed by the V2 runtime.
//! These are native database queries, not Python or HTTP reverse proxies.
//! Original read APIs expose the shared historical tables. Mutations retain their Rust ownership guard.
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
  t.error_code,t.error_message,t.retry_count,upload.value AS bilibili_upload,
  t.created_at,t.updated_at
FROM tasks t
LEFT JOIN app_settings title ON title.key=('task.title.'||t.id::text)
LEFT JOIN LATERAL (
    SELECT jsonb_build_object('job_id',p.id,'progress',GREATEST(0,LEAST(100,COALESCE(p.upload_progress,0)))) AS value
    FROM publish_jobs p WHERE p.task_id=t.id AND p.platform::text='bilibili'
      AND p.state::text='submitting' AND p.upload_active
    ORDER BY p.updated_at DESC,p.id DESC LIMIT 1
) upload ON true
WHERE true
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
    // Retained for V1 wire compatibility; the saved profile is authoritative.
    #[serde(rename="auto_publish")]
    _auto_publish: Option<bool>,
}
fn default_authorized() -> String {
    "authorized".into()
}

pub(crate) fn valid_license(value: &str) -> bool {
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
                "INSERT INTO tasks(id,source_type,source_url,source_license,source_proof_url,status,priority,created_by,origin_kind,retry_count)
                 VALUES($1,'local'::source_type,NULL,$2::source_license,$3,'created'::task_status,$4,$5,'v2_local_upload',0)"
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
    let config = json!({"legacy_auto_runtime_profile":true});
    let (task, flow, deduped) = state
        .scheduler
        .create_auto_youtube_with_proof(&req.url, &req.license, 0, config, proof.as_deref())
        .await?;
    if deduped {
        read_task(&state.db, task.id).await.map_err(|_| {
            AppError::Conflict("YouTube video belongs to the original V1 workflow".into())
        })?;
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
            json!({"legacy_auto_runtime_profile":true}),
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
    let sql = format!("{TASK_VIEW} AND t.id=$1 AND (t.runtime_owner='rust' OR EXISTS(SELECT 1 FROM vr2_workflows v WHERE v.task_id=t.id))");
    sqlx::query_as::<_, V1Task>(&sql)
        .bind(id)
        .fetch_optional(&db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("V2 task not found".into()))
}
/// Read the original task row even when historical work is not adopted.
pub(crate) async fn read_visible_task(db:&crate::Database,id:Uuid)->AppResult<V1Task> {
    sqlx::query_as::<_,V1Task>(&format!("{TASK_VIEW} AND t.id=$1"))
        .bind(id).fetch_optional(&db.pool).await?
        .ok_or_else(||AppError::NotFound("task not found".into()))
}
async fn fill_original_titles(state:&AppState,tasks:&mut [V1Task])->AppResult<()> {
    let missing:Vec<_>=tasks.iter().filter(|t|t.display_title.is_none()).map(|t|t.id).collect();
    if missing.is_empty() { return Ok(()); }
    // Match the original batched fallback, selecting only the latest metadata.
    let keys:Vec<(Uuid,String)>=sqlx::query_as("SELECT DISTINCT ON(task_id) task_id,storage_key FROM assets
        WHERE task_id=ANY($1) AND kind::text='metadata_json' ORDER BY task_id,created_at DESC,id DESC")
        .bind(&missing).fetch_all(&state.db.pool).await?;
    let keys:std::collections::HashMap<_,_>=keys.into_iter().collect();
    for task in tasks {
        let Some(key)=keys.get(&task.id) else {continue};
        // The original tolerates missing/corrupt metadata without losing the task.
        let Ok(path)=state.storage.resolve_existing(key).await else {continue};
        let Ok(meta)=tokio::fs::metadata(&path).await else {continue};
        if !meta.is_file() || meta.len()>2_000_000 {continue}
        let Ok(raw)=tokio::fs::read(path).await else {continue};
        let Ok(info)=serde_json::from_slice::<Value>(&raw) else {continue};
        task.display_title=["title","fulltitle","alt_title"].iter()
            .find_map(|key|info.get(*key).and_then(Value::as_str).map(str::trim).filter(|s|!s.is_empty()).map(str::to_owned));
    }
    Ok(())
}
async fn stop_task(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<V1Task>> {
    state.scheduler.stop_task_compat(id).await?;
    Ok(Json(read_visible_task(&state.db, id).await?))
}
async fn resume_task(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<V1Task>> {
    state.scheduler.resume_task_compat(id).await?;
    Ok(Json(read_visible_task(&state.db, id).await?))
}
async fn bulk_change(state: AppState, stop: bool) -> AppResult<Json<Value>> {
    let (matched,changed)=state.scheduler.bulk_task_control(stop).await?;
    Ok(Json(json!({"matched_count":matched,"changed_count":changed})))
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
    let mut rows = sqlx::query_as::<_, V1Task>(&sql)
        .bind(q.status.as_deref())
        .bind(q.limit.unwrap_or(50).clamp(1, 500))
        .fetch_all(&state.db.pool)
        .await?;
    // Python filters and limits first, compensates that page, then removes
    // rows whose status changed out of the requested filter; no refill.
    let ids:Vec<_>=rows.iter().map(|row|row.id).collect();
    let repaired=crate::publish_legacy_reconcile::repair(&state.db,&ids).await?;
    if !repaired.is_empty() {
        for row in &mut rows {
            if repaired.contains(&row.id) {*row=read_visible_task(&state.db,row.id).await?;}
        }
        if let Some(status)=q.status.as_deref(){rows.retain(|row|row.status==status);}
    }
    fill_original_titles(&state,&mut rows).await?;
    Ok(Json(rows))
}
async fn get_task(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<V1Task>> {
    crate::publish_legacy_reconcile::repair(&state.db,&[id]).await?;
    let mut task=read_visible_task(&state.db,id).await?;
    fill_original_titles(&state,std::slice::from_mut(&mut task)).await?;
    Ok(Json(task))
}
async fn task_assets(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<AssetInfo>>> {
    read_visible_task(&state.db, id).await?;
    let rows = sqlx::query_as::<_, AssetInfo>(
        "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at
         FROM assets WHERE task_id=$1 ORDER BY created_at ASC,id ASC",
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
         ORDER BY a.created_at DESC,a.id DESC LIMIT $1",
    )
    .bind(limit.saturating_mul(5).min(1000))
    .fetch_all(&state.db.pool)
    .await?;
    let mut seen = HashSet::new();
    let mut results = Vec::new();
    for asset in candidates {
        if !seen.insert(asset.task_id) {
            continue;
        }
        let task = read_visible_task(&state.db, asset.task_id).await?;
        let cover = sqlx::query_as::<_, AssetInfo>(
            "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at
             FROM assets WHERE task_id=$1 AND kind::text='cover_image'
             ORDER BY created_at DESC,id DESC LIMIT 1",
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
    read_visible_task(&state.db, id).await?;
    let rows = sqlx::query_scalar::<_, Value>(
        r#"SELECT jsonb_build_object('id',r.id,'task_id',r.task_id,'status',r.status,
            'progress',r.progress,'error_message',r.error_message,'created_at',r.created_at,'updated_at',r.updated_at)
           FROM (
             SELECT s.id,s.task_id,s.status::text AS status,s.progress,s.error_message,s.created_at,s.updated_at
             FROM subtitle_jobs s WHERE s.task_id=$1
             UNION ALL
             SELECT j.id,j.task_id,CASE WHEN j.state='pending' THEN 'queued' WHEN j.state='awaiting' THEN 'processing' ELSE j.state END,
               CASE WHEN j.state='succeeded' THEN 100 ELSE 0 END,j.last_error,j.created_at,j.updated_at
             FROM vr2_jobs j WHERE j.task_id=$1 AND j.stage='subtitle'
             AND NOT EXISTS(SELECT 1 FROM subtitle_jobs s WHERE s.id=j.id)
           ) r ORDER BY r.created_at DESC,r.id DESC LIMIT $2"#,
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
    read_visible_task(&state.db, id).await?;
    let rows=sqlx::query(
        "SELECT j.id,j.task_id,j.workflow_id,j.state,j.payload,j.result,j.operation_key,j.last_error,
           j.created_at,j.updated_at,s.id AS subtitle_job_id,
           r.output_video_key,r.source_video_key,r.source_srt_key,e.render_spec AS p2_spec,e.progress AS p2_progress,e.id AS p2_execution_id
         FROM vr2_jobs j LEFT JOIN vr2_jobs s
           ON s.workflow_id=j.workflow_id AND s.stage='subtitle'
         LEFT JOIN vr2_dubbing_runs r ON r.job_id=j.id
         LEFT JOIN render_executions e ON e.job_id=j.id AND e.runtime_engine='rust-v2' AND e.mode='dub_burn_in'
         WHERE j.task_id=$1 AND j.stage='dubbing'
         ORDER BY j.created_at DESC LIMIT $2"
    ).bind(id).bind(q.limit.unwrap_or(50).clamp(1,500)).fetch_all(&state.db.pool).await?;
    let mut output = Vec::with_capacity(rows.len());
    for row in rows {
        let status: String = row.try_get("state")?;
        let result: Option<Value> = row.try_get("result")?;
        let progress = result
            .as_ref()
            .and_then(|v| v["dubbing_progress"]["progress"].as_u64())
            .unwrap_or(0)
            .min(96);
        let phase = result
            .as_ref()
            .and_then(|v| v["dubbing_progress"]["phase"].as_str())
            .filter(|v| matches!(*v, "preparing" | "synthesizing" | "aligning" | "mixing"))
            .unwrap_or("preparing");
        let p2: Option<Value> = row.try_get("p2_spec")?;
        let mapped = match status.as_str() {
            "pending" => "queued",
            "awaiting" if p2.is_some() => "rendering",
            "awaiting" => "preparing",
            "running" => phase,
            "succeeded" => "succeeded",
            "unknown" => "needs_review",
            value => value,
        };
        output.push(json!({
            "id":row.try_get::<Uuid,_>("id")?,
            "task_id":row.try_get::<Uuid,_>("task_id")?,
            "subtitle_job_id":row.try_get::<Option<Uuid>,_>("subtitle_job_id")?,
            "status":mapped,
            "progress": if status=="succeeded"{100}else if status=="awaiting" && p2.is_some(){96+(row.try_get::<Option<i32>,_>("p2_progress")?.unwrap_or(0).clamp(0,99) as u64)/25}else if status=="running"{progress}else{0},
            "operation_key":row.try_get::<String,_>("operation_key")?,
            "request":row.try_get::<Value,_>("payload")?,
            "output":{
                "video_key":row.try_get::<Option<String>,_>("output_video_key")?,
                "subtitle_video_key":row.try_get::<Option<String>,_>("source_video_key")?,
                "subtitle_srt_key":row.try_get::<Option<String>,_>("source_srt_key")?,
                "render_job_id":row.try_get::<Option<Uuid>,_>("p2_execution_id")?,
                "audio_key":p2.as_ref().and_then(|v|v.pointer("/request/audio_key")),
                "voice_key":p2.as_ref().and_then(|v|v.pointer("/request/voice_key")),
                "aligned_srt_key":p2.as_ref().and_then(|v|v.pointer("/request/srt_key")),
            },
            "error_message":row.try_get::<Option<String>,_>("last_error")?,
            "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
            "updated_at":row.try_get::<DateTime<Utc>,_>("updated_at")?,
        }));
    }
    // Keep original recovered dubbing IDs selectable for a historical P1.
    let historical = sqlx::query_scalar::<_,Value>(
        "SELECT jsonb_build_object('id',d.id,'task_id',d.task_id,'subtitle_job_id',d.subtitle_job_id,
          'status',d.status,'progress',d.progress,'operation_key',d.operation_key,
          'request','{}'::jsonb,'output',jsonb_build_object('video_key',d.output_json->>'video_key'),
          'error_message',d.error_message,'created_at',d.created_at,'updated_at',d.updated_at)
         FROM dubbing_jobs d JOIN operation_inbox i ON i.task_id=d.task_id
           AND i.runtime_engine='rust-historical-p2-render' AND i.status='completed'
           AND i.result_json->>'historical_dubbing_id'=d.id::text
           AND i.result_json->>'video_key'=d.output_json->>'video_key'
         WHERE d.task_id=$1 AND d.status='succeeded'
         ORDER BY d.created_at DESC LIMIT $2"
    ).bind(id).bind(q.limit.unwrap_or(50).clamp(1,500)).fetch_all(&state.db.pool).await?;
    output.extend(historical);
    Ok(Json(output))
}
async fn publish_jobs(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Query(q): Query<LimitQuery>,
) -> AppResult<Json<Vec<Value>>> {
    read_visible_task(&state.db, id).await?;
    // A mixed batch owns original publish_jobs, not the synthetic stage ID.
    // Return each original target once, with the persisted batch identity.
    let mixed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1 AND request_json->>'runtime'='rust-mixed-v1')")
        .bind(id).fetch_one(&state.db.pool).await?;
    if mixed {
        let rows = sqlx::query_scalar::<_, Value>("SELECT jsonb_build_object(
          'id',p.id,'task_id',p.task_id,'batch_id',p.batch_id,'platform',p.platform::text,
          'state',CASE WHEN p.state::text='submitted' AND p.response_json->>'receipt_verified'='false'
            AND NULLIF(p.external_id,'') IS NULL AND NULLIF(p.external_url,'') IS NULL THEN 'unknown' ELSE p.state::text END,
          'aid',p.aid::text,'bvid',p.bvid,'external_id',p.external_id,'external_url',p.external_url,
          'account_id',p.account_id,'upload_progress',p.upload_progress,'upload_active',p.upload_active,
          'started_at',p.started_at,'finished_at',p.finished_at,'created_at',p.created_at,'updated_at',p.updated_at)
          FROM publish_jobs p WHERE p.task_id=$1 ORDER BY p.created_at DESC,p.id DESC LIMIT $2")
          .bind(id).bind(q.limit.unwrap_or(50).clamp(1,500)).fetch_all(&state.db.pool).await?;
        return Ok(Json(rows));
    }

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
    let historical=sqlx::query_scalar::<_,Value>(
      "SELECT jsonb_build_object('id',p.id,'task_id',p.task_id,'batch_id',p.batch_id,
        'platform',p.platform::text,'state',p.state::text,'aid',p.aid::text,'bvid',p.bvid,
        'external_id',p.external_id,'external_url',p.external_url,'account_id',p.account_id,
        'upload_progress',p.upload_progress,'upload_active',p.upload_active,
        'started_at',p.started_at,'finished_at',p.finished_at,'created_at',p.created_at,'updated_at',p.updated_at,
        'auto_dub_p2',CASE WHEN i.id IS NOT NULL THEN jsonb_build_object('state',i.p2_state,'title',i.p2_title,'error',i.p2_last_error,'dub_job_id',i.request_json->>'historical_dubbing_id','cid',i.p2_uploaded_cid,'retry_supported',false)
          WHEN p.response_json->'auto_dub_p2' IS NOT NULL THEN jsonb_build_object('state',p.response_json->'auto_dub_p2'->>'state','title',p.response_json->'auto_dub_p2'->>'title') ELSE NULL END)
       FROM publish_jobs p LEFT JOIN operation_inbox i ON i.id=p.id AND i.task_id=p.task_id AND i.runtime_engine='rust-historical-p2-append'
       WHERE p.task_id=$1 AND p.platform::text='bilibili' AND p.state::text='published'
         AND NOT EXISTS(SELECT 1 FROM vr2_jobs j WHERE j.id=p.id)
         AND EXISTS(SELECT 1 FROM operation_inbox r WHERE r.task_id=p.task_id AND r.runtime_engine='rust-historical-p2-render' AND r.status='completed')
       ORDER BY p.created_at DESC LIMIT $2"
    ).bind(id).bind(q.limit.unwrap_or(50).clamp(1,500)).fetch_all(&state.db.pool).await?;
    output.extend(historical);
    Ok(Json(output))
}
async fn playout_assets(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<Value>>> {
    read_visible_task(&state.db, id).await?;
    let rows = sqlx::query_scalar::<_, Value>(
        r#"SELECT jsonb_build_object(
          'id',p.id,'status',p.status,'task_id',p.task_id,'asset_id',p.asset_id,
          'ffplayout_channel_id',p.ffplayout_channel_id,
          'playout_path',p.relative_media_path,
          'relative_media_path',p.relative_media_path,
          'transfer_mode',p.transfer_mode,'source_checksum',p.source_checksum,
          'created_at',p.created_at,'updated_at',p.updated_at)
          FROM playout_asset_links p WHERE p.task_id=$1
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
    let asset = state.db.asset(asset_id).await?;
    if asset.task_id != task_id {
        return Err(AppError::NotFound("asset not found".into()));
    }
    crate::assets::download(State(state), Path(asset_id), request).await
}
pub(crate) async fn health() -> Json<Value> {
    Json(json!({"status":"ok"}))
}

pub(crate) fn service_router() -> Router<AppState> {
    Router::new()
        .merge(crate::youtube_sources_v1::router())
        .merge(crate::bilibili_settings_v1::router())
        .merge(crate::bilibili_discovery_v1::router())
        .merge(crate::bilibili_account_v1::router())
        .merge(crate::bilibili_publish_v1::router())
        .merge(crate::agent_skills_v1::router())
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
        .merge(crate::subtitle_jobs_create_v1::router())
        .merge(crate::subtitle_embedding_v1::router())
        .merge(crate::subtitle_translate_settings_v1::router())
        .merge(crate::subtitle_translate_test_v1::router())
        .merge(crate::subtitle_queue_v1::router())
        .merge(crate::subtitle_providers_v1::router())
        .merge(crate::subtitle_remaining_v1::router())
        .route("/youtube/ingest", post(youtube_ingest))
}

pub fn router() -> Router<AppState> {
    core_router().merge(service_router())
}

pub(crate) fn core_router() -> Router<AppState> {
    Router::new()
        .merge(crate::publish_meta::router())
        .merge(crate::legacy_reliability_v1::router())
        .merge(crate::youtube_meta::router())
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
        .merge(crate::legacy_missing_actions_v1::router())
        .merge(crate::youdub_control_v1::router())
        .merge(crate::maintenance_workdir_v1::router())
        .merge(crate::maintenance_terminal_storage_v1::router())
        .merge(crate::legacy_asset_delete_v1::router())
        .merge(crate::render_management_read_v1::router())
        .merge(crate::render_management_control_v1::router())
        .merge(crate::subtitle_retranslate_v1::router())
        .route("/tasks", get(list_tasks).post(create_task))
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
