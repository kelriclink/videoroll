//! Narrow model subprocess boundary. Rust owns all workflow and database state.
use crate::{AppError, AppResult};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::Mutex,
    time::Instant,
};
const MAX_REQUEST: usize = 256 * 1024;
const MAX_RESPONSE: usize = 12 * 1024 * 1024;
static SESSION: Mutex<Option<Session>> = Mutex::const_new(None);
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

struct Session {
    key: Value,
    id: u64,
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
    last_used: Instant,
}
fn unavailable(message: &str) -> AppError {
    AppError::Unavailable(message.into())
}
fn parse_idle_timeout(value: Option<&str>) -> u64 {
    value
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(300)
        .clamp(1, 600)
}
fn idle_timeout() -> Duration {
    Duration::from_secs(parse_idle_timeout(
        std::env::var("VIDEOROLL_V2_LOCAL_INFERENCE_IDLE_SECONDS")
            .ok()
            .as_deref(),
    ))
}
fn parse_gpu_lock_slots(value: Option<&str>) -> u8 {
    value
        .and_then(|value| value.parse::<u8>().ok())
        .unwrap_or(1)
        .clamp(1, 8)
}
fn gpu_lock_slots() -> u8 {
    parse_gpu_lock_slots(
        std::env::var("VIDEOROLL_V2_LOCAL_INFERENCE_GPU_SLOTS")
            .ok()
            .as_deref(),
    )
}
fn gpu_lock_path() -> AppResult<PathBuf> {
    let path = std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_GPU_LOCK")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("VIDEOROLL_V2_STORAGE_ROOT")
                .map(|p| PathBuf::from(p).join(".local-inference-gpu.lock"))
        })
        .ok_or_else(|| unavailable("local inference GPU lock path is not configured"))?;
    if !path.is_absolute() {
        return Err(AppError::Invalid(
            "GPU lock must use an absolute path".into(),
        ));
    }
    Ok(path)
}
async fn model_identity(request: &Value) -> AppResult<Value> {
    let mut key = json!({});
    for field in [
        "kind",
        "engine",
        "model_path",
        "device",
        "compute_type",
        "cpu_threads",
        "num_workers",
    ] {
        key[field] = request[field].clone();
    }
    if let Some(path) = request["model_path"].as_str() {
        let meta = tokio::fs::metadata(path).await?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            // Atomic model replacement changes the directory inode. Changes to
            // files inside a live model are not supported during inference.
            key["model_revision"] =
                json!([meta.dev(), meta.ino(), meta.mtime(), meta.mtime_nsec()]);
        }
    }
    Ok(key)
}
impl Session {
    fn spawn(key: Value, python: &Path, bridge: &Path, lock: &Path) -> AppResult<Self> {
        let mut cmd = Command::new(python);
        cmd.arg("-I").arg(bridge).arg("--serve").env_clear();
        for name in [
            "PATH",
            "LD_LIBRARY_PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "CUDA_VISIBLE_DEVICES",
            "ONEAPI_DEVICE_SELECTOR",
            "ZE_AFFINITY_MASK",
        ] {
            if let Some(v) = std::env::var_os(name) {
                cmd.env(name, v);
            }
        }
        cmd.env("HF_HUB_OFFLINE", "1")
            .env("TRANSFORMERS_OFFLINE", "1")
            .env("VIDEOROLL_LOCAL_GPU_LOCK", lock)
            .env("VIDEOROLL_LOCAL_GPU_SLOTS", gpu_lock_slots().to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(target_os = "linux")]
        unsafe {
            let parent = libc::getpid();
            cmd.pre_exec(move || {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getppid() != parent {
                    return Err(std::io::Error::other("inference parent exited"));
                }
                Ok(())
            });
        }
        let mut child = cmd.spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| unavailable("local inference stdin absent"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| unavailable("local inference stdout absent"))?;
        Ok(Self {
            key,
            id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
            child,
            stdin,
            stdout,
            last_used: Instant::now(),
        })
    }
    async fn exchange(&mut self, input: &[u8]) -> AppResult<Value> {
        self.stdin.write_u32(input.len() as u32).await?;
        self.stdin.write_all(input).await?;
        self.stdin.flush().await?;
        let n = self.stdout.read_u32().await? as usize;
        if n == 0 || n > MAX_RESPONSE {
            return Err(unavailable("local inference output frame invalid"));
        }
        let mut data = vec![0; n];
        self.stdout.read_exact(&mut data).await?;
        let envelope: Value = serde_json::from_slice(&data)
            .map_err(|_| unavailable("local inference output schema invalid"))?;
        if envelope["ok"] != json!(true) {
            return Err(unavailable(
                "local model inference failed (check runtime, model and device)",
            ));
        }
        envelope
            .get("result")
            .cloned()
            .ok_or_else(|| unavailable("local inference response missing result"))
    }
}
fn monitor_idle(id: u64) {
    tokio::spawn(async move {
        loop {
            let wait = {
                let mut slot = SESSION.lock().await;
                let Some(session) = slot.as_ref().filter(|s| s.id == id) else {
                    return;
                };
                let idle = idle_timeout();
                let elapsed = session.last_used.elapsed();
                if elapsed >= idle {
                    if let Some(mut session) = slot.take() {
                        let _ = session.child.kill().await;
                    }
                    return;
                }
                idle - elapsed
            };
            tokio::time::sleep(wait).await;
        }
    });
}
fn env(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| default.into())
}
pub fn configured() -> bool {
    std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_PYTHON").is_some()
        && std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_BRIDGE").is_some()
}
pub async fn model_path(root: &Path, model: &str, embedding: bool) -> AppResult<PathBuf> {
    let root = tokio::fs::canonicalize(root)
        .await
        .map_err(|_| AppError::Unavailable("local model directory missing".into()))?;
    if !root.is_dir() || !root.is_absolute() {
        return Err(AppError::Invalid("invalid model root".into()));
    }
    let candidate = if embedding {
        let name: String = model
            .replace(['/', '\\'], "--")
            .chars()
            .map(|c| {
                if c.is_alphanumeric() || "._-".contains(c) {
                    c
                } else {
                    '-'
                }
            })
            .take(96)
            .collect();
        if name.is_empty() || name == "." || name == ".." {
            return Err(AppError::Invalid("invalid embedding model name".into()));
        }
        root.join(name)
    } else if Path::new(model).is_absolute() {
        PathBuf::from(model)
    } else {
        let name = model.replace(['/', '\\'], "--");
        if name == "." || name == ".." || name.is_empty() {
            return Err(AppError::Invalid("invalid ASR model name".into()));
        }
        root.join(name)
    };
    let path = tokio::fs::canonicalize(candidate)
        .await
        .map_err(|_| AppError::Unavailable("local model not downloaded".into()))?;
    if !path.is_dir() || !path.starts_with(&root) || path == root {
        return Err(AppError::Invalid(
            "local model outside configured root".into(),
        ));
    }
    Ok(path)
}
pub async fn asr_settings(engine: &str, model: &str, settings: &Value) -> AppResult<Value> {
    if !configured() {
        return Err(AppError::Unavailable(
            "local inference runtime is not configured".into(),
        ));
    }
    let root = PathBuf::from(env("SUBTITLE_WHISPER_MODEL_DIR", "/models/whisper"));
    let path = model_path(&root, model, false).await?;
    let device = if engine == "openvino" {
        settings["openvino_device"]
            .as_str()
            .unwrap_or("GPU")
            .to_owned()
    } else {
        env("SUBTITLE_WHISPER_DEVICE", "cpu")
    };
    let n = |k: &str, default: i64| {
        env(k, &default.to_string())
            .parse::<i64>()
            .unwrap_or(default)
            .clamp(0, 128)
    };
    Ok(
        json!({"kind":"asr","engine":engine,"model_path":path,"device":device,
       "compute_type":env("SUBTITLE_WHISPER_COMPUTE_TYPE","int8"),
       "cpu_threads":n("SUBTITLE_WHISPER_CPU_THREADS",0),"num_workers":n("SUBTITLE_WHISPER_NUM_WORKERS",1).max(1),
       "num_beams":settings["openvino_num_beams"].as_i64().unwrap_or(1).clamp(1,32),
       "max_new_tokens":settings["openvino_max_new_tokens"].as_i64().unwrap_or(448).clamp(1,2048),
       "vad_enabled":settings["openvino_vad_enabled"].as_bool().unwrap_or(true),
       "vad_threshold":settings["openvino_vad_threshold"].as_f64().unwrap_or(0.5).clamp(0.1,0.95)}),
    )
}
pub async fn invoke(request: &Value, timeout: Duration) -> AppResult<Value> {
    let python = PathBuf::from(
        std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_PYTHON")
            .ok_or_else(|| unavailable("local inference Python runtime not configured"))?,
    );
    let bridge = PathBuf::from(
        std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_BRIDGE")
            .ok_or_else(|| unavailable("local inference bridge not configured"))?,
    );
    if !python.is_absolute() || !bridge.is_absolute() {
        return Err(AppError::Invalid(
            "local inference executables must use absolute paths".into(),
        ));
    }
    let input = serde_json::to_vec(request)
        .map_err(|_| AppError::Invalid("local inference request invalid".into()))?;
    if input.len() > MAX_REQUEST {
        return Err(AppError::Invalid(
            "local inference request too large".into(),
        ));
    }
    let lock = gpu_lock_path()?;
    let timeout = timeout.clamp(Duration::from_secs(1), Duration::from_secs(1800));
    tokio::time::timeout(timeout,async {
        // The mutex is held throughout one exchange. Once taken out of the
        // slot, a session is owned by this future, so cancellation/timeout
        // drops the child (kill_on_drop) rather than retaining a stale reply.
        let mut slot=SESSION.lock().await;
        let key=json!({"model":model_identity(request).await?,"python":python,"bridge":bridge,"gpu_lock":lock});
        let mut existing=slot.take();
        if let Some(session)=existing.as_mut(){
            if session.key!=key {
                tracing::info!(from_kind=%session.key["model"]["kind"].as_str().unwrap_or("unknown"),to_kind=%request["kind"].as_str().unwrap_or("unknown"),from_device=%session.key["model"]["device"].as_str().unwrap_or("unknown"),to_device=%request["device"].as_str().unwrap_or("unknown"),"local inference model identity changed; resident process will be replaced and the next call may incur cold-start latency");
            }
            if session.key!=key||session.child.try_wait()?.is_some(){
                let _=session.child.kill().await;
                existing=None;
            }
        }
        let fresh=existing.is_none();
        let mut session=match existing{Some(s)=>s,None=>Session::spawn(key,&python,&bridge,&lock)?};
        let response=session.exchange(&input).await?;
        session.last_used=Instant::now();
        let id=session.id;
        *slot=Some(session);
        if fresh{monitor_idle(id);}
        Ok(response)
    }).await.map_err(|_|unavailable("local inference deadline exceeded"))?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn local_model_idle_timeout_defaults_to_five_minutes_and_is_bounded() {
        assert_eq!(parse_idle_timeout(None), 300);
        assert_eq!(parse_idle_timeout(Some("0")), 1);
        assert_eq!(parse_idle_timeout(Some("9999")), 600);
        assert_eq!(parse_idle_timeout(Some("bad")), 300);
    }
    #[test]
    fn local_model_gpu_slots_default_to_one_and_are_bounded() {
        assert_eq!(parse_gpu_lock_slots(None), 1);
        assert_eq!(parse_gpu_lock_slots(Some("0")), 1);
        assert_eq!(parse_gpu_lock_slots(Some("99")), 8);
        assert_eq!(parse_gpu_lock_slots(Some("3")), 3);
    }
}
