//! V2 ffplayout file importer with stable translated names and durable refs.
//! This does not mutate a running ffplayout playlist without an explicit
//! future channel API transaction; importing media is not the same as playing it.
use crate::{AppError, AppResult, Database, api::AppState, storage::AssetRegistry};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::get,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::{FromRow, Row};
use std::path::{Path as FilePath, PathBuf};
use tokio::{fs, io::AsyncWriteExt};
use uuid::Uuid;

#[derive(Debug, FromRow, Serialize, Clone)]
pub struct PlayoutLink {
    pub id: Uuid,
    pub task_id: Uuid,
    pub asset_id: Option<Uuid>,
    pub source_asset_id: Uuid,
    pub channel_id: i32,
    pub status: String,
    pub relative_media_path: String,
    pub playout_path: String,
    pub transfer_mode: Option<String>,
    pub source_checksum: String,
    pub reason: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}
#[derive(Debug, Deserialize)]
pub struct ImportRequest {
    pub task_id: Uuid,
    pub channel_id: i32,
    pub part: Option<u8>,
    pub asset_id: Option<Uuid>,
    pub title: Option<String>,
}
#[derive(Debug, Deserialize)]
pub struct LinkQuery {
    pub task_id: Option<Uuid>,
    pub channel_id: Option<i32>,
}
fn safe_filename(title: &str, task_id: Uuid, part: u8) -> String {
    let mut cleaned = String::new();
    for c in title.trim().chars().take(110) {
        if c.is_alphanumeric() || matches!(c, ' ' | '.' | '-' | '_') {
            cleaned.push(c);
        } else if c.is_whitespace()
            || matches!(c, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|')
        {
            cleaned.push('_');
        }
    }
    let cleaned = cleaned.trim_matches(|c| matches!(c, ' ' | '.' | '_'));
    let cleaned = if cleaned.is_empty() {
        "VideoRoll"
    } else {
        cleaned
    };
    format!(
        "{cleaned}-{}-P{part}.mp4",
        &task_id.simple().to_string()[..8]
    )
}
fn checked_channel(channel_id: i32) -> AppResult<()> {
    if !(1..=100_000).contains(&channel_id) {
        return Err(AppError::Invalid(
            "ffplayout channel ID out of range".into(),
        ));
    }
    Ok(())
}
async fn playout_path(state: &AppState, filename: &str) -> AppResult<PathBuf> {
    let root =
        state.config.playout_root.as_ref().ok_or_else(|| {
            AppError::Unavailable("V2 ffplayout media root is not configured".into())
        })?;
    let root_meta = fs::symlink_metadata(root).await?;
    if root_meta.file_type().is_symlink() {
        return Err(AppError::Invalid(
            "ffplayout root cannot be a symbolic link".into(),
        ));
    }
    let root = fs::canonicalize(root).await?;
    let directory = root.join("VideoRoll");
    match fs::symlink_metadata(&directory).await {
        Ok(meta) if meta.file_type().is_symlink() => {
            return Err(AppError::Invalid(
                "ffplayout VideoRoll directory cannot be a link".into(),
            ));
        }
        Ok(meta) if !meta.is_dir() => {
            return Err(AppError::Invalid(
                "ffplayout VideoRoll path is not a directory".into(),
            ));
        }
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => fs::create_dir(&directory).await?,
        Err(e) => return Err(e.into()),
    }
    let dest = directory.join(filename);
    if !dest.starts_with(&root) {
        return Err(AppError::Invalid(
            "media path escaped ffplayout root".into(),
        ));
    }
    Ok(dest)
}
async fn find(db: &Database, id: Uuid) -> AppResult<PlayoutLink> {
    sqlx::query_as::<_, PlayoutLink>("SELECT * FROM vr2_playout_links WHERE id=$1")
        .bind(id)
        .fetch_optional(&db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("ffplayout asset link not found".into()))
}
async fn candidate_asset(state: &AppState, request: &ImportRequest) -> AppResult<(Uuid, String)> {
    let part = request.part.unwrap_or(1);
    if !matches!(part, 1 | 2) {
        return Err(AppError::Invalid("only P1 and P2 can be imported".into()));
    }
    let asset = if let Some(id) = request.asset_id {
        let asset = state.db.asset(id).await?;
        if asset.task_id != request.task_id {
            return Err(AppError::Conflict(
                "asset does not belong to requested task".into(),
            ));
        }
        if !matches!(asset.kind.as_str(), "video_final" | "video_dubbed") {
            return Err(AppError::Invalid(
                "ffplayout requires a final video asset".into(),
            ));
        }
        asset
    } else {
        let kind = if part == 1 {
            "video_final"
        } else {
            "video_dubbed"
        };
        let id:Option<Uuid>=sqlx::query_scalar(
            "SELECT id FROM assets WHERE task_id=$1 AND kind=$2::asset_kind ORDER BY created_at DESC LIMIT 1"
        ).bind(request.task_id).bind(kind).fetch_optional(&state.db.pool).await?;
        state
            .db
            .asset(id.ok_or_else(|| {
                AppError::NotFound(format!("video P{part} has not been rendered"))
            })?)
            .await?
    };
    if part == 2 {
        let associated:Option<Uuid>=sqlx::query_scalar(
            "SELECT job_id FROM vr2_dubbing_runs WHERE task_id=$1 AND output_video_key=$2 AND model_status='succeeded' ORDER BY finished_at DESC LIMIT 1"
        ).bind(request.task_id).bind(&asset.storage_key)
            .fetch_optional(&state.db.pool).await?;
        if associated.is_none() {
            return Err(AppError::Conflict(
                "P2 cannot enter playout before its matching YouDub run succeeds".into(),
            ));
        }
    }
    Ok((asset.id, asset.storage_key))
}
async fn title(db: &Database, task_id: Uuid) -> String {
    let result: Result<Option<String>, sqlx::Error> =
        sqlx::query_scalar("SELECT NULLIF(title,'') FROM youtube_video_meta WHERE task_id=$1")
            .bind(task_id)
            .fetch_optional(&db.pool)
            .await;
    result
        .ok()
        .flatten()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| format!("VideoRoll-{}", task_id.simple()))
}
async fn secure_link(source: &FilePath, destination: &FilePath) -> AppResult<String> {
    let info = fs::symlink_metadata(source).await?;
    if !info.is_file() || info.len() == 0 {
        return Err(AppError::Invalid("source video missing or empty".into()));
    }
    if let Ok(dest) = fs::symlink_metadata(destination).await {
        if dest.file_type().is_symlink() || !dest.is_file() {
            return Err(AppError::Conflict(
                "existing playout destination is unsafe".into(),
            ));
        }
        // An existing media filename is only reusable after SHA verification,
        // performed by the caller. This is not a successful fresh transfer.
        return Ok("existing".into());
    }
    match fs::hard_link(source, destination).await {
        Ok(()) => Ok("hardlink".into()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok("existing".into()),
        Err(_) => {
            let parent = destination
                .parent()
                .ok_or_else(|| AppError::Invalid("invalid destination path".into()))?;
            let temporary = parent.join(format!(
                ".{}.incomplete-{}",
                destination
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy(),
                Uuid::new_v4().simple()
            ));
            let result = async {
                let mut input = fs::File::open(source).await?;
                let mut output = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&temporary)
                    .await?;
                tokio::io::copy(&mut input, &mut output).await?;
                output.flush().await?;
                output.sync_all().await?;
                drop(output);
                // hardlink(temp,dest) is an atomic create-if-absent; rename()
                // might overwrite media currently referenced by a playlist.
                match fs::hard_link(&temporary, destination).await {
                    Ok(()) => Ok("copy".to_string()),
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                        Ok("existing".to_string())
                    }
                    Err(e) => Err(AppError::Io(e)),
                }
            }
            .await;
            let _ = fs::remove_file(&temporary).await;
            result
        }
    }
}
pub async fn import(state: &AppState, body: ImportRequest) -> AppResult<PlayoutLink> {
    checked_channel(body.channel_id)?;
    state.db.task(body.task_id).await?;
    let (asset_id, key) = candidate_asset(state, &body).await?;
    AssetRegistry::checked_key(&key)?;
    let source = state.storage.resolve_existing(&key).await?;
    let (sha, size) = state.storage.sha256(&key).await?;
    if size == 0 || size > 80 * 1024 * 1024 * 1024 {
        return Err(AppError::Invalid(
            "ffplayout source video size invalid".into(),
        ));
    }
    let name = body
        .title
        .clone()
        .unwrap_or(title(&state.db, body.task_id).await);
    let part = body.part.unwrap_or(1);
    let relative = format!("VideoRoll/{}", safe_filename(&name, body.task_id, part));
    let filename = relative.rsplit('/').next().unwrap_or_default();
    let dest = playout_path(state, filename).await?;
    let actual_path = dest.to_string_lossy().to_string();
    let mut tx = state.db.pool.begin().await?;
    let row=sqlx::query(
        "INSERT INTO vr2_playout_links(id,task_id,asset_id,source_asset_id,channel_id,relative_media_path,playout_path,source_checksum) VALUES($1,$2,$3,$3,$4,$5,$6,$7) ON CONFLICT(source_asset_id,channel_id) DO UPDATE SET asset_id=EXCLUDED.asset_id,status='staging',updated_at=now() RETURNING id,status,source_checksum,playout_path"
    ).bind(Uuid::new_v4()).bind(body.task_id).bind(asset_id).bind(body.channel_id)
        .bind(&relative).bind(&actual_path).bind(&sha)
        .fetch_one(&mut *tx).await?;
    let id: Uuid = row.try_get("id")?;
    let previous: String = row.try_get("source_checksum")?;
    let path: String = row.try_get("playout_path")?;
    if previous != sha || path != actual_path {
        return Err(AppError::Conflict(
            "ffplayout file already registered with different identity".into(),
        ));
    }
    tx.commit().await?;
    let transfer = secure_link(&source, &dest).await?;
    // The checksum of destination is verified even when the filename existed.
    let destination_sha = {
        let mut file = fs::File::open(&dest).await?;
        let mut hasher = sha2::Sha256::new();
        let mut buffer = vec![0u8; 64 * 1024];
        loop {
            use tokio::io::AsyncReadExt;
            let n = file.read(&mut buffer).await?;
            if n == 0 {
                break;
            }
            use sha2::Digest;
            hasher.update(&buffer[..n]);
        }
        use sha2::Digest;
        hex::encode(hasher.finalize())
    };
    if destination_sha != sha {
        return Err(AppError::Conflict(
            "ffplayout media checksum mismatch".into(),
        ));
    }
    state
        .storage
        .register(
            &key,
            "playout",
            id,
            "source_video",
            Utc::now() + chrono::Duration::days(365),
        )
        .await?;
    sqlx::query(
        "UPDATE vr2_playout_links SET status='ready',transfer_mode=$1,updated_at=now() WHERE id=$2",
    )
    .bind(if transfer == "existing" {
        "copy"
    } else {
        transfer.as_str()
    })
    .bind(id)
    .execute(&state.db.pool)
    .await?;
    find(&state.db, id).await
}
async fn create(
    State(state): State<AppState>,
    Json(body): Json<ImportRequest>,
) -> AppResult<(StatusCode, Json<PlayoutLink>)> {
    Ok((StatusCode::CREATED, Json(import(&state, body).await?)))
}
async fn get_link(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<PlayoutLink>> {
    Ok(Json(find(&state.db, id).await?))
}
async fn list(
    State(state): State<AppState>,
    Query(q): Query<LinkQuery>,
) -> AppResult<Json<Vec<PlayoutLink>>> {
    Ok(Json(sqlx::query_as::<_,PlayoutLink>(
        "SELECT * FROM vr2_playout_links WHERE ($1::uuid IS NULL OR task_id=$1) AND ($2::integer IS NULL OR channel_id=$2) ORDER BY updated_at DESC LIMIT 200"
    ).bind(q.task_id).bind(q.channel_id).fetch_all(&state.db.pool).await?))
}
async fn detach(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<PlayoutLink>> {
    let mut tx = state.db.pool.begin().await?;
    let link =
        sqlx::query_as::<_, PlayoutLink>("SELECT * FROM vr2_playout_links WHERE id=$1 FOR UPDATE")
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::NotFound("ffplayout asset link missing".into()))?;
    if link.status == "detached" {
        return Ok(Json(link));
    }
    let live_asset = link
        .asset_id
        .ok_or_else(|| AppError::Conflict("playout link is missing a live source asset".into()))?;
    let asset = state.db.asset(live_asset).await?;
    sqlx::query("UPDATE vr2_playout_links SET status='detached',updated_at=now() WHERE id=$1")
        .bind(id)
        .execute(&mut *tx)
        .await?;
    // Do NOT physically delete the destination: it may currently be playing.
    // The external playlist must separately acknowledge removal first.
    sqlx::query(
        "DELETE FROM vr2_object_refs WHERE storage_key=$1 AND owner_type='playout' AND owner_id=$2",
    )
    .bind(asset.storage_key)
    .bind(id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(find(&state.db, id).await?))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/playout/import", axum::routing::post(create))
        .route("/playout/links", get(list))
        .route("/playout/links/{id}", get(get_link).delete(detach))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sanitized_filename_stable_and_safe() {
        let task = Uuid::new_v4();
        let name = safe_filename("../测试/Footage:*?|<>", task, 1);
        assert!(!name.contains('/'));
        assert!(!name.contains('\\'));
        assert!(name.ends_with("-P1.mp4"));
    }
    #[tokio::test]
    async fn destination_is_not_overwritten_by_link() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("raw");
        let dest = dir.path().join("finished.mp4");
        fs::write(&source, b"one").await.unwrap();
        fs::write(&dest, b"two").await.unwrap();
        assert_eq!(secure_link(&source, &dest).await.unwrap(), "existing");
        assert_eq!(fs::read(&dest).await.unwrap(), b"two");
    }
}
