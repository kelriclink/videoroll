//! Spawn FFmpeg in a separate OS process. No native FFmpeg pointers cross Rust lifetimes.
use crate::{AppError, AppResult, Config, Database, domain::WorkItem, storage::AssetRegistry};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{path::Path, process::Stdio};
use tokio::{fs, process::Command};
use uuid::Uuid;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct RenderProfile {
    pub burn_in: bool,
    pub soft_sub: bool,
    pub video_codec: String,
    pub use_intel_gpu: bool,
    pub video_crf: Option<u8>,
    pub video_preset: Option<String>,
    pub ass_style: String,
}
impl Default for RenderProfile {
    fn default() -> Self {
        Self {
            burn_in: true,
            soft_sub: false,
            video_codec: "h264".into(),
            use_intel_gpu: false,
            video_crf: Some(23),
            video_preset: None,
            ass_style: "clean_white".into(),
        }
    }
}
pub async fn profile(db: &Database, job: &WorkItem) -> AppResult<RenderProfile> {
    let runtime = crate::settings::effective(db, "subtitle.auto_profile").await?;
    let settings: Option<Value> =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
            .bind(job.workflow_id)
            .fetch_optional(&db.pool)
            .await?;
    // Explicit runtime-profile requests use the original UI defaults as well
    // as the current settings; ordinary existing workflows are unchanged.
    let runtime = if settings
        .as_ref()
        .and_then(|v| v["standalone_subtitle_runtime_profile"].as_bool())
        == Some(true)
    {
        crate::subtitle_management_v1::normalized_profile(&runtime)
    } else {
        runtime
    };
    let mut p = serde_json::from_value::<RenderProfile>(runtime).unwrap_or_default();
    if let Some(overrides) = settings.and_then(|v| v.get("render").cloned()) {
        let override_values = serde_json::to_value(&p).unwrap_or_default();
        let mut combined = override_values;
        if let (Some(root), Some(ov)) = (combined.as_object_mut(), overrides.as_object()) {
            for (k, v) in ov {
                root.insert(k.clone(), v.clone());
            }
        }
        p = serde_json::from_value(combined)
            .map_err(|e| AppError::Invalid(format!("invalid render profile: {e}")))?;
    }
    Ok(p)
}
pub async fn latest_asset_key(
    db: &Database,
    task_id: Uuid,
    kind: &str,
) -> AppResult<Option<String>> {
    Ok(sqlx::query_scalar(
        "SELECT storage_key FROM assets WHERE task_id=$1 AND kind::text=$2 ORDER BY created_at DESC,id DESC LIMIT 1"
    ).bind(task_id).bind(kind).fetch_optional(&db.pool).await?)
}
pub async fn probe_video(binary: &Path, path: &Path) -> AppResult<Value> {
    let output = Command::new(binary)
        .arg("-v")
        .arg("error")
        .arg("-show_entries")
        .arg("format=duration:stream=codec_type,codec_name,width,height")
        .arg("-of")
        .arg("json")
        .arg(path)
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if !output.status.success() {
        return Err(AppError::Unavailable(
            "ffprobe cannot parse the rendered media".into(),
        ));
    }
    let json: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| AppError::Unavailable("invalid ffprobe output".into()))?;
    let duration = json
        .get("format")
        .and_then(|v| v.get("duration"))
        .and_then(Value::as_str)
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0);
    let video = json
        .get("streams")
        .and_then(Value::as_array)
        .is_some_and(|v| {
            v.iter()
                .any(|x| x.get("codec_type").and_then(Value::as_str) == Some("video"))
        });
    if !video || !duration.is_finite() || duration < 0.1 {
        return Err(AppError::Unavailable(
            "media missing a valid video stream or duration".into(),
        ));
    }
    Ok(json)
}
/// ffmpeg has its own filter syntax even when argv bypasses a shell. Use a
/// temporary path with a fixed UUID basename and no user-controlled quotes.
fn ffmpeg_subtitle_filter(path: &Path) -> AppResult<String> {
    let s = path
        .to_str()
        .ok_or_else(|| AppError::Invalid("non-UTF8 subtitle path".into()))?;
    if s.chars()
        .any(|c| matches!(c, '\'' | ':' | '[' | ']' | ',' | '\\' | '\n' | '\r' | '='))
    {
        return Err(AppError::Invalid(
            "subtitle path contains FFmpeg filter syntax".into(),
        ));
    }
    Ok(format!("subtitles={s}"))
}
/// Pure command-argument generation; the input never passes through a shell.
pub fn encode_args(
    source: &Path,
    output: &Path,
    subtitle_path: Option<&Path>,
    profile: &RenderProfile,
    device: Option<&str>,
) -> AppResult<Vec<String>> {
    let allowed_preset = profile
        .video_preset
        .as_deref()
        .filter(|s| {
            matches!(
                *s,
                "ultrafast"
                    | "superfast"
                    | "veryfast"
                    | "faster"
                    | "fast"
                    | "medium"
                    | "slow"
                    | "slower"
                    | "veryslow"
            )
        })
        .unwrap_or("medium");
    let crf = profile.video_crf.unwrap_or(23).clamp(1, 51);
    let codec = match profile.video_codec.as_str() {
        "h264" | "avc" => "h264",
        "hevc" | "h265" => "hevc",
        "av1" => "av1",
        _ => {
            return Err(AppError::Invalid(
                "unsupported requested video codec".into(),
            ));
        }
    };
    let mut args = vec!["-hide_banner", "-nostdin", "-y", "-v", "warning", "-i"]
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    args.push(source.to_string_lossy().to_string());
    if let Some(sub) = subtitle_path {
        args.push("-vf".into());
        args.push(ffmpeg_subtitle_filter(sub)?);
    }
    let gpu = profile.use_intel_gpu && device.is_some();
    let encoder = if gpu {
        match codec {
            "h264" => "h264_vaapi",
            "hevc" => "hevc_vaapi",
            _ => "av1_vaapi",
        }
    } else {
        match codec {
            "h264" => "libx264",
            "hevc" => "libx265",
            _ => "libsvtav1",
        }
    };
    if gpu {
        // Render ASS/subtitles on CPU first; upload only the finished NV12 frames.
        let vf = if let Some(sub) = subtitle_path {
            format!("{},format=nv12,hwupload", ffmpeg_subtitle_filter(sub)?)
        } else {
            "format=nv12,hwupload".to_string()
        };
        if let Some(index) = args.iter().position(|v| v == "-vf") {
            args[index + 1] = vf
        } else {
            args.push("-vf".into());
            args.push(vf);
        }
        args.push("-vaapi_device".into());
        args.push(device.unwrap().to_owned());
    }
    args.push("-c:v".into());
    args.push(encoder.into());
    if gpu {
        args.push("-qp".into());
        args.push(crf.to_string());
    } else if codec == "av1" {
        args.push("-crf".into());
        args.push(crf.to_string());
        args.push("-preset".into());
        args.push("8".into());
    } else {
        args.push("-crf".into());
        args.push(crf.to_string());
        args.push("-preset".into());
        args.push(allowed_preset.into());
    }
    args.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]
        .into_iter()
        .map(str::to_owned),
    );
    args.push(output.to_string_lossy().to_string());
    Ok(args)
}
pub async fn transcode(
    config: &Config,
    source: &Path,
    output: &Path,
    subtitles: Option<&Path>,
    profile: &RenderProfile,
) -> AppResult<()> {
    let device = std::env::var("INTEL_GPU_RENDER_DEVICE")
        .ok()
        .filter(|s| s.starts_with("/dev/dri/renderD"));
    let plans = if profile.use_intel_gpu && device.is_some() {
        vec![true, false]
    } else {
        vec![false]
    };
    for try_gpu in plans {
        let mut options = profile.clone();
        options.use_intel_gpu = try_gpu;
        let args = encode_args(source, output, subtitles, &options, device.as_deref())?;
        let result = Command::new(&config.ffmpeg_binary)
            .args(args)
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output()
            .await?;
        if result.status.success() {
            return Ok(());
        }
        let text = String::from_utf8_lossy(&result.stderr);
        tracing::warn!(gpu=try_gpu,error=%text.chars().rev().take(1200).collect::<String>().chars().rev().collect::<String>(),"FFmpeg encode failed");
        if try_gpu {
            let _ = fs::remove_file(output).await;
            continue;
        }
        return Err(AppError::Unavailable(format!(
            "FFmpeg render failed with {}",
            result.status
        )));
    }
    Err(AppError::Unavailable("no usable video encoder".into()))
}
pub(crate) async fn ensure_task_asset(
    db: &Database,
    registry: &AssetRegistry,
    task_id: Uuid,
    job: &WorkItem,
    kind: &str,
    key: &str,
) -> AppResult<Value> {
    let key = AssetRegistry::checked_key(key)?;
    let (sha, size) = registry.sha256(key).await?;
    let mut tx = db.pool.begin().await?;
    let status: Option<String> =
        sqlx::query_scalar("SELECT upper(status::text) FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_optional(&mut *tx)
            .await?;
    if status.as_deref() == Some("CANCELED") || status.is_none() {
        return Err(AppError::Conflict(
            "render result cannot commit for stopped task".into(),
        ));
    }
    sqlx::query(
        "INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days') ON CONFLICT(storage_key) DO UPDATE SET retain_until=GREATEST(storage_objects.retain_until,now()+interval '2 days') WHERE storage_objects.state='live'"
    ).bind(key).bind(&sha).bind(size).execute(&mut *tx).await?;
    sqlx::query(
        "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'workflow',$2,$3) ON CONFLICT DO NOTHING"
    ).bind(key).bind(job.workflow_id).bind(kind).execute(&mut *tx).await?;
    let existing:Option<Uuid>=sqlx::query_scalar(
        "SELECT id FROM assets WHERE task_id=$1 AND storage_key=$2 AND kind::text=$3 ORDER BY created_at DESC LIMIT 1"
    ).bind(task_id).bind(key).bind(kind).fetch_optional(&mut *tx).await?;
    if existing.is_none() {
        sqlx::query(
            "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES($1,$2,$3::asset_kind,$4,$5,$6)"
        ).bind(Uuid::new_v4()).bind(task_id).bind(kind).bind(key).bind(&sha).bind(size)
            .execute(&mut *tx).await?;
    }
    if matches!(kind, "subtitle_srt" | "subtitle_ass") {
        let format = if kind == "subtitle_srt" { "srt" } else { "ass" };
        sqlx::query(
            "INSERT INTO subtitles(id,task_id,version,format,language,storage_key) SELECT $1,$2,$3,$4::subtitle_format,'zh',$5 WHERE NOT EXISTS(SELECT 1 FROM subtitles WHERE task_id=$2 AND version=$3 AND format::text=$4 AND storage_key=$5)"
        ).bind(Uuid::new_v4()).bind(task_id).bind(job.generation).bind(format).bind(key)
            .execute(&mut *tx).await?;
        sqlx::query("UPDATE tasks SET status='subtitle_ready'::task_status,updated_at=now() WHERE id=$1 AND status IN ('created'::task_status,'ingested'::task_status,'downloaded'::task_status,'audio_extracted'::task_status,'asr_done'::task_status,'translated'::task_status)")
            .bind(task_id).execute(&mut *tx).await?;
    }
    if kind == "video_final" {
        sqlx::query(
            "UPDATE tasks SET status='rendered'::task_status,updated_at=now() WHERE id=$1 AND status IN ('created'::task_status,'ingested'::task_status,'downloaded'::task_status,'audio_extracted'::task_status,'asr_done'::task_status,'translated'::task_status,'subtitle_ready'::task_status)"
        ).bind(task_id).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(json!({"storage_key":key,"sha256":sha,"size_bytes":size}))
}
/// Video rendering is native Rust orchestration; FFmpeg remains an external codec.
pub async fn render(
    config: &Config,
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
) -> AppResult<Value> {
    // A new selective subtitle version invalidates the previously burned-in
    // video; never reuse a video_final from an earlier subtitle generation.
    let selective: bool = sqlx::query_scalar(
        "SELECT configuration ? 'selective_retranslate'
        FROM vr2_workflows WHERE id=$1",
    )
    .bind(job.workflow_id)
    .fetch_one(&db.pool)
    .await?;
    let existing = if selective {
        None
    } else {
        latest_asset_key(db, job.task_id, "video_final").await?
    };
    if let Some(key) = existing
        && let Ok(path) = registry.resolve_existing(&key).await
        && probe_video(&config.ffprobe_binary, &path).await.is_ok()
    {
        return Ok(json!({"storage_key":key,"reused":true}));
    }
    let source_key = latest_asset_key(db, job.task_id, "video_raw")
        .await?
        .ok_or_else(|| AppError::NotFound("render input video_raw asset missing".into()))?;
    let input = registry.resolve_existing(&source_key).await?;
    probe_video(&config.ffprobe_binary, &input).await?;
    let cfg = profile(db, job).await?;
    let subtitle_key = latest_asset_key(db, job.task_id, "subtitle_ass")
        .await?
        .or(latest_asset_key(db, job.task_id, "subtitle_srt").await?);
    let subtitle = match subtitle_key {
        Some(key) => Some(registry.resolve_existing(&key).await?),
        None => None,
    };
    if !cfg.burn_in || subtitle.is_none() {
        // Reuse v2 original asset, keeping an explicit new reference.
        return ensure_task_asset(db, registry, job.task_id, job, "video_final", &source_key).await;
    }
    let output_key = format!("v2/render/{}/{}.mp4", job.task_id, job.id);
    let output = registry.resolve_existing(&output_key).await?;
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent).await?;
    }
    if fs::metadata(&output).await.is_ok()
        && probe_video(&config.ffprobe_binary, &output).await.is_ok()
    {
        return ensure_task_asset(db, registry, job.task_id, job, "video_final", &output_key).await;
    }
    let temp = output.with_extension("partial.mp4");
    let _ = fs::remove_file(&temp).await;
    transcode(config, &input, &temp, subtitle.as_deref(), &cfg).await?;
    probe_video(&config.ffprobe_binary, &temp).await?;
    fs::rename(&temp, &output).await?;
    ensure_task_asset(db, registry, job.task_id, job, "video_final", &output_key).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn supported_encoders_and_presets() {
        let src = Path::new("/tmp/source.mp4");
        let dst = Path::new("/tmp/output.mp4");
        let opts = RenderProfile::default();
        let args = encode_args(src, dst, None, &opts, None).unwrap();
        assert!(args.iter().any(|s| s == "libx264"));
        let mut av1 = opts;
        av1.video_codec = "av1".into();
        av1.use_intel_gpu = true;
        let args = encode_args(src, dst, None, &av1, Some("/dev/dri/renderD128")).unwrap();
        assert!(args.iter().any(|s| s == "av1_vaapi"));
        assert!(args.iter().any(|s| s == "/dev/dri/renderD128"));
        av1.video_codec = "rawvideo".into();
        assert!(encode_args(src, dst, None, &av1, None).is_err());
    }
    #[test]
    fn reject_filter_argument_injection() {
        assert!(ffmpeg_subtitle_filter(Path::new("/tmp/b;safe.srt")).is_ok());
        assert!(ffmpeg_subtitle_filter(Path::new("/tmp/b'a.srt")).is_err());
        assert!(ffmpeg_subtitle_filter(Path::new("/tmp/b:c.srt")).is_err());
    }
}
