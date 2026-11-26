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
fn truncate_bytes(value: &str, limit: usize) -> String {
    let mut end = value.len().min(limit);
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].trim_matches([' ', '.']).to_owned()
}
fn safe_filename(title: &str, key: &str, asset: Uuid) -> String {
    let path = FilePath::new(key);
    let extension = path
        .extension()
        .and_then(|v| v.to_str())
        .unwrap_or("mp4")
        .to_ascii_lowercase();
    let extension = if ["mp4", "mkv", "webm", "mov", "m4v"].contains(&extension.as_str()) {
        extension.as_str()
    } else {
        "mp4"
    };
    let title = if title.trim().is_empty() {
        path.file_stem().and_then(|v| v.to_str()).unwrap_or("")
    } else {
        title
    };
    let sanitized = title
        .chars()
        .map(|c| {
            if c.is_control() || matches!(c, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|') {
                '_'
            } else {
                c
            }
        })
        .collect::<String>();
    let name = sanitized.split_whitespace().collect::<Vec<_>>().join(" ");
    let name = name.trim_matches([' ', '.']);
    let reserved = name.split('.').next().unwrap_or("").to_ascii_uppercase();
    let reserved = matches!(reserved.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (reserved.len() == 4
            && (reserved.starts_with("COM") || reserved.starts_with("LPT"))
            && matches!(reserved.as_bytes()[3], b'1'..=b'9'));
    let fallback = format!("视频-{}", &asset.simple().to_string()[..8]);
    let name = if name.is_empty() || reserved {
        &fallback
    } else {
        name
    };
    format!(
        "{}.{}",
        truncate_bytes(name, 180 - extension.len() - 1),
        extension
    )
}
fn collision_names(filename: &str, asset: Uuid) -> Vec<String> {
    let path = FilePath::new(filename);
    let stem = path.file_stem().and_then(|v| v.to_str()).unwrap_or("视频");
    let ext = path.extension().and_then(|v| v.to_str()).unwrap_or("mp4");
    let suffix = &asset.simple().to_string()[..8];
    vec![
        filename.to_owned(),
        format!(
            "{}-{suffix}.{ext}",
            truncate_bytes(stem, 180 - 1 - ext.len() - 9)
        ),
        format!("{asset}.{ext}"),
    ]
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
    if filename.is_empty()
        || filename == "."
        || filename == ".."
        || filename.contains(['/', '\\'])
        || filename.chars().any(char::is_control)
    {
        return Err(AppError::Invalid("invalid playout basename".into()));
    }
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
    if (part == 1 && asset.kind != "video_final") || (part == 2 && asset.kind != "video_dubbed") {
        return Err(AppError::Invalid(
            "playout part does not match asset kind".into(),
        ));
    }
    if part == 2 {
        let associated:Option<Uuid>=sqlx::query_scalar(
            "SELECT job_id FROM vr2_dubbing_runs WHERE task_id=$1 AND output_video_key=$2 AND model_status='succeeded' ORDER BY created_at DESC LIMIT 1"
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
async fn title(db: &Database, task_id: Uuid) -> AppResult<String> {
    let result:Option<String>=sqlx::query_scalar("SELECT COALESCE(NULLIF(trim(value_json->>'translated_title'),''),NULLIF(trim(value_json->>'source_title'),'')) FROM app_settings WHERE key=$1")
        .bind(format!("task.title.{task_id}")).fetch_optional(&db.pool).await?.flatten();
    Ok(result.unwrap_or_default())
}
async fn file_sha(path: &FilePath) -> AppResult<String> {
    use sha2::Digest;
    use tokio::io::AsyncReadExt;
    let mut file = fs::File::open(path).await?;
    let mut sha = sha2::Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).await?;
        if n == 0 {
            break;
        }
        sha.update(&buf[..n]);
    }
    Ok(hex::encode(sha.finalize()))
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
    let name = match body.title.as_deref().filter(|v| !v.trim().is_empty()) {
        Some(v) => v.to_owned(),
        None => title(&state.db, body.task_id).await?,
    };
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('videoroll:playout:filename',0))")
        .execute(&mut *tx)
        .await?;
    let previous:Option<(String,String,String)>=sqlx::query_as("SELECT relative_media_path,source_checksum,runtime_engine FROM playout_asset_links WHERE asset_id=$1 AND ffplayout_channel_id=$2")
        .bind(asset_id).bind(body.channel_id).fetch_optional(&mut *tx).await?;
    let filename = if let Some((relative, checksum, engine)) = previous {
        if engine != "rust-v2" || checksum != sha {
            return Err(AppError::Conflict(
                "historical or changed playout link requires reconciliation".into(),
            ));
        }
        let file = relative
            .strip_prefix("VideoRoll/")
            .filter(|v| !v.is_empty() && !v.contains(['/', '\\']) && *v != "." && *v != "..")
            .ok_or_else(|| {
                AppError::Conflict("historical nested path requires reconciliation".into())
            })?;
        file.to_owned()
    } else {
        let mut chosen = None;
        for candidate in collision_names(&safe_filename(&name, &key, asset_id), asset_id) {
            let relative = format!("VideoRoll/{candidate}");
            let occupied: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM playout_asset_links WHERE relative_media_path=$1)",
            )
            .bind(&relative)
            .fetch_one(&mut *tx)
            .await?;
            if occupied {
                continue;
            }
            let path = playout_path(state, &candidate).await?;
            match fs::symlink_metadata(&path).await {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    chosen = Some(candidate);
                    break;
                }
                Ok(_) => continue,
                Err(e) => return Err(e.into()),
            }
        }
        chosen
            .ok_or_else(|| AppError::Conflict("cannot allocate unique playout filename".into()))?
    };
    let relative = format!("VideoRoll/{filename}");
    let dest = playout_path(state, &filename).await?;
    let actual_path = dest.to_string_lossy().to_string();
    let row = sqlx::query(
        "INSERT INTO playout_asset_links(
          id,task_id,asset_id,source_asset_id,ffplayout_channel_id,
          relative_media_path,playout_path,source_checksum,runtime_engine,status)
          VALUES($1,$2,$3,$3,$4,$5,$6,$7,'rust-v2','staging')
          ON CONFLICT(asset_id,ffplayout_channel_id)
          DO UPDATE SET asset_id=EXCLUDED.asset_id,status='staging',updated_at=now()
          WHERE playout_asset_links.runtime_engine='rust-v2'
          RETURNING id,status,source_checksum,playout_path",
    )
    .bind(Uuid::new_v4())
    .bind(body.task_id)
    .bind(asset_id)
    .bind(body.channel_id)
    .bind(&relative)
    .bind(&actual_path)
    .bind(&sha)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| {
        AppError::Conflict("asset already linked by an original VideoRoll channel".into())
    })?;
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
    let destination_sha = file_sha(&dest).await?;
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
        use std::os::unix::fs::MetadataExt;
        let src = fs::metadata(&source).await?;
        let dst = fs::metadata(&dest).await?;
        if src.dev() == dst.dev() && src.ino() == dst.ino() {
            "hardlink"
        } else {
            "copy"
        }
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
    sqlx::query(
        "UPDATE vr2_playout_links SET status='detached',asset_id=NULL,updated_at=now() WHERE id=$1",
    )
    .bind(id)
    .execute(&mut *tx)
    .await?;
    // Do NOT physically delete the destination: it may currently be playing.
    // The external playlist must separately acknowledge removal first.
    sqlx::query(
        "DELETE FROM storage_object_refs WHERE storage_key=$1 AND owner_type='playout' AND owner_id=$2",
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
        let asset = Uuid::new_v4();
        assert_eq!(
            safe_filename("中文标题", "raw/a.mkv", asset),
            "中文标题.mkv"
        );
        let name = safe_filename("../测试/Footage:*?|<>", "raw/a.mp4", asset);
        assert!(!name.contains('/'));
        assert!(!name.contains('\\'));
        assert!(safe_filename("CON.txt", "raw/a.mp4", asset).starts_with("视频-"));
        let long = safe_filename(&"中文".repeat(200), "raw/a.webm", asset);
        for name in collision_names(&long, asset) {
            assert!(name.len() <= 180);
            assert!(name.ends_with(".webm"));
        }
        assert_eq!(collision_names("视频.mp4", asset)[0], "视频.mp4");
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
