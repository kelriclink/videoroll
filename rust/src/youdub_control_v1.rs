//! Read-only original YouDub control-plane DTO from the *actual* native runtime.
//! We do not infer GPU residency from a setting: the Unix resident socket
//! must answer a real ping before gpu_loaded can be true.
use crate::{AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::get};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

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
async fn resident_loaded(runtime: &Path) -> bool {
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

async fn status(state: &AppState) -> AppResult<Value> {
    let runtime = runtime_path(state);
    let config: Value = match tokio::fs::read(runtime.join("videoroll-control.json")).await {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    };
    let enabled = config.get("enabled").and_then(Value::as_bool) == Some(true);
    let resident = enabled && config.get("resident").and_then(Value::as_bool) == Some(true);
    let ready = installed(state, &runtime);
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
    let active = from_python.saturating_add(from_rust);
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
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/dubbing", get(get_settings))
        .route("/settings/dubbing/native", get(get_native))
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
}
