use crate::hardware::Device;
use crate::logging::SharedLog;
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
        let overlay_args = qsv_overlay_args(
            input,
            ass_name,
            output,
            &encoder,
            preset,
            quality,
            &media,
        );
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
        let hwdecode_args = qsv_hwdecode_cpu_ass_args(
            input,
            ass_name,
            output,
            &encoder,
            preset,
            quality,
            &media,
        );
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
    let args = software_filter_args(
        input,
        ass_name,
        output,
        device,
        &encoder,
        preset,
        quality,
    );
    let pipeline = match device.backend.as_str() {
        "nvidia" => "cpu-decode-cpu-ass-nvenc",
        "qsv" => "cpu-decode-cpu-ass-qsv",
        _ => "cpu-decode-cpu-ass-software",
    };
    run_ffmpeg(
        ffmpeg,
        work_dir,
        &args,
        cancel,
        log,
        pipeline,
        &encoder,
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
    let work_dir = output.parent().context("mux output has no parent directory")?;
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
    let mut file = File::open(path)
        .with_context(|| format!("cannot open {} for SHA256", path.display()))?;
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
            let mut progress = base_progress(&progress_pipeline, &progress_encoder, &progress_media);
            for line in buffered.lines().map_while(Result::ok) {
                let Some((key, value)) = line.split_once('=') else {
                    continue;
                };
                match key {
                    "frame" => progress.frame = value.parse().unwrap_or(progress.frame),
                    "fps" => progress.fps = value.parse().unwrap_or(progress.fps),
                    "bitrate" => progress.bitrate = value.to_string(),
                    "total_size" => progress.total_size = value.parse().unwrap_or(progress.total_size),
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
