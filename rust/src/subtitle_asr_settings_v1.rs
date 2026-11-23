//! Original subtitle-service ASR settings (Python asr_settings_store.py).
//! Read and update the original subtitle.asr AppSetting, not a cosmetic V2
//! UI-only shadow. Only the admin-isolated Rust deployment enables writes.
use crate::{AppError, AppResult, api::AppState, subtitle_providers_v1};
use axum::{Json, Router, extract::State, routing::get};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};

const KEY: &str = "subtitle.asr";
const ENGINES: &[&str] = &[
    "mock",
    "faster-whisper",
    "openvino",
    "external-whisper",
    "groq-whisper",
    "cloudflare-workers-ai",
];
fn bad(s: &str) -> AppError {
    AppError::Invalid(s.into())
}
fn env(k: &str, default: &str) -> String {
    std::env::var(k)
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or(default.into())
}
fn env_n(k: &str, n: i64) -> i64 {
    env(k, &n.to_string()).parse().unwrap_or(n)
}
fn env_f(k: &str, n: f64) -> f64 {
    env(k, &n.to_string()).parse().unwrap_or(n)
}
fn env_b(k: &str, n: bool) -> bool {
    let s = env(k, if n { "true" } else { "false" });
    matches!(s.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on")
}
fn string(v: &Value, key: &str, fallback: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback)
        .to_string()
}
fn integer(v: &Value, key: &str, n: i64, min: i64, max: i64) -> i64 {
    v.get(key)
        .and_then(Value::as_i64)
        .filter(|x| *x != 0)
        .unwrap_or(n)
        .clamp(min, max)
}
fn number(v: &Value, key: &str, n: f64, min: f64, max: f64) -> f64 {
    v.get(key)
        .and_then(Value::as_f64)
        .filter(|x| *x != 0.0)
        .unwrap_or(n)
        .clamp(min, max)
}
fn boolean(v: &Value, key: &str, n: bool) -> bool {
    v.get(key).and_then(Value::as_bool).unwrap_or(n)
}
fn secret_set(v: &Value, key: &str, env_key: &str) -> bool {
    v.get(key)
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty())
        || std::env::var(env_key).is_ok_and(|s| !s.is_empty())
}
fn short(s: String, max: usize) -> String {
    s.chars().take(max).collect()
}
pub(crate) fn view(v: &Value) -> Value {
    let mut engine = string(
        v,
        "default_engine",
        &env("SUBTITLE_ASR_ENGINE", "faster-whisper"),
    );
    if !ENGINES.contains(&engine.as_str()) {
        engine = "faster-whisper".into();
    }
    let model_default = match engine.as_str() {
        "openvino" => env("SUBTITLE_OPENVINO_MODEL", ""),
        "external-whisper" => env("SUBTITLE_EXTERNAL_WHISPER_MODEL", "whisper-1"),
        "groq-whisper" => env("SUBTITLE_GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
        "cloudflare-workers-ai" => env(
            "SUBTITLE_CLOUDFLARE_MODEL",
            "@cf/openai/whisper-large-v3-turbo",
        ),
        _ => env("SUBTITLE_WHISPER_MODEL", "tiny"),
    };
    let mut model = string(v, "default_model", &model_default);
    if engine == "groq-whisper"
        && !matches!(
            model.as_str(),
            "whisper-large-v3" | "whisper-large-v3-turbo"
        )
    {
        model = model_default;
    }
    let ext = v
        .get("external_whisper")
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null);
    let groq = v
        .get("groq_whisper")
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null);
    let cloud = v
        .get("cloudflare_workers_ai")
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null);
    let ext_url = string(
        ext,
        "base_url",
        &env("SUBTITLE_EXTERNAL_WHISPER_BASE_URL", ""),
    );
    let ext_url = if ext_url.is_empty() {
        ext_url
    } else {
        subtitle_providers_v1::normalize_openai_url(&ext_url)
    };
    let vad = number(
        v,
        "openvino_vad_threshold",
        env_f("SUBTITLE_OPENVINO_VAD_THRESHOLD", 0.5),
        0.1,
        0.95,
    );
    let mut threshold = vad;
    if v.get("openvino_vad_threshold")
        .and_then(Value::as_f64)
        .is_some_and(|x| x != 0.0 && !(0.1..=0.95).contains(&x))
    {
        threshold = env_f("SUBTITLE_OPENVINO_VAD_THRESHOLD", 0.5);
    }
    json!({
       "default_engine":engine,
       "default_language":string(v,"default_language","auto"),
       "default_model":model,
       "openvino_device":string(v,"openvino_device",&env("SUBTITLE_OPENVINO_DEVICE","GPU")),
       "openvino_num_beams":integer(v,"openvino_num_beams",env_n("SUBTITLE_OPENVINO_NUM_BEAMS",1),1,i64::MAX),
       "openvino_max_new_tokens":integer(v,"openvino_max_new_tokens",env_n("SUBTITLE_OPENVINO_MAX_NEW_TOKENS",448),1,i64::MAX),
       "openvino_vad_enabled":boolean(v,"openvino_vad_enabled",env_b("SUBTITLE_OPENVINO_VAD_ENABLED",true)),
       "openvino_vad_threshold":threshold,
       "model_download_proxy":short(string(v,"model_download_proxy",""),2048),
       "external_whisper_base_url":short(ext_url,2048),
       "external_whisper_model":short(string(ext,"model",&env("SUBTITLE_EXTERNAL_WHISPER_MODEL","whisper-1")),256),
       "external_whisper_api_key_set":secret_set(ext,"api_key_enc","SUBTITLE_EXTERNAL_WHISPER_API_KEY"),
       "external_whisper_batch_size":integer(ext,"batch_size",env_n("SUBTITLE_EXTERNAL_WHISPER_BATCH_SIZE",1),1,32),
       "external_whisper_vad_enabled":boolean(ext,"vad_enabled",env_b("SUBTITLE_EXTERNAL_WHISPER_VAD_ENABLED",true)),
       "external_whisper_vad_threshold":number(ext,"vad_threshold",env_f("SUBTITLE_EXTERNAL_WHISPER_VAD_THRESHOLD",0.5),0.1,0.95),
       "external_whisper_min_silence_ms":integer(ext,"min_silence_ms",env_n("SUBTITLE_EXTERNAL_WHISPER_MIN_SILENCE_MS",2000),50,5000),
       "external_whisper_speech_pad_ms":ext.get("speech_pad_ms").and_then(Value::as_i64)
           .unwrap_or(env_n("SUBTITLE_EXTERNAL_WHISPER_SPEECH_PAD_MS",400)).clamp(0,2000),
       "external_whisper_condition_on_previous_text":boolean(ext,"condition_on_previous_text",env_b("SUBTITLE_EXTERNAL_WHISPER_CONDITION_ON_PREVIOUS_TEXT",true)),
       "external_whisper_max_segment_seconds":number(ext,"max_segment_seconds",env_f("SUBTITLE_EXTERNAL_WHISPER_MAX_SEGMENT_SECONDS",12.0),1.0,30.0),
       "external_whisper_max_segment_chars":integer(ext,"max_segment_chars",env_n("SUBTITLE_EXTERNAL_WHISPER_MAX_SEGMENT_CHARS",120),10,500),
       "groq_whisper_model":short(string(groq,"model",&env("SUBTITLE_GROQ_WHISPER_MODEL","whisper-large-v3-turbo")),256),
       "groq_whisper_api_key_set":secret_set(groq,"api_key_enc","SUBTITLE_GROQ_WHISPER_API_KEY"),
       "cloudflare_workers_ai_account_id":short(string(cloud,"account_id",&env("SUBTITLE_CLOUDFLARE_ACCOUNT_ID","")),128),
       "cloudflare_workers_ai_model":short(string(cloud,"model",&env("SUBTITLE_CLOUDFLARE_MODEL","@cf/openai/whisper-large-v3-turbo")),256),
       "cloudflare_workers_ai_api_key_set":secret_set(cloud,"api_key_enc","SUBTITLE_CLOUDFLARE_API_KEY")
    })
}
fn patch_nested(
    obj: &mut Map<String, Value>,
    field: &str,
    kind: &str,
    input: &Value,
) -> AppResult<()> {
    match kind {
        "secret" => {
            let s = input
                .as_str()
                .ok_or_else(|| bad("API key must be a string"))?
                .trim();
            if s.chars().count() > 4096 {
                return Err(bad("ASR API key exceeds limit"));
            }
            if s.is_empty() {
                obj.remove(field);
            } else {
                obj.insert(field.into(), json!(format!("dbplain:v1:{s}")));
            }
        }
        "text" => {
            let s = input
                .as_str()
                .ok_or_else(|| bad("ASR field must be string"))?
                .trim();
            if s.is_empty() {
                obj.remove(field);
            } else {
                obj.insert(field.into(), json!(s));
            }
        }
        _ => return Err(bad("invalid nested ASR field")),
    }
    Ok(())
}
fn validate_number(field: &str, val: &Value) -> AppResult<()> {
    let limit = match field {
        "openvino_num_beams" => (1., 16.),
        "openvino_max_new_tokens" => (1., 4096.),
        "openvino_vad_threshold" => (0.1, 0.95),
        "external_whisper_batch_size" => (1., 32.),
        "external_whisper_vad_threshold" => (0.1, 0.95),
        "external_whisper_min_silence_ms" => (50., 5000.),
        "external_whisper_speech_pad_ms" => (0., 2000.),
        "external_whisper_max_segment_seconds" => (1., 30.),
        "external_whisper_max_segment_chars" => (10., 500.),
        _ => return Err(bad("unknown ASR numeric field")),
    };
    let n = val.as_f64().ok_or_else(|| bad("ASR number required"))?;
    if !n.is_finite() || !(limit.0..=limit.1).contains(&n) {
        return Err(bad("ASR numeric field out of supported range"));
    }
    if field.contains("size")
        || field.ends_with("_ms")
        || field.ends_with("_chars")
        || field == "openvino_num_beams"
        || field == "openvino_max_new_tokens"
    {
        if val.as_i64().is_none() {
            return Err(bad("ASR integer required"));
        }
    }
    Ok(())
}
pub(crate) fn patch(stored: &mut Map<String, Value>, body: &Value) -> AppResult<()> {
    let input = body
        .as_object()
        .ok_or_else(|| bad("ASR settings must be object"))?;
    for (key, value) in input {
        if value.is_null() {
            continue;
        }
        match key.as_str() {
            "default_engine" => {
                let s = value
                    .as_str()
                    .ok_or_else(|| bad("ASR engine must be string"))?
                    .trim();
                if !s.is_empty() && !ENGINES.contains(&s) {
                    return Err(bad("ASR engine is unsupported"));
                }
                if s.is_empty() {
                    stored.remove(key);
                } else {
                    stored.insert(key.clone(), json!(s));
                }
            }
            "default_language" | "default_model" | "openvino_device" | "model_download_proxy" => {
                let s = value
                    .as_str()
                    .ok_or_else(|| bad("ASR text required"))?
                    .trim();
                if key == "model_download_proxy" && s.chars().count() > 2048 {
                    return Err(bad("model download proxy too long"));
                }
                if s.is_empty() {
                    stored.remove(key);
                } else {
                    stored.insert(key.clone(), json!(s));
                }
            }
            "openvino_vad_enabled" => {
                if !value.is_boolean() {
                    return Err(bad("ASR boolean required"));
                }
                stored.insert(key.clone(), value.clone());
            }
            "openvino_num_beams" | "openvino_max_new_tokens" | "openvino_vad_threshold" => {
                validate_number(key, value)?;
                stored.insert(key.clone(), value.clone());
            }
            "external_whisper_base_url"
            | "external_whisper_model"
            | "external_whisper_api_key"
            | "external_whisper_batch_size"
            | "external_whisper_vad_enabled"
            | "external_whisper_vad_threshold"
            | "external_whisper_min_silence_ms"
            | "external_whisper_speech_pad_ms"
            | "external_whisper_condition_on_previous_text"
            | "external_whisper_max_segment_seconds"
            | "external_whisper_max_segment_chars" => {
                let mut ext = stored
                    .get("external_whisper")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let sub = key.trim_start_matches("external_whisper_");
                match sub {
                    "base_url" => {
                        patch_nested(&mut ext, "base_url", "text", value)?;
                        if let Some(s) = ext.get("base_url").and_then(Value::as_str) {
                            ext.insert(
                                "base_url".into(),
                                json!(subtitle_providers_v1::normalize_openai_url(s)),
                            );
                        }
                    }
                    "model" | "api_key" => patch_nested(
                        &mut ext,
                        if sub == "api_key" {
                            "api_key_enc"
                        } else {
                            "model"
                        },
                        if sub == "api_key" { "secret" } else { "text" },
                        value,
                    )?,
                    "vad_enabled" | "condition_on_previous_text" => {
                        if !value.is_boolean() {
                            return Err(bad("ASR boolean required"));
                        }
                        ext.insert(sub.into(), value.clone());
                    }
                    _ => {
                        validate_number(key, value)?;
                        ext.insert(sub.into(), value.clone());
                    }
                }
                stored.insert("external_whisper".into(), Value::Object(ext));
            }
            "groq_whisper_model" | "groq_whisper_api_key" => {
                let mut sub = stored
                    .get("groq_whisper")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let field = key.trim_start_matches("groq_whisper_");
                if field == "model"
                    && !value.as_str().is_some_and(|s| {
                        s.is_empty() || matches!(s, "whisper-large-v3" | "whisper-large-v3-turbo")
                    })
                {
                    return Err(bad("unsupported Groq whisper model"));
                }
                patch_nested(
                    &mut sub,
                    if field == "api_key" {
                        "api_key_enc"
                    } else {
                        field
                    },
                    if field == "api_key" { "secret" } else { "text" },
                    value,
                )?;
                stored.insert("groq_whisper".into(), Value::Object(sub));
            }
            "cloudflare_workers_ai_account_id"
            | "cloudflare_workers_ai_model"
            | "cloudflare_workers_ai_api_key" => {
                let mut sub = stored
                    .get("cloudflare_workers_ai")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let field = key.trim_start_matches("cloudflare_workers_ai_");
                patch_nested(
                    &mut sub,
                    if field == "api_key" {
                        "api_key_enc"
                    } else {
                        field
                    },
                    if field == "api_key" { "secret" } else { "text" },
                    value,
                )?;
                stored.insert("cloudflare_workers_ai".into(), Value::Object(sub));
            }
            _ => return Err(bad("unknown ASR settings field")),
        }
    }
    Ok(())
}
async fn asr_get(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let row: Option<Value> = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(KEY)
        .fetch_optional(&state.db.pool)
        .await?;
    Ok(Json(view(&row.unwrap_or_else(|| json!({})))))
}
async fn asr_put(State(state): State<AppState>, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("1" | "true")
    ) {
        return Err(AppError::Conflict(
            "V1 subtitle settings write disabled".into(),
        ));
    }
    let mut tx: Transaction<'_, Postgres> = state.db.pool.begin().await?;
    let row: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(KEY)
            .fetch_optional(&mut *tx)
            .await?;
    let mut value = row.and_then(|v| v.as_object().cloned()).unwrap_or_default();
    patch(&mut value, &body)?;
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,2)
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,
             version=2,updated_at=now()",
    )
    .bind(KEY)
    .bind(json!(value))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(view(&json!(value))))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/asr/settings", get(asr_get).put(asr_put))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn views_and_updates() {
        let mut values = Map::new();
        patch(
            &mut values,
            &json!({
                "default_engine":"groq-whisper","groq_whisper_model":"whisper-large-v3",
                "external_whisper_vad_threshold":0.7,"external_whisper_batch_size":4,
                "external_whisper_api_key":"test-token"
            }),
        )
        .unwrap();
        let read = view(&json!(values));
        assert_eq!(read["default_engine"], "groq-whisper");
        assert_eq!(read["groq_whisper_model"], "whisper-large-v3");
        assert_eq!(read["external_whisper_batch_size"], 4);
        assert_eq!(read["external_whisper_api_key_set"], true);
        assert!(patch(&mut values, &json!({"default_engine":"not-real"})).is_err());
        assert!(patch(&mut values, &json!({"external_whisper_batch_size":40})).is_err());
    }
}
