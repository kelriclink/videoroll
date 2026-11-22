//! Streaming asset IO for standalone GPU/AI workers. No full-video buffering in RAM.
use crate::{AppError, AppResult, api::AppState, domain::AssetInfo, storage::AssetRegistry};
use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, Path, Query, Request, State},
    http::{StatusCode, header},
    response::Response,
    routing::{get, post},
};
use futures_util::StreamExt;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use sqlx::Row;
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
};
use tokio_util::io::ReaderStream;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
struct UploadParams {
    kind: String,
    extension: String,
}
const VIDEO_MAX: u64 = 80 * 1024 * 1024 * 1024;
const AUDIO_MAX: u64 = 10 * 1024 * 1024 * 1024;
const TEXT_MAX: u64 = 64 * 1024 * 1024;
fn allowed_asset(kind: &str, ext: &str) -> Option<u64> {
    match (kind, ext) {
        ("video_raw" | "video_final" | "video_dubbed", "mp4" | "webm" | "mkv" | "mov") => {
            Some(VIDEO_MAX)
        }
        ("audio_wav" | "dubbing_audio", "wav" | "m4a" | "flac") => Some(AUDIO_MAX),
        ("subtitle_srt", "srt")
        | ("subtitle_ass", "ass")
        | ("segments_json" | "metadata_json" | "publish_result", "json")
        | ("log", "log") => Some(TEXT_MAX),
        ("cover_image", "jpg" | "jpeg" | "png") => Some(30 * 1024 * 1024),
        _ => None,
    }
}
async fn list_assets(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Json<Vec<AssetInfo>>> {
    state.db.task(task_id).await?;
    Ok(Json(state.db.list_assets(task_id).await?))
}
/// Streaming upload, atomically renamed after the final byte and committed digest.
/// The caller must authenticate as a trusted internal worker.
async fn upload(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Query(p): Query<UploadParams>,
    request: Request,
) -> AppResult<(StatusCode, Json<AssetInfo>)> {
    let max_size = allowed_asset(&p.kind, &p.extension)
        .ok_or_else(|| AppError::Invalid("unsupported output kind/extension".into()))?;
    state.db.task(task_id).await?;
    let file_id = Uuid::new_v4();
    let directory = format!("v2/artifacts/{task_id}");
    let dir = state.storage.resolve_existing(&directory).await?;
    fs::create_dir_all(&dir).await?;
    let key = format!("{directory}/{file_id}.{}", p.extension);
    AssetRegistry::checked_key(&key)?;
    let completed = state.storage.resolve_existing(&key).await?;
    let temporary = dir.join(format!("{file_id}.incomplete"));
    let mut committed = false;
    let result=async {
        let mut file=fs::OpenOptions::new().write(true).create_new(true).open(&temporary).await?;
        let mut sha=Sha256::new();
        let mut bytes=0u64;
        let mut stream=request.into_body().into_data_stream();
        while let Some(part)=stream.next().await {
            let chunk=part.map_err(|e|AppError::Io(std::io::Error::other(e)))?;
            bytes=bytes.checked_add(chunk.len() as u64)
                .ok_or_else(||AppError::Invalid("upload byte count overflow".into()))?;
            if bytes>max_size {return Err(AppError::Invalid("upload exceeds media kind byte limit".into()));}
            sha.update(&chunk);
            file.write_all(&chunk).await?;
        }
        if bytes==0 {return Err(AppError::Invalid("zero-byte media is not a valid asset".into()));}
        file.flush().await?;
        file.sync_all().await?;
        drop(file);
        fs::rename(&temporary,&completed).await?;
        let digest=hex::encode(sha.finalize());
        let mut tx=state.db.pool.begin().await?;
        let status:Option<String>=sqlx::query_scalar(
            "SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE"
        ).bind(task_id).fetch_optional(&mut *tx).await?;
        if status.as_deref()==Some("CANCELED") {
            return Err(AppError::Conflict("task was canceled while uploading an artifact".into()));
        }
        if status.is_none(){return Err(AppError::NotFound("task not found".into()));}
        sqlx::query(
            "INSERT INTO vr2_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days')"
        ).bind(&key).bind(&digest).bind(bytes as i64).execute(&mut *tx).await?;
        sqlx::query(
            "INSERT INTO vr2_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'task',$2,$3)"
        ).bind(&key).bind(task_id).bind(&p.kind).execute(&mut *tx).await?;
        sqlx::query(
            "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES($1,$2,$3::asset_kind,$4,$5,$6)"
        ).bind(file_id).bind(task_id).bind(&p.kind).bind(&key).bind(&digest).bind(bytes as i64)
            .execute(&mut *tx).await?;
        tx.commit().await?;
        committed=true;
        state.db.asset(file_id).await
    }.await;
    match result {
        Ok(asset) => Ok((StatusCode::CREATED, Json(asset))),
        Err(error) => {
            let _ = fs::remove_file(&temporary).await;
            if !committed {
                let _ = fs::remove_file(&completed).await;
            }
            Err(error)
        }
    }
}
/// Parse one HTTP range, including suffix ranges; multi-range requests are rejected.
/// Original React local file-upload contract, handled by Rust directly.
/// Strictly V2-owned tasks; streaming multipart never buffers the whole video.
pub(crate) async fn upload_video_form(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    mut multipart: axum::extract::Multipart,
) -> AppResult<(StatusCode, Json<AssetInfo>)> {
    let ownership: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_managed_tasks WHERE task_id=$1)")
            .bind(task_id)
            .fetch_one(&state.db.pool)
            .await?;
    if !ownership {
        return Err(AppError::NotFound("V2 manual-upload task not found".into()));
    }
    let source: Option<String> =
        sqlx::query_scalar("SELECT source_type::text FROM tasks WHERE id=$1")
            .bind(task_id)
            .fetch_optional(&state.db.pool)
            .await?;
    if source.as_deref() != Some("local") {
        return Err(AppError::Invalid(
            "manual video upload requires a local V2 task".into(),
        ));
    }
    let mut field = multipart
        .next_field()
        .await
        .map_err(|_| AppError::Invalid("malformed multipart video upload".into()))?
        .ok_or_else(|| AppError::Invalid("upload requires a file field".into()))?;
    if field.name() != Some("file") {
        return Err(AppError::Invalid(
            "expected multipart field named file".into(),
        ));
    }
    let filename = field.file_name().unwrap_or_default();
    let ext = filename
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or_default()
        .rsplit('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(ext.as_str(), "mp4" | "webm" | "mkv" | "mov") {
        return Err(AppError::Invalid(
            "unsupported local video extension".into(),
        ));
    }
    let directory = format!("v2/artifacts/{task_id}");
    let dir = state.storage.resolve_existing(&directory).await?;
    fs::create_dir_all(&dir).await?;
    let file_id = Uuid::new_v4();
    let key = format!("{directory}/{file_id}.{ext}");
    let dest = state.storage.resolve_existing(&key).await?;
    let temporary = dir.join(format!("{file_id}.uploading"));
    let mut committed = false;
    let result = async {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .await?;
        let mut sha = Sha256::new();
        let mut bytes = 0u64;
        while let Some(chunk) = field
            .chunk()
            .await
            .map_err(|_| AppError::Invalid("malformed multipart video chunk".into()))?
        {
            bytes = bytes
                .checked_add(chunk.len() as u64)
                .ok_or_else(|| AppError::Invalid("video byte count overflow".into()))?;
            if bytes > VIDEO_MAX {
                return Err(AppError::Invalid(
                    "local video exceeds maximum supported size".into(),
                ));
            }
            sha.update(&chunk);
            file.write_all(&chunk).await?;
        }
        if bytes == 0 {
            return Err(AppError::Invalid("empty local video".into()));
        }
        file.flush().await?;
        file.sync_all().await?;
        drop(file);
        // Verify a valid video exists before committing any asset record.
        crate::media::ffmpeg::probe_video(&state.config.ffprobe_binary, &temporary).await?;
        fs::rename(&temporary, &dest).await?;
        let sha = hex::encode(sha.finalize());
        let mut tx = state.db.pool.begin().await?;
        let task = sqlx::query(
            "SELECT source_type::text AS source_type,status::text AS status
             FROM tasks WHERE id=$1 FOR UPDATE",
        )
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;
        let task_status: String = task.try_get("status")?;
        if task.try_get::<String, _>("source_type")? != "local"
            || !matches!(task_status.as_str(), "CREATED" | "INGESTED")
        {
            return Err(AppError::Conflict(
                "task is no longer accepting a local source".into(),
            ));
        }
        sqlx::query(
            "INSERT INTO vr2_objects(storage_key,checksum_sha256,size_bytes,state,retain_until)
             VALUES($1,$2,$3,'live',now()+interval '2 days')",
        )
        .bind(&key)
        .bind(&sha)
        .bind(bytes as i64)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "INSERT INTO vr2_object_refs(storage_key,owner_type,owner_id,role)
             VALUES($1,'task',$2,'video_raw')",
        )
        .bind(&key)
        .bind(task_id)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes)
             VALUES($1,$2,'video_raw'::asset_kind,$3,$4,$5)",
        )
        .bind(file_id)
        .bind(task_id)
        .bind(&key)
        .bind(&sha)
        .bind(bytes as i64)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "UPDATE tasks SET status='INGESTED'::task_status,updated_at=now()
             WHERE id=$1 AND status='CREATED'::task_status",
        )
        .bind(task_id)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        committed = true;
        state.db.asset(file_id).await
    }
    .await;
    match result {
        Ok(asset) => Ok((StatusCode::CREATED, Json(asset))),
        Err(error) => {
            let _ = fs::remove_file(temporary).await;
            if !committed {
                let _ = fs::remove_file(dest).await;
            }
            Err(error)
        }
    }
}

pub fn parse_range(value: &str, size: u64) -> Option<(u64, u64)> {
    if size == 0 {
        return None;
    }
    let raw = value.trim().strip_prefix("bytes=")?;
    if raw.contains(',') {
        return None;
    }
    let (left, right) = raw.split_once('-')?;
    if left.is_empty() {
        let suffix = right.parse::<u64>().ok()?;
        if suffix == 0 {
            return None;
        }
        let start = size.saturating_sub(suffix);
        return Some((start, size - 1));
    }
    let start = left.parse::<u64>().ok()?;
    if start >= size {
        return None;
    }
    let end = if right.is_empty() {
        size - 1
    } else {
        right.parse::<u64>().ok()?.min(size - 1)
    };
    if end < start {
        return None;
    }
    Some((start, end))
}
pub(crate) async fn download(
    State(state): State<AppState>,
    Path(asset_id): Path<Uuid>,
    request: Request,
) -> AppResult<Response> {
    let asset = state.db.asset(asset_id).await?;
    let path = state.storage.resolve_existing(&asset.storage_key).await?;
    let mut file = fs::File::open(path).await?;
    let size = file.metadata().await?.len();
    if size == 0 {
        return Err(AppError::NotFound("asset file is empty".into()));
    }
    let requested = request
        .headers()
        .get(header::RANGE)
        .and_then(|v| v.to_str().ok());
    let (start, end, code) = match requested {
        Some(raw) => {
            let (start, end) = parse_range(raw, size)
                .ok_or_else(|| AppError::Invalid("invalid or unsupported Range header".into()))?;
            (start, end, StatusCode::PARTIAL_CONTENT)
        }
        None => (0, size - 1, StatusCode::OK),
    };
    file.seek(std::io::SeekFrom::Start(start)).await?;
    let length = end - start + 1;
    let stream = ReaderStream::new(file.take(length));
    let mut response = Response::new(Body::from_stream(stream));
    *response.status_mut() = code;
    let headers = response.headers_mut();
    // Browser <video> playback requires the correct MIME type with nosniff.
    let ext = asset
        .storage_key
        .rsplit('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    let mime = match ext.as_str() {
        "mp4" | "m4v" => "video/mp4",
        "webm" => "video/webm",
        "mkv" => "video/x-matroska",
        "mov" => "video/quicktime",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "m4a" => "audio/mp4",
        "flac" => "audio/flac",
        "srt" => "application/x-subrip",
        "ass" => "text/plain; charset=utf-8",
        "json" => "application/json",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        _ => "application/octet-stream",
    };
    headers.insert(header::CONTENT_TYPE, mime.parse().unwrap());
    headers.insert(header::ACCEPT_RANGES, "bytes".parse().unwrap());
    headers.insert(header::CONTENT_LENGTH, length.to_string().parse().unwrap());
    headers.insert(header::CACHE_CONTROL, "private, no-store".parse().unwrap());
    headers.insert("x-content-type-options", "nosniff".parse().unwrap());
    if code == StatusCode::PARTIAL_CONTENT {
        headers.insert(
            header::CONTENT_RANGE,
            format!("bytes {start}-{end}/{size}").parse().unwrap(),
        );
    }
    Ok(response)
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{task_id}/assets", get(list_assets))
        .route(
            "/tasks/{task_id}/assets/upload",
            post(upload).layer(DefaultBodyLimit::disable()),
        )
        .route("/assets/{asset_id}/download", get(download))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn upload_kind_contract_is_explicit() {
        assert!(allowed_asset("video_final", "mp4").is_some());
        assert_eq!(allowed_asset("subtitle_ass", "exe"), None);
        assert_eq!(allowed_asset("video_final", "html"), None);
    }
    #[test]
    fn range_parser_guards_bounds() {
        assert_eq!(parse_range("bytes=0-9", 100), Some((0, 9)));
        assert_eq!(parse_range("bytes=-15", 100), Some((85, 99)));
        assert_eq!(parse_range("bytes=50-", 100), Some((50, 99)));
        assert_eq!(parse_range("bytes=50-900", 100), Some((50, 99)));
        assert_eq!(parse_range("bytes=100-", 100), None);
        assert_eq!(parse_range("bytes=0-9,20-39", 100), None);
    }
}
