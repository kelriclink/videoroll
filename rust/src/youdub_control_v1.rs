//! Read-only original YouDub control-plane DTO from the *actual* native runtime.
//! We do not infer GPU residency from a setting: the Unix resident socket
//! must answer a real ping before gpu_loaded can be true.
use crate::{AppError, AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::get};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    process::{Command as StdCommand, Stdio},
    time::Duration,
};
use tokio::process::Command;

fn runtime_path(state: &AppState) -> PathBuf {
    state
        .config
        .youdub_runtime
        .clone()
        .or_else(|| std::env::var_os("VIDEOROLL_YOUDUB_RUNTIME").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("/DATA/youdub-runtime"))
}
fn model_path(runtime: &Path) -> PathBuf {
    std::env::var_os("VOXCPM_MODEL_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| runtime.join("models/OpenBMB__VoxCPM2-modelscope"))
}
fn native_python(state: &AppState, runtime: &Path) -> PathBuf {
    state
        .config
        .youdub_python
        .clone()
        .or_else(|| std::env::var_os("VIDEOROLL_YOUDUB_PYTHON").map(PathBuf::from))
        .unwrap_or_else(|| runtime.join("venv/bin/python"))
}
fn installed(state: &AppState, runtime: &Path) -> bool {
    let model = model_path(runtime);
    runtime.join("backend/app/pipeline.py").is_file()
        && runtime.join("submodule/demucs/demucs/api.py").is_file()
        && native_python(state, runtime).is_file()
        && model
            .join("model.safetensors")
            .metadata()
            .is_ok_and(|m| m.len() > 1_000_000_000)
        && model.join("audiovae.pth").is_file()
}
pub(crate) async fn resident_loaded(runtime: &Path) -> bool {
    #[cfg(unix)]
    {
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::UnixStream,
        };
        let sock = runtime.join("voxcpm-resident.sock");
        let result = tokio::time::timeout(Duration::from_millis(400), async move {
            let mut stream = UnixStream::connect(sock).await?;
            stream.write_all(b"{\"command\":\"ping\"}\n").await?;
            let mut buffer = vec![0u8; 8192];
            let n = stream.read(&mut buffer).await?;
            let value: Value = serde_json::from_slice(&buffer[..n])?;
            Ok::<bool, Box<dyn std::error::Error + Send + Sync>>(
                value.pointer("/result/gpu_loaded").and_then(Value::as_bool) == Some(true),
            )
        })
        .await;
        result.ok().and_then(Result::ok).unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        let _ = runtime;
        false
    }
}

fn engine_status_payload(
    ready: bool,
    enabled: bool,
    resident: bool,
    running: i64,
    loaded: Option<bool>,
) -> Value {
    json!({
      "engine":"youdub:voxcpm2","installed":ready,"enabled":enabled,
      "resident":resident,
      "accepting_jobs":ready && enabled,"active_jobs":running,
      "busy":running>0,"mode":if resident{"resident"}else{"on_demand"},
      "gpu_loaded":loaded,
      "message":if loaded==Some(true){"模型已驻留在 GPU，连续任务复用模型"}
         else if resident{"驻留进程正在加载模型；请等待显存状态更新"}
         else if ready && enabled{"模型按任务加载，结束后释放显存"}
         else if ready{"已停止接受新配音任务"}
         else{"YouDub VoxCPM2 模型或运行环境尚未安装"}
    })
}

async fn active_jobs(state: &AppState) -> AppResult<i64> {
    let from_python: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM dubbing_jobs WHERE request_json->>'model_path'='youdub:voxcpm2'
         AND status IN('queued','preparing','synthesizing','aligning','mixing',
                       'rendering','pause_requested','cancel_requested')",
    )
    .fetch_one(&state.db.pool)
    .await?;
    let from_rust: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM operation_inbox WHERE runtime_engine='rust-v2'
          AND stage='dubbing' AND status IN('pending','awaiting','running','processing')",
    )
    .fetch_one(&state.db.pool)
    .await?;
    Ok(from_python.saturating_add(from_rust))
}
async fn status(state: &AppState) -> AppResult<Value> {
    let runtime = runtime_path(state);
    let config: Value = match tokio::fs::read(runtime.join("videoroll-control.json")).await {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    };
    let enabled = config.get("enabled").and_then(Value::as_bool) == Some(true);
    let resident = enabled && config.get("resident").and_then(Value::as_bool) == Some(true);
    let ready = installed(state, &runtime);
    let active = active_jobs(state).await?;
    let loaded = if resident && active == 0 {
        Some(resident_loaded(&runtime).await)
    } else if active > 0 {
        None
    } else {
        Some(false)
    };
    Ok(engine_status_payload(
        ready, enabled, resident, active, loaded,
    ))
}
async fn get_native(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(status(&state).await?))
}
async fn get_settings(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let native = status(&state).await?;
    // Rust scheduler and its internal worker are in this process; reporting
    // a deprecated Python systemd unit as inactive would mislead after cutover.
    let available = state.config.worker_count > 0;
    Ok(Json(json!({"native":native,"worker":{
      "state":if available{"active"}else{"inactive"},
      "available":available
    }})))
}

#[derive(Debug, Deserialize)]
struct EngineControl {
    enabled: bool,
    #[serde(default)]
    resident: Option<bool>,
}
fn bridge_path(state: &AppState) -> PathBuf {
    if let Some(path) = state
        .config
        .youdub_bridge
        .clone()
        .or_else(|| std::env::var_os("VIDEOROLL_V2_YOUDUB_BRIDGE").map(PathBuf::from))
    {
        return path;
    }
    // The service may be started from the repository root or from rust/.
    // Explicit deployment overrides still take precedence.
    for path in [
        "scripts/native/youdub_bridge.py",
        "../scripts/native/youdub_bridge.py",
    ] {
        let candidate = PathBuf::from(path);
        if candidate.is_file() {
            return candidate;
        }
    }
    PathBuf::from("scripts/native/youdub_bridge.py")
}
fn control_config(runtime: &Path) -> Value {
    fs::read(runtime.join("videoroll-control.json"))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_else(|| json!({}))
}
fn checked_runtime(runtime: &Path) -> AppResult<()> {
    fs::create_dir_all(runtime)?;
    let meta = fs::symlink_metadata(runtime)?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err(AppError::Conflict(
            "YouDub runtime is not a normal directory".into(),
        ));
    }
    Ok(())
}
fn write_control(runtime: &Path, enabled: bool, resident: bool) -> AppResult<()> {
    let path = runtime.join("videoroll-control.json");
    let mut tmp = tempfile::NamedTempFile::new_in(runtime)?;
    serde_json::to_writer(&mut tmp, &json!({"enabled":enabled,"resident":resident}))
        .map_err(|e| AppError::Invalid(format!("invalid YouDub control JSON: {e}")))?;
    tmp.write_all(b"\n")?;
    tmp.as_file().sync_all()?;
    tmp.persist(&path).map_err(|err| AppError::Io(err.error))?;
    Ok(())
}
fn resident_pid(runtime: &Path) -> AppResult<Option<u32>> {
    match fs::read_to_string(runtime.join("voxcpm-resident.pid")) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
        Ok(text) => {
            let pid: u32 = text
                .trim()
                .parse()
                .map_err(|_| AppError::Conflict("invalid resident PID file".into()))?;
            if pid < 2 {
                return Err(AppError::Conflict("unsafe resident PID".into()));
            }
            Ok(Some(pid))
        }
    }
}
#[cfg(target_os = "linux")]
fn pid_matches(pid: u32, bridge: &Path) -> AppResult<bool> {
    let bytes = match fs::read(format!("/proc/{pid}/cmdline")) {
        Ok(b) => b,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e.into()),
    };
    let expect = bridge.to_string_lossy();
    let fields = bytes
        .split(|b| *b == 0)
        .filter(|v| !v.is_empty())
        .map(String::from_utf8_lossy)
        .collect::<Vec<_>>();
    Ok(fields.iter().any(|v| v.as_ref() == expect.as_ref())
        && fields.iter().any(|v| v.as_ref() == "--serve"))
}
#[cfg(not(target_os = "linux"))]
fn pid_matches(_pid: u32, _bridge: &Path) -> AppResult<bool> {
    Err(AppError::Conflict(
        "resident process management requires Linux".into(),
    ))
}
async fn gpu_free_megabytes() -> AppResult<i64> {
    let mut command = Command::new("nvidia-smi");
    command
        .args(["--query-gpu=memory.free", "--format=csv,noheader,nounits"])
        .kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(8), command.output())
        .await
        .map_err(|_| AppError::Conflict("nvidia-smi GPU memory query timed out".into()))?
        .map_err(|e| AppError::Conflict(format!("nvidia-smi unavailable: {e}")))?;
    if !output.status.success() {
        return Err(AppError::Conflict(
            "nvidia-smi GPU memory query failed".into(),
        ));
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .next()
        .and_then(|text| text.trim().parse().ok())
        .ok_or_else(|| AppError::Conflict("invalid GPU memory query result".into()))
}
async fn check_resident_process(runtime: &Path, bridge: &Path) -> AppResult<bool> {
    let Some(pid) = resident_pid(runtime)? else {
        // A live unowned socket must never be overwritten.
        if resident_loaded(runtime).await {
            return Err(AppError::Conflict(
                "unowned YouDub resident socket is active".into(),
            ));
        }
        return Ok(false);
    };
    if pid_matches(pid, bridge)? {
        return Ok(true);
    }
    if Path::new(&format!("/proc/{pid}")).exists() {
        let state = fs::read_to_string(format!("/proc/{pid}/stat"))
            .ok()
            .and_then(|v| v.rsplit_once(") ").map(|(_, rest)| rest.starts_with('Z')))
            .unwrap_or(false);
        if !state {
            return Err(AppError::Conflict(
                "resident PID belongs to another process".into(),
            ));
        }
    }
    Ok(false)
}
fn remove_stale_files(runtime: &Path) -> AppResult<()> {
    for name in ["voxcpm-resident.pid", "voxcpm-resident.sock"] {
        let p = runtime.join(name);
        match fs::remove_file(p) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}
async fn stop_resident(runtime: &Path, bridge: &Path) -> AppResult<()> {
    let owned = check_resident_process(runtime, bridge).await?;
    if owned {
        let pid = resident_pid(runtime)?
            .ok_or_else(|| AppError::Conflict("resident PID vanished".into()))?;
        let status = Command::new("/bin/kill")
            .arg("-TERM")
            .arg(pid.to_string())
            .status()
            .await?;
        if !status.success() && pid_matches(pid, bridge)? {
            return Err(AppError::Conflict(
                "could not signal YouDub resident".into(),
            ));
        }
        for _ in 0..50 {
            if !pid_matches(pid, bridge)? {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        if pid_matches(pid, bridge)? {
            let status = Command::new("/bin/kill")
                .arg("-KILL")
                .arg(pid.to_string())
                .status()
                .await?;
            if !status.success() || pid_matches(pid, bridge)? {
                return Err(AppError::Conflict("YouDub resident did not stop".into()));
            }
        }
    }
    if !resident_loaded(runtime).await {
        remove_stale_files(runtime)?;
    } else {
        return Err(AppError::Conflict("resident socket still active".into()));
    }
    Ok(())
}
pub(crate) async fn start_resident(state: &AppState, runtime: &Path) -> AppResult<bool> {
    let bridge = fs::canonicalize(bridge_path(state))
        .map_err(|_| AppError::Conflict("YouDub bridge is missing".into()))?;
    if !bridge.is_file() {
        return Err(AppError::Conflict("YouDub bridge is not a file".into()));
    }
    if check_resident_process(runtime, &bridge).await? {
        return Ok(false);
    }
    let free = gpu_free_megabytes().await?;
    if free < 12_000 {
        return Err(AppError::Conflict(format!(
            "GPU free VRAM is only {free} MiB; residence needs >=12000 MiB"
        )));
    }
    remove_stale_files(runtime)?;
    let python = native_python(state, runtime);
    if !python.is_file() {
        return Err(AppError::Conflict("YouDub native Python is missing".into()));
    }
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(runtime.join("voxcpm-resident.log"))?;
    let mut command = StdCommand::new(python);
    command
        .arg(&bridge)
        .arg("--serve")
        .env("VOXCPM_OPTIMIZE", "false")
        .env("VOXCPM_MODEL_DIR", model_path(runtime))
        .env("VIDEOROLL_YOUDUB_RUNTIME", runtime)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    // Match the Python original start_new_session=True: the heavy resident
    // must not inherit the Rust API's terminal/session lifecycle.
    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let mut child = command
        .spawn()
        .map_err(|e| AppError::Conflict(format!("failed to start YouDub resident: {e}")))?;
    // Persist ownership before yielding; a canceled HTTP future must not
    // silently orphan an active GPU-resident child without a PID file.
    if let Err(err) = fs::write(
        runtime.join("voxcpm-resident.pid"),
        format!("{}\n", child.id()),
    ) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(err.into());
    }
    tokio::time::sleep(Duration::from_millis(80)).await;
    if let Some(exit) = child.try_wait()? {
        remove_stale_files(runtime)?;
        return Err(AppError::Conflict(format!(
            "YouDub resident exited during startup: {exit}"
        )));
    }
    Ok(true)
}
async fn configure_native(
    State(state): State<AppState>,
    Json(body): Json<EngineControl>,
) -> AppResult<Json<Value>> {
    let runtime = runtime_path(&state);
    // Python uses flock on this exact same file. The file guard is held
    // across model validation, busy check and process/config updates.
    let root = runtime.clone();
    let guard = tokio::task::spawn_blocking(move || {
        checked_runtime(&root)?;
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(root.join(".videoroll-control.lock"))?;
        file.lock()?;
        Ok::<_, AppError>(file)
    })
    .await
    .map_err(|e| AppError::Unavailable(format!("YouDub lock task crashed: {e}")))??;
    let original = control_config(&runtime);
    let old_resident = original.get("resident").and_then(Value::as_bool) == Some(true);
    let requested_resident = body.resident.unwrap_or(old_resident) && body.enabled;
    if body.enabled && !installed(&state, &runtime) {
        return Err(AppError::Conflict(
            "YouDub VoxCPM2 runtime/model is not ready".into(),
        ));
    }
    if (!body.enabled || requested_resident != old_resident) && active_jobs(&state).await? > 0 {
        return Err(AppError::Conflict(
            "queued/running YouDub jobs must finish before disabling or changing model residency"
                .into(),
        ));
    }
    let mut started = false;
    if requested_resident {
        started = start_resident(&state, &runtime).await?;
    } else {
        let source = bridge_path(&state);
        let bridge = fs::canonicalize(&source).unwrap_or(source);
        // Disabling needs the exact existing bridge identity so the PID guard
        // can never accidentally kill another process.
        stop_resident(&runtime, &bridge).await?;
    }
    if let Err(e) = write_control(&runtime, body.enabled, requested_resident) {
        if started {
            let source = bridge_path(&state);
            let bridge = fs::canonicalize(&source).unwrap_or(source);
            let _ = stop_resident(&runtime, &bridge).await;
        }
        return Err(e);
    }
    drop(guard);
    Ok(Json(status(&state).await?))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/dubbing", get(get_settings))
        .route(
            "/settings/dubbing/native",
            get(get_native).put(configure_native),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn busy_is_not_a_successful_gpu_ping() {
        let v = engine_status_payload(true, true, true, 2, None);
        assert_eq!(v["active_jobs"], 2);
        assert_eq!(v["gpu_loaded"], Value::Null);
        assert_eq!(v["accepting_jobs"], true);
    }
    #[test]
    fn disabled_runtime_never_claims_gpu_loaded() {
        let v = engine_status_payload(true, false, false, 0, Some(false));
        assert_eq!(v["accepting_jobs"], false);
        assert_eq!(v["resident"], false);
    }
    #[test]
    fn original_control_requires_enabled_but_preserves_unspecified_resident() {
        let v: EngineControl = serde_json::from_value(json!({"enabled":true})).unwrap();
        assert!(v.enabled);
        assert_eq!(v.resident, None);
        let v: EngineControl =
            serde_json::from_value(json!({"enabled":false,"resident":true})).unwrap();
        assert!(!v.enabled);
        assert_eq!(v.resident, Some(true));
    }
    #[test]
    fn original_control_rejects_missing_or_nonboolean_enabled() {
        assert!(serde_json::from_value::<EngineControl>(json!({"resident":true})).is_err());
        assert!(serde_json::from_value::<EngineControl>(json!({"enabled":"yes"})).is_err());
    }
}
