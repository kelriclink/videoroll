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
// Serialize requests within one role, but allow idle eviction during an exchange.
static EXCHANGE_GATE: Mutex<()> = Mutex::const_new(());
static SESSIONS: Mutex<Vec<Session>> = Mutex::const_new(Vec::new());
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

struct Session {
    key: Value,
    id: u64,
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
    last_used: Instant,
    idle_task: Option<tokio::task::AbortHandle>,
    idle_generation: u64,
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
fn is_cpu(model: &Value) -> bool {
    let device = model["device"]
        .as_str()
        .unwrap_or("")
        .trim()
        .to_ascii_uppercase();
    matches!(
        device.as_str(),
        "CPU" | "OPENVINO" | "OPENVINO:" | "OPENVINO:CPU"
    )
}
fn is_probe(model: &Value) -> bool {
    model["kind"] == "devices"
}
fn is_vad(model: &Value) -> bool {
    model["kind"] == "vad" && is_cpu(model)
}

fn pool_capacity() -> usize {
    std::env::var("VIDEOROLL_V2_LOCAL_INFERENCE_POOL_SIZE")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(2)
        .clamp(1, 2)
}
fn session_idle_timeout(model: &Value) -> Duration {
    if is_vad(model) {
        return Duration::from_secs(60);
    }
    if is_cpu(model) {
        return idle_timeout();
    }
    Duration::from_secs(
        std::env::var("VIDEOROLL_V2_LOCAL_INFERENCE_GPU_IDLE_SECONDS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(idle_timeout().as_secs().min(30))
            .clamp(1, 600),
    )
}
fn conflicts(old: &Value, new: &Value) -> bool {
    // One model per operation kind; conservatively at most one accelerator
    // model per role so a new child never waits for its own resident GPU slot.
    old["kind"] == new["kind"] || (!is_cpu(old) && !is_cpu(new))
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
            idle_task: None,
            idle_generation: 0,
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
fn monitor_idle(id: u64, generation: u64, idle: Duration) -> tokio::task::AbortHandle {
    tokio::spawn(async move {
        tokio::time::sleep(idle).await;
        let mut pool = SESSIONS.lock().await;
        if let Some(index) = pool
            .iter()
            .position(|s| s.id == id && s.idle_generation == generation)
        {
            let mut expired = pool.remove(index);
            let _ = expired.child.kill().await;
        }
    })
    .abort_handle()
}
async fn evict(mut session: Session) {
    if let Some(task) = session.idle_task.take() {
        task.abort();
    }
    let _ = session.child.kill().await;
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
    tokio::time::timeout(timeout, async {
        let _gate = EXCHANGE_GATE.lock().await;
        let key = json!({"model":model_identity(request).await?,"python":python,"bridge":bridge,"gpu_lock":lock});
        let probe = is_probe(&key["model"]);
        let mut existing = None;
        if !probe {
            let mut pool = SESSIONS.lock().await;
            if let Some(index) = pool.iter().position(|s| s.key == key) {
                let mut session = pool.remove(index);
                if let Some(task) = session.idle_task.take() { task.abort(); }
                if session.child.try_wait()?.is_none() { existing = Some(session); }
            }
            // Evict stale identities and same-role GPU holders before spawning.
            // Hold the pool lock through reaping so a replacement cannot race
            // the old process releasing its resource lock.
            let mut index = 0;
            while index < pool.len() {
                if conflicts(&pool[index].key["model"], &key["model"]) {
                    evict(pool.remove(index)).await;
                } else { index += 1; }
            }
            // Keep one bounded CPU VAD session independently of the two
            // heavyweight ASR/embedding roles. Otherwise every timing pass
            // evicts a warm model and defeats the prior pool optimization.
            let vad=is_vad(&key["model"]);
            let capacity=if vad{1}else{pool_capacity()};
            while pool.iter().filter(|s|is_vad(&s.key["model"])==vad).count()>=capacity{
                let index=pool.iter().enumerate().filter(|(_,s)|is_vad(&s.key["model"])==vad).min_by_key(|(_,s)|s.last_used).unwrap().0;
                evict(pool.remove(index)).await;
            }
        }
        // While the exchange waits for admission or inference, idle siblings
        // can still be reaped. Cancellation drops only the checked-out child.
        let mut session = match existing { Some(s) => s, None => Session::spawn(key, &python, &bridge, &lock)? };
        let response = session.exchange(&input).await?;
        if probe { evict(session).await; return Ok(response); }
        session.last_used = Instant::now();
        session.idle_generation = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let idle = session_idle_timeout(&session.key["model"]);
        let mut pool = SESSIONS.lock().await;
        session.idle_task = Some(monitor_idle(session.id, session.idle_generation, idle));
        pool.push(session);
        Ok(response)
    }).await.map_err(|_| unavailable("local inference deadline exceeded"))?
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
