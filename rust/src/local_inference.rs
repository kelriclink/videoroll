//! Narrow model subprocess boundary. Rust owns all workflow and database state.
use crate::{AppError, AppResult};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::Semaphore,
};
static SLOT: Semaphore = Semaphore::const_new(1);
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
    let python = std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_PYTHON").ok_or_else(|| {
        AppError::Unavailable("local inference Python runtime not configured".into())
    })?;
    let bridge = std::env::var_os("VIDEOROLL_V2_LOCAL_INFERENCE_BRIDGE")
        .ok_or_else(|| AppError::Unavailable("local inference bridge not configured".into()))?;
    if !Path::new(&python).is_absolute() || !Path::new(&bridge).is_absolute() {
        return Err(AppError::Invalid(
            "local inference executables must use absolute paths".into(),
        ));
    }
    let input = serde_json::to_vec(request)
        .map_err(|_| AppError::Invalid("local inference request invalid".into()))?;
    if input.len() > 256 * 1024 {
        return Err(AppError::Invalid(
            "local inference request too large".into(),
        ));
    }
    let timeout = timeout.clamp(Duration::from_secs(1), Duration::from_secs(1800));
    tokio::time::timeout(timeout, async {
        let _slot = SLOT
            .acquire()
            .await
            .map_err(|_| AppError::Unavailable("local inference slot unavailable".into()))?;
        let mut cmd = Command::new(python);
        cmd.arg("-I").arg(bridge).env_clear();
        for key in [
            "PATH",
            "LD_LIBRARY_PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "CUDA_VISIBLE_DEVICES",
            "ONEAPI_DEVICE_SELECTOR",
            "ZE_AFFINITY_MASK",
        ] {
            if let Some(v) = std::env::var_os(key) {
                cmd.env(key, v);
            }
        }
        cmd.env("HF_HUB_OFFLINE", "1")
            .env("TRANSFORMERS_OFFLINE", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(target_os = "linux")]
        unsafe {
            cmd.pre_exec(|| {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = cmd.spawn()?;
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| AppError::Unavailable("local inference stdin absent".into()))?;
        stdin.write_all(&input).await?;
        drop(stdin);
        let mut output = vec![];
        child
            .stdout
            .take()
            .ok_or_else(|| AppError::Unavailable("local inference stdout absent".into()))?
            .take(12 * 1024 * 1024 + 1)
            .read_to_end(&mut output)
            .await?;
        if output.len() > 12 * 1024 * 1024 {
            return Err(AppError::Unavailable(
                "local inference output too large".into(),
            ));
        }
        if !child.wait().await?.success() {
            return Err(AppError::Unavailable(
                "local model inference failed (check installed runtime, model and device)".into(),
            ));
        }
        serde_json::from_slice(&output)
            .map_err(|_| AppError::Unavailable("local inference output schema invalid".into()))
    })
    .await
    .map_err(|_| AppError::Unavailable("local inference deadline exceeded".into()))?
}
