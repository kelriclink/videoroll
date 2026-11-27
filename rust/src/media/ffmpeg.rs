//! Spawn FFmpeg in a separate OS process. No native FFmpeg pointers cross Rust lifetimes.
use crate::media::render_optimization as render_opt;
use crate::{AppError, AppResult, Config, Database, domain::WorkItem, storage::AssetRegistry};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Instant,
};
use tempfile::Builder;
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
        .is_some_and(|v| v["standalone_subtitle_runtime_profile"].as_bool()==Some(true)
            || v["legacy_auto_runtime_profile"].as_bool()==Some(true))
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
        .arg("format=duration,format_name:stream=codec_type,codec_name,width,height,pix_fmt,bits_per_raw_sample,avg_frame_rate,r_frame_rate")
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
    if s.chars().any(|c| {
        matches!(
            c,
            '\'' | ':' | '[' | ']' | ',' | ';' | '\\' | '\n' | '\r' | '='
        )
    }) {
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
                    | "placebo"
            )
        })
        .unwrap_or("medium");
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
    let crf = profile.video_crf.unwrap_or(23).min(if codec=="av1"{63}else{51});
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
        // Preserve the native default but honor the saved numeric SVT preset.
        let preset=profile.video_preset.as_deref().and_then(|s|s.trim().parse::<i32>().ok())
            .map(|n|n.clamp(0,13)).unwrap_or(8);
        args.push(preset.to_string());
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
#[derive(Debug, Clone)]
struct RenderInfo {
    width: u32,
    height: u32,
    duration_seconds: f64,
    frame_rate: String,
    pixel_format: String,
    bit_depth: u32,
}
fn render_info(probe: &Value) -> AppResult<RenderInfo> {
    let streams = probe
        .get("streams")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Unavailable("ffprobe video stream missing".into()))?;
    let stream = streams
        .iter()
        .find(|row| row.get("codec_type").and_then(Value::as_str) == Some("video"))
        .ok_or_else(|| AppError::Unavailable("ffprobe video stream missing".into()))?;
    let number = |key: &str| {
        stream
            .get(key)
            .and_then(Value::as_u64)
            .unwrap_or(0)
            .min(u32::MAX as u64) as u32
    };
    let duration = probe
        .get("format")
        .and_then(|v| v.get("duration"))
        .and_then(|v| {
            v.as_str()
                .and_then(|s| s.parse::<f64>().ok())
                .or_else(|| v.as_f64())
        })
        .filter(|v| v.is_finite() && *v > 0.0)
        .ok_or_else(|| AppError::Unavailable("ffprobe video duration missing".into()))?;
    let pixel_format = stream
        .get("pix_fmt")
        .and_then(Value::as_str)
        .unwrap_or("yuv420p")
        .to_owned();
    let raw_depth = stream.get("bits_per_raw_sample").and_then(|v| {
        v.as_str()
            .and_then(|s| s.parse::<u32>().ok())
            .or_else(|| v.as_u64().map(|n| n as u32))
    });
    let bit_depth = render_opt::pixel_format_bit_depth(&pixel_format, raw_depth);
    let frame_rate = ["avg_frame_rate", "r_frame_rate"]
        .iter()
        .filter_map(|key| stream.get(*key).and_then(Value::as_str))
        .find(|rate| render_opt::parse_rational(rate).is_some())
        .unwrap_or("30/1")
        .to_owned();
    let width = number("width");
    let height = number("height");
    if width == 0 || height == 0 {
        return Err(AppError::Unavailable(
            "ffprobe video dimensions invalid".into(),
        ));
    }
    Ok(RenderInfo {
        width,
        height,
        duration_seconds: duration,
        frame_rate,
        pixel_format,
        bit_depth,
    })
}
fn vaapi_device_args(device: &str) -> Vec<String> {
    vec![
        "-init_hw_device".into(),
        format!("vaapi=va:{device}"),
        "-filter_hw_device".into(),
        "va".into(),
    ]
}
fn vaapi_decode_args() -> Vec<String> {
    vec![
        "-hwaccel".into(),
        "vaapi".into(),
        "-hwaccel_device".into(),
        "va".into(),
        "-hwaccel_output_format".into(),
        "vaapi".into(),
    ]
}
fn ffmpeg_ass_filter(path: &Path) -> AppResult<String> {
    let filter = ffmpeg_subtitle_filter(path)?;
    let path = filter
        .strip_prefix("subtitles=")
        .ok_or_else(|| AppError::Invalid("invalid ASS filter path".into()))?;
    Ok(format!("ass={path}:alpha=1"))
}
async fn ffmpeg_filter_available(config: &Config, name: &str) -> bool {
    let output = Command::new(&config.ffmpeg_binary)
        .args(["-hide_banner", "-filters"])
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await;
    let Ok(output) = output else { return false };
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    text.lines()
        .any(|line| line.split_whitespace().nth(1) == Some(name))
}
async fn run_ffmpeg(config: &Config, args: &[String], label: &str) -> AppResult<()> {
    let output = Command::new(&config.ffmpeg_binary)
        .args(args)
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let detail = stderr
        .chars()
        .rev()
        .take(1600)
        .collect::<String>()
        .chars()
        .rev()
        .collect::<String>();
    tracing::warn!(operation=label,status=%output.status,stderr=%detail,"FFmpeg media operation failed");
    Err(AppError::Unavailable(format!(
        "FFmpeg {label} failed with {}",
        output.status
    )))
}
async fn vaapi_decode_preflight(
    config: &Config,
    source: &Path,
    device: &str,
    vaapi_format: &str,
    software_format: &str,
) -> bool {
    let mut args = vec![
        "-hide_banner".into(),
        "-nostdin".into(),
        "-v".into(),
        "error".into(),
    ];
    args.extend(vaapi_device_args(device));
    args.extend(vaapi_decode_args());
    args.extend([
        "-i".into(),
        source.to_string_lossy().to_string(),
        "-map".into(),
        "0:v:0".into(),
        "-frames:v".into(),
        "1".into(),
        "-an".into(),
        "-sn".into(),
        "-dn".into(),
        "-vf".into(),
        format!("scale_vaapi=format={vaapi_format},hwdownload,format={software_format}"),
        "-f".into(),
        "null".into(),
        "-".into(),
    ]);
    match run_ffmpeg(config, &args, "VAAPI decode preflight").await {
        Ok(()) => true,
        Err(error) => {
            tracing::warn!(error=%error,"VAAPI decode unavailable; using software decode and GPU upload");
            false
        }
    }
}
fn intel_h264_preset_quality(preset: Option<&str>) -> Option<u8> {
    match preset?.trim().to_ascii_lowercase().as_str() {
        "placebo" | "veryslow" => Some(1),
        "slower" => Some(2),
        "slow" => Some(3),
        "medium" => Some(4),
        "fast" => Some(5),
        "faster" => Some(6),
        "veryfast" => Some(7),
        "superfast" | "ultrafast" => Some(8),
        _ => None,
    }
}
fn intel_av1_preset_quality(preset: Option<&str>) -> Option<u8> {
    if let Some(value) = intel_h264_preset_quality(preset) {
        return Some(value);
    }
    let number = preset?.trim().parse::<i32>().ok()?.clamp(0, 13);
    Some(1 + ((number as f64 * 7.0 / 13.0).round() as u8))
}
fn vaapi_encoder_args(profile: &RenderProfile, codec: &str) -> Vec<String> {
    let quality = profile.video_crf.unwrap_or(23);
    let (encoder, flag, max_quality) = match codec {
        "h264" => ("h264_vaapi", "-qp", 51),
        "hevc" => ("hevc_vaapi", "-qp", 51),
        _ => ("av1_vaapi", "-global_quality", 63),
    };
    let mut args = vec![
        "-c:v".into(),
        encoder.into(),
        "-rc_mode".into(),
        "CQP".into(),
        flag.into(),
        quality.min(max_quality).to_string(),
    ];
    let preset_quality = match codec {
        "h264" => intel_h264_preset_quality(profile.video_preset.as_deref()),
        "av1" => intel_av1_preset_quality(profile.video_preset.as_deref()),
        _ => None,
    };
    if let Some(preset) = preset_quality {
        args.extend(["-quality".into(), preset.to_string()]);
    }
    args
}

struct EventCache {
    timeline: PathBuf,
    frame_count: usize,
    cache_bytes: u64,
    build_seconds: f64,
    savings_ratio: f64,
}
async fn build_event_cache(
    config: &Config,
    ass_path: &Path,
    ass_text: &str,
    width: u32,
    overlay_height: u32,
    overlay_rate: &str,
    duration: f64,
    work_dir: &Path,
) -> AppResult<Option<EventCache>> {
    let (plan, reason) = render_opt::plan_event_cache(ass_text, overlay_rate, duration);
    let Some(plan) = plan else {
        tracing::info!(reason=%reason,"ASS event cache skipped");
        return Ok(None);
    };
    let started = Instant::now();
    let dir = work_dir.join("ass-event-cache");
    fs::create_dir_all(&dir).await?;
    let blank = dir.join("blank.png");
    let clock = dir.join("clock.ffconcat");
    let blank_args = vec![
        "-hide_banner".into(),
        "-nostdin".into(),
        "-loglevel".into(),
        "error".into(),
        "-y".into(),
        "-f".into(),
        "lavfi".into(),
        "-i".into(),
        format!("color=c=black@0.0:s={width}x{overlay_height}:r=1,format=rgba"),
        "-frames:v".into(),
        "1".into(),
        blank.to_string_lossy().to_string(),
    ];
    run_ffmpeg(config, &blank_args, "ASS event cache blank frame").await?;
    let mut clock_lines = vec!["ffconcat version 1.0".to_owned()];
    for (index, boundary) in plan.boundaries_us.iter().enumerate() {
        clock_lines.push("file blank.png".into());
        clock_lines.push("option framerate 1000".into());
        if let Some(next) = plan.boundaries_us.get(index + 1) {
            let delta = next.saturating_sub(*boundary);
            if delta == 0 {
                return Err(AppError::Invalid(
                    "ASS event timestamps are not strictly increasing".into(),
                ));
            }
            clock_lines.push(format!("duration {:.6}", delta as f64 / 1_000_000.0));
        }
    }
    fs::write(&clock, clock_lines.join("\n") + "\n").await?;
    let ass_filter = ffmpeg_ass_filter(ass_path)?;
    let pattern = dir.join("state_%06d.png");
    let render_args = vec![
        "-hide_banner".into(),
        "-nostdin".into(),
        "-loglevel".into(),
        "error".into(),
        "-y".into(),
        "-f".into(),
        "concat".into(),
        "-safe".into(),
        "0".into(),
        "-i".into(),
        clock.to_string_lossy().to_string(),
        "-vf".into(),
        format!("{ass_filter},format=rgba"),
        "-fps_mode".into(),
        "passthrough".into(),
        pattern.to_string_lossy().to_string(),
    ];
    run_ffmpeg(config, &render_args, "ASS event cache rasterization").await?;
    let mut entries = fs::read_dir(&dir).await?;
    let mut frames = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        let path = entry.path();
        let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
        if name.starts_with("state_") && name.ends_with(".png") {
            frames.push(path)
        }
    }
    frames.sort();
    if frames.len() != plan.boundaries_us.len() {
        return Err(AppError::Unavailable(format!(
            "ASS event cache frame count mismatch: expected={} actual={}",
            plan.boundaries_us.len(),
            frames.len()
        )));
    }
    let mut cache_bytes = 0_u64;
    for path in &frames {
        cache_bytes = cache_bytes.saturating_add(fs::metadata(path).await?.len());
    }
    if cache_bytes > 256 * 1024 * 1024 {
        tracing::warn!(
            cache_bytes,
            "ASS event cache exceeds 256 MiB; using live overlay"
        );
        return Ok(None);
    }
    let mut timeline_lines = vec!["ffconcat version 1.0".to_owned()];
    for (index, path) in frames.iter().enumerate() {
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or_else(|| AppError::Invalid("invalid event cache image filename".into()))?;
        if !name.starts_with("state_") || !name.ends_with(".png") {
            return Err(AppError::Invalid(
                "unsafe event cache image filename".into(),
            ));
        }
        timeline_lines.push(format!("file {name}"));
        timeline_lines.push("option framerate 1000".into());
        if let Some(next) = plan.boundaries_us.get(index + 1) {
            timeline_lines.push(format!(
                "duration {:.6}",
                next.saturating_sub(plan.boundaries_us[index]) as f64 / 1_000_000.0
            ));
        }
    }
    let timeline = dir.join("overlay_events.ffconcat");
    fs::write(&timeline, timeline_lines.join("\n") + "\n").await?;
    Ok(Some(EventCache {
        timeline,
        frame_count: frames.len(),
        cache_bytes,
        build_seconds: started.elapsed().as_secs_f64(),
        savings_ratio: plan.savings_ratio,
    }))
}
async fn run_vaapi_overlay(
    config: &Config,
    source: &Path,
    output: &Path,
    device: &str,
    hw_decode: bool,
    vaapi_format: &str,
    software_format: &str,
    profile: &RenderProfile,
    codec: &str,
    video_width: u32,
    overlay_height: u32,
    overlay_y: u32,
    overlay_rate: &str,
    ass_path: Option<&Path>,
    timeline: Option<&Path>,
) -> AppResult<()> {
    let mut args = vec![
        "-hide_banner".into(),
        "-nostdin".into(),
        "-y".into(),
        "-v".into(),
        "warning".into(),
    ];
    args.extend(vaapi_device_args(device));
    if hw_decode {
        args.extend(vaapi_decode_args())
    }
    args.extend(["-i".into(), source.to_string_lossy().to_string()]);
    if let Some(path) = timeline {
        args.extend([
            "-f".into(),
            "concat".into(),
            "-safe".into(),
            "0".into(),
            "-i".into(),
            path.to_string_lossy().to_string(),
        ]);
    } else {
        args.extend(["-f".into(),"lavfi".into(),"-i".into(),format!("color=c=black@0.0:s={video_width}x{overlay_height}:r={overlay_rate},format=yuva420p")]);
    }
    let main = if hw_decode {
        format!("scale_vaapi=format={vaapi_format}")
    } else {
        format!("format={software_format},hwupload")
    };
    let graph = if timeline.is_some() {
        format!(
            "[0:v]{main}[main];[1:v]format=bgra,hwupload[sub];[main][sub]overlay_vaapi=x=0:y={overlay_y}:shortest=0:repeatlast=1:eof_action=repeat[out]"
        )
    } else {
        let ass = ffmpeg_ass_filter(
            ass_path.ok_or_else(|| AppError::Invalid("live overlay ASS path missing".into()))?,
        )?;
        format!(
            "[0:v]{main}[main];[1:v]{ass},format=bgra,hwupload[sub];[main][sub]overlay_vaapi=x=0:y={overlay_y}:shortest=1[out]"
        )
    };
    args.extend([
        "-filter_complex".into(),
        graph,
        "-map".into(),
        "[out]".into(),
    ]);
    args.extend(vaapi_encoder_args(profile, codec));
    args.extend([
        "-map".into(),
        "0:a?".into(),
        "-c:a".into(),
        "aac".into(),
        "-b:a".into(),
        "192k".into(),
        "-movflags".into(),
        "+faststart".into(),
        output.to_string_lossy().to_string(),
    ]);
    run_ffmpeg(config, &args, "VAAPI subtitle overlay").await
}
async fn run_vaapi_legacy(
    config: &Config,
    source: &Path,
    output: &Path,
    subtitle: Option<&Path>,
    device: &str,
    hw_decode: bool,
    vaapi_format: &str,
    software_format: &str,
    profile: &RenderProfile,
    codec: &str,
) -> AppResult<()> {
    let mut args = vec![
        "-hide_banner".into(),
        "-nostdin".into(),
        "-y".into(),
        "-v".into(),
        "warning".into(),
    ];
    args.extend(vaapi_device_args(device));
    if hw_decode {
        args.extend(vaapi_decode_args())
    }
    args.extend(["-i".into(), source.to_string_lossy().to_string()]);
    let filter = if let Some(sub) = subtitle {
        let sub = ffmpeg_subtitle_filter(sub)?;
        if hw_decode {
            format!(
                "scale_vaapi=format={vaapi_format},hwdownload,format={software_format},{sub},format={software_format},hwupload"
            )
        } else {
            format!("{sub},format={software_format},hwupload")
        }
    } else if hw_decode {
        format!("scale_vaapi=format={vaapi_format}")
    } else {
        format!("format={software_format},hwupload")
    };
    args.extend(["-vf".into(), filter]);
    args.extend(vaapi_encoder_args(profile, codec));
    args.extend([
        "-map".into(),
        "0:v:0".into(),
        "-map".into(),
        "0:a?".into(),
        "-c:a".into(),
        "aac".into(),
        "-b:a".into(),
        "192k".into(),
        "-movflags".into(),
        "+faststart".into(),
        output.to_string_lossy().to_string(),
    ]);
    run_ffmpeg(config, &args, "VAAPI legacy subtitle render").await
}
async fn render_intel_gpu(
    config: &Config,
    source: &Path,
    output: &Path,
    subtitle: Option<&Path>,
    profile: &RenderProfile,
    device: &str,
) -> AppResult<Value> {
    let probe = probe_video(&config.ffprobe_binary, source).await?;
    let info = render_info(&probe)?;
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
    let preserve_10bit = info.bit_depth > 8 && codec == "av1";
    let vaapi_format = if preserve_10bit { "p010" } else { "nv12" };
    let software_format = if preserve_10bit { "p010le" } else { "nv12" };
    let hw_decode =
        vaapi_decode_preflight(config, source, device, vaapi_format, software_format).await;
    let overlay_available = ffmpeg_filter_available(config, "overlay_vaapi").await;
    let mut metrics = json!({"encoder":format!("{codec}_vaapi"),"decode_mode":if hw_decode{"vaapi"}else{"software-upload"},"source_bit_depth":info.bit_depth,"output_bit_depth":if preserve_10bit{10}else{8},"source_pixel_format":info.pixel_format,"source_frame_rate":info.frame_rate,"source_duration_seconds":info.duration_seconds,"video_width":info.width,"video_height":info.height,"overlay_vaapi_available":overlay_available});
    if let Some(subtitle) = subtitle.filter(|_| overlay_available) {
        let ass_text = fs::read_to_string(subtitle).await?;
        let parent = output.parent().unwrap_or_else(|| Path::new("."));
        let work = Builder::new()
            .prefix("videoroll-render-")
            .tempdir_in(parent)?;
        let band = render_opt::prepare_overlay_band(&ass_text, info.width, info.height);
        let (overlay_ass, overlay_height, band_used) = if let Some((script, height)) = band {
            let path = work.path().join("subtitle-band.ass");
            fs::write(&path, script).await?;
            (path, height, true)
        } else {
            (subtitle.to_path_buf(), info.height, false)
        };
        let static_overlay = band_used && render_opt::ass_is_static(&ass_text);
        let overlay_rate = if static_overlay {
            render_opt::reduced_overlay_rate(&info.frame_rate, 15.0)
        } else {
            info.frame_rate.clone()
        };
        let overlay_y = info.height.saturating_sub(overlay_height);
        let mut event_cache = None;
        if static_overlay {
            let (cache_plan, cache_reason) =
                render_opt::plan_event_cache(&ass_text, &overlay_rate, info.duration_seconds);
            if cache_plan.is_none() {
                metrics["event_cache_skip_reason"] = json!(cache_reason);
            }
            match build_event_cache(
                config,
                &overlay_ass,
                &ass_text,
                info.width,
                overlay_height,
                &overlay_rate,
                info.duration_seconds,
                work.path(),
            )
            .await
            {
                Ok(cache) => event_cache = cache,
                Err(error) => {
                    metrics["event_cache_build_fallback"] = json!(error.to_string());
                    tracing::warn!(error=%error,"ASS event cache failed; using live VAAPI overlay")
                }
            }
        }
        if let Some(cache) = event_cache.as_ref() {
            match run_vaapi_overlay(
                config,
                source,
                output,
                device,
                hw_decode,
                vaapi_format,
                software_format,
                profile,
                codec,
                info.width,
                overlay_height,
                overlay_y,
                &overlay_rate,
                None,
                Some(&cache.timeline),
            )
            .await
            {
                Ok(()) => {
                    metrics["subtitle_overlay_mode"] = json!("event-vfr");
                    metrics["subtitle_overlay_fps"] = json!(overlay_rate);
                    metrics["subtitle_event_cache_states"] = json!(cache.frame_count);
                    metrics["subtitle_event_cache_bytes"] = json!(cache.cache_bytes);
                    metrics["subtitle_event_cache_build_seconds"] = json!(cache.build_seconds);
                    metrics["subtitle_event_cache_savings_ratio"] = json!(cache.savings_ratio);
                    return Ok(metrics);
                }
                Err(error) => {
                    metrics["event_cache_overlay_fallback"] = json!(error.to_string());
                    tracing::warn!(error=%error,"ASS event-cache overlay failed; retrying live overlay");
                    let _ = fs::remove_file(output).await;
                }
            }
        }
        match run_vaapi_overlay(
            config,
            source,
            output,
            device,
            hw_decode,
            vaapi_format,
            software_format,
            profile,
            codec,
            info.width,
            overlay_height,
            overlay_y,
            &overlay_rate,
            Some(&overlay_ass),
            None,
        )
        .await
        {
            Ok(()) => {
                metrics["subtitle_overlay_mode"] = json!(if static_overlay {
                    "live-static"
                } else if band_used {
                    "live-band-dynamic"
                } else {
                    "live-full-frame"
                });
                metrics["subtitle_overlay_fps"] = json!(overlay_rate);
                return Ok(metrics);
            }
            Err(error) => {
                metrics["live_overlay_fallback"] = json!(error.to_string());
                tracing::warn!(error=%error,"live VAAPI overlay failed; retrying legacy CPU ASS composition");
                let _ = fs::remove_file(output).await;
            }
        }
        run_vaapi_legacy(
            config,
            source,
            output,
            Some(subtitle),
            device,
            hw_decode,
            vaapi_format,
            software_format,
            profile,
            codec,
        )
        .await?;
        metrics["subtitle_overlay_mode"] = json!("legacy-full-frame-fallback");
        return Ok(metrics);
    }
    run_vaapi_legacy(
        config,
        source,
        output,
        subtitle,
        device,
        hw_decode,
        vaapi_format,
        software_format,
        profile,
        codec,
    )
    .await?;
    metrics["subtitle_overlay_mode"] = json!(if subtitle.is_some() {
        "legacy-full-frame"
    } else {
        "none"
    });
    Ok(metrics)
}
fn render_gpu_auto_enabled() -> bool {
    let value = std::env::var("VIDEOROLL_V2_RENDER_GPU_AUTO")
        .ok()
        .map(|value| value.to_ascii_lowercase());
    matches!(value.as_deref(), Some("true" | "1" | "yes" | "on"))
}
fn render_gpu_requested(
    profile_explicit: bool,
    device_available: bool,
    auto_enabled: bool,
) -> bool {
    profile_explicit || (device_available && auto_enabled)
}
pub async fn transcode(
    config: &Config,
    source: &Path,
    output: &Path,
    subtitles: Option<&Path>,
    profile: &RenderProfile,
) -> AppResult<Value> {
    let device = std::env::var("INTEL_GPU_RENDER_DEVICE")
        .ok()
        .filter(|s| s.starts_with("/dev/dri/renderD") && Path::new(s).exists());
    let gpu_requested = render_gpu_requested(
        profile.use_intel_gpu,
        device.is_some(),
        render_gpu_auto_enabled(),
    );
    let mut gpu_fallback_reason = None;
    if gpu_requested {
        if let Some(device) = device.as_deref() {
            match render_intel_gpu(config, source, output, subtitles, profile, device).await {
                Ok(mut metrics) => {
                    metrics["gpu_requested"] = json!(gpu_requested);
                    metrics["gpu_device"] = json!(device);
                    metrics["gpu_auto_enabled"] = json!(render_gpu_auto_enabled());
                    return Ok(metrics);
                }
                Err(error) => {
                    gpu_fallback_reason = Some(error.to_string());
                    tracing::warn!(error=%error,"optimized VAAPI render failed; falling back to software encoding");
                    let _ = fs::remove_file(output).await;
                }
            }
        } else {
            gpu_fallback_reason = Some(
                "INTEL_GPU_RENDER_DEVICE is unset or is not an existing /dev/dri/renderD* node"
                    .into(),
            );
        }
    }
    let mut cpu_profile = profile.clone();
    cpu_profile.use_intel_gpu = false;
    let args = encode_args(source, output, subtitles, &cpu_profile, None)?;
    run_ffmpeg(config, &args, "software render").await?;
    let mut metrics = json!({
        "encoder":match cpu_profile.video_codec.as_str(){"av1"=>"libsvtav1","hevc"|"h265"=>"libx265",_=>"libx264"},
        "pipeline":"cpu-ass-software-encoder-fallback",
        "decode_mode":"software",
        "subtitle_overlay_mode":if subtitles.is_some(){"cpu-libass"}else{"none"},
        "gpu_requested":gpu_requested
    });
    if let Some(reason) = gpu_fallback_reason {
        metrics["gpu_fallback_reason"] = json!(reason);
    }
    Ok(metrics)
}
pub(crate) async fn ensure_task_asset(
    db: &Database,
    registry: &AssetRegistry,
    task_id: Uuid,
    job: &WorkItem,
    kind: &str,
    key: &str,
) -> AppResult<Value> {
    commit_task_asset(db, registry, task_id, job, kind, key, None).await
}
/// Install a fully written attempt-private file under the same lease and
/// object locks as its ledger entry. A stale attempt never renames over the
/// canonical output. A failed DB commit leaves only recoverable local bytes.
pub(crate) async fn install_task_asset(
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
    kind: &str,
    key: &str,
    prepared: &Path,
) -> AppResult<Value> {
    commit_task_asset(db, registry, job.task_id, job, kind, key, Some(prepared)).await
}
pub(crate) async fn write_task_asset(
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
    kind: &str,
    key: &str,
    body: &[u8],
) -> AppResult<Value> {
    use tokio::io::AsyncWriteExt;
    let dest = registry.resolve_existing(key).await?;
    let parent = dest
        .parent()
        .ok_or_else(|| AppError::Invalid("asset parent missing".into()))?;
    fs::create_dir_all(parent).await?;
    let temp = Builder::new()
        .prefix(".asset-attempt-")
        .suffix(".vr2-upload.partial")
        .tempfile_in(parent)?;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .truncate(true)
        .open(temp.path())
        .await?;
    file.write_all(body).await?;
    file.sync_all().await?;
    drop(file);
    install_task_asset(db, registry, job, kind, key, temp.path()).await
}
async fn commit_task_asset(
    db: &Database,
    registry: &AssetRegistry,
    task_id: Uuid,
    job: &WorkItem,
    kind: &str,
    key: &str,
    prepared: Option<&Path>,
) -> AppResult<Value> {
    let key = AssetRegistry::checked_key(key)?;
    let dest = registry.resolve_existing(key).await?;
    let (sha, size) = if let Some(prepared) = prepared {
        if prepared.parent() != dest.parent() || prepared == dest {
            return Err(AppError::Invalid(
                "asset attempt path must be private and colocated".into(),
            ));
        }
        let name = prepared
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or_else(|| AppError::Invalid("asset attempt filename invalid".into()))?;
        let temp_key = Path::new(key).with_file_name(name);
        let temp_key = temp_key
            .to_str()
            .ok_or_else(|| AppError::Invalid("asset attempt key invalid".into()))?;
        if registry.resolve_existing(temp_key).await? != prepared {
            return Err(AppError::Invalid("asset attempt identity mismatch".into()));
        }
        registry.sha256(temp_key).await?
    } else {
        registry.sha256(key).await?
    };
    let mut tx = db.pool.begin().await?;
    // Only the current stage can publish an asset. Match the scheduler's
    // Task -> Workflow -> Job -> storage object lock order.
    if task_id != job.task_id {
        return Err(AppError::Conflict("asset task identity mismatch".into()));
    }
    let active:Option<Uuid>=sqlx::query_scalar("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust' AND status<>'canceled'::task_status FOR UPDATE").bind(task_id).fetch_optional(&mut *tx).await?;
    if active.is_none() {
        return Err(AppError::Conflict("asset task stopped".into()));
    }
    let active:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_workflows WHERE id=$1 AND task_id=$2 AND generation=$3 AND current_stage=$4 AND state='running' FOR UPDATE").bind(job.workflow_id).bind(task_id).bind(job.generation).bind(job.stage.as_str()).fetch_optional(&mut *tx).await?;
    if active.is_none() {
        return Err(AppError::Conflict(
            "asset workflow stopped or superseded".into(),
        ));
    }
    let active:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_jobs WHERE id=$1 AND task_id=$2 AND workflow_id=$3 AND generation=$4 AND stage=$5 AND state='running' AND lease_token=$6 AND lease_until>clock_timestamp() FOR UPDATE").bind(job.id).bind(task_id).bind(job.workflow_id).bind(job.generation).bind(job.stage.as_str()).bind(job.lease_token).fetch_optional(&mut *tx).await?;
    if active.is_none() {
        return Err(AppError::Conflict("asset stage lease expired".into()));
    }
    let registered=sqlx::query(
        "INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days') ON CONFLICT(storage_key) DO UPDATE SET retain_until=GREATEST(storage_objects.retain_until,now()+interval '2 days') WHERE storage_objects.state='live' AND storage_objects.checksum_sha256=EXCLUDED.checksum_sha256 AND storage_objects.size_bytes=EXCLUDED.size_bytes"
    ).bind(key).bind(&sha).bind(size).execute(&mut *tx).await?;
    if registered.rows_affected() != 1 {
        return Err(AppError::Conflict(
            "asset storage identity mismatch or GC fenced".into(),
        ));
    }
    let mismatch:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE storage_key=$1 AND ((sha256 IS NOT NULL AND sha256<>$2) OR (size_bytes IS NOT NULL AND size_bytes<>$3)))").bind(key).bind(&sha).bind(size).fetch_one(&mut *tx).await?;
    if mismatch {
        return Err(AppError::Conflict(
            "original asset checksum or size mismatch".into(),
        ));
    }
    let active:bool=sqlx::query_scalar("SELECT COALESCE(lease_until>clock_timestamp(),false) FROM vr2_jobs WHERE id=$1 AND lease_token=$2 AND state='running'").bind(job.id).bind(job.lease_token).fetch_one(&mut *tx).await?;
    if !active {
        return Err(AppError::Conflict(
            "asset lease expired before commit".into(),
        ));
    }
    if let Some(prepared) = prepared {
        // Do not dispatch an asynchronous rename that could outlive the
        // transaction on cancellation. The only potentially long IO (copy,
        // encode, hash, fsync of the file) already happened outside the lock.
        std::fs::rename(prepared, &dest)?;
        if let Some(parent) = dest.parent() {
            std::fs::File::open(parent)?.sync_all()?;
        }
    }
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
pub(crate) fn is_soft_sub_media(probe:&Value)->bool{
    probe["format"]["format_name"].as_str().is_some_and(|s|s.split(',').any(|f|f=="matroska"))
        && probe["streams"].as_array().is_some_and(|s|s.iter().any(|v|v["codec_type"]=="subtitle" && v["codec_name"]=="subrip"))
}
async fn mux_soft_sub(config:&Config,input:&Path,srt:&Path,output:&Path)->AppResult<Value>{
    let args=vec!["-hide_banner".into(),"-nostdin".into(),"-y".into(),"-v".into(),"error".into(),
        "-i".into(),input.to_string_lossy().into_owned(),"-i".into(),srt.to_string_lossy().into_owned(),
        "-map".into(),"0:v:0".into(),"-map".into(),"0:a?".into(),"-map".into(),"1:0".into(),
        "-c:v".into(),"copy".into(),"-c:a".into(),"copy".into(),"-c:s".into(),"srt".into(),
        "-metadata:s:s:0".into(),"language=chi".into(),"-f".into(),"matroska".into(),output.to_string_lossy().into_owned()];
    run_ffmpeg(config,&args,"soft subtitle mux").await?;
    let probe=probe_video(&config.ffprobe_binary,output).await?;
    if !is_soft_sub_media(&probe){return Err(AppError::Conflict("soft subtitle output lacks MKV/SRT track".into()))}
    Ok(json!({"encoder":"copy","pipeline":"soft_sub_mkv","subtitle_overlay_mode":"soft_sub"}))
}
pub async fn render(
    config: &Config,
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
) -> AppResult<Value> {
    // A new selective subtitle version invalidates the previously burned-in
    // video; never reuse a video_final from an earlier subtitle generation.
    let selective: bool = sqlx::query_scalar(
        "SELECT configuration ? 'selective_retranslate' OR configuration ? 'subtitle_resume_plan' OR configuration ? 'after_render_publish'
        FROM vr2_workflows WHERE id=$1",
    )
    .bind(job.workflow_id)
    .fetch_one(&db.pool)
    .await?;
    let cfg = profile(db, job).await?;
    let existing = if selective || cfg.soft_sub {
        None
    } else {
        latest_asset_key(db, job.task_id, "video_final").await?
    };
    if let Some(key) = existing
        && let Ok(path) = registry.resolve_existing(&key).await
        && probe_video(&config.ffprobe_binary, &path).await.is_ok()
    {
        let mut result =
            ensure_task_asset(db, registry, job.task_id, job, "video_final", &key).await?;
        result["reused"] = json!(true);
        return Ok(result);
    }
    let pinned =
        crate::subtitle_resume_v1::render_inputs(db, registry, job.task_id, job.workflow_id)
            .await?;
    let explicit =
        crate::social_publish::explicit_render_inputs(db, registry, job.task_id, job.workflow_id)
            .await?;
    let source_key = if let Some((raw, _, _)) = &explicit {
        raw.clone()
    } else if let Some((raw, _, _)) = &pinned {
        raw.clone()
    } else {
        latest_asset_key(db, job.task_id, "video_raw")
            .await?
            .ok_or_else(|| AppError::NotFound("render input video_raw asset missing".into()))?
    };
    let input = registry.resolve_existing(&source_key).await?;
    probe_video(&config.ffprobe_binary, &input).await?;
    let (ass,srt)=if let Some((_,ass,srt))=explicit{(ass,srt)}
        else if let Some((_,ass,srt))=pinned{(Some(ass),Some(srt))}
        else{(latest_asset_key(db,job.task_id,"subtitle_ass").await?,
              latest_asset_key(db,job.task_id,"subtitle_srt").await?)};
    // Match the remote worker: hard subtitles take precedence when both
    // switches are on; otherwise mux a separate SRT track.
    let burn=cfg.burn_in && (ass.is_some() || srt.is_some());
    let soft=!burn && cfg.soft_sub && srt.is_some();
    let subtitle_key=if burn{ass.or(srt)}else if soft{srt}else{None};
    let subtitle = match subtitle_key {
        Some(key) => Some(registry.resolve_existing(&key).await?),
        None => None,
    };
    if !burn && !soft {
        // Reuse v2 original asset, keeping an explicit new reference.
        return ensure_task_asset(db, registry, job.task_id, job, "video_final", &source_key).await;
    }
    let extension=if soft{"mkv"}else{"mp4"};
    let output_key = format!("v2/render/{}/{}.{extension}", job.task_id, job.id);
    let output = registry.resolve_existing(&output_key).await?;
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent).await?;
    }
    if fs::metadata(&output).await.is_ok()
        && probe_video(&config.ffprobe_binary, &output).await.is_ok_and(|v|!soft || is_soft_sub_media(&v))
    {
        return ensure_task_asset(db, registry, job.task_id, job, "video_final", &output_key).await;
    }
    let temp = Builder::new()
        .prefix(".render-attempt-")
        .suffix(&format!(".partial.{extension}"))
        .tempfile_in(
            output
                .parent()
                .ok_or_else(|| AppError::Invalid("render output parent missing".into()))?,
        )?;
    let render_metrics = if soft{
        mux_soft_sub(config,&input,subtitle.as_deref().ok_or_else(||AppError::Conflict("soft subtitle input missing".into()))?,temp.path()).await?
    }else{transcode(config, &input, temp.path(), subtitle.as_deref(), &cfg).await?};
    probe_video(&config.ffprobe_binary, temp.path()).await?;
    fs::File::open(temp.path()).await?.sync_all().await?;
    let mut result =
        install_task_asset(db, registry, job, "video_final", &output_key, temp.path()).await?;
    result["render_metrics"] = render_metrics;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn configured_intel_render_node_is_used_automatically_unless_disabled() {
        assert!(render_gpu_requested(false, true, true));
        assert!(!render_gpu_requested(false, true, false));
        assert!(!render_gpu_requested(false, false, true));
        assert!(render_gpu_requested(true, false, false));
    }
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
        av1.use_intel_gpu=false;
        av1.video_preset=Some("4".into());
        av1.video_crf=Some(60);
        let args=encode_args(src,dst,None,&av1,None).unwrap();
        assert!(args.windows(2).any(|v|v[0]=="-preset" && v[1]=="4"));
        assert!(args.windows(2).any(|v|v[0]=="-crf" && v[1]=="60"));
        let args=vaapi_encoder_args(&av1,"av1");
        assert!(args.windows(2).any(|v|v[0]=="-global_quality" && v[1]=="60"));
        av1.video_codec="h264".into();
        av1.video_preset=Some("placebo".into());
        let args=encode_args(src,dst,None,&av1,None).unwrap();
        assert!(args.windows(2).any(|v|v[0]=="-crf" && v[1]=="51"));
        assert!(args.windows(2).any(|v|v[0]=="-preset" && v[1]=="placebo"));
        av1.video_crf=Some(0);
        assert!(encode_args(src,dst,None,&av1,None).unwrap().windows(2).any(|v|v[0]=="-crf" && v[1]=="0"));
        av1.video_codec = "rawvideo".into();
        assert!(encode_args(src, dst, None, &av1, None).is_err());
    }
    // Opt-in benchmark: no DB connection, no production jobs, identical fixture
    // and encoder quality as the Python reference.
    #[tokio::test]
    async fn vaapi_render_benchmark() {
        let Ok(root) = std::env::var("VIDEOROLL_RENDER_BENCH_ROOT") else {
            return;
        };
        let config = Config::from_env().expect("isolated benchmark config");
        let root = PathBuf::from(root);
        let profile = RenderProfile {
            video_codec: "av1".into(),
            use_intel_gpu: true,
            video_crf: Some(24),
            video_preset: Some("4".into()),
            ..RenderProfile::default()
        };
        for name in ["1080p60", "4k60"] {
            let source = root.join(format!("{name}.mp4"));
            let ass = root.join(format!("{name}.ass"));
            let output = root.join(format!("{name}-rust.mp4"));
            let start = Instant::now();
            let metrics = transcode(&config, &source, &output, Some(&ass), &profile)
                .await
                .unwrap();
            let elapsed = start.elapsed().as_secs_f64();
            assert_eq!(metrics["subtitle_overlay_mode"], "event-vfr", "{metrics}");
            assert_eq!(metrics["decode_mode"], "vaapi", "{metrics}");
            println!(
                "BENCH {}",
                json!({"engine":"rust","fixture":name,"seconds":elapsed,"metrics":metrics})
            );
        }
    }
    #[tokio::test]
    async fn vaapi_two_concurrent_render_benchmark() {
        let Ok(root) = std::env::var("VIDEOROLL_RENDER_BENCH_ROOT") else {
            return;
        };
        let config = Config::from_env().expect("isolated benchmark config");
        let root = PathBuf::from(root);
        let source = root.join("4k60.mp4");
        let ass = root.join("4k60.ass");
        let a = root.join("4k60-concurrent-a.mp4");
        let b = root.join("4k60-concurrent-b.mp4");
        let profile = RenderProfile {
            video_codec: "av1".into(),
            use_intel_gpu: true,
            video_crf: Some(24),
            video_preset: Some("4".into()),
            ..RenderProfile::default()
        };
        let start = Instant::now();
        let (ra, rb) = tokio::join!(
            transcode(&config, &source, &a, Some(&ass), &profile),
            transcode(&config, &source, &b, Some(&ass), &profile)
        );
        let elapsed = start.elapsed().as_secs_f64();
        for result in [ra.unwrap(), rb.unwrap()] {
            assert_eq!(result["subtitle_overlay_mode"], "event-vfr", "{result}");
            assert_eq!(result["decode_mode"], "vaapi", "{result}");
        }
        println!(
            "CONCURRENT_BENCH {}",
            json!({"jobs":2,"fixture":"4k60","seconds":elapsed})
        );
    }
    #[tokio::test]
    async fn vaapi_event_cache_hardware_smoke() {
        if std::env::var_os("VIDEOROLL_RUN_VAAPI_SMOKE").is_none() {
            return;
        }
        let config = Config::from_env().expect("isolated smoke config");
        let source =
            PathBuf::from(std::env::var("VIDEOROLL_RENDER_SMOKE_SOURCE").expect("smoke source"));
        let subtitle =
            PathBuf::from(std::env::var("VIDEOROLL_RENDER_SMOKE_ASS").expect("smoke ASS"));
        let output_dir = Builder::new()
            .prefix("videoroll-vaapi-smoke-")
            .tempdir()
            .unwrap();
        let output = output_dir.path().join("event-cache.mp4");
        let profile = RenderProfile {
            burn_in: true,
            soft_sub: false,
            video_codec: "h264".into(),
            use_intel_gpu: false,
            video_crf: Some(23),
            video_preset: Some("fast".into()),
            ass_style: "clean_white".into(),
        };
        let metrics = transcode(&config, &source, &output, Some(&subtitle), &profile)
            .await
            .unwrap();
        assert_eq!(
            metrics["subtitle_overlay_mode"], "event-vfr",
            "metrics={metrics}"
        );
        assert_eq!(metrics["decode_mode"], "vaapi");
        assert!(metrics["subtitle_event_cache_states"].as_u64().unwrap_or(0) >= 2);
        let probe = probe_video(&config.ffprobe_binary, &output).await.unwrap();
        assert!(
            probe["streams"]
                .as_array()
                .unwrap()
                .iter()
                .any(|stream| stream["codec_type"] == "video")
        );
        let mut dynamic_text = std::fs::read_to_string(&subtitle).unwrap();
        dynamic_text = dynamic_text.replace(
            "First static subtitle",
            "{\\fad(100,100)}First animated subtitle",
        );
        let dynamic_ass = output_dir.path().join("dynamic.ass");
        std::fs::write(&dynamic_ass, dynamic_text).unwrap();
        let dynamic_output = output_dir.path().join("live-dynamic.mp4");
        let dynamic_metrics = transcode(
            &config,
            &source,
            &dynamic_output,
            Some(&dynamic_ass),
            &profile,
        )
        .await
        .unwrap();
        assert_eq!(
            dynamic_metrics["subtitle_overlay_mode"], "live-band-dynamic",
            "metrics={dynamic_metrics}"
        );
        assert_eq!(dynamic_metrics["subtitle_overlay_fps"], "60/1");
    }
    #[tokio::test]
    async fn vaapi_p010_av1_smoke() {
        if std::env::var_os("VIDEOROLL_RUN_VAAPI_SMOKE").is_none() {
            return;
        }
        let Ok(source) = std::env::var("VIDEOROLL_RENDER_SMOKE_10BIT_SOURCE") else {
            return;
        };
        let config = Config::from_env().expect("isolated smoke config");
        let subtitle =
            PathBuf::from(std::env::var("VIDEOROLL_RENDER_SMOKE_ASS").expect("smoke ASS"));
        let output_dir = Builder::new()
            .prefix("videoroll-vaapi-p010-smoke-")
            .tempdir()
            .unwrap();
        let output = output_dir.path().join("p010-av1.mp4");
        let profile = RenderProfile {
            burn_in: true,
            soft_sub: false,
            video_codec: "av1".into(),
            use_intel_gpu: false,
            video_crf: Some(24),
            video_preset: Some("4".into()),
            ass_style: "clean_white".into(),
        };
        let metrics = transcode(
            &config,
            Path::new(&source),
            &output,
            Some(&subtitle),
            &profile,
        )
        .await
        .unwrap();
        assert_eq!(metrics["output_bit_depth"], 10, "metrics={metrics}");
        let probe = probe_video(&config.ffprobe_binary, &output).await.unwrap();
        assert!(probe["streams"].as_array().unwrap().iter().any(|stream|stream["codec_type"]=="video"&&stream["pix_fmt"]=="yuv420p10le"));
    }
    #[test]
    fn reject_filter_argument_injection() {
        assert!(ffmpeg_subtitle_filter(Path::new("/tmp/b;unsafe.srt")).is_err());
        assert!(ffmpeg_subtitle_filter(Path::new("/tmp/b'a.srt")).is_err());
        assert!(ffmpeg_subtitle_filter(Path::new("/tmp/b:c.srt")).is_err());
    }
}
