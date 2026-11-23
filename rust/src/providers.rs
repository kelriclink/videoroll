//! Read legacy translation provider catalog without mutating v1 settings.
//! Fail closed for removed model/provider IDs and unsupported legacy ciphertext.
use crate::{AppError, AppResult, Config, Database};
use reqwest::Url;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

const PROVIDERS_KEY: &str = "subtitle.model_providers";
#[derive(Debug, Clone)]
pub struct TranslationProvider {
    pub id: String,
    pub endpoint: Url,
    pub model: String,
    pub api_type: String,
    pub api_key: Option<String>,
    pub temperature: f64,
    pub max_output_tokens: Option<u64>,
    pub context_window: Option<u64>,
    pub enable_thinking: bool,
    pub timeout_seconds: u64,
    pub max_retries: u32,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublicProvider {
    pub id: String,
    pub name: String,
    pub api_type: String,
    pub base_url: String,
    pub api_key_set: bool,
    pub models: Vec<Value>,
    pub editable: bool,
}
fn endpoint(value: &str) -> AppResult<Url> {
    let mut parsed =
        Url::parse(value).map_err(|_| AppError::Invalid("translation base URL invalid".into()))?;
    let allowed = parsed.scheme() == "https"
        || (parsed.scheme() == "http"
            && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1")));
    if !allowed || parsed.username() != "" || parsed.password().is_some() {
        return Err(AppError::Invalid(
            "translation base URL requires HTTPS or loopback".into(),
        ));
    }
    if !parsed.path().ends_with("/chat/completions") {
        let path = parsed.path().trim_end_matches('/');
        let final_path = if path.is_empty() || path == "/" {
            "/v1/chat/completions".to_string()
        } else {
            format!("{path}/chat/completions")
        };
        parsed.set_path(&final_path);
    }
    Ok(parsed)
}
pub(crate) fn decode_secret(encoded: &str) -> AppResult<Option<String>> {
    if encoded.trim().is_empty() {
        return Ok(None);
    }
    if encoded.starts_with("gAAAA") {
        return Err(AppError::Unavailable(
            "legacy Fernet credential cannot be recovered without the original encryption key"
                .into(),
        ));
    }
    let decoded = encoded.strip_prefix("dbplain:v1:").unwrap_or(encoded);
    if decoded.is_empty()
        || decoded.len() > 16384
        || decoded.contains('\r')
        || decoded.contains('\n')
    {
        return Err(AppError::Invalid("invalid stored translation token".into()));
    }
    Ok(Some(decoded.into()))
}
fn parse_positive(value: Option<&Value>) -> Option<u64> {
    let value = value?;
    let v = value
        .as_u64()
        .or_else(|| value.as_str().and_then(|s| s.parse().ok()))?;
    if (1..=9_007_199_254_740_991).contains(&v) {
        Some(v)
    } else {
        None
    }
}
pub async fn read_catalog(db: &Database) -> AppResult<Vec<Value>> {
    let row: Option<Value> = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(PROVIDERS_KEY)
        .fetch_optional(&db.pool)
        .await?;
    let fallback = row.unwrap_or(json!({}));
    let rows = fallback
        .get("providers")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if rows.len() > 200 {
        return Err(AppError::Invalid(
            "provider catalog exceeds configured limit".into(),
        ));
    }
    Ok(rows)
}
pub async fn public_catalog(db: &Database) -> AppResult<Vec<PublicProvider>> {
    let mut items = Vec::new();
    for v in read_catalog(db).await? {
        let (Some(id), Some(name), Some(base)) = (
            v.get("id").and_then(Value::as_str),
            v.get("name").and_then(Value::as_str),
            v.get("base_url").and_then(Value::as_str),
        ) else {
            continue;
        };
        let models = v
            .get("models")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        items.push(PublicProvider {
            id: id.into(),
            name: name.into(),
            base_url: base.into(),
            api_type: v
                .get("api_type")
                .and_then(Value::as_str)
                .unwrap_or("openai")
                .into(),
            api_key_set: v
                .get("api_key_enc")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty()),
            models,
            editable: true,
        });
    }
    Ok(items)
}
pub async fn resolve(
    db: &Database,
    config: &Config,
    provider_id: &str,
    model_id: &str,
) -> AppResult<TranslationProvider> {
    let provider = provider_id.trim();
    if provider == "legacy" {
        // The native control page writes original subtitle.translate.openai.
        // Resolve it at job execution time, otherwise a successful settings
        // update would leave V2 translating with stale startup env values.
        let saved = crate::settings::effective(db, "subtitle.translate").await?;
        let open = saved
            .get("openai")
            .filter(|v| v.is_object())
            .unwrap_or(&Value::Null);
        let stored_base = open
            .get("base_url")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        let stored_model = open
            .get("model")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        let base = stored_base.or(config.translate_endpoint.as_deref())
            .ok_or_else(|| AppError::Unavailable(
                "legacy translation endpoint missing; configure subtitle.translate or V2_TRANSLATE_ENDPOINT".into()
            ))?;
        let model = stored_model.or(config.translate_model.as_deref())
            .ok_or_else(|| AppError::Unavailable(
                "legacy translation model missing; configure subtitle.translate or V2_TRANSLATE_MODEL".into()
            ))?;
        if !model_id.is_empty() && model_id != model {
            return Err(AppError::Conflict(
                "legacy translation model differs from selected model".into(),
            ));
        }
        let api_type = open
            .get("api_type")
            .and_then(Value::as_str)
            .unwrap_or("openai");
        if !matches!(api_type, "openai" | "cerebras") {
            return Err(AppError::Invalid(
                "unsupported saved translation API type".into(),
            ));
        }
        let key = match open.get("api_key_enc").and_then(Value::as_str) {
            Some(encoded) if !encoded.is_empty() => decode_secret(encoded)?,
            _ => config.translate_api_key.clone(),
        };
        return Ok(TranslationProvider {
            id: "legacy".into(),
            endpoint: endpoint(base)?,
            model: model.into(),
            api_type: api_type.into(),
            api_key: key,
            temperature: open
                .get("temperature")
                .and_then(Value::as_f64)
                .unwrap_or(0.2),
            max_output_tokens: None,
            context_window: None,
            enable_thinking: open
                .get("enable_thinking")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            timeout_seconds: open
                .get("timeout_seconds")
                .and_then(Value::as_u64)
                .unwrap_or(180)
                .clamp(5, 1200),
            max_retries: open
                .get("max_retries")
                .and_then(Value::as_u64)
                .unwrap_or(3)
                .clamp(1, 10) as u32,
        });
    }
    if provider == "index_translate" {
        return Err(AppError::Invalid(
            "Index-Translate must use its own API adapter, not an OpenAI provider".into(),
        ));
    }
    let entry = read_catalog(db)
        .await?
        .into_iter()
        .find(|v| v.get("id").and_then(Value::as_str) == Some(provider))
        .ok_or_else(|| AppError::NotFound("selected translation provider was removed".into()))?;
    if model_id.trim().is_empty() {
        return Err(AppError::Invalid(
            "selected provider requires an explicit model ID".into(),
        ));
    }
    let model = entry
        .get("models")
        .and_then(Value::as_array)
        .and_then(|rows| {
            rows.iter()
                .find(|m| m.get("id").and_then(Value::as_str) == Some(model_id))
        })
        .ok_or_else(|| AppError::NotFound("selected translation model was removed".into()))?;
    let raw = entry
        .get("base_url")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Invalid("translation provider base_url absent".into()))?;
    let api_type = entry
        .get("api_type")
        .and_then(Value::as_str)
        .unwrap_or("openai");
    if !matches!(api_type, "openai" | "cerebras") {
        return Err(AppError::Invalid(
            "unsupported translation provider API type".into(),
        ));
    }
    let ctx = parse_positive(model.get("context_window"));
    let output = parse_positive(model.get("max_output_tokens"));
    if ctx.is_some() && output.is_some() && output >= ctx {
        return Err(AppError::Invalid(
            "model maximum output must fit into context".into(),
        ));
    }
    let secret = decode_secret(
        entry
            .get("api_key_enc")
            .and_then(Value::as_str)
            .unwrap_or(""),
    )?;
    let timeout = entry
        .get("timeout_seconds")
        .and_then(Value::as_u64)
        .unwrap_or(180)
        .clamp(5, 1200);
    let retries = entry
        .get("max_retries")
        .and_then(Value::as_u64)
        .unwrap_or(3)
        .clamp(1, 10) as u32;
    Ok(TranslationProvider {
        id: provider.into(),
        endpoint: endpoint(raw)?,
        model: model_id.into(),
        api_type: api_type.into(),
        api_key: secret,
        temperature: model
            .get("temperature")
            .and_then(Value::as_f64)
            .unwrap_or(0.2)
            .clamp(0., 2.),
        max_output_tokens: output,
        context_window: ctx,
        enable_thinking: model
            .get("enable_thinking")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        timeout_seconds: timeout,
        max_retries: retries,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_db_credentials_are_not_exposed_or_silently_dropped() {
        assert_eq!(
            decode_secret("dbplain:v1:secret").unwrap(),
            Some("secret".into())
        );
        assert_eq!(decode_secret("").unwrap(), None);
        assert!(decode_secret("gAAAAold-fernet").is_err());
    }
    #[test]
    fn custom_urls_are_not_ssrf_links() {
        assert_eq!(
            endpoint("https://api.example.com/v1").unwrap().path(),
            "/v1/chat/completions"
        );
        assert!(endpoint("http://127.0.0.1:3333/v1").is_ok());
        assert!(endpoint("http://10.0.0.1:3000/v1").is_err());
        assert!(endpoint("file:///tmp/password").is_err());
    }
    #[test]
    fn token_limits_are_exact_safe_integers() {
        assert_eq!(parse_positive(Some(&json!("200000"))), Some(200_000));
        assert_eq!(parse_positive(Some(&json!(9_007_199_254_740_992u64))), None);
        assert_eq!(parse_positive(Some(&json!(0))), None);
    }
}
