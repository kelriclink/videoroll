use crate::hardware::Device;
use crate::logging::SharedLog;
use anyhow::{Context, Result};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

pub fn render_burn_in(
    ffmpeg: &Path,
    input: &Path,
    ass: &Path,
    output: &Path,
    device: &Device,
    codec: &str,
    preset: Option<&str>,
    quality: Option<i64>,
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
) -> Result<()> {
    let encoder = choose_encoder(device, codec)
        .with_context(|| format!("{} cannot encode {}", device.name, codec))?;

    let work_dir = output
        .parent()
        .context("render output has no parent directory")?;
    let ass_name = ass
        .file_name()
        .and_then(|name| name.to_str())
        .context("ASS filename is not valid UTF-8")?;

    let mut args = vec![
        "-y".to_string(),
        "-hide_banner".to_string(),
        "-i".to_string(),
        input.display().to_string(),
        "-vf".to_string(),
        format!("ass={ass_name}"),
        "-c:v".to_string(),
        encoder.clone(),
    ];

    match device.backend.as_str() {
        "nvidia" => {
            args.extend([
                "-preset".to_string(),
                normalize_nvenc_preset(preset).to_string(),
                "-rc".to_string(),
                "vbr".to_string(),
                "-cq".to_string(),
                quality.unwrap_or(24).clamp(0, 51).to_string(),
                "-b:v".to_string(),
                "0".to_string(),
            ]);
            if let Some(index) = device.index {
                args.extend(["-gpu".to_string(), index.to_string()]);
            }
        }
        "qsv" => {
            args.extend([
                "-preset".to_string(),
                normalize_qsv_preset(preset).to_string(),
                "-global_quality".to_string(),
                quality.unwrap_or(24).clamp(1, 51).to_string(),
            ]);
        }
        _ => {
            if encoder == "libsvtav1" {
                args.extend([
                    "-preset".to_string(),
                    normalize_svt_preset(preset).to_string(),
                    "-crf".to_string(),
                    quality.unwrap_or(24).clamp(0, 63).to_string(),
                ]);
            } else {
                args.extend([
                    "-preset".to_string(),
                    normalize_software_preset(preset).to_string(),
                    "-crf".to_string(),
                    quality.unwrap_or(20).clamp(0, 51).to_string(),
                ]);
            }
        }
    }

    args.extend([
        "-c:a".to_string(),
        "copy".to_string(),
        output.display().to_string(),
    ]);

    log.info(format!(
        "render start: device={} backend={} encoder={} codec={}",
        device.name, device.backend, encoder, codec
    ));
    run_ffmpeg(ffmpeg, work_dir, &args, cancel, log)?;
    Ok(())
}

pub fn mux_soft_sub(
    ffmpeg: &Path,
    input: &Path,
    srt: &Path,
    output: &Path,
    cancel: Arc<AtomicBool>,
    log: &SharedLog,
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
    run_ffmpeg(ffmpeg, work_dir, &args, cancel, log)
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
        "veryfast" | "faster" | "fast" | "medium" | "slow" | "slower" | "veryslow" => {
            match value.unwrap_or("").to_ascii_lowercase().as_str() {
                "veryfast" => "veryfast",
                "faster" => "faster",
                "fast" => "fast",
                "slow" => "slow",
                "slower" => "slower",
                "veryslow" => "veryslow",
                _ => "medium",
            }
        }
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
) -> Result<()> {
    let mut command = Command::new(ffmpeg);
    command
        .current_dir(work_dir)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
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

    let stderr = child.stderr.take();
    let reader_log = log.clone();
    let reader = stderr.map(|stderr| {
        thread::spawn(move || {
            let buffered = BufReader::new(stderr);
            for line in buffered.lines().map_while(Result::ok) {
                if line.contains("frame=") || line.contains("Error") || line.contains("error") {
                    reader_log.info(format!("ffmpeg: {line}"));
                }
            }
        })
    });

    let status = loop {
        if cancel.load(Ordering::Relaxed) {
            let _ = child.kill();
            let _ = child.wait();
            if let Some(reader) = reader {
                let _ = reader.join();
            }
            anyhow::bail!("render execution canceled");
        }
        match child.try_wait()? {
            Some(status) => break status,
            None => thread::sleep(Duration::from_millis(400)),
        }
    };

    if let Some(reader) = reader {
        let _ = reader.join();
    }

    if !status.success() {
        anyhow::bail!("FFmpeg exited with {status}");
    }
    Ok(())
}
