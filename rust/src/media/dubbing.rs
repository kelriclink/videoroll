//! Isolated subprocess adapter for the third-party YouDub VoxCPM2/Demucs pipeline.
//! The original model runtime is not embedded inside the Tokio API.
use crate::{
    AppError, AppResult, Config, Database,
    domain::WorkItem,
    media::ffmpeg::{ensure_task_asset, latest_asset_key, probe_video},
    storage::AssetRegistry,
};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{fs, process::Command};
use uuid::Uuid;

pub async fn auto_enabled(db: &Database, job: &WorkItem) -> AppResult<bool> {
    let setting: Option<Value> =
        Some(crate::settings::effective(db, "subtitle.auto_profile").await?);
    let mut enabled = setting
        .as_ref()
        .and_then(|v| v.get("auto_dub_p2"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let settings: Option<Value> =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
            .bind(job.workflow_id)
            .fetch_optional(&db.pool)
            .await?;
    if let Some(value) = settings
        .as_ref()
        .and_then(|v| v.get("auto_dub_p2"))
        .and_then(Value::as_bool)
    {
        enabled = value;
    }
    if let Some(value) = settings
        .as_ref()
        .and_then(|v| v.pointer("/dubbing/enabled"))
        .and_then(Value::as_bool)
    {
        enabled = value;
    }
    Ok(enabled)
}
fn ensure_isolated_runtime(path: &Path) -> AppResult<()> {
    if !path.is_absolute() {
        return Err(AppError::Invalid(
            "YouDub runtime must be an absolute isolated path".into(),
        ));
    }
    if path == Path::new("/DATA/youdub-runtime") || path == Path::new("/") {
        return Err(AppError::Invalid(
            "v2 cannot use original production YouDub runtime".into(),
        ));
    }
    Ok(())
}
fn bridge_args<'a>(mode: &'a str, task_id: &'a str) -> Vec<&'a str> {
    vec![mode, "--task-id", task_id]
}
fn parse_json_stdout(output: &[u8]) -> AppResult<Value> {
    let value = std::str::from_utf8(output)
        .map_err(|_| AppError::Unavailable("YouDub emitted invalid UTF-8".into()))?;
    // Some third-party ML dependencies print progress before the JSON line.
    for row in value.lines().rev() {
        if let Ok(json) = serde_json::from_str::<Value>(row.trim())
            && json.is_object()
        {
            return Ok(json);
        }
    }
    Err(AppError::Unavailable(
        "YouDub adapter did not return JSON".into(),
    ))
}
async fn active_lease(db: &Database, job: &WorkItem) -> AppResult<()> {
    let active:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id WHERE j.id=$1 AND j.lease_token=$2 AND j.state='running' AND j.lease_until>now() AND w.state='running')")
        .bind(job.id).bind(job.lease_token).fetch_one(&db.pool).await?;
    if !active {
        return Err(AppError::Conflict("YouDub lease lost".into()));
    }
    Ok(())
}
async fn gpu_slot(runtime: &Path, db: &Database, job: &WorkItem) -> AppResult<std::fs::File> {
    fs::create_dir_all(runtime).await?;
    // This is the same per-runtime flock used by the old Python adapter.
    // Never hold a SQL transaction during GPU admission or inference.
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(runtime.join(".videoroll-native-gpu.lock"))?;
    use std::os::fd::AsRawFd;
    loop {
        active_lease(db, job).await?;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(file);
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::WouldBlock {
            return Err(error.into());
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}
fn missing_task(error: &AppError) -> bool {
    // Only an explicit missing task response authorizes creation. Do not
    // treat SQLite busy, transport, permission or malformed JSON as absent.
    matches!(error,AppError::NotFound(message) if message=="YouDub task absent")
}
async fn command(config: &Config, runtime: &Path, args: &[&str]) -> AppResult<Value> {
    let python = config
        .youdub_python
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("YouDub Python runtime is not configured".into()))?;
    let bridge = config
        .youdub_bridge
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("YouDub bridge path is not configured".into()))?;
    let result = Command::new(python)
        .arg(bridge)
        .args(args)
        .env("VIDEOROLL_YOUDUB_RUNTIME", runtime)
        .env("VIDEOROLL_YOUDUB_STATUS_PROTOCOL", "2")
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if !result.status.success() {
        let error = String::from_utf8_lossy(&result.stderr);
        if args.first() == Some(&"--status")
            && result.status.code() == Some(44)
            && error.trim() == "VIDEOROLL_YOUDUB_TASK_NOT_FOUND"
        {
            return Err(AppError::NotFound("YouDub task absent".into()));
        }
        return Err(AppError::Unavailable(format!(
            "YouDub bridge exited with {}: {}",
            result.status,
            error
                .chars()
                .rev()
                .take(500)
                .collect::<String>()
                .chars()
                .rev()
                .collect::<String>()
        )));
    }
    parse_json_stdout(&result.stdout)
}
pub async fn process(
    config: &Config,
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
) -> AppResult<Value> {
    if !auto_enabled(db, job).await? {
        return Ok(json!({"skipped":"automatic P2 dubbing is disabled"}));
    }
    let no_speech:Option<Value>=sqlx::query_scalar(
        "SELECT result FROM vr2_jobs WHERE workflow_id=$1 AND stage='subtitle' AND state='succeeded'"
    ).bind(job.workflow_id).fetch_optional(&db.pool).await?.flatten();
    if no_speech
        .as_ref()
        .and_then(|v| v.get("no_speech"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        return Ok(json!({"skipped":"no credible speech: P2 was not generated"}));
    }
    let runtime = config.youdub_runtime.as_deref().ok_or_else(|| {
        AppError::Unavailable("separate YouDub v2 runtime directory must be configured".into())
    })?;
    ensure_isolated_runtime(runtime)?;
    let srt_key = latest_asset_key(db, job.task_id, "subtitle_srt")
        .await?
        .ok_or_else(|| AppError::NotFound("P2 requires translated subtitle_srt".into()))?;
    let video_key = latest_asset_key(db, job.task_id, "video_raw")
        .await?
        .ok_or_else(|| AppError::NotFound("P2 requires original video_raw".into()))?;
    let srt = registry.resolve_existing(&srt_key).await?;
    let video = registry.resolve_existing(&video_key).await?;
    let video_info = probe_video(&config.ffprobe_binary, &video).await?;
    if video_info.get("streams").is_none() || fs::metadata(&srt).await?.len() < 10 {
        return Err(AppError::Unavailable(
            "P2 source media or subtitles are invalid".into(),
        ));
    }
    let native_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("VideoRoll-v2-YouDub:{}", job.workflow_id).as_bytes(),
    );
    let id = native_id.to_string();
    let dest_key = format!("v2/dubbing/{}/{}.mp4", job.task_id, job.id);
    let dest = registry.resolve_existing(&dest_key).await?;
    let existing=sqlx::query("SELECT source_video_key,source_srt_key,output_video_key,model_job_id FROM vr2_dubbing_runs WHERE job_id=$1")
        .bind(job.id).fetch_optional(&db.pool).await?;
    use sqlx::Row;
    if let Some(proof) = existing.as_ref() {
        if proof.try_get::<String, _>("source_video_key")? != video_key
            || proof.try_get::<String, _>("source_srt_key")? != srt_key
            || proof.try_get::<String, _>("output_video_key")? != dest_key
            || proof.try_get::<Uuid, _>("model_job_id")? != native_id
        {
            return Err(AppError::Conflict(
                "YouDub source or receipt changed; cannot reuse output".into(),
            ));
        }
        if fs::metadata(&dest).await.is_ok()
            && probe_video(&config.ffprobe_binary, &dest).await.is_ok()
        {
            let expected:Option<String>=sqlx::query_scalar("SELECT sha256 FROM assets WHERE task_id=$1 AND kind::text='video_dubbed' AND storage_key=$2 ORDER BY created_at DESC LIMIT 1")
                .bind(job.task_id).bind(&dest_key).fetch_optional(&db.pool).await?.flatten();
            let actual = registry.sha256(&dest_key).await?.0;
            if expected
                .as_ref()
                .is_some_and(|v| v.eq_ignore_ascii_case(&actual))
            {
                active_lease(db, job).await?;
                let outcome =
                    ensure_task_asset(db, registry, job.task_id, job, "video_dubbed", &dest_key)
                        .await?;
                return Ok(json!({"native_id":native_id,"video_key":dest_key,"result":outcome}));
            }
            return Err(AppError::Conflict(
                "YouDub output checksum is absent or mismatched".into(),
            ));
        }
    }
    let _gpu = gpu_slot(runtime, db, job).await?;
    active_lease(db, job).await?;
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).await?;
    }
    // The dedicated v2 runtime is persistent, and can resume YouDub stages.
    command(config, runtime, &["--prepare"]).await?;
    // Keep the input identity durable across creation, interrupted copies,
    // retries and native status recovery. Never attach an old model result
    // to a changed subtitle/source just because the workflow UUID matches.
    let source_pin = json!({
        "schema":1,"native_id":native_id,"video_key":video_key,"srt_key":srt_key,
        "video_sha256":registry.sha256(&video_key).await?.0,
        "srt_sha256":registry.sha256(&srt_key).await?.0
    });
    let pin_dir = runtime.join(".videoroll-source-pins");
    fs::create_dir_all(&pin_dir).await?;
    let pin = pin_dir.join(format!("{id}.json"));
    let pinned = match fs::read(&pin).await {
        Ok(bytes) => {
            let previous: Value = serde_json::from_slice(&bytes)
                .map_err(|_| AppError::Conflict("YouDub source pin is invalid".into()))?;
            if previous != source_pin {
                return Err(AppError::Conflict(
                    "YouDub input changed after native task creation".into(),
                ));
            }
            true
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
        Err(e) => return Err(e.into()),
    };
    let state = match command(config, runtime, &bridge_args("--status", &id)).await {
        Ok(value) if pinned || existing.is_some() => value,
        Ok(_) => {
            return Err(AppError::Conflict(
                "unproven historical YouDub task; reconcile source receipt before reuse".into(),
            ));
        }
        Err(error) if missing_task(&error) => {
            if existing.is_some() {
                return Err(AppError::Conflict(
                    "previously confirmed YouDub task is absent".into(),
                ));
            }
            if !pinned {
                let temporary = pin.with_extension(format!("{}.partial", Uuid::new_v4()));
                let bytes = serde_json::to_vec(&source_pin)
                    .map_err(|_| AppError::Invalid("YouDub source pin encode failed".into()))?;
                {
                    use tokio::io::AsyncWriteExt;
                    let mut file = fs::OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(&temporary)
                        .await?;
                    file.write_all(&bytes).await?;
                    file.sync_all().await?;
                }
                fs::rename(&temporary, &pin).await?;
            }
            let video = video
                .to_str()
                .ok_or_else(|| AppError::Invalid("YouDub video path is not UTF-8".into()))?;
            let srt = srt
                .to_str()
                .ok_or_else(|| AppError::Invalid("YouDub SRT path is not UTF-8".into()))?;
            command(
                config,
                runtime,
                &[
                    "--create",
                    "--task-id",
                    &id,
                    "--video",
                    video,
                    "--srt",
                    srt,
                    "--direction",
                    "en-zh",
                ],
            )
            .await?
        }
        Err(error) => return Err(error),
    };
    let status = state.get("status").and_then(Value::as_str).unwrap_or("");
    let final_result = match status {
        "succeeded" => state,
        "queued" | "paused" => command(config, runtime, &bridge_args("--run", &id)).await?,
        "failed" => {
            command(config, runtime, &bridge_args("--resume", &id)).await?;
            command(config, runtime, &bridge_args("--run", &id)).await?
        }
        other => {
            return Err(AppError::Unavailable(format!(
                "YouDub has non-resumable state {other}; manual reconciliation required"
            )));
        }
    };
    if final_result.get("status").and_then(Value::as_str) != Some("succeeded") {
        return Err(AppError::Unavailable(
            "YouDub did not report completed status".into(),
        ));
    }
    let output = final_result
        .get("output_video")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .ok_or_else(|| AppError::Unavailable("YouDub output path absent".into()))?;
    // The adapter must never import arbitrary paths returned by a third-party script.
    let root = fs::canonicalize(runtime).await?;
    let source = fs::canonicalize(&output).await?;
    if !source.starts_with(&root) {
        return Err(AppError::Invalid(
            "YouDub produced an output outside v2 isolated runtime".into(),
        ));
    }
    probe_video(&config.ffprobe_binary, &source).await?;
    let temp = dest.with_extension("partial");
    fs::copy(&source, &temp).await?;
    let file = fs::OpenOptions::new().read(true).open(&temp).await?;
    file.sync_all().await?;
    drop(file);
    active_lease(db, job).await?;
    if let Ok(meta) = fs::metadata(&dest).await {
        // A prior worker may have copied the exact native result and died
        // before writing its proof. Compare actual bytes, not existence.
        // Use registry relative path with the actual extension replacement.
        let partial_key = Path::new(&dest_key)
            .with_extension("partial")
            .to_string_lossy()
            .into_owned();
        let same = meta.len() == fs::metadata(&temp).await?.len()
            && registry.sha256(&dest_key).await?.0 == registry.sha256(&partial_key).await?.0;
        if !same {
            let _ = fs::remove_file(&temp).await;
            return Err(AppError::Conflict(
                "existing YouDub output differs from recovered native receipt".into(),
            ));
        }
        fs::remove_file(&temp).await?;
    } else {
        fs::rename(temp, &dest).await?;
    }
    let outcome =
        ensure_task_asset(db, registry, job.task_id, job, "video_dubbed", &dest_key).await?;
    sqlx::query(
        "UPDATE operation_inbox SET
            dubbing_source_video_key=$4,dubbing_source_srt_key=$5,
            dubbing_output_video_key=$6,dubbing_model_job_id=$7,
            dubbing_model_status='succeeded',dubbing_run_created_at=now(),updated_at=now()
          WHERE id=$1 AND workflow_id=$2 AND task_id=$3
            AND runtime_engine='rust-v2' AND stage='dubbing'
            AND dubbing_model_status IS NULL
            AND EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
                WHERE j.id=$1 AND j.lease_token=$8 AND j.state='running' AND j.lease_until>now() AND w.state='running')",
    )
    .bind(job.id)
    .bind(job.workflow_id)
    .bind(job.task_id)
    .bind(&video_key)
    .bind(&srt_key)
    .bind(&dest_key)
    .bind(native_id)
    .bind(job.lease_token)
    .execute(&db.pool)
    .await?;
    // Completed dubbing is an immutable proof for the selected subtitle and
    // source media. Never silently replace it when a replay differs.
    let proof = sqlx::query(
        "SELECT source_video_key,source_srt_key,output_video_key,model_job_id
         FROM vr2_dubbing_runs WHERE job_id=$1",
    )
    .bind(job.id)
    .fetch_optional(&db.pool)
    .await?
    .ok_or_else(|| {
        AppError::Conflict("native YouDub operation no longer owns dubbing evidence".into())
    })?;
    if proof.try_get::<String, _>("source_video_key")? != video_key
        || proof.try_get::<String, _>("source_srt_key")? != srt_key
        || proof.try_get::<String, _>("output_video_key")? != dest_key
        || proof.try_get::<Uuid, _>("model_job_id")? != native_id
    {
        return Err(AppError::Conflict(
            "native YouDub output differs from already persisted proof".into(),
        ));
    }
    Ok(json!({"native_id":native_id,"video_key":dest_key,"result":outcome}))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_explicit_absence_allows_native_create() {
        assert!(missing_task(&AppError::NotFound(
            "YouDub task absent".into()
        )));
        assert!(!missing_task(&AppError::Unavailable(
            "SQLite database locked".into()
        )));
        assert!(!missing_task(&AppError::Unavailable(
            "Unknown YouDub task".into()
        )));
    }
    #[test]
    fn requires_dedicated_external_runtime() {
        assert!(ensure_isolated_runtime(Path::new("/DATA/youdub-runtime")).is_err());
        assert!(ensure_isolated_runtime(Path::new("/var/lib/videoroll-v2/youdub")).is_ok());
        assert!(ensure_isolated_runtime(Path::new("relative/work")).is_err());
    }
    #[test]
    fn bridge_output_requires_result_object() {
        assert!(parse_json_stdout(b"warning\n{\"status\":\"succeeded\"}\n").is_ok());
        assert!(parse_json_stdout(b"something failed\n").is_err());
    }
}
