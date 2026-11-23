//! Python subtitle_service model-provider list, with native SQL readback.
//! Secrets never cross this V1 response boundary; custom rows are read from
//! the same original AppSetting used by the Python model provider store.
use crate::{AppError, AppResult, api::AppState, providers, settings};
use axum::{Json, Router, extract::State, routing::get};
use serde_json::{Value, json};

fn text_or(value: Option<&Value>, default: &str) -> String {
    value
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(default)
        .to_owned()
}
fn float_or(value: Option<&Value>, default: f64) -> f64 {
    value
        .and_then(Value::as_f64)
        .filter(|v| *v != 0.0)
        .unwrap_or(default)
}
fn number_or(value: Option<&Value>, default: i64) -> i64 {
    value
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .unwrap_or(default)
}
fn bool_or(value: Option<&Value>, default: bool) -> bool {
    value.and_then(Value::as_bool).unwrap_or(default)
}
pub(crate) fn normalize_openai_url(url: &str) -> String {
    let url = url.trim();
    if url.is_empty() {
        return "https://api.openai.com/v1".into();
    }
    let raw = if url.contains("://") {
        url.to_owned()
    } else {
        format!("https://{url}")
    };
    let raw = raw.trim_end_matches('/');
    let authority = raw.split_once("://").map(|(_, v)| v).unwrap_or("");
    if !authority.contains('/') {
        format!("{raw}/v1")
    } else {
        raw.to_owned()
    }
}
fn public_models(raw: &Value) -> AppResult<Vec<Value>> {
    let arr = raw
        .as_array()
        .ok_or_else(|| AppError::Invalid("provider models must be an array".into()))?;
    if arr.len() > 200 {
        return Err(AppError::Invalid("too many provider models".into()));
    }
    let mut models = Vec::new();
    for entry in arr {
        let id = entry.get("id").and_then(Value::as_str).unwrap_or("").trim();
        if id.is_empty() || id.len() > 256 {
            return Err(AppError::Invalid("invalid translation model ID".into()));
        }
        let ctx = entry
            .get("context_window")
            .filter(|v| !v.is_null())
            .cloned();
        let output = entry
            .get("max_output_tokens")
            .filter(|v| !v.is_null())
            .cloned();
        for val in [ctx.as_ref(), output.as_ref()].into_iter().flatten() {
            if !val
                .as_u64()
                .is_some_and(|n| (1..=9_007_199_254_740_991).contains(&n))
            {
                return Err(AppError::Invalid("invalid token limit".into()));
            }
        }
        if let (Some(ctx), Some(output)) = (ctx.as_ref(), output.as_ref()) {
            if output.as_u64() >= ctx.as_u64() {
                return Err(AppError::Invalid("output tokens must fit context".into()));
            }
        }
        let temp = entry.get("temperature").cloned().unwrap_or(json!(0.2));
        if temp.as_f64().is_some_and(|v| !(0.0..=2.0).contains(&v)) {
            return Err(AppError::Invalid("invalid model temperature".into()));
        }
        let effort = entry
            .get("reasoning_effort")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("medium");
        let format = entry
            .get("reasoning_format")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("parsed");
        if !matches!(effort, "low" | "medium" | "high")
            || !matches!(format, "parsed" | "raw" | "hidden")
        {
            return Err(AppError::Invalid("invalid model reasoning mode".into()));
        }
        models.push(json!({
            "id":id,"context_window":ctx,"max_output_tokens":output,
            "temperature":temp,
            "enable_thinking":bool_or(entry.get("enable_thinking"),false),
            "reasoning_effort":effort,"reasoning_format":format
        }));
    }
    Ok(models)
}
pub(crate) fn catalog(translate: &Value, custom: Vec<Value>) -> AppResult<Vec<Value>> {
    let openai = translate.get("openai").unwrap_or(&Value::Null);
    let v = |key: &str| translate.get(key);
    let open = |key: &str| openai.get(key);
    let fallback_base =
        std::env::var("OPENAI_BASE_URL").unwrap_or_else(|_| "https://api.openai.com/v1".into());
    let fallback_model = std::env::var("OPENAI_MODEL").unwrap_or_else(|_| "gpt-4o-mini".into());
    let fallback_temp = std::env::var("OPENAI_TEMPERATURE")
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.2);
    let fallback_timeout = std::env::var("OPENAI_TIMEOUT_SECONDS")
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(180.0);
    let fallback_retry = std::env::var("SUBTITLE_TRANSLATE_MAX_RETRIES")
        .ok()
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(2);
    let open_key_set = open("api_key_enc")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.trim().is_empty())
        || std::env::var("OPENAI_API_KEY").is_ok_and(|s| !s.trim().is_empty());
    let open_base = text_or(open("base_url"), &fallback_base);
    let open_base = normalize_openai_url(&open_base);
    let open_model = text_or(open("model"), &fallback_model);
    let compat = json!({
      "id":"legacy","name":"OpenAI 兼容（内建）",
      "base_url":open_base,"api_type":text_or(open("api_type"),"openai"),
      "api_key_set":open_key_set,
      "models":[{
        "id":open_model,"context_window":null,"max_output_tokens":null,
        "temperature":float_or(open("temperature"),fallback_temp),
        "enable_thinking":bool_or(open("enable_thinking"),false),
        "reasoning_effort":text_or(open("cerebras_reasoning_effort"),"medium"),
        "reasoning_format":text_or(open("cerebras_reasoning_format"),"parsed")
      }],
      "timeout_seconds":float_or(open("timeout_seconds"),fallback_timeout),
      "max_retries":number_or(open("max_retries"),number_or(v("openai_max_retries"),3)),
      "kind":"compatibility","editable":true,"deletable":false,
      "supports_remote_catalog":false,"options":{}
    });
    let index = json!({
      "id":"index_translate","name":"Index-Translate（官方免费）",
      "base_url":"https://index-translate.bilibili.com/v1",
      "api_type":"index_translate","api_key_set":false,
      "models":[{
        "id":"Index-Translate-35B-A3B","context_window":null,
        "max_output_tokens":number_or(v("index_max_tokens"),4096),
        "temperature":v("index_temperature").and_then(Value::as_f64).unwrap_or(0.0),
        "enable_thinking":false,"reasoning_effort":"medium","reasoning_format":"hidden"
      }],
      "timeout_seconds":float_or(v("index_timeout_seconds"),120.0),
      "max_retries":number_or(v("default_max_retries"),fallback_retry),
      "kind":"builtin","editable":true,"deletable":false,
      "supports_remote_catalog":false,
      "options":{
        "batch_size":number_or(v("index_batch_size"),12),
        "context_chars":number_or(v("index_context_chars"),6000),
        "domain":text_or(v("index_domain"),"视频字幕"),
        "glossary":text_or(v("index_glossary"),""),
        "instruction":text_or(v("index_instruction"),"")
      }
    });
    let mut out = vec![compat, index];
    for row in custom {
        let id = row
            .get("id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| AppError::Invalid("provider ID missing".into()))?;
        let name = row
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| AppError::Invalid("provider name missing".into()))?;
        let base = row
            .get("base_url")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| AppError::Invalid("provider URL missing".into()))?;
        let models = public_models(row.get("models").unwrap_or(&json!([])))?;
        out.push(json!({
          "id":id,"name":name,"base_url":base,
          "api_type":text_or(row.get("api_type"),"openai"),
          "api_key_set":row.get("api_key_enc").and_then(Value::as_str)
             .is_some_and(|s|!s.is_empty()),
          "models":models,
          "timeout_seconds":number_or(row.get("timeout_seconds"),180),
          "max_retries":number_or(row.get("max_retries"),3),
          "kind":"custom","editable":true,"deletable":true,
          "supports_remote_catalog":true,"options":{}
        }));
    }
    Ok(out)
}
async fn get_catalog(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let translate = settings::effective(&state.db, "subtitle.translate").await?;
    let providers = providers::read_catalog(&state.db).await?;
    Ok(Json(json!(catalog(&translate, providers)?)))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/subtitle/translate/model-providers",
            get(get_catalog).post(crate::subtitle_provider_write_v1::create),
        )
        .route(
            "/subtitle/translate/model-providers/{provider_id}",
            axum::routing::put(crate::subtitle_provider_write_v1::update)
                .delete(crate::subtitle_provider_write_v1::delete),
        )
        .route(
            "/subtitle/translate/model-providers/{provider_id}/remote-models",
            get(crate::subtitle_remote_models_v1::remote_models),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn builtin_models_and_invalid_custom_rejected() {
        let c = catalog(&json!({"index_temperature":0.0}), vec![]).unwrap();
        assert_eq!(c[0]["id"], "legacy");
        assert_eq!(c[1]["models"][0]["temperature"], json!(0.0));
        let bad = json!({"id":"foo","name":"Bar","base_url":"https://example.com",
           "api_key_enc":"dont-show","models":[{"id":"x","context_window":10,"max_output_tokens":10}]});
        assert!(catalog(&json!({}), vec![bad]).is_err());
    }
}
