//! Ingestion runs yt-dlp as a child, not inside the API or a held SQL transaction.
use crate::{
    AppError, AppResult, Database, domain::WorkItem, scheduler::Scheduler, storage::AssetRegistry,
};
use reqwest::Url;
use sqlx::Row;
use std::{
    path::{Path, PathBuf},
    process::Stdio,
};
use tokio::{
    fs,
    process::Command,
    time::{Duration, interval},
};
use uuid::Uuid;

pub fn validate_youtube_url(value: &str) -> AppResult<Url> {
    let url = Url::parse(value).map_err(|_| AppError::Invalid("invalid URL".into()))?;
    if url.scheme() != "https" && url.scheme() != "http" {
        return Err(AppError::Invalid("source must use HTTP(S)".into()));
    }
    let domain = url
        .domain()
        .ok_or_else(|| AppError::Invalid("URL host missing".into()))?;
    if !matches!(
        domain,
        "youtube.com"
            | "www.youtube.com"
            | "m.youtube.com"
            | "music.youtube.com"
            | "youtu.be"
            | "www.youtu.be"
    ) {
        return Err(AppError::Invalid(
            "only YouTube video sources are accepted".into(),
        ));
    }
    if url.username() != "" || url.password().is_some() {
        return Err(AppError::Invalid(
            "credentials in URL are not supported".into(),
        ));
    }
    Ok(url)
}
pub fn youtube_video_id(value: &str) -> AppResult<String> {
    let url = validate_youtube_url(value)?;
    let host = url.domain().unwrap_or_default();
    let id = if host.ends_with("youtu.be") {
        url.path_segments()
            .and_then(|mut p| p.next())
            .unwrap_or_default()
            .to_string()
    } else {
        let segments: Vec<&str> = url.path_segments().map(|p| p.collect()).unwrap_or_default();
        match segments.first().copied().unwrap_or_default() {
            "watch" => url
                .query_pairs()
                .find(|(k, _)| k == "v")
                .map(|(_, v)| v.to_string())
                .unwrap_or_default(),
            "shorts" | "live" | "embed" => segments.get(1).copied().unwrap_or_default().to_string(),
            _ => String::new(),
        }
    };
    if id.len() != 11
        || !id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err(AppError::Invalid(
            "URL is not a single YouTube video with a valid video ID".into(),
        ));
    }
    Ok(id)
}
async fn find_finished_video(root: &Path) -> AppResult<Option<PathBuf>> {
    let mut entries = fs::read_dir(root).await?;
    let mut candidates = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        let path = entry.path();
        let metadata = entry.metadata().await?;
        if !metadata.is_file() || metadata.len() == 0 {
            continue;
        }
        let Some(ext) = path.extension().and_then(|s| s.to_str()) else {
            continue;
        };
        if !["mp4", "mkv", "webm", "mov"].contains(&ext.to_lowercase().as_str()) {
            continue;
        }
        candidates.push(path);
    }
    candidates.sort();
    Ok(candidates.into_iter().next())
}
pub async fn download_youtube(
    db: &Database,
    registry: &AssetRegistry,
    scheduler: &Scheduler,
    job: &WorkItem,
    binary: &Path,
) -> AppResult<serde_json::Value> {
    let task = db.task(job.task_id).await?;
    let source = task
        .source_url
        .ok_or_else(|| AppError::Invalid("missing YouTube source".into()))?;
    youtube_video_id(&source)?;
    let rel_dir = format!("v2/raw/{}", job.task_id);
    let output_dir = registry.resolve_existing(&rel_dir).await?;
    fs::create_dir_all(&output_dir).await?;

    // Resume from an already registered source when it exists and passed integrity.
    if let Some(row)=sqlx::query(
        "SELECT storage_key FROM assets WHERE task_id=$1 AND kind='video_raw'::asset_kind ORDER BY created_at DESC LIMIT 1"
    ).bind(job.task_id).fetch_optional(&db.pool).await? {
        let key:String=row.try_get("storage_key")?;
        if let Ok(path)=registry.resolve_existing(&key).await
            && fs::metadata(path).await.map(|m|m.len()>0).unwrap_or(false) {
            // A previous worker may have committed its legacy Asset but crashed
            // before registering the v2 ownership link. Reconcile it on reuse.
            // Legacy paths stay outside v2 GC jurisdiction.
            if key.starts_with("v2/") {
                registry.register(
                    &key,"workflow",job.workflow_id,"video_raw",
                    chrono::Utc::now()+chrono::Duration::days(2)
                ).await?;
            }
            return Ok(serde_json::json!({"source":"existing_asset","storage_key":key}));
        }
    }
    // A run can have produced a full media file, then crash before DB commit.
    let video = match find_finished_video(&output_dir).await? {
        Some(existing) => existing,
        None => {
            let template = output_dir.join("source.%(ext)s");
            let mut command = Command::new(binary);
            command
                .arg("--no-playlist")
                .arg("--continue")
                .arg("--no-overwrites")
                .arg("--newline")
                .arg("--merge-output-format")
                .arg("mp4")
                .arg("-o")
                .arg(&template)
                .arg("--")
                .arg(&source)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .kill_on_drop(true);
            let mut child = command.spawn().map_err(AppError::Io)?;
            let mut tick = interval(Duration::from_secs(30));
            loop {
                tokio::select! {
                    result=child.wait()=>{
                        let status=result?;
                        if !status.success(){
                            return Err(AppError::Unavailable(format!("yt-dlp exited with {status}; inspect the task's worker logs")));
                        }
                        break;
                    },
                    _=tick.tick()=>{
                        if !scheduler.heartbeat(job).await? {
                            return Err(AppError::Conflict("download execution lease lost".into()));
                        }
                    }
                }
            }
            find_finished_video(&output_dir).await?.ok_or_else(|| {
                AppError::Unavailable("yt-dlp returned success without a completed video".into())
            })?
        }
    };
    let root = fs::canonicalize(&registry.root).await?;
    let key = video
        .strip_prefix(&root)
        .map_err(|_| AppError::Invalid("downloaded file must belong to storage root".into()))?
        .to_string_lossy()
        .replace('\\', "/");
    let (digest, size) = registry.sha256(&key).await?;
    registry
        .register(
            &key,
            "workflow",
            job.workflow_id,
            "video_raw",
            chrono::Utc::now() + chrono::Duration::days(2),
        )
        .await?;
    // A successful download can be re-delivered. Register the same asset at most once.
    let mut tx = db.pool.begin().await?;
    sqlx::query(
        "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) SELECT $1,$2,'video_raw'::asset_kind,$3,$4,$5 WHERE NOT EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND kind='video_raw'::asset_kind AND storage_key=$3)"
    ).bind(Uuid::new_v4()).bind(job.task_id).bind(&key).bind(&digest).bind(size)
        .execute(&mut *tx).await?;
    sqlx::query(
        "UPDATE tasks SET status='DOWNLOADED'::task_status,updated_at=now() WHERE id=$1 AND status IN ('CREATED'::task_status,'INGESTED'::task_status)"
    ).bind(job.task_id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(serde_json::json!({"storage_key":key,"sha256":digest,"size_bytes":size}))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn youtube_validation_is_not_an_arbitrary_ssrf_gate() {
        for s in [
            "https://youtube.com/watch?v=0123456789A",
            "https://youtu.be/0123456789A",
            "https://www.youtube.com/shorts/0123456789A",
            "https://www.youtube.com/live/0123456789A",
        ] {
            assert_eq!(youtube_video_id(s).unwrap(), "0123456789A");
        }
        for s in [
            "http://127.0.0.1/admin",
            "https://youtube.com.evil.test/v",
            "file:///etc/passwd",
            "https://user:pass@youtube.com/watch",
            "https://www.youtube.com/channel/example",
            "https://www.youtube.com/watch?v=invalid",
        ] {
            assert!(youtube_video_id(s).is_err());
        }
    }
}
