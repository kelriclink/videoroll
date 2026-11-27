use crate::hardware::Device;
use crate::logging::SharedLog;
// Reuse the same pure ASS event planner as the Linux renderer.
#[allow(dead_code)]
#[path = "../../../rust/src/media/render_optimization.rs"]
mod render_optimization;
use anyhow::{Context, Result};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Default)]
pub struct MediaInfo {
    pub codec: String,
    pub width: u32,
    pub height: u32,
    pub pix_fmt: String,
    pub frame_rate: String,
    pub fps: f64,
    pub duration_seconds: Option<f64>,
}

#[derive(Debug, Clone, Default)]
pub struct RenderProgress {
    pub pipeline: String,
    pub encoder: String,
    pub source_codec: String,
    pub source_width: u32,
    pub source_height: u32,
    pub source_pix_fmt: String,
    pub frame: u64,
    pub fps: f64,
    pub speed: f64,
    pub out_time_seconds: f64,
    pub duration_seconds: Option<f64>,
    pub percent: f64,
    pub elapsed_seconds: f64,
    pub bitrate: String,
    pub total_size: u64,
}

pub type ProgressCallback = Arc<dyn Fn(RenderProgress) + Send + Sync + 'static>;

pub fn probe_media(ffprobe: &Path, input: &Path) -> Result<MediaInfo> {
    let output = Command::new(ffprobe)
        .args([
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt,r_frame_rate:format=duration",
            "-of",
            "json",
        ])
        .arg(input)
        .output()
        .with_context(|| format!("cannot execute {}", ffprobe.display()))?;
    if !output.status.success() {
        anyhow::bail!("ffprobe failed with {}", output.status);
    }
    let value: Value = serde_json::from_slice(&output.stdout).context("invalid ffprobe JSON")?;
    let stream = value
        .get("streams")
        .and_then(Value::as_array)
        .and_then(|streams| streams.first())
        .context("ffprobe returned no video stream")?;

    let frame_rate = stream
        .get("r_frame_rate")
        .and_then(Value::as_str)
        .unwrap_or("30/1")
        .to_string();

    Ok(MediaInfo {
        codec: stream
            .get("codec_name")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        width: stream.get("width").and_then(Value::as_u64).unwrap_or(0) as u32,
        height: stream.get("height").and_then(Value::as_u64).unwrap_or(0) as u32,
        pix_fmt: stream
            .get("pix_fmt")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        fps: parse_fraction(&frame_rate).unwrap_or(0.0),
        frame_rate,
        duration_seconds: value
            .get("format")
            .and_then(|format| format.get("duration"))
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value > 0.0),
    })
}

pub fn render_burn_in(
    ffmpeg: &Path,
    ffprobe: &Path,
    input: &Path,
    ass: &Path,
    output: &Path,
    device: &Device,
    codec: &str,
    preset: Option<&str>,
    quality: Option<i64>,
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
    progress: Option<ProgressCallback>,
) -> Result<()> {
    let encoder = choose_encoder(device, codec)
        .with_context(|| format!("{} cannot encode {}", device.name, codec))?;
    let media = probe_media(ffprobe, input).unwrap_or_default();

    let work_dir = output
        .parent()
        .context("render output has no parent directory")?;
    let ass_name = ass
        .file_name()
        .and_then(|name| name.to_str())
        .context("ASS filename is not valid UTF-8")?;

    log.info(format!(
        "render start: device={} backend={} encoder={} codec={} source={}x{} {} {:.3}fps",
        device.name,
        device.backend,
        encoder,
        codec,
        media.width,
        media.height,
        media.pix_fmt,
        media.fps
    ));

    if device.backend == "qsv" {
        // Candidate path: Windows QSV/D3D11 needs physical-node validation
        // before the sparse timeline can be enabled by default.
        if qsv_event_cache_enabled() {
            match build_qsv_event_cache(ffmpeg, work_dir, ass, &media, Arc::clone(&cancel), log) {
                Ok(Some(cache)) => {
                    let _ = std::fs::remove_file(output);
                    let args = qsv_event_overlay_args(
                        input,
                        &cache.relative_timeline(),
                        output,
                        &encoder,
                        preset,
                        quality,
                        &media,
                    );
                    let result = run_ffmpeg(
                        ffmpeg,
                        work_dir,
                        &args,
                        Arc::clone(&cancel),
                        log,
                        "qsv-hwdecode-overlay_qsv-event-vfr",
                        &encoder,
                        &media,
                        progress.clone(),
                    )
                    .and_then(|_| validate_qsv_cache_output(ffprobe, output, &media));
                    match result {
                        Ok(()) => return Ok(()),
                        Err(error) => {
                            let _ = std::fs::remove_file(output);
                            if cancel.load(Ordering::Relaxed) {
                                return Err(error);
                            }
                            log.warn(format!(
                                "QSV event cache render failed; retrying live overlay: {error:#}"
                            ));
                        }
                    }
                }
                Ok(None) => {}
                Err(error) => {
                    if cancel.load(Ordering::Relaxed) {
                        return Err(error);
                    }
                    log.warn(format!(
                        "QSV event cache build failed; retrying live overlay: {error:#}"
                    ));
                }
            }
        }
        let overlay_args =
            qsv_overlay_args(input, ass_name, output, &encoder, preset, quality, &media);
        if let Some(args) = overlay_args {
            let _ = std::fs::remove_file(output);
            match run_ffmpeg(
                ffmpeg,
                work_dir,
                &args,
                Arc::clone(&cancel),
                log,
                "qsv-hwdecode-overlay_qsv",
                &encoder,
                &media,
                progress.clone(),
            ) {
                Ok(()) => return Ok(()),
                Err(error) => {
                    log.warn(format!(
                        "QSV GPU overlay pipeline failed, falling back to hardware decode + CPU ASS: {error:#}"
                    ));
                }
            }
        }

        let _ = std::fs::remove_file(output);
        let hwdecode_args =
            qsv_hwdecode_cpu_ass_args(input, ass_name, output, &encoder, preset, quality, &media);
        match run_ffmpeg(
            ffmpeg,
            work_dir,
            &hwdecode_args,
            Arc::clone(&cancel),
            log,
            "qsv-hwdecode-cpu-ass-hwupload",
            &encoder,
            &media,
            progress.clone(),
        ) {
            Ok(()) => return Ok(()),
            Err(error) => {
                log.warn(format!(
                    "QSV hardware-decode subtitle pipeline failed, falling back to CPU decode + QSV encode: {error:#}"
                ));
            }
        }
    }

    let _ = std::fs::remove_file(output);
    let args = software_filter_args(input, ass_name, output, device, &encoder, preset, quality);
    let pipeline = match device.backend.as_str() {
        "nvidia" => "cpu-decode-cpu-ass-nvenc",
        "qsv" => "cpu-decode-cpu-ass-qsv",
        _ => "cpu-decode-cpu-ass-software",
    };
    run_ffmpeg(
        ffmpeg, work_dir, &args, cancel, log, pipeline, &encoder, &media, progress,
    )
}

/// Replace source audio without re-encoding the video. Pad short soundtracks
/// to video duration; do not truncate the video to the dubbing duration.
pub fn mux_dubbed_input(
    ffmpeg: &Path,
    ffprobe: &Path,
    input: &Path,
    audio: &Path,
    output: &Path,
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
    progress: Option<ProgressCallback>,
) -> Result<()> {
    let media = probe_media(ffprobe, input)?;
    let duration = media
        .duration_seconds
        .filter(|d| d.is_finite() && *d > 0.0)
        .context("P2 source duration unavailable")?;
    let args = vec![
        "-y".into(),
        "-hide_banner".into(),
        "-i".into(),
        input.display().to_string(),
        "-i".into(),
        audio.display().to_string(),
        "-map".into(),
        "0:v:0".into(),
        "-map".into(),
        "1:a:0".into(),
        "-c:v".into(),
        "copy".into(),
        "-c:a".into(),
        "aac".into(),
        "-af".into(),
        "apad".into(),
        "-t".into(),
        format!("{duration:.6}"),
        "-shortest".into(),
        output.display().to_string(),
    ];
    let work_dir = output.parent().context("P2 remux output has no parent")?;
    run_ffmpeg(
        ffmpeg,
        work_dir,
        &args,
        cancel,
        log,
        "p2-audio-mux",
        "copy",
        &media,
        progress,
    )
}

pub fn mux_soft_sub(
    ffmpeg: &Path,
    ffprobe: &Path,
    input: &Path,
    srt: &Path,
    output: &Path,
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
    progress: Option<ProgressCallback>,
) -> Result<()> {
    let args = vec![
        "-y".to_string(),
        "-hide_banner".to_string(),
        "-i".to_string(),
        input.display().to_string(),
        "-i".to_string(),
        srt.display().to_string(),
        "-map".to_string(),
        "0:v:0".to_string(),
        "-map".to_string(),
        "0:a?".to_string(),
        "-map".to_string(),
        "1:0".to_string(),
        "-c:v".to_string(),
        "copy".to_string(),
        "-c:a".to_string(),
        "copy".to_string(),
        "-c:s".to_string(),
        "srt".to_string(),
        output.display().to_string(),
    ];
    let work_dir = output
        .parent()
        .context("mux output has no parent directory")?;
    let media = probe_media(ffprobe, input).unwrap_or_default();
    run_ffmpeg(
        ffmpeg,
        work_dir,
        &args,
        cancel,
        log,
        "stream-copy-soft-sub",
        "copy",
        &media,
        progress,
    )
}

pub fn sha256_file(path: &Path) -> Result<String> {
    let mut file =
        File::open(path).with_context(|| format!("cannot open {} for SHA256", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        hasher.update(&buffer[..size]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub fn output_path(work_dir: &Path, mode: &str) -> PathBuf {
    if mode == "soft_sub" {
        work_dir.join("video_softsub.mkv")
    } else {
        work_dir.join("video_burnin.mp4")
    }
}

fn qsv_event_cache_enabled() -> bool {
    std::env::var("VIDEOROLL_RENDER_QSV_EVENT_CACHE")
        .ok()
        .is_some_and(|v| {
            matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
}

// Full-frame cache is deliberately conservative: avoid ASS geometry changes
// until the Windows renderer has passed a physical QSV pixel-equivalence test.
const CACHE_BUDGET: u64 = 256 * 1024 * 1024;
struct QsvEventCache {
    dir: PathBuf,
    folder: String,
}
impl QsvEventCache {
    fn relative_timeline(&self) -> PathBuf {
        PathBuf::from(&self.folder).join("overlay.ffconcat")
    }
}
impl Drop for QsvEventCache {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn qsv_cache_plan(text: &str, media: &MediaInfo) -> Option<render_optimization::EventCachePlan> {
    if media.width == 0 || media.height == 0 {
        return None;
    }
    // The shared planner assumes standard ASS v4+ event fields. Refuse
    // reordered formats and noncanonical/malformed Dialogue records.
    let expected = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text";
    let mut events = false;
    let mut format_ok = false;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            events = trimmed.eq_ignore_ascii_case("[Events]");
        }
        if !events {
            continue;
        }
        if let Some(format) = trimmed.strip_prefix("Format:") {
            if !format.trim().eq_ignore_ascii_case(expected) {
                return None;
            }
            format_ok = true;
        }
        if trimmed.to_ascii_lowercase().starts_with("dialogue:") {
            if !format_ok || !line.starts_with("Dialogue:") {
                return None;
            }
            let fields = line.splitn(10, ',').collect::<Vec<_>>();
            if fields.len() != 10
                || render_optimization::timestamp_us(fields[2])?
                    <= render_optimization::timestamp_us(fields[1])?
            {
                return None;
            }
        }
    }
    if !format_ok {
        return None;
    }
    let rate = render_optimization::reduced_overlay_rate(&media.frame_rate, 15.0);
    let plan = render_optimization::plan_event_cache(text, &rate, media.duration_seconds?).0?;
    // Bound rasterization before spawning FFmpeg. Raw RGBA is a
    // conservative admission estimate; reserve 20% for PNG overhead.
    let bytes = u64::from(media.width)
        .checked_mul(u64::from(media.height))?
        .checked_mul(4)?
        .checked_mul(plan.boundaries_us.len() as u64)?;
    (bytes <= CACHE_BUDGET * 4 / 5).then_some(plan)
}

fn build_qsv_event_cache(
    ffmpeg: &Path,
    work_dir: &Path,
    ass: &Path,
    media: &MediaInfo,
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
) -> Result<Option<QsvEventCache>> {
    if std::fs::metadata(ass)?.len() > 8 * 1024 * 1024 {
        return Ok(None);
    }
    let text = std::fs::read_to_string(ass)?;
    let Some(plan) = qsv_cache_plan(&text, media) else {
        return Ok(None);
    };
    let started = Instant::now();
    let folder = format!("qsv-ass-cache-{}", uuid::Uuid::new_v4().simple());
    let dir = work_dir.join(&folder);
    std::fs::create_dir(&dir)?;
    let cache = QsvEventCache { dir, folder };
    std::fs::write(cache.dir.join("subtitle.ass"), text)?;
    let mut clock = String::from("ffconcat version 1.0\n");
    let mut timeline = String::from("ffconcat version 1.0\n");
    for (i, time) in plan.boundaries_us.iter().enumerate() {
        clock.push_str("file blank.png\noption framerate 1000\n");
        timeline.push_str(&format!(
            "file state_{:06}.png\noption framerate 1000\n",
            i + 1
        ));
        if let Some(next) = plan.boundaries_us.get(i + 1) {
            let duration = (next - time) as f64 / 1_000_000.0;
            let line = format!("duration {duration:.6}\n");
            clock.push_str(&line);
            timeline.push_str(&line);
        }
    }
    std::fs::write(cache.dir.join("clock.ffconcat"), clock)?;
    let blank = vec![
        "-hide_banner".into(),
        "-v".into(),
        "error".into(),
        "-y".into(),
        "-f".into(),
        "lavfi".into(),
        "-i".into(),
        format!(
            "color=c=black@0.0:s={}x{}:r=1,format=rgba",
            media.width, media.height
        ),
        "-frames:v".into(),
        "1".into(),
        "-threads".into(),
        "1".into(),
        "blank.png".into(),
    ];
    run_ffmpeg(
        ffmpeg,
        &cache.dir,
        &blank,
        Arc::clone(&cancel),
        log,
        "qsv-event-cache-blank",
        "png",
        media,
        None,
    )?;
    let raster = vec![
        "-hide_banner".into(),
        "-v".into(),
        "error".into(),
        "-y".into(),
        "-f".into(),
        "concat".into(),
        "-safe".into(),
        "0".into(),
        "-i".into(),
        "clock.ffconcat".into(),
        "-vf".into(),
        "ass=subtitle.ass:alpha=1,format=rgba".into(),
        "-fps_mode".into(),
        "passthrough".into(),
        "-threads".into(),
        "1".into(),
        "state_%06d.png".into(),
    ];
    run_ffmpeg(
        ffmpeg,
        &cache.dir,
        &raster,
        cancel,
        log,
        "qsv-event-cache-rasterize",
        "png",
        media,
        None,
    )?;
    let mut bytes = 0_u64;
    for i in 1..=plan.boundaries_us.len() {
        let size = std::fs::metadata(cache.dir.join(format!("state_{i:06}.png")))?.len();
        if size == 0 {
            anyhow::bail!("empty event cache image");
        }
        bytes = bytes.saturating_add(size);
    }
    if cache
        .dir
        .join(format!("state_{:06}.png", plan.boundaries_us.len() + 1))
        .exists()
        || bytes > CACHE_BUDGET
    {
        anyhow::bail!("event cache exceeds its frame count or byte budget");
    }
    std::fs::write(cache.dir.join("overlay.ffconcat"), timeline)?;
    log.info(format!(
        "QSV event cache: states={} bytes={} build={:.3}s",
        plan.boundaries_us.len(),
        bytes,
        started.elapsed().as_secs_f64()
    ));
    Ok(Some(cache))
}

fn qsv_event_overlay_args(
    input: &Path,
    timeline: &Path,
    output: &Path,
    encoder: &str,
    preset: Option<&str>,
    quality: Option<i64>,
    media: &MediaInfo,
) -> Vec<String> {
    let ten_bit = media.pix_fmt.contains("10") || media.pix_fmt.contains("p010");
    let format = if ten_bit && encoder != "h264_qsv" {
        "p010"
    } else {
        "nv12"
    };
    let graph = format!(
        "[0:v]scale_qsv=format={format}[main];[1:v]format=bgra,hwupload=extra_hw_frames=64[sub];[main][sub]overlay_qsv=shortest=0:repeatlast=1:eof_action=repeat[out]"
    );
    let mut args = vec![
        "-y".into(),
        "-hide_banner".into(),
        "-init_hw_device".into(),
        "qsv:hw,child_device_type=d3d11va".into(),
        "-filter_hw_device".into(),
        "hw".into(),
        "-hwaccel".into(),
        "qsv".into(),
        "-hwaccel_output_format".into(),
        "qsv".into(),
        "-i".into(),
        input.display().to_string(),
        "-f".into(),
        "concat".into(),
        "-safe".into(),
        "0".into(),
        "-i".into(),
        timeline.display().to_string(),
        "-filter_complex".into(),
        graph,
        "-map".into(),
        "[out]".into(),
        "-map".into(),
        "0:a?".into(),
        "-c:v".into(),
        encoder.to_owned(),
    ];
    args.extend(encoder_quality_args("qsv", None, encoder, preset, quality));
    args.extend(["-c:a".into(), "copy".into(), output.display().to_string()]);
    args
}
fn validate_qsv_cache_output(ffprobe: &Path, output: &Path, source: &MediaInfo) -> Result<()> {
    let actual = probe_media(ffprobe, output)?;
    let tolerance = (2.0 / source.fps.max(1.0)).max(0.25);
    if actual.width != source.width
        || actual.height != source.height
        || (actual.fps - source.fps).abs() > 0.05
        || source
            .duration_seconds
            .zip(actual.duration_seconds)
            .is_none_or(|(a, b)| (a - b).abs() > tolerance)
    {
        anyhow::bail!("QSV sparse overlay output metadata differs from source");
    }
    Ok(())
}

fn qsv_overlay_args(
    input: &Path,
    ass_name: &str,
    output: &Path,
    encoder: &str,
    preset: Option<&str>,
    quality: Option<i64>,
    media: &MediaInfo,
) -> Option<Vec<String>> {
    if media.width == 0 || media.height == 0 {
        return None;
    }
    let frame_rate = if media.frame_rate.trim().is_empty() || media.frame_rate == "0/0" {
        "30/1"
    } else {
        media.frame_rate.as_str()
    };
    let ten_bit = media.pix_fmt.contains("10") || media.pix_fmt.contains("p010");
    let hardware_format = if ten_bit { "p010" } else { "nv12" };
    let overlay_source = format!(
        "color=c=black@0.0:s={}x{}:r={},format=yuva420p",
        media.width, media.height, frame_rate
    );
    let filter = format!(
        "[0:v]scale_qsv=format={hardware_format}[main];\
         [1:v]ass={ass_name}:alpha=1,format=bgra,hwupload=extra_hw_frames=64[sub];\
         [main][sub]overlay_qsv=shortest=1[out]"
    );

    let mut args = vec![
        "-y".to_string(),
        "-hide_banner".to_string(),
        "-init_hw_device".to_string(),
        "qsv:hw,child_device_type=d3d11va".to_string(),
        "-filter_hw_device".to_string(),
        "hw".to_string(),
        "-hwaccel".to_string(),
        "qsv".to_string(),
        "-hwaccel_output_format".to_string(),
        "qsv".to_string(),
        "-i".to_string(),
        input.display().to_string(),
        "-f".to_string(),
        "lavfi".to_string(),
        "-i".to_string(),
        overlay_source,
        "-filter_complex".to_string(),
        filter,
        "-map".to_string(),
        "[out]".to_string(),
        "-map".to_string(),
        "0:a?".to_string(),
        "-c:v".to_string(),
        encoder.to_string(),
    ];
    args.extend(encoder_quality_args("qsv", None, encoder, preset, quality));
    args.extend([
        "-c:a".to_string(),
        "copy".to_string(),
        output.display().to_string(),
    ]);
    Some(args)
}

fn qsv_hwdecode_cpu_ass_args(
    input: &Path,
    ass_name: &str,
    output: &Path,
    encoder: &str,
    preset: Option<&str>,
    quality: Option<i64>,
    media: &MediaInfo,
) -> Vec<String> {
    let ten_bit = media.pix_fmt.contains("10") || media.pix_fmt.contains("p010");
    let software_format = if ten_bit { "p010le" } else { "nv12" };
    let filter = format!(
        "hwdownload,format={software_format},ass={ass_name},format={software_format},hwupload=extra_hw_frames=64"
    );
    let mut args = vec![
        "-y".to_string(),
        "-hide_banner".to_string(),
        "-init_hw_device".to_string(),
        "qsv:hw,child_device_type=d3d11va".to_string(),
        "-filter_hw_device".to_string(),
        "hw".to_string(),
        "-hwaccel".to_string(),
        "qsv".to_string(),
        "-hwaccel_output_format".to_string(),
        "qsv".to_string(),
        "-i".to_string(),
        input.display().to_string(),
        "-vf".to_string(),
        filter,
        "-c:v".to_string(),
        encoder.to_string(),
    ];
    args.extend(encoder_quality_args("qsv", None, encoder, preset, quality));
    args.extend([
        "-c:a".to_string(),
        "copy".to_string(),
        output.display().to_string(),
    ]);
    args
}

fn software_filter_args(
    input: &Path,
    ass_name: &str,
    output: &Path,
    device: &Device,
    encoder: &str,
    preset: Option<&str>,
    quality: Option<i64>,
) -> Vec<String> {
    let mut args = vec![
        "-y".to_string(),
        "-hide_banner".to_string(),
        "-i".to_string(),
        input.display().to_string(),
        "-vf".to_string(),
        format!("ass={ass_name}"),
        "-c:v".to_string(),
        encoder.to_string(),
    ];
    args.extend(encoder_quality_args(
        &device.backend,
        device.index,
        encoder,
        preset,
        quality,
    ));
    args.extend([
        "-c:a".to_string(),
        "copy".to_string(),
        output.display().to_string(),
    ]);
    args
}

fn encoder_quality_args(
    backend: &str,
    index: Option<u32>,
    encoder: &str,
    preset: Option<&str>,
    quality: Option<i64>,
) -> Vec<String> {
    match backend {
        "nvidia" => {
            let mut args = vec![
                "-preset".to_string(),
                normalize_nvenc_preset(preset).to_string(),
                "-rc".to_string(),
                "vbr".to_string(),
                "-cq".to_string(),
                quality.unwrap_or(24).clamp(0, 51).to_string(),
                "-b:v".to_string(),
                "0".to_string(),
            ];
            if let Some(index) = index {
                args.extend(["-gpu".to_string(), index.to_string()]);
            }
            args
        }
        "qsv" => vec![
            "-preset".to_string(),
            normalize_qsv_preset(preset).to_string(),
            "-global_quality".to_string(),
            quality.unwrap_or(24).clamp(1, 51).to_string(),
        ],
        _ if encoder == "libsvtav1" => vec![
            "-preset".to_string(),
            normalize_svt_preset(preset).to_string(),
            "-crf".to_string(),
            quality.unwrap_or(24).clamp(0, 63).to_string(),
        ],
        _ => vec![
            "-preset".to_string(),
            normalize_software_preset(preset).to_string(),
            "-crf".to_string(),
            quality.unwrap_or(20).clamp(0, 51).to_string(),
        ],
    }
}

fn choose_encoder(device: &Device, codec: &str) -> Option<String> {
    let wanted = match codec.to_ascii_lowercase().as_str() {
        "av1" => match device.backend.as_str() {
            "nvidia" => vec!["av1_nvenc"],
            "qsv" => vec!["av1_qsv"],
            _ => vec!["libsvtav1", "libaom-av1"],
        },
        "h264" | "avc" => match device.backend.as_str() {
            "nvidia" => vec!["h264_nvenc"],
            "qsv" => vec!["h264_qsv"],
            _ => vec!["libx264"],
        },
        "hevc" | "h265" => match device.backend.as_str() {
            "nvidia" => vec!["hevc_nvenc"],
            "qsv" => vec!["hevc_qsv"],
            _ => vec!["libx265"],
        },
        _ => Vec::new(),
    };
    wanted
        .into_iter()
        .find(|name| device.encoders.iter().any(|encoder| encoder == name))
        .map(str::to_string)
}

fn normalize_nvenc_preset(value: Option<&str>) -> &'static str {
    match value.unwrap_or("").to_ascii_lowercase().as_str() {
        "p1" | "ultrafast" | "superfast" => "p1",
        "p2" | "veryfast" | "faster" => "p2",
        "p3" | "fast" => "p3",
        "p5" | "slow" => "p5",
        "p6" | "slower" => "p6",
        "p7" | "veryslow" | "placebo" => "p7",
        _ => "p4",
    }
}

fn normalize_qsv_preset(value: Option<&str>) -> &'static str {
    match value.unwrap_or("").to_ascii_lowercase().as_str() {
        "veryfast" => "veryfast",
        "faster" => "faster",
        "fast" => "fast",
        "slow" => "slow",
        "slower" => "slower",
        "veryslow" => "veryslow",
        "ultrafast" | "superfast" => "veryfast",
        "placebo" => "veryslow",
        _ => "medium",
    }
}

fn normalize_software_preset(value: Option<&str>) -> &'static str {
    match value.unwrap_or("").to_ascii_lowercase().as_str() {
        "ultrafast" => "ultrafast",
        "superfast" => "superfast",
        "veryfast" => "veryfast",
        "faster" => "faster",
        "fast" => "fast",
        "medium" => "medium",
        "slow" => "slow",
        "slower" => "slower",
        "veryslow" => "veryslow",
        "placebo" => "placebo",
        _ => "veryfast",
    }
}

fn normalize_svt_preset(value: Option<&str>) -> &'static str {
    match value.and_then(|raw| raw.parse::<u8>().ok()) {
        Some(value) if value <= 13 => Box::leak(value.to_string().into_boxed_str()),
        _ => "6",
    }
}

fn run_ffmpeg(
    ffmpeg: &Path,
    work_dir: &Path,
    args: &[String],
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
    pipeline: &str,
    encoder: &str,
    media: &MediaInfo,
    callback: Option<ProgressCallback>,
) -> Result<()> {
    if let Some(callback) = &callback {
        callback(base_progress(pipeline, encoder, media));
    }

    let mut command = Command::new(ffmpeg);
    command
        .current_dir(work_dir)
        .args(["-progress", "pipe:1", "-nostats"])
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = command
        .spawn()
        .with_context(|| format!("cannot start {}", ffmpeg.display()))?;

    let stdout = child.stdout.take();
    let progress_callback = callback.clone();
    let progress_pipeline = pipeline.to_string();
    let progress_encoder = encoder.to_string();
    let progress_media = media.clone();
    let progress_reader = stdout.map(|stdout| {
        thread::spawn(move || {
            let start = Instant::now();
            let buffered = BufReader::new(stdout);
            let mut progress =
                base_progress(&progress_pipeline, &progress_encoder, &progress_media);
            for line in buffered.lines().map_while(Result::ok) {
                let Some((key, value)) = line.split_once('=') else {
                    continue;
                };
                match key {
                    "frame" => progress.frame = value.parse().unwrap_or(progress.frame),
                    "fps" => progress.fps = value.parse().unwrap_or(progress.fps),
                    "bitrate" => progress.bitrate = value.to_string(),
                    "total_size" => {
                        progress.total_size = value.parse().unwrap_or(progress.total_size)
                    }
                    "out_time_us" => {
                        progress.out_time_seconds =
                            value.parse::<f64>().unwrap_or(0.0) / 1_000_000.0;
                    }
                    "speed" => {
                        progress.speed = value
                            .trim_end_matches('x')
                            .parse::<f64>()
                            .unwrap_or(progress.speed);
                    }
                    "progress" => {
                        progress.elapsed_seconds = start.elapsed().as_secs_f64();
                        progress.percent = progress
                            .duration_seconds
                            .filter(|duration| *duration > 0.0)
                            .map(|duration| {
                                (progress.out_time_seconds / duration * 100.0).clamp(0.0, 100.0)
                            })
                            .unwrap_or(0.0);
                        if let Some(callback) = &progress_callback {
                            callback(progress.clone());
                        }
                    }
                    _ => {}
                }
            }
        })
    });

    let stderr = child.stderr.take();
    let reader_log = log.clone();
    let error_reader = stderr.map(|stderr| {
        thread::spawn(move || {
            let buffered = BufReader::new(stderr);
            for line in buffered.lines().map_while(Result::ok) {
                let lower = line.to_ascii_lowercase();
                if lower.contains("error")
                    || lower.contains("failed")
                    || lower.contains("warning")
                    || lower.contains("qsv")
                    || lower.contains("d3d11")
                    || lower.contains("overlay")
                {
                    reader_log.info(format!("ffmpeg: {line}"));
                }
            }
        })
    });

    let status = loop {
        if cancel.load(Ordering::Relaxed) {
            let _ = child.kill();
            let _ = child.wait();
            if let Some(reader) = progress_reader {
                let _ = reader.join();
            }
            if let Some(reader) = error_reader {
                let _ = reader.join();
            }
            anyhow::bail!("render execution canceled");
        }
        match child.try_wait()? {
            Some(status) => break status,
            None => thread::sleep(Duration::from_millis(300)),
        }
    };

    if let Some(reader) = progress_reader {
        let _ = reader.join();
    }
    if let Some(reader) = error_reader {
        let _ = reader.join();
    }

    if !status.success() {
        anyhow::bail!("FFmpeg exited with {status}");
    }
    Ok(())
}

fn base_progress(pipeline: &str, encoder: &str, media: &MediaInfo) -> RenderProgress {
    RenderProgress {
        pipeline: pipeline.to_string(),
        encoder: encoder.to_string(),
        source_codec: media.codec.clone(),
        source_width: media.width,
        source_height: media.height,
        source_pix_fmt: media.pix_fmt.clone(),
        duration_seconds: media.duration_seconds,
        ..RenderProgress::default()
    }
}

fn parse_fraction(value: &str) -> Option<f64> {
    if let Some((left, right)) = value.split_once('/') {
        let numerator = left.parse::<f64>().ok()?;
        let denominator = right.parse::<f64>().ok()?;
        if denominator == 0.0 {
            None
        } else {
            Some(numerator / denominator)
        }
    } else {
        value.parse::<f64>().ok()
    }
}

#[cfg(test)]
mod qsv_event_cache_tests {
    use super::*;
    fn ass() -> String {
        r#"[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,30,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,First static subtitle
Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,Second static subtitle
"#.into()
    }
    fn media() -> MediaInfo {
        MediaInfo {
            width: 640,
            height: 360,
            frame_rate: "60/1".into(),
            fps: 60.0,
            pix_fmt: "yuv420p".into(),
            duration_seconds: Some(8.0),
            ..MediaInfo::default()
        }
    }
    #[test]
    fn qsv_cache_is_bounded_and_conservative() {
        let m = media();
        assert_eq!(
            qsv_cache_plan(&ass(), &m).unwrap().boundaries_us,
            vec![0, 1_000_000, 3_000_000, 5_000_000, 7_000_000]
        );
        assert!(qsv_cache_plan(
            &ass().replace("First static", r"{\fad(100,100)}First static"),
            &m
        )
        .is_none());
        assert!(
            qsv_cache_plan(&ass().replace("Layer, Start, End", "Layer, End, Start"), &m).is_none()
        );
        assert!(qsv_cache_plan(&ass().replace("0:00:01.00", "broken"), &m).is_none());
        assert!(qsv_cache_plan(
            &ass(),
            &MediaInfo {
                width: 7680,
                height: 4320,
                ..m.clone()
            }
        )
        .is_none());
        let args = qsv_event_overlay_args(
            Path::new("source.mp4"),
            Path::new("cache/overlay.ffconcat"),
            Path::new("out.mp4"),
            "av1_qsv",
            None,
            None,
            &m,
        );
        assert!(args
            .iter()
            .any(|arg| arg.contains("shortest=0:repeatlast=1")));
        assert!(args.iter().any(|arg| arg == "concat"));
    }

    // Run native worker cache code with the Linux FFmpeg executable.
    // This proves event rasterization/timing, not Windows QSV hardware.
    #[test]
    fn qsv_cache_cpu_pixel_equivalence() {
        let Ok(ffmpeg) = std::env::var("VIDEOROLL_TEST_QSV_CACHE_FFMPEG") else {
            return;
        };
        let root = tempfile::tempdir().unwrap();
        let paths = crate::paths::AppPaths {
            root: root.path().to_path_buf(),
            bin: root.path().join("bin"),
            config: root.path().join("config"),
            logs: root.path().join("logs"),
            cache: root.path().join("cache"),
            work: root.path().join("work"),
        };
        paths.ensure().unwrap();
        let log = SharedLog::new(&paths);
        let subtitle = paths.work.join("fixture.ass");
        std::fs::write(&subtitle, ass()).unwrap();
        let ffmpeg = Path::new(&ffmpeg);
        let cache = build_qsv_event_cache(
            ffmpeg,
            &paths.work,
            &subtitle,
            &media(),
            Arc::new(AtomicBool::new(false)),
            &log,
        )
        .unwrap()
        .unwrap();
        let run = |cached: bool| {
            let mut command = Command::new(ffmpeg);
            command.current_dir(&paths.work).args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-filter_complex_threads",
                "1",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=60:duration=8",
            ]);
            if cached {
                command
                    .args(["-f", "concat", "-safe", "0", "-i"])
                    .arg(cache.relative_timeline());
                command.args(["-filter_complex","[1:v]format=rgba[sub];[0:v][sub]overlay=shortest=0:repeatlast=1:eof_action=repeat[out]"]);
            } else {
                command.args(["-f","lavfi","-i","color=c=black@0.0:s=640x360:r=60,format=rgba","-filter_complex",
                    "[1:v]ass=fixture.ass:alpha=1,format=rgba[sub];[0:v][sub]overlay=shortest=0:repeatlast=1:eof_action=repeat[out]"]);
            }
            let out = command
                .args([
                    "-map",
                    "[out]",
                    "-frames:v",
                    "480",
                    "-threads",
                    "1",
                    "-f",
                    "framemd5",
                    "-",
                ])
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            String::from_utf8(out.stdout)
                .unwrap()
                .lines()
                .filter(|l| !l.starts_with('#'))
                .map(|l| l.rsplit(',').next().unwrap().trim().to_owned())
                .collect::<Vec<_>>()
        };
        let live = run(false);
        let cached = run(true);
        assert_eq!(live.len(), 480);
        assert_eq!(live, cached);
        let dir = cache.dir.clone();
        drop(cache);
        assert!(!dir.exists());
        println!(
            "PASS native Windows worker cache rasterization, CPU reference 480/480 frame hashes, and cache cleanup"
        );
    }
}

#[cfg(test)]
mod p2_audio_mux_tests {
    use super::*;
    #[test]
    fn mux_replaces_audio_and_preserves_video_frames() {
        let Ok(ffmpeg) = std::env::var("VIDEOROLL_TEST_QSV_CACHE_FFMPEG") else {
            return;
        };
        let ffmpeg = Path::new(&ffmpeg);
        let ffprobe = ffmpeg.with_file_name("ffprobe");
        let root = tempfile::tempdir().unwrap();
        let paths = crate::paths::AppPaths {
            root: root.path().into(),
            bin: root.path().join("bin"),
            config: root.path().join("config"),
            logs: root.path().join("logs"),
            cache: root.path().join("cache"),
            work: root.path().join("work"),
        };
        paths.ensure().unwrap();
        let log = SharedLog::new(&paths);
        let source = paths.work.join("source.mp4");
        let audio = paths.work.join("mixed.wav");
        let output = paths.work.join("muxed.mkv");
        assert!(Command::new(ffmpeg)
            .args([
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x120:r=25:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-y"
            ])
            .arg(&source)
            .status()
            .unwrap()
            .success());
        assert!(Command::new(ffmpeg)
            .args([
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:duration=0.7",
                "-y"
            ])
            .arg(&audio)
            .status()
            .unwrap()
            .success());
        mux_dubbed_input(
            ffmpeg,
            &ffprobe,
            &source,
            &audio,
            &output,
            Arc::new(AtomicBool::new(false)),
            &log,
            None,
        )
        .unwrap();
        let hashes = |path: &Path| {
            let o = Command::new(ffmpeg)
                .args(["-v", "error", "-i"])
                .arg(path)
                .args(["-map", "0:v:0", "-f", "framemd5", "-"])
                .output()
                .unwrap();
            assert!(o.status.success());
            String::from_utf8(o.stdout)
                .unwrap()
                .lines()
                .filter(|l| !l.starts_with('#'))
                .map(|l| l.rsplit(',').next().unwrap().trim().to_owned())
                .collect::<Vec<_>>()
        };
        let original = hashes(&source);
        let muxed = hashes(&output);
        assert_eq!(original.len(), 50);
        assert_eq!(muxed, original);
        // Decode output audio and count positive zero crossings. The first
        // half-second should contain 880Hz rather than the original 440Hz.
        let o = Command::new(ffmpeg)
            .args(["-v", "error", "-i"])
            .arg(&output)
            .args([
                "-map", "0:a:0", "-t", "0.5", "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
            ])
            .output()
            .unwrap();
        assert!(o.status.success());
        let samples = o
            .stdout
            .chunks_exact(2)
            .map(|p| i16::from_le_bytes([p[0], p[1]]))
            .collect::<Vec<_>>();
        let crossings = samples.windows(2).filter(|v| v[0] < 0 && v[1] >= 0).count();
        assert!(
            (380..470).contains(&crossings),
            "wrong dubbed audio: {crossings}"
        );
        assert!(
            probe_media(&ffprobe, &output)
                .unwrap()
                .duration_seconds
                .unwrap()
                >= 1.95
        );
        println!("PASS 50/50 identical video frames; 880Hz replacement audio and full 2s video preserved");
    }
}
