//! Extra original task actions. The native task operation is dispatched to the
//! durable Rust Stage ledger, NEVER directly to a Python Hatchet worker.
use crate::{AppError, AppResult, api::AppState, domain::Stage, legacy_contracts::read_task};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
};
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

async fn enqueue_youtube_download(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let task = read_task(&state.db, task_id).await?;
    if task.source_type != "youtube" {
        return Err(AppError::NotFound("YouTube task not found".into()));
    }
    if matches!(
        task.status.as_str(),
        "PUBLISHED" | "PUBLISHING" | "CANCELED"
    ) {
        return Err(AppError::Conflict(
            "terminal task cannot enqueue download".into(),
        ));
    }
    let latest = sqlx::query(
        "SELECT j.id,j.state FROM vr2_jobs j
         JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE j.task_id=$1 AND j.stage='download'
          ORDER BY w.generation DESC,j.created_at DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?;
    if let Some(row) = latest {
        let status: String = row.try_get("state")?;
        if matches!(
            status.as_str(),
            "pending" | "running" | "awaiting" | "succeeded"
        ) {
            return Ok(Json(json!({"task_id":task_id,
                "status":if status=="succeeded"{"completed"}else{"queued"}})));
        }
        if status == "unknown" {
            return Err(AppError::Conflict(
                "download state is uncertain; resolve before retry".into(),
            ));
        }
        if status == "failed" && task.status == "FAILED" {
            return Err(AppError::Conflict(
                "failed task must be explicitly resumed before downloading".into(),
            ));
        }
    }
    let workflow = state
        .scheduler
        .restart_at(
            task_id,
            Stage::Download,
            json!({"auto_publish":false,"auto_publish_platforms":[]}),
        )
        .await?;
    let job_exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE workflow_id=$1 AND stage='download')",
    )
    .bind(workflow.id)
    .fetch_one(&state.db.pool)
    .await?;
    if !job_exists {
        return Err(AppError::Unavailable(
            "download stage was not persisted".into(),
        ));
    }
    Ok(Json(json!({"task_id":task_id,"status":"queued"})))
}

/// Original Python download action returned its complete media/metadata DTO.
/// The Rust worker is durable and async: the legacy action returns that DTO
/// only for verified existing assets, otherwise queues the real download and
/// uses the original structured 409 in-progress error (never a fake success).
fn legacy_asset(asset: &crate::domain::AssetInfo) -> Value {
    json!({"id":asset.id,"kind":asset.kind,"storage_key":asset.storage_key,
        "sha256":asset.sha256,"size_bytes":asset.size_bytes,
        "duration_ms":asset.duration_ms,"created_at":asset.created_at})
}
fn download_conflict(code: &str, message: &str) -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({"detail":{"code":code,"message":message}})),
    )
        .into_response()
}
async fn valid_media(state: &AppState, asset: &crate::domain::AssetInfo) -> AppResult<bool> {
    if !asset.storage_key.starts_with("v2/") {
        return Ok(false);
    }
    let path = state.storage.resolve_existing(&asset.storage_key).await?;
    match tokio::fs::metadata(&path).await {
        Ok(meta) => Ok(meta.is_file() && meta.len() > 0),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}
async fn latest_asset(
    state: &AppState,
    task_id: Uuid,
    kind: &str,
) -> AppResult<Option<crate::domain::AssetInfo>> {
    sqlx::query_as::<_, crate::domain::AssetInfo>(
        "SELECT id,task_id,kind::text AS kind,storage_key,sha256,size_bytes,duration_ms,created_at
         FROM assets WHERE task_id=$1 AND kind::text=$2 AND storage_key LIKE 'v2/%'
         ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(task_id)
    .bind(kind)
    .fetch_optional(&state.db.pool)
    .await
    .map_err(Into::into)
}
async fn native_youtube_download(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Response> {
    let task = read_task(&state.db, task_id).await?;
    if task.source_type != "youtube" {
        return Err(AppError::NotFound("YouTube task not found".into()));
    }
    let url = task
        .source_url
        .as_deref()
        .ok_or_else(|| AppError::Invalid("missing YouTube source URL".into()))?;
    crate::ingest::youtube_video_id(url)?;
    // The original V1 operation key is also a cross-generation ownership
    // fence; never launch Rust while Python/Hatchet may still own the lease.
    let legacy = sqlx::query(
        "SELECT status,lease_until FROM operation_inbox
        WHERE operation_key=$1 LIMIT 1",
    )
    .bind(format!("youtube-download:{task_id}"))
    .fetch_optional(&state.db.pool)
    .await?;
    if let Some(row) = legacy {
        let status: String = row.try_get("status")?;
        if status == "unknown" || status == "pending" || status == "processing" {
            return Ok(download_conflict(
                "YOUTUBE_DOWNLOAD_IN_PROGRESS",
                "legacy Python YouTube download still owns or may own this operation; reconcile it before Rust execution",
            ));
        }
    }
    if let Some(raw) = latest_asset(&state, task_id, "video_raw").await?
        && valid_media(&state, &raw).await?
    {
        // Metadata extraction is real and can fail: do not invent DTO fields.
        let Json(fetched) =
            crate::youtube_meta::fetch_metadata(State(state.clone()), Path(task_id)).await?;
        let meta_asset: crate::domain::AssetInfo =
            serde_json::from_value(fetched["metadata_asset"].clone()).map_err(|_| {
                AppError::Unavailable("metadata asset missing from source-backed response".into())
            })?;
        let cover = latest_asset(&state, task_id, "cover_image").await?;
        let cover = match cover {
            Some(item) if valid_media(&state, &item).await? => Some(legacy_asset(&item)),
            _ => None,
        };
        return Ok(Json(
            json!({"metadata":fetched["metadata"],"metadata_asset":legacy_asset(&meta_asset),
            "video_asset":legacy_asset(&raw),"cover_asset":cover}),
        )
        .into_response());
    }
    if matches!(
        task.status.as_str(),
        "PUBLISHED" | "PUBLISHING" | "CANCELED"
    ) {
        return Err(AppError::Conflict(
            "terminal task cannot download media".into(),
        ));
    }
    // An existing successful receipt without a valid artifact cannot be
    // treated as an available video or silently replayed after a crash.
    let latest: Option<String> = sqlx::query_scalar(
        "SELECT state FROM vr2_jobs
        WHERE task_id=$1 AND stage='download' ORDER BY generation DESC,created_at DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?;
    if latest.as_deref() == Some("succeeded") {
        return Ok(download_conflict(
            "YOUTUBE_DOWNLOAD_RESULT_RECOVERING",
            "download succeeded but media object is unavailable; reconcile storage before retry",
        ));
    }
    if latest.as_deref() == Some("unknown") {
        return Ok(download_conflict(
            "YOUTUBE_DOWNLOAD_RESULT_RECOVERING",
            "download result uncertain; reconcile before retry",
        ));
    }
    let Json(state_info) = enqueue_youtube_download(State(state.clone()), Path(task_id)).await?;
    let state_label = state_info["status"].as_str().unwrap_or("queued");
    Ok(download_conflict(
        "YOUTUBE_DOWNLOAD_IN_PROGRESS",
        &format!(
            "download {state_label} in durable Rust worker; poll /tasks/{task_id}/youtube_download_progress and retry for the complete legacy asset response"
        ),
    ))
}

async fn auto_dubbing(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let task = read_task(&state.db, task_id).await?;
    let profile = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
    let enabled = profile.get("auto_publish").and_then(Value::as_bool) == Some(true)
        && profile.get("auto_dub_p2").and_then(Value::as_bool) == Some(true)
        && profile
            .get("auto_publish_platforms")
            .and_then(Value::as_array)
            .is_some_and(|v| v.iter().any(|p| p.as_str() == Some("bilibili")));
    if !enabled {
        return Ok(Json(
            json!({"status":"skipped","detail":"automatic P2 is disabled","dubbing_job_id":null}),
        ));
    }
    let origin: Option<String> =
        sqlx::query_scalar("SELECT runtime_origin FROM tasks WHERE id=$1 AND runtime_owner='rust'")
            .bind(task_id)
            .fetch_optional(&state.db.pool)
            .await?;
    if task.source_type != "youtube" || origin.as_deref() != Some("youtube_auto") {
        return Err(AppError::Conflict(
            "automatic P2 requires an automatic YouTube task".into(),
        ));
    }
    if matches!(
        task.status.as_str(),
        "CANCELED" | "PUBLISHED" | "PUBLISHING"
    ) {
        return Err(AppError::Conflict(
            "task state forbids a new dubbing stage".into(),
        ));
    }
    let subtitle: Option<(Uuid, Option<Value>)> = sqlx::query_as(
        "SELECT id,result FROM vr2_jobs WHERE task_id=$1 AND stage='subtitle'
         AND state='succeeded' ORDER BY generation DESC,created_at DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?;
    if subtitle
        .as_ref()
        .and_then(|(_, r)| r.as_ref())
        .and_then(|r| r.get("no_speech"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        return Ok(Json(
            json!({"status":"skipped","detail":"no speech: no P2 voiceover needed","dubbing_job_id":null}),
        ));
    }
    let ready:bool=sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND kind='video_raw'::asset_kind AND storage_key LIKE 'v2/%')
           AND EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND kind='subtitle_srt'::asset_kind AND storage_key LIKE 'v2/%')"
    ).bind(task_id).fetch_one(&state.db.pool).await?;
    if subtitle.is_none() || !ready {
        return Err(AppError::Conflict(
            "automatic P2 needs Chinese subtitles and source video".into(),
        ));
    }
    if state.config.youdub_bridge.is_none() || state.config.youdub_python.is_none() {
        return Err(AppError::Unavailable(
            "native YouDub runtime is not configured".into(),
        ));
    }
    let existing = sqlx::query(
        "SELECT id,state FROM vr2_jobs WHERE task_id=$1 AND stage='dubbing'
         ORDER BY generation DESC,created_at DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?;
    if let Some(row) = existing {
        let status: String = row.try_get("state")?;
        if matches!(
            status.as_str(),
            "pending" | "running" | "awaiting" | "succeeded"
        ) {
            return Ok(Json(
                json!({"status":if status=="succeeded"{"succeeded"}else{"queued"},
               "detail":if status=="succeeded"{"existing YouDub result ready"}else{"existing YouDub stage reused"},
               "dubbing_job_id":row.try_get::<Uuid,_>("id")?}),
            ));
        }
        if status == "unknown" {
            return Err(AppError::Conflict(
                "YouDub result uncertain: no automatic replay".into(),
            ));
        }
    }
    let wf=state.scheduler.restart_at(task_id,Stage::Dubbing,
        json!({"auto_dub_p2":true,"dubbing":{"enabled":true},"auto_publish":false,"auto_publish_platforms":[]})).await?;
    let id:Uuid=sqlx::query_scalar(
        "SELECT id FROM vr2_jobs WHERE workflow_id=$1 AND stage='dubbing' ORDER BY created_at DESC LIMIT 1"
    ).bind(wf.id).fetch_optional(&state.db.pool).await?
       .ok_or_else(||AppError::Unavailable("YouDub stage was not persisted".into()))?;
    Ok(Json(
        json!({"status":"queued","detail":"native YouDub stage scheduled","dubbing_job_id":id}),
    ))
}

/// Original task-detail `Add to ffplayout` entrypoint. The native importer
/// already uses the original playout_asset_links table; map its result to the
/// legacy DTO without exposing V2-only audit fields or absolute media paths.
async fn add_asset_to_playout(
    State(state): State<AppState>,
    Path((task_id, asset_id)): Path<(Uuid, Uuid)>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    let asset = state.db.asset(asset_id).await?;
    if asset.task_id != task_id
        || asset.kind != "video_final"
        || !asset.storage_key.starts_with("v2/")
    {
        return Err(AppError::NotFound("final task video not found".into()));
    }
    let link = crate::playout::import(
        &state,
        crate::playout::ImportRequest {
            task_id,
            channel_id: 1,
            part: Some(1),
            asset_id: Some(asset_id),
            title: None,
        },
    )
    .await?;
    if link.status != "ready" || link.asset_id != Some(asset_id) {
        return Err(AppError::Unavailable(
            "playout transfer did not complete".into(),
        ));
    }
    let transfer = link
        .transfer_mode
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("playout transfer mode missing".into()))?;
    if !matches!(transfer, "copy" | "hardlink") {
        return Err(AppError::Unavailable(
            "unrecognized playout transfer mode".into(),
        ));
    }
    Ok(Json(json!({
        "id":link.id,"task_id":link.task_id,"asset_id":asset_id,"status":"ready",
        "ffplayout_channel_id":link.channel_id,
        "playout_path":link.relative_media_path,
        "relative_media_path":link.relative_media_path,
        "transfer_mode":transfer,"source_checksum":link.source_checksum,
        "created_at":link.created_at,"updated_at":link.updated_at
    })))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/tasks/{task_id}/actions/youtube_download_enqueue",
            post(enqueue_youtube_download),
        )
        .route(
            "/tasks/{task_id}/actions/youtube_download",
            post(native_youtube_download),
        )
        .route("/tasks/{task_id}/actions/auto_dubbing", post(auto_dubbing))
        .route(
            "/tasks/{task_id}/assets/{asset_id}/playout",
            post(add_asset_to_playout),
        )
}
