use crate::api::ApiClient;
use crate::config::{AppConfig, Credential};
use crate::ffmpeg;
use crate::hardware::{self, Device, HardwareSnapshot};
use crate::logging::SharedLog;
use crate::paths::AppPaths;
use crate::protocol::{
    ClaimRequest, ClaimResponse, EnrollRequest, WorkerHeartbeat,
};
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

const WORKER_VERSION: &str = "0.1.0";

#[derive(Debug, Clone, Default)]
pub struct WorkerStatus {
    pub running: bool,
    pub connected: bool,
    pub draining: bool,
    pub active_jobs: usize,
    pub worker_id: Option<Uuid>,
    pub phase: String,
    pub last_error: String,
    pub devices: Vec<Device>,
}

pub struct WorkerHandle {
    stop_accepting: Arc<AtomicBool>,
    status: Arc<Mutex<WorkerStatus>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl WorkerHandle {
    pub fn start(
        config: AppConfig,
        enrollment_token: String,
        paths: AppPaths,
        log: SharedLog,
    ) -> Self {
        let stop_accepting = Arc::new(AtomicBool::new(false));
        let status = Arc::new(Mutex::new(WorkerStatus {
            running: true,
            phase: "starting".to_string(),
            ..WorkerStatus::default()
        }));
        let stop_for_thread = Arc::clone(&stop_accepting);
        let status_for_thread = Arc::clone(&status);

        let thread = thread::spawn(move || {
            if let Err(error) = run_worker(
                config,
                enrollment_token,
                paths,
                log.clone(),
                stop_for_thread,
                Arc::clone(&status_for_thread),
            ) {
                log.error(format!("worker stopped: {error:#}"));
                if let Ok(mut state) = status_for_thread.lock() {
                    state.last_error = format!("{error:#}");
                    state.phase = "error".to_string();
                }
            }
            if let Ok(mut state) = status_for_thread.lock() {
                state.running = false;
                state.connected = false;
                if state.phase != "error" {
                    state.phase = "stopped".to_string();
                }
            }
        });

        Self {
            stop_accepting,
            status,
            thread: Some(thread),
        }
    }

    pub fn stop_accepting(&self) {
        self.stop_accepting.store(true, Ordering::Relaxed);
        if let Ok(mut state) = self.status.lock() {
            state.draining = true;
            state.phase = "draining".to_string();
        }
    }

    pub fn status(&self) -> WorkerStatus {
        self.status.lock().map(|state| state.clone()).unwrap_or_default()
    }

    pub fn is_running(&self) -> bool {
        self.status().running
    }
}

impl Drop for WorkerHandle {
    fn drop(&mut self) {
        self.stop_accepting.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            if thread.is_finished() {
                let _ = thread.join();
            }
        }
    }
}

fn run_worker(
    config: AppConfig,
    enrollment_token: String,
    paths: AppPaths,
    log: SharedLog,
    stop_accepting: Arc<AtomicBool>,
    status: Arc<Mutex<WorkerStatus>>,
) -> Result<()> {
    paths.ensure()?;
    let ffmpeg_path = paths.ffmpeg();
    if !ffmpeg_path.is_file() {
        anyhow::bail!(
            "FFmpeg not found at {}; reinstall the complete package",
            ffmpeg_path.display()
        );
    }

    set_phase(&status, "scanning hardware");
    let hardware = hardware::scan(&ffmpeg_path)?;
    log_devices(&hardware, &log);
    if let Ok(mut state) = status.lock() {
        state.devices = hardware.devices.clone();
    }

    let api_base = config.normalized_server_url();
    if api_base.is_empty() {
        anyhow::bail!("server URL is empty");
    }
    let api = ApiClient::new(api_base.clone())?;

    set_phase(&status, "pairing");
    let credential = match Credential::load(&paths) {
        Some(credential) => credential,
        None => {
            let token = enrollment_token.trim();
            if token.is_empty() {
                anyhow::bail!("this node is not paired; provide a one-time vre_* enrollment token");
            }
            enroll(&api, &config, &hardware, token, &paths, &log)?
        }
    };

    log.info(format!(
        "worker ready: id={} server={}",
        credential.worker_id, api_base
    ));
    if let Ok(mut state) = status.lock() {
        state.worker_id = Some(credential.worker_id);
        state.connected = true;
        state.phase = "online".to_string();
    }

    let active: Arc<Mutex<HashMap<Uuid, Device>>> = Arc::new(Mutex::new(HashMap::new()));
    let mut next_worker_heartbeat = Instant::now();
    let mut next_claim = Instant::now();

    loop {
        let stopping = stop_accepting.load(Ordering::Relaxed);
        let active_count = active.lock().map(|items| items.len()).unwrap_or(0);
        update_active_status(&status, active_count, stopping);

        if stopping && active_count == 0 {
            log.info("worker drained; exiting");
            return Ok(());
        }

        let now = Instant::now();
        if now >= next_worker_heartbeat {
            match send_worker_heartbeat(
                &api,
                &credential,
                &hardware,
                &active,
                stopping,
            ) {
                Ok(()) => {
                    if let Ok(mut state) = status.lock() {
                        state.connected = true;
                    }
                }
                Err(error) => {
                    log.warn(format!("worker heartbeat failed: {error:#}"));
                    if let Ok(mut state) = status.lock() {
                        state.connected = false;
                    }
                }
            }
            next_worker_heartbeat = now + Duration::from_secs(10);
        }

        if !stopping && now >= next_claim {
            let (available_slots, available_encoders) =
                available_capacity(&config, &hardware.devices, &active);
            if available_slots > 0 {
                let request = ClaimRequest {
                    available_slots,
                    accepted_transfer_modes: vec!["http".to_string()],
                    available_encoders,
                };
                match api.claim(credential.worker_id, &credential.credential, &request) {
                    Ok(claim) => {
                        let retry = claim.retry_after_seconds.max(1);
                        if claim.execution.is_some() && claim.render_spec.is_some() {
                            if let Err(error) = start_execution(
                                &config,
                                &paths,
                                &hardware.devices,
                                &api,
                                &credential,
                                claim,
                                Arc::clone(&active),
                                log.clone(),
                            ) {
                                log.error(format!("cannot start claimed execution: {error:#}"));
                            } else {
                                next_claim = Instant::now();
                                continue;
                            }
                        }
                        next_claim = Instant::now() + Duration::from_secs(retry);
                    }
                    Err(error) => {
                        log.warn(format!("claim failed: {error:#}"));
                        next_claim = Instant::now() + Duration::from_secs(3);
                    }
                }
            } else {
                next_claim = Instant::now() + Duration::from_secs(1);
            }
        }

        thread::sleep(Duration::from_millis(250));
    }
}

fn enroll(
    api: &ApiClient,
    config: &AppConfig,
    hardware: &HardwareSnapshot,
    token: &str,
    paths: &AppPaths,
    log: &SharedLog,
) -> Result<Credential> {
    let payloads = hardware
        .devices
        .iter()
        .map(|device| device.payload(0, Vec::new()))
        .collect::<Vec<_>>();
    let request = EnrollRequest {
        worker_key: format!(
            "windows-native-{}",
            hardware::computer_name().to_ascii_lowercase().replace(' ', "-")
        ),
        name: config.node_name.clone(),
        platform: "windows".to_string(),
        architecture: Some(std::env::consts::ARCH.to_string()),
        version: WORKER_VERSION.to_string(),
        protocol_version: 1,
        render_spec_versions: vec![1],
        capabilities: hardware.capabilities(payloads.clone()),
        resources: hardware.resources(payloads),
        labels: json!({"standalone": true, "native": true, "language": "rust"}),
        max_concurrency: config.max_concurrency.clamp(1, 32),
        enrollment_token: token.to_string(),
    };
    let enrolled = api.enroll(&request)?;
    let credential = Credential {
        worker_id: enrolled.worker.id,
        credential: enrolled.credential,
    };
    credential.save(paths)?;
    log.info(format!(
        "paired as {} ({})",
        enrolled.worker.worker_key, credential.worker_id
    ));
    Ok(credential)
}

fn send_worker_heartbeat(
    api: &ApiClient,
    credential: &Credential,
    hardware: &HardwareSnapshot,
    active: &Arc<Mutex<HashMap<Uuid, Device>>>,
    stopping: bool,
) -> Result<()> {
    let active_snapshot = active.lock().map(|items| items.clone()).unwrap_or_default();
    let mut device_payloads = Vec::new();
    for device in &hardware.devices {
        let ids = active_snapshot
            .iter()
            .filter(|(_, assigned)| assigned.id == device.id)
            .map(|(id, _)| id.to_string())
            .collect::<Vec<_>>();
        device_payloads.push(device.payload(ids.len(), ids));
    }
    let status = if stopping {
        "draining"
    } else if active_snapshot.is_empty() {
        "online"
    } else {
        "busy"
    };
    let payload = WorkerHeartbeat {
        status: status.to_string(),
        resources: hardware.resources(device_payloads.clone()),
        capabilities: hardware.capabilities(device_payloads),
        active_execution_ids: active_snapshot.keys().copied().collect(),
    };
    api.worker_heartbeat(credential.worker_id, &credential.credential, &payload)?;
    Ok(())
}

fn available_capacity(
    config: &AppConfig,
    devices: &[Device],
    active: &Arc<Mutex<HashMap<Uuid, Device>>>,
) -> (usize, Vec<String>) {
    let active_snapshot = active.lock().map(|items| items.clone()).unwrap_or_default();
    let node_remaining = config
        .max_concurrency
        .clamp(1, 32)
        .saturating_sub(active_snapshot.len());
    let free_devices = devices
        .iter()
        .filter(|device| {
            active_snapshot
                .values()
                .filter(|assigned| assigned.id == device.id)
                .count()
                < device.max_concurrency
        })
        .collect::<Vec<_>>();
    let encoders = free_devices
        .iter()
        .flat_map(|device| device.encoders.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    (node_remaining.min(free_devices.len()), encoders)
}

fn start_execution(
    config: &AppConfig,
    paths: &AppPaths,
    devices: &[Device],
    api: &ApiClient,
    credential: &Credential,
    claim: ClaimResponse,
    active: Arc<Mutex<HashMap<Uuid, Device>>>,
    log: SharedLog,
) -> Result<()> {
    let execution = claim
        .execution
        .clone()
        .context("claim response is missing execution")?;
    let spec = claim
        .render_spec
        .clone()
        .context("claim response is missing render spec")?;
    let device = select_device(devices, &active, &spec)?;
    if let Ok(mut items) = active.lock() {
        items.insert(execution.id, device.clone());
    }

    let api = api.clone();
    let credential = credential.clone();
    let paths = paths.clone();
    let config = config.clone();
    let active_for_thread = Arc::clone(&active);
    thread::spawn(move || {
        let execution_id = execution.id;
        let result = run_execution(
            &config,
            &paths,
            &api,
            &credential,
            claim,
            device.clone(),
            log.clone(),
        );
        if let Err(error) = result {
            log.error(format!("execution {execution_id} failed: {error:#}"));
        }
        if let Ok(mut items) = active_for_thread.lock() {
            items.remove(&execution_id);
        }
    });
    Ok(())
}

fn select_device(
    devices: &[Device],
    active: &Arc<Mutex<HashMap<Uuid, Device>>>,
    spec: &crate::protocol::RenderSpec,
) -> Result<Device> {
    let active_snapshot = active.lock().map(|items| items.clone()).unwrap_or_default();
    let codec = spec
        .request
        .get("render")
        .and_then(Value::as_object)
        .and_then(|render| render.get("video_codec"))
        .and_then(Value::as_str)
        .unwrap_or("av1");

    let mut candidates = devices
        .iter()
        .filter(|device| {
            let used = active_snapshot
                .values()
                .filter(|assigned| assigned.id == device.id)
                .count();
            if used >= device.max_concurrency {
                return false;
            }
            if spec.mode == "burn_in" {
                device.supports_codec(codec)
            } else {
                true
            }
        })
        .cloned()
        .collect::<Vec<_>>();

    if spec.mode != "burn_in" {
        candidates.sort_by_key(|device| device.backend != "software");
    } else {
        candidates.sort_by_key(|device| device.backend == "software");
    }

    candidates
        .into_iter()
        .next()
        .context("no compatible free local render device")
}

fn run_execution(
    _config: &AppConfig,
    paths: &AppPaths,
    api: &ApiClient,
    credential: &Credential,
    claim: ClaimResponse,
    device: Device,
    log: SharedLog,
) -> Result<()> {
    let execution = claim.execution.context("execution missing")?;
    let spec = claim.render_spec.context("render spec missing")?;
    if spec.schema_version != 1 {
        anyhow::bail!("unsupported render spec version {}", spec.schema_version);
    }

    let execution_id = execution.id;
    let fence = execution.fence_token.clone();
    let root = paths.work.join(execution_id.to_string());
    fs::create_dir_all(&root)
        .with_context(|| format!("cannot create execution work directory {}", root.display()))?;

    log.info(format!(
        "execution claimed: id={} task={} render_job={} mode={} device={}",
        execution_id, spec.task_id, spec.render_job_id, spec.mode, device.name
    ));

    let heartbeat_stop = Arc::new(AtomicBool::new(false));
    let cancel = Arc::new(AtomicBool::new(false));
    let heartbeat_thread = start_execution_heartbeat(
        api.clone(),
        credential.clone(),
        execution_id,
        fence.clone(),
        device.clone(),
        Arc::clone(&heartbeat_stop),
        Arc::clone(&cancel),
        log.clone(),
    );

    let result = (|| -> Result<()> {
        let mut artifacts = HashMap::<String, PathBuf>::new();
        for artifact in &spec.artifacts {
            let target = artifact_target(&root, &artifact.role, &artifact.storage_key);
            api.download_artifact(
                execution_id,
                &artifact.role,
                &credential.credential,
                &target,
            )?;
            if let Some(expected) = artifact.sha256.as_deref() {
                let actual = ffmpeg::sha256_file(&target)?;
                if !actual.eq_ignore_ascii_case(expected) {
                    anyhow::bail!("artifact checksum mismatch: {}", artifact.role);
                }
            }
            artifacts.insert(artifact.role.clone(), target);
        }

        let input = artifacts.get("input").context("input artifact missing")?;
        let render = spec
            .request
            .get("render")
            .and_then(Value::as_object);
        let codec = render
            .and_then(|value| value.get("video_codec"))
            .and_then(Value::as_str)
            .unwrap_or("av1");
        let preset = render
            .and_then(|value| value.get("video_preset"))
            .and_then(Value::as_str);
        let quality = render
            .and_then(|value| value.get("video_crf"))
            .and_then(Value::as_i64);

        api.execution_post(
            execution_id,
            "progress",
            &credential.credential,
            &json!({
                "fence_token": fence,
                "progress": 20,
                "metrics": metrics(&device),
                "stage": "rendering",
            }),
        )?;

        if spec.mode == "noop" {
            api.execution_post(
                execution_id,
                "complete",
                &credential.credential,
                &json!({"fence_token": fence, "output": {}}),
            )?;
            return Ok(());
        }

        let output = ffmpeg::output_path(&root, &spec.mode);
        if spec.mode == "burn_in" {
            let ass = artifacts.get("ass").context("burn-in ASS artifact missing")?;
            ffmpeg::render_burn_in(
                &paths.ffmpeg(),
                input,
                ass,
                &output,
                &device,
                codec,
                preset,
                quality,
                Arc::clone(&cancel),
                &log,
            )?;
        } else if spec.mode == "soft_sub" {
            let srt = artifacts.get("srt").context("soft-sub SRT artifact missing")?;
            ffmpeg::mux_soft_sub(
                &paths.ffmpeg(),
                input,
                srt,
                &output,
                Arc::clone(&cancel),
                &log,
            )?;
        } else {
            anyhow::bail!("unsupported render mode {}", spec.mode);
        }

        if cancel.load(Ordering::Relaxed) {
            anyhow::bail!("render execution canceled by coordinator");
        }

        api.execution_post(
            execution_id,
            "progress",
            &credential.credential,
            &json!({
                "fence_token": fence,
                "progress": 90,
                "metrics": metrics(&device),
                "stage": "uploading",
            }),
        )?;

        let asset_id = api.upload_output(
            execution_id,
            &fence,
            &credential.credential,
            &output,
        )?;
        let size = fs::metadata(&output)?.len();
        let sha256 = ffmpeg::sha256_file(&output)?;
        api.execution_post(
            execution_id,
            "complete",
            &credential.credential,
            &json!({
                "fence_token": fence,
                "output_asset_id": asset_id,
                "output": {"sha256": sha256, "size_bytes": size},
            }),
        )?;
        log.info(format!("execution complete: id={} bytes={}", execution_id, size));
        Ok(())
    })();

    heartbeat_stop.store(true, Ordering::Relaxed);
    let _ = heartbeat_thread.join();

    if let Err(error) = &result {
        if !ApiClient::is_fence_status(error) {
            let _ = api.execution_post(
                execution_id,
                "fail",
                &credential.credential,
                &json!({
                    "fence_token": fence,
                    "error": format!("{error:#}").chars().take(8192).collect::<String>(),
                    "retryable": true,
                }),
            );
        }
    }

    let _ = fs::remove_dir_all(&root);
    result
}

fn start_execution_heartbeat(
    api: ApiClient,
    credential: Credential,
    execution_id: Uuid,
    fence: String,
    device: Device,
    stop: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
    log: SharedLog,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut next_cancel = Instant::now();
        while !stop.load(Ordering::Relaxed) && !cancel.load(Ordering::Relaxed) {
            let heartbeat = api.execution_post(
                execution_id,
                "heartbeat",
                &credential.credential,
                &json!({
                    "fence_token": fence,
                    "progress": null,
                    "metrics": metrics(&device),
                }),
            );
            if let Err(error) = heartbeat {
                if ApiClient::is_fence_status(&error) {
                    log.warn(format!("execution fenced: {execution_id}"));
                    cancel.store(true, Ordering::Relaxed);
                    break;
                }
                log.warn(format!("execution heartbeat failed: {execution_id}: {error:#}"));
            }

            if Instant::now() >= next_cancel {
                match api.cancel_state(execution_id, &credential.credential) {
                    Ok(state) if state.cancel_requested => {
                        log.warn(format!(
                            "execution cancel requested: {}: {}",
                            execution_id,
                            state.reason.unwrap_or_else(|| "coordinator request".to_string())
                        ));
                        cancel.store(true, Ordering::Relaxed);
                        break;
                    }
                    Ok(_) => {}
                    Err(error) => {
                        if ApiClient::is_fence_status(&error) {
                            cancel.store(true, Ordering::Relaxed);
                            break;
                        }
                    }
                }
                next_cancel = Instant::now() + Duration::from_secs(5);
            }

            for _ in 0..20 {
                if stop.load(Ordering::Relaxed) || cancel.load(Ordering::Relaxed) {
                    return;
                }
                thread::sleep(Duration::from_millis(500));
            }
        }
    })
}

fn metrics(device: &Device) -> Value {
    json!({
        "device_id": device.id,
        "device_name": device.name,
        "backend": device.backend,
    })
}

fn artifact_target(root: &Path, role: &str, storage_key: &str) -> PathBuf {
    if role == "ass" {
        return root.join("subtitle.ass");
    }
    if role == "srt" {
        return root.join("subtitle.srt");
    }
    let extension = Path::new(storage_key)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("bin");
    root.join(format!("{role}.{extension}"))
}

fn set_phase(status: &Arc<Mutex<WorkerStatus>>, phase: &str) {
    if let Ok(mut state) = status.lock() {
        state.phase = phase.to_string();
    }
}

fn update_active_status(status: &Arc<Mutex<WorkerStatus>>, active: usize, draining: bool) {
    if let Ok(mut state) = status.lock() {
        state.active_jobs = active;
        state.draining = draining;
        state.phase = if draining {
            "draining".to_string()
        } else if active > 0 {
            "busy".to_string()
        } else {
            "online".to_string()
        };
    }
}

fn log_devices(hardware: &HardwareSnapshot, log: &SharedLog) {
    for device in &hardware.devices {
        log.info(format!(
            "device: {} backend={} encoders={}",
            device.name,
            device.backend,
            device.encoders.join(",")
        ));
    }
}
