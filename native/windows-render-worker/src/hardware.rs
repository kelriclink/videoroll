use crate::logging::SharedLog;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub backend: String,
    pub index: Option<u32>,
    pub encoders: Vec<String>,
}

impl Device {
    pub fn payload(&self, active_jobs: usize, execution_ids: Vec<String>) -> Value {
        json!({
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "path": "",
            "index": self.index,
            "encoders": self.encoders,
            "active_jobs": active_jobs,
            "status": if active_jobs > 0 { "busy" } else { "idle" },
            "execution_ids": execution_ids,
        })
    }

    pub fn supports_codec(&self, codec: &str) -> bool {
        let names = encoder_names(codec);
        self.encoders.iter().any(|encoder| names.contains(encoder.as_str()))
    }
}

#[derive(Debug, Clone)]
pub struct HardwareSnapshot {
    pub devices: Vec<Device>,
    pub all_encoders: Vec<String>,
    pub filters: Vec<String>,
}

impl HardwareSnapshot {
    pub fn capabilities(&self, device_payloads: Vec<Value>) -> Value {
        let hardware_names = self
            .devices
            .iter()
            .filter(|device| device.backend != "software")
            .map(|device| device.name.clone())
            .collect::<Vec<_>>();
        let backend_count = self
            .devices
            .iter()
            .map(|device| device.backend.as_str())
            .collect::<BTreeSet<_>>()
            .len();
        json!({
            "backend": if self.devices.len() > 1 || backend_count > 1 {
                "multi".to_string()
            } else {
                self.devices.first().map(|d| d.backend.clone()).unwrap_or_else(|| "software".to_string())
            },
            "gpu_model": if hardware_names.is_empty() { "CPU".to_string() } else { hardware_names.join(", ") },
            "encoders": self.all_encoders,
            "filters": self.filters,
            "devices": device_payloads,
        })
    }

    pub fn resources(&self, device_payloads: Vec<Value>) -> Value {
        json!({
            "hostname": computer_name(),
            "cpu_count": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1),
            "device_count": self.devices.len(),
            "devices": device_payloads,
        })
    }
}

pub fn scan(ffmpeg: &Path, log: Option<&SharedLog>) -> Result<HardwareSnapshot> {
    let encoders = ffmpeg_list(ffmpeg, "-encoders").context("cannot query FFmpeg encoders")?;
    let filters = ffmpeg_list(ffmpeg, "-filters").unwrap_or_default();
    let encoder_set = encoders.iter().cloned().collect::<BTreeSet<_>>();

    let mut devices = Vec::new();
    if cfg!(target_os = "windows") {
        devices.extend(scan_intel_qsv(ffmpeg, &encoder_set, log));
        devices.extend(scan_nvidia(ffmpeg, &encoder_set, log));
    }

    let software = ["libx264", "libx265", "libsvtav1", "libaom-av1"]
        .into_iter()
        .filter(|name| encoder_set.contains(*name))
        .map(str::to_string)
        .collect::<Vec<_>>();
    if !software.is_empty() {
        devices.push(Device {
            id: "software:cpu".to_string(),
            name: "CPU fallback".to_string(),
            backend: "software".to_string(),
            index: None,
            encoders: software,
        });
    }

    if devices.is_empty() {
        anyhow::bail!("no usable QSV, NVENC, or software FFmpeg encoder was detected");
    }

    let all_encoders = devices
        .iter()
        .flat_map(|device| device.encoders.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();

    Ok(HardwareSnapshot {
        devices,
        all_encoders,
        filters,
    })
}

fn scan_intel_qsv(
    ffmpeg: &Path,
    encoders: &BTreeSet<String>,
    log: Option<&SharedLog>,
) -> Vec<Device> {
    let candidates = ["h264_qsv", "hevc_qsv", "av1_qsv"]
        .into_iter()
        .filter(|encoder| encoders.contains(*encoder))
        .filter(|encoder| probe_encoder(ffmpeg, encoder, "qsv", None, log))
        .map(str::to_string)
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return Vec::new();
    }

    let name = detect_intel_name().unwrap_or_else(|| "Intel Quick Sync Video".to_string());
    vec![Device {
        id: "qsv:intel".to_string(),
        name,
        backend: "qsv".to_string(),
        index: None,
        encoders: candidates,
    }]
}

fn scan_nvidia(
    ffmpeg: &Path,
    encoders: &BTreeSet<String>,
    log: Option<&SharedLog>,
) -> Vec<Device> {
    let nvenc = ["h264_nvenc", "hevc_nvenc", "av1_nvenc"]
        .into_iter()
        .filter(|encoder| encoders.contains(*encoder))
        .collect::<Vec<_>>();
    if nvenc.is_empty() {
        return Vec::new();
    }

    let Some(command) = nvidia_smi_path() else {
        return Vec::new();
    };
    let output = match Command::new(command)
        .args([
            "--query-gpu=index,uuid,name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
    {
        Ok(output) if output.status.success() => String::from_utf8_lossy(&output.stdout).into_owned(),
        _ => return Vec::new(),
    };

    let mut devices = Vec::new();
    for line in output.lines() {
        let parts = line.splitn(4, ',').map(str::trim).collect::<Vec<_>>();
        if parts.len() < 3 {
            continue;
        }
        let Ok(index) = parts[0].parse::<u32>() else {
            continue;
        };
        let supported = nvenc
            .iter()
            .copied()
            .filter(|encoder| probe_encoder(ffmpeg, encoder, "nvidia", Some(index), log))
            .map(str::to_string)
            .collect::<Vec<_>>();
        if supported.is_empty() {
            continue;
        }
        let uuid = if parts[1].is_empty() {
            format!("gpu-{index}")
        } else {
            parts[1].to_string()
        };
        devices.push(Device {
            id: format!("nvidia:{uuid}"),
            name: parts[2].to_string(),
            backend: "nvidia".to_string(),
            index: Some(index),
            encoders: supported,
        });
    }
    devices
}

fn probe_encoder(
    ffmpeg: &Path,
    encoder: &str,
    backend: &str,
    index: Option<u32>,
    log: Option<&SharedLog>,
) -> bool {
    // AV1 QSV requires at least 128x96 on current Intel oneVPL hardware.
    // Use a small but valid NV12 sample for every hardware probe so codec
    // capability is not rejected just because the synthetic frame is invalid.
    let mut command = Command::new(ffmpeg);
    command.args([
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x144:r=30",
        "-vf",
        "format=nv12",
        "-frames:v",
        "4",
        "-c:v",
        encoder,
        "-g",
        "30",
    ]);
    if backend == "nvidia" {
        if let Some(index) = index {
            command.args(["-gpu", &index.to_string()]);
        }
    }
    command.args(["-f", "null", "-"]).stdin(Stdio::null());

    match command.output() {
        Ok(output) if output.status.success() => true,
        Ok(output) => {
            if let Some(log) = log {
                let stderr = String::from_utf8_lossy(&output.stderr);
                let detail = stderr
                    .lines()
                    .filter(|line| !line.trim().is_empty())
                    .last()
                    .unwrap_or("FFmpeg returned no error detail");
                log.warn(format!(
                    "hardware encoder probe failed: backend={} encoder={} gpu={:?}: {}",
                    backend, index, encoder, detail
                ));
            }
            false
        }
        Err(error) => {
            if let Some(log) = log {
                log.warn(format!(
                    "hardware encoder probe could not start: backend={} encoder={} gpu={:?}: {}",
                    backend, index, encoder, error
                ));
            }
            false
        }
    }
}

fn ffmpeg_list(ffmpeg: &Path, arg: &str) -> Result<Vec<String>> {
    let output = Command::new(ffmpeg)
        .args(["-hide_banner", arg])
        .output()
        .with_context(|| format!("cannot execute {}", ffmpeg.display()))?;
    if !output.status.success() {
        anyhow::bail!("FFmpeg {arg} failed");
    }
    let text = format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let mut names = BTreeSet::new();
    for line in text.lines() {
        let fields = line.split_whitespace().collect::<Vec<_>>();
        if fields.len() >= 2 && fields[0].len() >= 1 {
            let name = fields[1];
            if name.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-')) {
                names.insert(name.to_string());
            }
        }
    }
    Ok(names.into_iter().collect())
}

fn detect_intel_name() -> Option<String> {
    let output = Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_VideoController | Where-Object {$_.Name -match 'Intel'} | Select-Object -First 1 -ExpandProperty Name)",
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let name = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!name.is_empty()).then_some(name)
}

fn nvidia_smi_path() -> Option<PathBuf> {
    let simple = PathBuf::from("nvidia-smi.exe");
    if Command::new(&simple).arg("--help").stdout(Stdio::null()).stderr(Stdio::null()).status().is_ok() {
        return Some(simple);
    }
    if let Ok(windir) = std::env::var("WINDIR") {
        let candidate = PathBuf::from(windir).join("System32").join("nvidia-smi.exe");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    if let Ok(program_files) = std::env::var("ProgramW6432") {
        let candidate = PathBuf::from(program_files)
            .join("NVIDIA Corporation")
            .join("NVSMI")
            .join("nvidia-smi.exe");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

pub fn encoder_names(codec: &str) -> BTreeSet<&'static str> {
    match codec.to_ascii_lowercase().as_str() {
        "av1" => ["av1_nvenc", "av1_qsv", "libsvtav1", "libaom-av1"].into_iter().collect(),
        "h264" | "avc" => ["h264_nvenc", "h264_qsv", "libx264"].into_iter().collect(),
        "hevc" | "h265" => ["hevc_nvenc", "hevc_qsv", "libx265"].into_iter().collect(),
        _ => BTreeSet::new(),
    }
}

pub fn computer_name() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "windows-render-node".to_string())
}
