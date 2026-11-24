//! Python-compatible knowledge Embedding text, credentials and OpenAI-compatible
//! HTTP request. Uses subtitle.translate original settings and pgvector, not
//! the V2 RAG shadow table. Local inference uses the isolated model adapter.
use crate::{AppError, AppResult, api::AppState};
use reqwest::{Client, Url, redirect::Policy};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{net::IpAddr, time::Duration};

#[derive(Clone)]
pub struct Settings {
    pub provider: String,
    pub model_dir: String,
    pub device: String,
    pub model: String,
    pub dimensions: usize,
    pub api_key: Option<String>,
    pub endpoint: Url,
    pub timeout_seconds: f64,
}
pub async fn load(state: &AppState) -> AppResult<Settings> {
    load_db(&state.db).await
}
pub async fn load_db(db: &crate::Database) -> AppResult<Settings> {
    let raw: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")
            .fetch_optional(&db.pool)
            .await?;
    let raw = raw.unwrap_or(Value::Null);
    let cfg = crate::subtitle_translate_settings_v1::view(&raw);
    let provider = cfg["rag_embedding_provider"]
        .as_str()
        .unwrap_or("openai")
        .trim()
        .to_ascii_lowercase();
    let model = cfg["rag_embedding_model"]
        .as_str()
        .unwrap_or("text-embedding-3-small")
        .trim()
        .to_owned();
    let dimensions = cfg["rag_embedding_dimensions"]
        .as_u64()
        .unwrap_or(1536)
        .clamp(1, 4096) as usize;
    let openai = raw.get("rag_embedding_openai").unwrap_or(&Value::Null);
    let cipher = openai
        .get("api_key_enc")
        .and_then(Value::as_str)
        .unwrap_or("");
    let key = if provider == "local" {
        None
    } else {
        crate::providers::decode_secret(cipher)?.or_else(|| {
            std::env::var("SUBTITLE_RAG_EMBEDDING_API_KEY")
                .ok()
                .filter(|s| !s.trim().is_empty())
        })
    };
    let raw_base = openai
        .get("base_url")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or("https://api.openai.com/v1");
    let endpoint = endpoint(if provider == "local" {
        "https://api.openai.com/v1"
    } else {
        raw_base
    })?;
    let timeout = openai
        .get("timeout_seconds")
        .and_then(Value::as_f64)
        .or_else(|| cfg["rag_embedding_timeout_seconds"].as_f64())
        .unwrap_or(60.)
        .clamp(5., 600.);
    Ok(Settings {
        provider,
        model_dir: cfg["rag_embedding_model_dir"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("/models/embeddings")
            .into(),
        device: cfg["rag_embedding_device"].as_str().unwrap_or("cpu").into(),
        model,
        dimensions,
        api_key: key,
        endpoint,
        timeout_seconds: timeout,
    })
}
pub(crate) fn endpoint(base: &str) -> AppResult<Url> {
    let base = base.trim().trim_end_matches('/');
    let base = if base.contains("://") {
        base.to_owned()
    } else {
        format!("https://{base}")
    };
    let mut parsed =
        Url::parse(&base).map_err(|_| AppError::Invalid("embedding URL invalid".into()))?;
    if parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err(AppError::Invalid(
            "embedding URL must not contain credentials, query or fragment".into(),
        ));
    }
    let loopback = parsed
        .host_str()
        .and_then(|h| h.parse::<IpAddr>().ok())
        .is_some_and(|ip| ip.is_loopback())
        || matches!(parsed.host_str(), Some("localhost"));
    if parsed.scheme() != "https" && !(parsed.scheme() == "http" && loopback) {
        return Err(AppError::Invalid(
            "embedding endpoint requires HTTPS or loopback".into(),
        ));
    }
    let path = parsed.path().trim_end_matches('/');
    let path = if path.ends_with("/embeddings") {
        path.to_owned()
    } else if path.is_empty() || path == "/" {
        "/v1/embeddings".into()
    } else {
        format!("{path}/embeddings")
    };
    parsed.set_path(&path);
    Ok(parsed)
}
pub fn embed_text(
    kind: &str,
    term: &str,
    translation: &str,
    domain: &str,
    aliases: &[String],
    title: &str,
    content: &str,
    description: &str,
) -> String {
    let fields = [
        format!("type: {kind}"),
        format!("domain: {domain}"),
        format!("term: {term}"),
        format!("translation: {translation}"),
        format!(
            "aliases: {}",
            aliases
                .iter()
                .filter(|s| !s.is_empty())
                .cloned()
                .collect::<Vec<_>>()
                .join(", ")
        ),
        format!("title: {title}"),
        format!("description: {description}"),
        format!("content: {content}"),
    ];
    fields
        .into_iter()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty() && !s.ends_with(':'))
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_owned()
}
pub fn hash_text(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}
pub fn model_key(settings: &Settings) -> String {
    format!("{}:{}", settings.provider, settings.model)
}
pub async fn embed(settings: &Settings, text: &str) -> AppResult<Vec<f32>> {
    if settings.provider == "local" {
        if text.trim().is_empty() || text.len() > 128 * 1024 {
            return Err(AppError::Invalid(
                "local embedding text size invalid".into(),
            ));
        }
        // The admin-selected directory must remain inside the deployment's
        // explicitly trusted model root (no arbitrary model code/path loading).
        let root = std::env::var("SUBTITLE_RAG_EMBEDDING_MODEL_DIR")
            .unwrap_or_else(|_| "/models/embeddings".into());
        let root = tokio::fs::canonicalize(root)
            .await
            .map_err(|_| AppError::Unavailable("embedding model root missing".into()))?;
        let requested_dir = if settings.model_dir == "/models/embeddings" {
            root.clone()
        } else {
            std::path::PathBuf::from(&settings.model_dir)
        };
        let requested = tokio::fs::canonicalize(&requested_dir)
            .await
            .map_err(|_| AppError::Unavailable("embedding model directory missing".into()))?;
        if !requested.starts_with(&root) {
            return Err(AppError::Invalid(
                "embedding directory outside trusted root".into(),
            ));
        }
        let path = crate::local_inference::model_path(&requested, &settings.model, true).await?;
        let value = crate::local_inference::invoke(
            &json!({"kind":"embedding","model_path":path,"device":settings.device,"text":text}),
            Duration::from_secs_f64(settings.timeout_seconds),
        )
        .await?;
        let vector = value["embedding"]
            .as_array()
            .ok_or_else(|| AppError::Unavailable("local embedding response missing vector".into()))?
            .iter()
            .map(|v| v.as_f64().map(|v| v as f32).filter(|v| v.is_finite()))
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| AppError::Unavailable("invalid embedding values".into()))?;
        if vector.len() != settings.dimensions || vector.iter().all(|v| *v == 0.) {
            return Err(AppError::Unavailable(format!(
                "local embedding dimension mismatch/empty vector: expected {}, got {}",
                settings.dimensions,
                vector.len()
            )));
        }
        return Ok(vector);
    }
    if settings.provider != "openai" {
        return Err(AppError::Invalid("unknown embedding provider".into()));
    }
    let key = settings
        .api_key
        .as_ref()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::Unavailable("OpenAI embedding API key is not set".into()))?;
    let client = Client::builder()
        .redirect(Policy::none())
        .timeout(Duration::from_secs_f64(settings.timeout_seconds))
        .build()?;
    let body = json!({"model":settings.model,"input":text,"dimensions":settings.dimensions});
    let mut latest_error = "embedding request failed".to_owned();
    for attempt in 0..3 {
        let request = client
            .post(settings.endpoint.clone())
            .bearer_auth(key)
            .json(&body)
            .send()
            .await;
        let response = match request {
            Ok(resp) => resp,
            Err(err) => {
                latest_error = format!("embedding transport failed: {}", err.is_timeout());
                if attempt < 2 {
                    tokio::time::sleep(Duration::from_millis(250 * (attempt + 1))).await;
                    continue;
                }
                break;
            }
        };
        let status = response.status();
        if !status.is_success() {
            latest_error = format!("embedding endpoint returned HTTP {}", status.as_u16());
            if attempt < 2 && (status.as_u16() == 429 || status.is_server_error()) {
                tokio::time::sleep(Duration::from_millis(250 * (attempt + 1))).await;
                continue;
            }
            break;
        }
        let data: Value = response.json().await.map_err(|_| {
            AppError::Unavailable("embedding endpoint returned malformed JSON".into())
        })?;
        let raw = data
            .get("data")
            .and_then(Value::as_array)
            .and_then(|rows| rows.first())
            .and_then(|item| item.get("embedding"))
            .and_then(Value::as_array)
            .ok_or_else(|| {
                AppError::Unavailable("embedding response missing data[0].embedding".into())
            })?;
        let vector = raw
            .iter()
            .map(|v| {
                v.as_f64().filter(|v| v.is_finite()).and_then(|v| {
                    if v.abs() <= f32::MAX as f64 {
                        Some(v as f32)
                    } else {
                        None
                    }
                })
            })
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| {
                AppError::Unavailable("embedding vector contains invalid values".into())
            })?;
        if vector.len() != settings.dimensions {
            return Err(AppError::Unavailable(format!(
                "embedding dimension mismatch: expected {}, got {}",
                settings.dimensions,
                vector.len()
            )));
        }
        return Ok(vector);
    }
    Err(AppError::Unavailable(latest_error))
}
fn vector_component(value: f32) -> String {
    // Python uses %.8g, not eight decimal places. Preserve tiny vector
    // components to avoid silently damaging cosine similarity rankings.
    let f = value as f64;
    if f == 0.0 {
        return "0".into();
    }
    let exponent = f.abs().log10().floor() as i32;
    if !(-4..8).contains(&exponent) {
        return format!("{f:.7e}");
    }
    let places = (7 - exponent) as usize;
    let output = format!("{f:.*}", places);
    if output.contains('.') {
        output
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_owned()
    } else {
        output
    }
}
pub fn vector_literal(vector: &[f32]) -> String {
    format!(
        "[{}]",
        vector
            .iter()
            .copied()
            .map(vector_component)
            .collect::<Vec<_>>()
            .join(",")
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn text_and_endpoint_match_python() {
        let s = embed_text(
            "term",
            "Trace",
            "",
            "PCB",
            &["wire".into(), "line".into()],
            "",
            "",
            "",
        );
        assert_eq!(
            s,
            "type: term\ndomain: PCB\nterm: Trace\naliases: wire, line"
        );
        assert_eq!(
            hash_text("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            endpoint("https://example.org").unwrap().path(),
            "/v1/embeddings"
        );
        assert_eq!(
            endpoint("http://127.0.0.1:18095/v1").unwrap().path(),
            "/v1/embeddings"
        );
        assert_eq!(vector_literal(&[0.5, -0.25, 0.125]), "[0.5,-0.25,0.125]");
        assert!(vector_literal(&[0.00000012_f32]).contains("e-7"));
        assert_eq!(vector_literal(&[0.123456789_f32]), "[0.12345679]");
        assert!(endpoint("http://198.51.100.3/v1").is_err());
        assert!(endpoint("https://u:p@example.org/v1").is_err());
    }
}
