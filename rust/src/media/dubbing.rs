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
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if !result.status.success() {
        let error = String::from_utf8_lossy(&result.stderr);
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
    if fs::metadata(&dest).await.is_ok() && probe_video(&config.ffprobe_binary, &dest).await.is_ok()
    {
        return ensure_task_asset(db, registry, job.task_id, job, "video_dubbed", &dest_key).await;
    }
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).await?;
    }
    // The dedicated v2 runtime is persistent, and can resume YouDub stages.
    command(config, runtime, &["--prepare"]).await?;
    let state = match command(config, runtime, &bridge_args("--status", &id)).await {
        Ok(value) => value,
        Err(_) => {
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
    fs::rename(temp, &dest).await?;
    let outcome =
        ensure_task_asset(db, registry, job.task_id, job, "video_dubbed", &dest_key).await?;
    sqlx::query(
        "INSERT INTO vr2_dubbing_runs(job_id,workflow_id,task_id,source_video_key,source_srt_key,output_video_key,model_job_id,model_status) VALUES($1,$2,$3,$4,$5,$6,$7,'succeeded') ON CONFLICT(job_id) DO NOTHING"
    ).bind(job.id).bind(job.workflow_id).bind(job.task_id).bind(video_key).bind(srt_key)
        .bind(&dest_key).bind(native_id).execute(&db.pool).await?;
    Ok(json!({"native_id":native_id,"video_key":dest_key,"result":outcome}))
}
#[cfg(test)]
mod tests {
    use super::*;
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
