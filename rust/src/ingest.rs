//! Ingestion runs yt-dlp as a child, not inside the API or a held SQL transaction.
use crate::{
    AppError, AppResult, Config, Database, domain::WorkItem, scheduler::Scheduler,
    storage::AssetRegistry,
};
use reqwest::Url;
use sqlx::Row;
use std::path::{Path, PathBuf};
use tokio::fs;
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
pub(crate) async fn find_finished_video(
    config: &Config,
    root: &Path,
) -> AppResult<Option<PathBuf>> {
    let mut entries = fs::read_dir(root).await?;
    let mut candidates = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        let path = entry.path();
        let metadata = entry.metadata().await?;
        if !entry.file_type().await?.is_file()
            || !metadata.is_file()
            || metadata.len() == 0
            || path.file_stem().and_then(|s| s.to_str()) != Some("source")
        {
            continue;
        }
        let Some(ext) = path.extension().and_then(|s| s.to_str()) else {
            continue;
        };
        if !["mp4", "mkv", "webm", "mov"].contains(&ext.to_lowercase().as_str()) {
            continue;
        }
        if tokio::time::timeout(
            std::time::Duration::from_secs(30),
            crate::media::ffmpeg::probe_video(&config.ffprobe_binary, &path),
        )
        .await
        .is_ok_and(|r| r.is_ok())
        {
            candidates.push(path);
        }
    }
    candidates.sort();
    if candidates.len() > 1 {
        return Err(AppError::Conflict(
            "multiple completed source formats require reconciliation".into(),
        ));
    }
    Ok(candidates.into_iter().next())
}
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub(crate) struct SourceRecovery {
    pub asset_id: Uuid,
    pub storage_key: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub video_id: String,
}
impl SourceRecovery {
    pub(crate) fn from_asset(asset: &crate::domain::AssetInfo, video_id: String) -> Option<Self> {
        let sha = asset.sha256.as_deref()?;
        let size = asset.size_bytes?;
        if size <= 0 || sha.len()!=64 || !sha.bytes().all(|b| b.is_ascii_hexdigit())
            || asset.kind!="video_raw"
            || !asset.storage_key.starts_with(&format!("v2/raw/{}/",asset.task_id))
            || AssetRegistry::checked_key(&asset.storage_key).is_err() {
            return None;
        }
        Some(Self { asset_id:asset.id, storage_key:asset.storage_key.clone(),
            sha256:sha.to_ascii_lowercase(),size_bytes:size,video_id })
    }
}
pub async fn download_youtube(
    db: &Database,
    registry: &AssetRegistry,
    scheduler: &Scheduler,
    job: &WorkItem,
    config: &Config,
) -> AppResult<serde_json::Value> {
    let task = db.task(job.task_id).await?;
    let source = if task.source_type == "youtube" {
        let url = task
            .source_url
            .as_deref()
            .ok_or_else(|| AppError::Invalid("missing YouTube source".into()))?;
        youtube_video_id(url)?;
        Some(url.to_owned())
    } else if task.source_type == "local" {
        None
    } else {
        return Err(AppError::Invalid("unsupported V2 download source".into()));
    };
    let recovery: Option<serde_json::Value> = sqlx::query_scalar(
        "SELECT configuration->'source_recovery' FROM vr2_workflows WHERE id=$1",
    ).bind(job.workflow_id).fetch_one(&db.pool).await?;
    let recovery: Option<SourceRecovery> = recovery.map(serde_json::from_value).transpose()
        .map_err(|_|AppError::Conflict("invalid source recovery proof".into()))?;
    if let Some(proof) = &recovery {
        let raw = sqlx::query_as::<_,crate::domain::AssetInfo>(
            "SELECT id,task_id,kind::text AS kind,storage_key,sha256,size_bytes,duration_ms,created_at
             FROM assets WHERE task_id=$1 AND kind::text='video_raw' ORDER BY created_at DESC,id DESC LIMIT 1",
        ).bind(job.task_id).fetch_optional(&db.pool).await?;
        let current = raw.as_ref().and_then(|a| SourceRecovery::from_asset(a,
            source.as_deref().and_then(|u|youtube_video_id(u).ok()).unwrap_or_default()));
        if current.as_ref()!=Some(proof) {
            return Err(AppError::Conflict("historical source identity changed during recovery".into()));
        }
        let path = registry.resolve_existing(&proof.storage_key).await?;
        match fs::symlink_metadata(&path).await {
            Err(e) if e.kind()==std::io::ErrorKind::NotFound => {},
            Err(e) => return Err(e.into()),
            Ok(meta) if meta.is_file() && meta.len()>0 => {},
            Ok(_) => return Err(AppError::Conflict("historical source path is not a nonempty regular file".into())),
        }
    }
    let rel_dir = if recovery.is_some() {
        format!("v2/raw/{}/recovery-{}", job.task_id,job.workflow_id)
    } else {
        format!("v2/raw/{}", job.task_id)
    };
    let output_dir = registry.resolve_existing(&rel_dir).await?;
    fs::create_dir_all(&output_dir).await?;

    // Resume from an already registered source when it exists and passed integrity.
    let mut missing_source: Option<(String, String, Option<i64>)> = None;
    if let Some(row) = sqlx::query(
        "SELECT storage_key,sha256,size_bytes FROM assets WHERE task_id=$1 AND kind='video_raw'::asset_kind
         AND storage_key LIKE 'v2/%' ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(job.task_id)
    .fetch_optional(&db.pool)
    .await?
    {
        let key: String = row.try_get("storage_key")?;
        if let Ok(path) = registry.resolve_existing(&key).await
            && fs::metadata(path)
                .await
                .map(|m| m.len() > 0)
                .unwrap_or(false)
        {
            let expected:Option<String>=row.try_get("sha256")?;
            let expected_size:Option<i64>=row.try_get("size_bytes")?;
            let (actual,size)=registry.sha256(&key).await?;
            if expected.as_ref().is_some_and(|v|!v.eq_ignore_ascii_case(&actual))||expected_size.is_some_and(|v|v!=size){
                return Err(AppError::Conflict("registered source checksum/size mismatch; reconcile source before retry".into()));
            }
            let path=registry.resolve_existing(&key).await?;
            tokio::time::timeout(std::time::Duration::from_secs(30),crate::media::ffmpeg::probe_video(&config.ffprobe_binary,&path)).await.map_err(|_|AppError::Unavailable("source probe timed out".into()))??;
            // A previous worker may have committed its legacy Asset but crashed
            // before registering the v2 ownership link. Reconcile it on reuse.
            // Legacy paths stay outside v2 GC jurisdiction.
            if key.starts_with("v2/") {
                registry
                    .register(
                        &key,
                        "workflow",
                        job.workflow_id,
                        "video_raw",
                        chrono::Utc::now() + chrono::Duration::days(2),
                    )
                    .await?;
            }
            // A local file upload is already a successful download-stage
            // receipt. Without this transition the UI can show 'CREATED'
            // while later subtitle/render stages run.
            sqlx::query(
                "UPDATE tasks SET status='downloaded'::task_status,updated_at=now()
                 WHERE id=$1 AND status IN ('created'::task_status,'ingested'::task_status)",
            )
            .bind(job.task_id)
            .execute(&db.pool)
            .await?;
            return Ok(serde_json::json!({"source":"existing_asset","storage_key":key,"sha256":actual,"size_bytes":size}));
        }
        let expected:Option<String>=row.try_get("sha256")?;
        let expected=expected.filter(|v|v.len()==64&&v.bytes().all(|b|b.is_ascii_hexdigit()))
            .ok_or_else(||AppError::Conflict("missing registered source has no verified checksum".into()))?;
        missing_source=Some((key,expected,row.try_get("size_bytes")?));
    }
    // Local uploads have no remote URL. They MUST reuse a registered
    // task-owned video_raw asset and are never sent through yt-dlp.
    let source = source.ok_or_else(|| {
        AppError::Conflict(
            "local task has no registered video_raw; upload it before starting".into(),
        )
    })?;
    // A run can have produced a full media file, then crash before DB commit.
    let video = match find_finished_video(config, &output_dir).await? {
        Some(existing) => existing,
        None => {
            crate::download_runtime::run(config, db, scheduler, job, &output_dir, &source).await?
        }
    };
    let root = fs::canonicalize(&registry.root).await?;
    let mut key = video
        .strip_prefix(&root)
        .map_err(|_| AppError::Invalid("downloaded file must belong to storage root".into()))?
        .to_string_lossy()
        .replace('\\', "/");
    let (digest, size) = registry.sha256(&key).await?;
    // The downloader emits its own info JSON from the same extraction that
    // produced the video. Avoid a second network request after media succeeds.
    let info_path = video.with_extension("info.json");
    let downloaded_info = crate::youtube_meta::read_downloaded_info(registry, &info_path, &source).await?;
    if let Some((old_key, expected, expected_size)) = missing_source {
        if !expected.eq_ignore_ascii_case(&digest) || expected_size.is_some_and(|v| v != size) {
            return Err(AppError::Conflict("redownloaded source differs from historical asset; reconcile before using derived media".into()));
        }
        if old_key != key {
            let old_path = registry.resolve_existing(&old_key).await?;
            if let Some(parent) = old_path.parent() {
                fs::create_dir_all(parent).await?;
            }
            if fs::symlink_metadata(&old_path).await.is_ok() {
                return Err(AppError::Conflict(
                    "historical source reappeared during recovery".into(),
                ));
            }
            if !scheduler.heartbeat(job).await? {
                return Err(AppError::Conflict("download lease lost before source restoration".into()));
            }
            // Link rather than rename: never overwrite a source restored by
            // another process after the existence check. Both paths are under
            // the same storage root; unsupported links fail closed.
            fs::hard_link(&video, &old_path).await?;
            if let Err(error) = fs::remove_file(&video).await {
                tracing::warn!(%error,"recovery staging file retained");
            }
            key = old_key;
        }
    }
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
    if let Some(info) = downloaded_info.as_ref() {
        crate::youtube_meta::persist_downloaded_info(&mut tx, job.task_id, info, &source).await?;
    }
    sqlx::query(
        "UPDATE tasks SET status='downloaded'::task_status,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1 AND status IN ('created'::task_status,'ingested'::task_status)"
    ).bind(job.task_id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(serde_json::json!({"storage_key":key,"sha256":digest,"size_bytes":size}))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_requires_task_owned_native_raw_and_digest() {
        let task=Uuid::new_v4();
        let mut asset=crate::domain::AssetInfo {
            id:Uuid::new_v4(),task_id:task,kind:"video_raw".into(),
            storage_key:format!("v2/raw/{task}/native/source.mp4"),
            sha256:Some("AB".repeat(32)),size_bytes:Some(42),duration_ms:None,
            created_at:chrono::Utc::now(),
        };
        let proof=SourceRecovery::from_asset(&asset,"0123456789A".into()).unwrap();
        assert_eq!(proof.sha256,"ab".repeat(32));
        asset.storage_key=format!("v2/raw/{}/source.mp4",Uuid::new_v4());
        assert!(SourceRecovery::from_asset(&asset,"0123456789A".into()).is_none());
        asset.storage_key=format!("v2/raw/{task}/source.mp4");
        asset.sha256=None;
        assert!(SourceRecovery::from_asset(&asset,"0123456789A".into()).is_none());
        asset.sha256=Some("ab".repeat(32));
        asset.size_bytes=Some(0);
        assert!(SourceRecovery::from_asset(&asset,"0123456789A".into()).is_none());
    }
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
