//! Original Subtitle Service `POST /subtitle/translate/test` native diagnostics.
//! Resolves the selected automatic translation model from original settings,
//! makes a real bounded request and returns the original `translated_text` DTO.
use crate::{AppError, AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::post};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::Duration;

#[derive(Debug, Deserialize)]
#[serde(default)]
struct TranslateRequest {
    text: String,
    target_lang: String,
    style: String,
}
impl Default for TranslateRequest {
    fn default() -> Self {
        Self {
            text: "Hello world.".into(),
            target_lang: "zh".into(),
            style: "口语自然".into(),
        }
    }
}
fn validate(body: &TranslateRequest) -> AppResult<(&str, &str, &str)> {
    let text = body.text.trim();
    if text.is_empty() {
        return Err(AppError::Invalid("text is required".into()));
    }
    if text.chars().count() > 2000 {
        return Err(AppError::Invalid("text too long (max 2000 chars)".into()));
    }
    let target = body.target_lang.trim();
    let style = body.style.trim();
    if target.len() > 80
        || style.len() > 200
        || target.chars().any(char::is_control)
        || style.chars().any(char::is_control)
    {
        return Err(AppError::Invalid(
            "invalid translation test language or style".into(),
        ));
    }
    Ok((
        text,
        if target.is_empty() { "zh" } else { target },
        if style.is_empty() {
            "口语自然"
        } else {
            style
        },
    ))
}
fn message_text(value: &Value) -> AppResult<&str> {
    value
        .pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| AppError::Unavailable("translation model returned empty content".into()))
}
async fn test(
    State(state): State<AppState>,
    Json(input): Json<TranslateRequest>,
) -> AppResult<Json<Value>> {
    let (source, target, style) = validate(&input)?;
    let settings = crate::settings::effective(&state.db, "subtitle.translate").await?;
    let profile = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
    let provider = profile
        .get("translate_provider")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .or_else(|| settings.get("default_provider").and_then(Value::as_str))
        .unwrap_or("openai");
    let (endpoint, model_name, timeout, body, key, operation) = match provider {
        "openai" => {
            let selected = profile
                .get("translate_model_provider_id")
                .and_then(Value::as_str)
                .filter(|v| !v.is_empty())
                .unwrap_or("legacy");
            let model_id = profile
                .get("translate_model_id")
                .and_then(Value::as_str)
                .unwrap_or("");
            let resolved =
                crate::providers::resolve(&state.db, &state.config, selected, model_id).await?;
            let prompt = format!(
                "请翻译下面这段文本。\n要求：\n- 只输出 JSON 对象，不要输出解释；\n- 只翻译内容本身，不要补充；\n- 目标语言：{target}\n- 风格：{style}\n\n输入文本：\n{source}\n\n输出 JSON：{{\"translation\":\"\"}}"
            );
            let mut body = json!({"model":resolved.model,"temperature":resolved.temperature,
                "response_format":{"type":"json_object"},"messages":[
                    {"role":"system","content":"You are a professional translator. Return ONLY valid JSON."},
                    {"role":"user","content":prompt}]});
            if resolved.enable_thinking {
                body["chat_template_kwargs"] = json!({"enable_thinking":true});
            }
            if let Some(max) = resolved.max_output_tokens {
                body["max_tokens"] = json!(max.min(8192));
            }
            (
                resolved.endpoint,
                resolved.model,
                resolved.timeout_seconds,
                body,
                resolved.api_key,
                "subtitle_translate_test",
            )
        }
        "index_translate" => {
            let opts = crate::media::index_translate::options(&state.db).await?;
            let endpoint = crate::media::index_translate::url()?;
            let prompt = format!(
                "请将以下文本翻译为{target}，风格为{style}，只输出译文，不要解释。\n\n{source}"
            );
            let body = json!({"model":crate::media::index_translate::MODEL,
                "temperature":opts.temperature,"max_tokens":opts.max_tokens,
                "stream":false,"chat_template_kwargs":{"enable_thinking":false},
                "messages":[{"role":"user","content":prompt}]});
            (
                endpoint,
                crate::media::index_translate::MODEL.to_string(),
                opts.timeout_seconds,
                body,
                None,
                "index_translate_test",
            )
        }
        _ => {
            return Err(AppError::Unavailable(format!(
                "当前自动处理翻译方式不支持连接测试: {provider}"
            )));
        }
    };
    let client = Client::builder()
        .timeout(Duration::from_secs(timeout.clamp(5, 300)))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let mut request = client.post(endpoint.clone()).json(&body);
    if operation == "index_translate_test" {
        request = request.header("User-Agent", "Index-Translate-Client/1.0");
    }
    if let Some(token) = key {
        request = request.bearer_auth(token);
    }
    let envelope = crate::ai_usage::chat_json(
        &state.db,
        request,
        &endpoint,
        &model_name,
        operation,
        None,
        256 * 1024,
        "translation test",
    )
    .await?;
    let raw = message_text(&envelope)?;
    let translated = if provider == "openai" {
        let object: Value = serde_json::from_str(raw)
            .map_err(|_| AppError::Unavailable("translation response is not JSON".into()))?;
        object
            .get("translation")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .unwrap_or(source)
            .to_owned()
    } else {
        raw.to_owned()
    };
    Ok(Json(json!({"translated_text":translated})))
}

pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/translate/test", post(test))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_original_text_limits_and_model_message() {
        let mut body = TranslateRequest::default();
        assert_eq!(validate(&body).unwrap().0, "Hello world.");
        body.text = "\n".into();
        assert!(validate(&body).is_err());
        body.text = "x".repeat(2001);
        assert!(validate(&body).is_err());
        body.text = "Hello".into();
        body.target_lang = "\n".into();
        assert_eq!(validate(&body).unwrap().1, "zh");
        assert!(message_text(&json!({"choices":[{"message":{"content":"  OK  "}}]})).is_ok());
        assert!(message_text(&json!({"choices":[]})).is_err());
    }
}
