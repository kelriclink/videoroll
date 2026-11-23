//! Native Hugging Face snapshot model download for Python's
//! /subtitle/models/download, without a Python ASR / huggingface_hub service.
//!
//! Retrieves the live repo siblings manifest and streams every blob through
//! the configured ASR model proxy into a staging directory. The existing
//! model stays usable if a force replacement fails.
use crate::{AppError, AppResult, api::AppState, subtitle_models_v1};
use axum::{Json, Router, extract::State, routing::post};
use futures_util::StreamExt;
use reqwest::{Client, Proxy, Url};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{path::PathBuf, time::Duration};
use tokio::{fs, io::AsyncWriteExt};
use uuid::Uuid;

const MAX_REPO_BYTES: u64 = 512 * 1024 * 1024 * 1024;
const MAX_FILES: usize = 100_000;
fn def_engine() -> String {
    "faster-whisper".into()
}
#[derive(Debug, Deserialize)]
struct Request {
    #[serde(default = "def_engine")]
    engine: String,
    model: String,
    name: Option<String>,
    revision: Option<String>,
    #[serde(default)]
    force: bool,
}
#[derive(Debug, Deserialize)]
struct Sibling {
    rfilename: String,
    #[serde(default)]
    size: Option<u64>,
}
#[derive(Debug, Deserialize)]
struct Manifest {
    siblings: Vec<Sibling>,
}
fn model_repository(engine: &str, model: &str) -> AppResult<(String, String)> {
    let engine = engine.trim().to_lowercase();
    let model = model.trim();
    if !matches!(engine.as_str(), "faster-whisper" | "openvino" | "embedding") {
        return Err(AppError::Invalid(format!(
            "unsupported model download engine: {engine}"
        )));
    }
    if model.is_empty() {
        return Err(AppError::Invalid("model is required".into()));
    }
    let repo = match (engine.as_str(), model) {
        ("faster-whisper", "tiny") => "Systran/faster-whisper-tiny",
        ("faster-whisper", "base") => "Systran/faster-whisper-base",
        ("faster-whisper", "small") => "Systran/faster-whisper-small",
        ("faster-whisper", "medium") => "Systran/faster-whisper-medium",
        ("faster-whisper", "large-v1") => "Systran/faster-whisper-large-v1",
        ("faster-whisper", "large-v2") => "Systran/faster-whisper-large-v2",
        ("faster-whisper", "large-v3") => "Systran/faster-whisper-large-v3",
        ("openvino", "tiny") => "OpenVINO/whisper-tiny-fp16-ov",
        ("openvino", "base") => "OpenVINO/whisper-base-fp16-ov",
        ("openvino", "small") => "OpenVINO/whisper-small-fp16-ov",
        ("openvino", "medium") => "OpenVINO/whisper-medium-fp16-ov",
        ("openvino", "large-v3") => "OpenVINO/whisper-large-v3-fp16-ov",
        ("embedding", "bge-small-zh-v1.5") => "BAAI/bge-small-zh-v1.5",
        ("embedding", "bge-m3") => "BAAI/bge-m3",
        _ => model,
    }
    .to_owned();
    let default_name = if engine == "faster-whisper" && model != repo {
        model.to_owned()
    } else if repo != model {
        repo.split_once('/')
            .map(|(_, name)| name.to_string())
            .unwrap_or_default()
    } else {
        model.replace('/', "--")
    };
    // Python's custom model output uses replace('/',"--"), not a single '-'.
    let default_name = if repo == model {
        model.replace('/', "--")
    } else {
        default_name
    };
    Ok((repo, default_name))
}
fn segments(path: &str) -> AppResult<Vec<&str>> {
    let parts: Vec<&str> = path.split('/').collect();
    if path.starts_with('/')
        || path.ends_with('/')
        || parts.is_empty()
        || parts.len() > 50
        || path.len() > 4096
        || parts.iter().any(|p| {
            p.is_empty()
                || *p == "."
                || *p == ".."
                || p.contains('\\')
                || p.chars().any(char::is_control)
        })
    {
        return Err(AppError::Invalid(
            "invalid Hugging Face repository or file path".into(),
        ));
    }
    Ok(parts)
}
fn make_url(root: &Url, parts: &[&str]) -> AppResult<Url> {
    let mut uri = root.clone();
    uri.set_path("");
    uri.set_query(None);
    uri.path_segments_mut()
        .map_err(|_| AppError::Invalid("Hugging Face base URL is not hierarchical".into()))?
        .extend(parts.iter().copied());
    Ok(uri)
}
fn hf_base() -> AppResult<Url> {
    if let Ok(base) = std::env::var("VIDEOROLL_V2_TEST_HF_BASE") {
        let parsed = Url::parse(&base)
            .map_err(|_| AppError::Invalid("invalid model registry URL".into()))?;
        let loopback = matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
        if !loopback
            || !matches!(
                std::env::var("VIDEOROLL_V2_ALLOW_TEST_HF_LOOPBACK")
                    .ok()
                    .as_deref(),
                Some("true" | "1")
            )
        {
            return Err(AppError::Invalid(
                "model registry override permitted only on loopback test server".into(),
            ));
        }
        return Ok(parsed);
    }
    Url::parse("https://huggingface.co")
        .map_err(|_| AppError::Unavailable("invalid default Hub URL".into()))
}
fn enabled() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    )
}
async fn snapshot(
    State(state): State<AppState>,
    Json(body): Json<Request>,
) -> AppResult<Json<Value>> {
    snapshot_into(state, body, subtitle_models_v1::model_dir()).await
}
pub(crate) async fn embedding_snapshot(
    state: AppState,
    model: String,
    name: Option<String>,
    revision: Option<String>,
    force: bool,
    model_dir: PathBuf,
) -> AppResult<Json<Value>> {
    snapshot_into(
        state,
        Request {
            engine: "embedding".into(),
            model,
            name,
            revision,
            force,
        },
        model_dir,
    )
    .await
}
fn safe_embedding_name(name: &str) -> AppResult<String> {
    let raw = name.trim();
    if raw.is_empty() {
        return Err(AppError::Invalid("model is required".into()));
    }
    let result = raw
        .replace(['/', '\\'], "--")
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '-'
            }
        })
        .take(96)
        .collect::<String>();
    if result == "." || result == ".." || result.is_empty() {
        return Err(AppError::Invalid("invalid embedding model name".into()));
    }
    Ok(result)
}
async fn snapshot_into(state: AppState, body: Request, root: PathBuf) -> AppResult<Json<Value>> {
    if !enabled() {
        return Err(AppError::Conflict("V1 model download disabled".into()));
    }
    let (repo, default_name) = model_repository(&body.engine, &body.model)?;
    let supplied_name = body
        .name
        .as_deref()
        .filter(|s| !s.is_empty())
        .unwrap_or(&default_name)
        .trim();
    let name = if body.engine == "embedding" {
        safe_embedding_name(supplied_name)?
    } else {
        if !subtitle_models_v1::name_is_valid(supplied_name) {
            return Err(AppError::Invalid("invalid model name".into()));
        }
        supplied_name.to_owned()
    };
    let revision = body
        .revision
        .as_deref()
        .filter(|v| !v.is_empty())
        .unwrap_or("main");
    if revision.len() > 256
        || revision.chars().any(char::is_control)
        || revision
            .split('/')
            .any(|x| x.is_empty() || x == ".." || x == ".")
    {
        return Err(AppError::Invalid("invalid model revision".into()));
    }
    let repo_segments = segments(&repo)?;
    if repo_segments.len() > 2 {
        return Err(AppError::Invalid("invalid HF model repository ID".into()));
    }
    fs::create_dir_all(&root).await?;
    let dest = root.join(&name);
    let exists = fs::try_exists(&dest).await?;
    if exists && !body.force {
        return Err(AppError::Unavailable(
            "model already exists; set force=true to overwrite".into(),
        ));
    }
    let raw: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.asr'")
            .fetch_optional(&state.db.pool)
            .await?;
    let cfg = crate::subtitle_asr_settings_v1::view(&raw.unwrap_or(Value::Null));
    let proxy = cfg
        .get("model_download_proxy")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let mut builder = Client::builder()
        .timeout(Duration::from_secs(600))
        .redirect(reqwest::redirect::Policy::limited(10));
    if !proxy.is_empty() {
        builder = builder.proxy(Proxy::all(proxy).map_err(|e| AppError::Invalid(e.to_string()))?);
    }
    let client = builder.build()?;
    let base = hf_base()?;
    let repo_parts = repo_segments;
    let mut manifest_parts = vec!["api", "models"];
    manifest_parts.extend(repo_parts.iter().copied());
    manifest_parts.extend(["revision", revision]);
    let manifest_url = make_url(&base, &manifest_parts)?;
    let token = std::env::var("HF_TOKEN")
        .ok()
        .or_else(|| std::env::var("HUGGING_FACE_HUB_TOKEN").ok());
    let manifest_request = client.get(manifest_url);
    let manifest_request = if let Some(token) = token.as_deref() {
        manifest_request.bearer_auth(token)
    } else {
        manifest_request
    };
    let manifest_response = manifest_request.send().await?.error_for_status()?;
    let manifest: Manifest = manifest_response.json().await?;
    if manifest.siblings.len() > MAX_FILES {
        return Err(AppError::TooLarge(
            "Hugging Face model has too many files".into(),
        ));
    }
    let staging = root.join(format!(".{name}.downloading-{}", Uuid::new_v4().simple()));
    fs::create_dir(&staging).await?;
    let result = async {
        let mut total = 0u64;
        for sibling in &manifest.siblings {
            let filename = &sibling.rfilename;
            let path = segments(filename)?;
            let file = path.iter().fold(PathBuf::new(), |mut prefix, element| {
                prefix.push(element);
                prefix
            });
            if let Some(size) = sibling.size {
                if total
                    .checked_add(size)
                    .is_none_or(|next| next > MAX_REPO_BYTES)
                {
                    return Err(AppError::TooLarge("model snapshot exceeds 512 GiB".into()));
                }
            }
            let mut parts = repo_parts.clone();
            parts.extend(["resolve", revision]);
            parts.extend(path);
            let url = make_url(&base, &parts)?;
            let request = client.get(url);
            let request = if let Some(token) = token.as_deref() {
                request.bearer_auth(token)
            } else {
                request
            };
            let response = request.send().await?.error_for_status()?;
            let dest_file = staging.join(file);
            if let Some(parent) = dest_file.parent() {
                fs::create_dir_all(parent).await?;
            }
            let mut output = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(dest_file)
                .await?;
            let mut stream = response.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk?;
                total = total
                    .checked_add(chunk.len() as u64)
                    .ok_or_else(|| AppError::TooLarge("model snapshot size overflow".into()))?;
                if total > MAX_REPO_BYTES {
                    return Err(AppError::TooLarge("model snapshot exceeds 512 GiB".into()));
                }
                output.write_all(&chunk).await?;
            }
            output.sync_all().await?;
        }
        let backup = root.join(format!(".{name}.old-{}", Uuid::new_v4().simple()));
        if exists {
            fs::rename(&dest, &backup).await?;
        }
        if let Err(e) = fs::rename(&staging, &dest).await {
            if exists {
                let _ = fs::rename(&backup, &dest).await;
            }
            return Err(e.into());
        }
        if exists {
            let _ = fs::remove_dir_all(&backup).await;
        }
        let actual = tokio::task::spawn_blocking({
            let dest = dest.clone();
            move || subtitle_models_v1::dir_bytes(&dest)
        })
        .await
        .map_err(|_| AppError::Unavailable("model size inspection failed".into()))?;
        Ok(Json(
            json!({"name":name,"path":dest.to_string_lossy(),"size_bytes":actual}),
        ))
    }
    .await;
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging).await;
    }
    result
}
pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/models/download", post(snapshot))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn repository_names() {
        assert_eq!(
            model_repository("faster-whisper", "tiny").unwrap(),
            ("Systran/faster-whisper-tiny".into(), "tiny".into())
        );
        assert_eq!(
            model_repository("openvino", "base").unwrap(),
            (
                "OpenVINO/whisper-base-fp16-ov".into(),
                "whisper-base-fp16-ov".into()
            )
        );
        assert_eq!(
            model_repository("embedding", "bge-m3").unwrap(),
            ("BAAI/bge-m3".into(), "bge-m3".into())
        );
        assert_eq!(
            model_repository("faster-whisper", "ORG/custom").unwrap(),
            ("ORG/custom".into(), "ORG--custom".into())
        );
        assert!(model_repository("unknown", "tiny").is_err());
    }
}
