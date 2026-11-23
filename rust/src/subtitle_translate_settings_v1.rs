//! Native Python-shaped translation advanced settings (57 fields).
//! Base defaults are generated from the actual Python settings model, not guessed.
use crate::{AppError, AppResult, api::AppState, subtitle_providers_v1 as providers};
use axum::{Json, Router, extract::State, routing::get};
use serde_json::{Map, Value, json};
use sqlx::Transaction;

fn bad(msg: &str) -> AppError {
    AppError::Invalid(msg.into())
}
const KEY: &str = "subtitle.translate";
fn defaults() -> Value {
    serde_json::from_str(include_str!("translate_defaults_v1.json")).expect("valid defaults")
}
fn present(v: Option<&Value>) -> Option<&Value> {
    v.filter(|v| {
        !v.is_null()
            && match v {
                Value::String(s) => !s.is_empty(),
                Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.),
                _ => true,
            }
    })
}
fn pick(v: Option<&Value>, default: &Value) -> Value {
    present(v).cloned().unwrap_or_else(|| default.clone())
}
fn n(v: Option<&Value>, default: f64) -> f64 {
    v.and_then(Value::as_f64)
        .filter(|x| *x != 0.)
        .unwrap_or(default)
}
fn i(v: Option<&Value>, default: i64) -> i64 {
    v.and_then(Value::as_i64)
        .filter(|x| *x != 0)
        .unwrap_or(default)
}
fn b(v: Option<&Value>, default: bool) -> bool {
    v.and_then(Value::as_bool).unwrap_or(default)
}
fn csv(s: &str, default: &str) -> String {
    let s = if s.is_empty() { default } else { s };
    let mut unique = Vec::<String>::new();
    let mut result = Vec::<String>::new();
    for term in s.replace('\n', ",").split(',') {
        let term = term.split_whitespace().collect::<Vec<_>>().join(" ");
        let key = term.to_lowercase();
        if key.is_empty() || unique.contains(&key) {
            continue;
        }
        unique.push(key);
        result.push(term.chars().take(80).collect());
        if result.len() >= 20 {
            break;
        }
    }
    result.join(",")
}
pub(crate) fn view(raw: &Value) -> Value {
    let mut result = defaults();
    if !raw.is_object() {
        return result;
    }
    let old = result.clone();
    for key in [
        "default_provider",
        "default_target_lang",
        "default_style",
        "index_domain",
        "index_glossary",
        "index_instruction",
        "rag_embedding_provider",
        "rag_embedding_model",
        "rag_embedding_model_dir",
        "rag_embedding_device",
        "rag_search_url",
        "rag_search_language",
        "rag_domain",
    ] {
        result[key] = pick(raw.get(key), &old[key]);
    }
    for key in [
        "default_batch_size",
        "default_max_retries",
        "index_max_tokens",
        "index_batch_size",
        "index_context_chars",
        "rag_top_k",
        "rag_embedding_dimensions",
        "rag_dictionary_top_k",
        "rag_search_pageno",
        "rag_agent_parallelism",
    ] {
        result[key] = json!(i(raw.get(key), old[key].as_i64().unwrap_or(1)));
    }
    for key in [
        "default_enable_summary",
        "rag_enabled",
        "rag_auto_discover_terms",
        "rag_auto_learn_terms",
        "rag_dictionary_enabled",
        "rag_dictionary_auto_promote",
        "rag_wiki_enabled",
        "rag_search_enabled",
        "rag_agent_skills_enabled",
        "rag_agent_builtin_skills_enabled",
        "rag_agent_user_skills_enabled",
    ] {
        result[key] = json!(b(raw.get(key), old[key].as_bool().unwrap_or(false)));
    }
    for key in [
        "rag_min_score",
        "rag_dictionary_min_quality",
        "rag_agent_timeout_seconds",
        "index_timeout_seconds",
        "rag_embedding_timeout_seconds",
    ] {
        result[key] = json!(n(raw.get(key), old[key].as_f64().unwrap_or(0.)));
    }
    result["index_temperature"] = raw
        .get("index_temperature")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or(json!(0.0));
    // Python read uses the original default when rag_top_k is zero.
    for key in ["rag_top_k", "rag_dictionary_top_k"] {
        if result[key] == 0 {
            result[key] = old[key].clone();
        }
    }
    for (key, default) in [
        ("rag_search_categories", "general"),
        ("rag_search_engines", ""),
        ("rag_search_fallback_engines", "bing,baidu"),
    ] {
        let value = raw.get(key).and_then(Value::as_str).unwrap_or("");
        result[key] = json!(csv(value, default));
    }
    let language = result["rag_search_language"]
        .as_str()
        .unwrap_or("all")
        .trim()
        .chars()
        .take(32)
        .collect::<String>();
    result["rag_search_language"] = json!(language);
    let time = raw
        .get("rag_search_time_range")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    result["rag_search_time_range"] = json!(if matches!(time.as_str(), "day" | "month" | "year") {
        time
    } else {
        String::new()
    });
    result["rag_search_safesearch"] = json!(i(raw.get("rag_search_safesearch"), 0).clamp(0, 2));
    result["rag_search_pageno"] = json!(
        result["rag_search_pageno"]
            .as_i64()
            .unwrap_or(1)
            .clamp(1, 100)
    );
    let open = raw
        .get("openai")
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null);
    let embed = raw
        .get("rag_embedding_openai")
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null);
    let orig_base = old["openai_base_url"]
        .as_str()
        .unwrap_or("https://api.openai.com/v1");
    result["openai_base_url"] = json!(providers::normalize_openai_url(
        pick(open.get("base_url"), &old["openai_base_url"])
            .as_str()
            .unwrap_or(orig_base)
    ));
    result["openai_model"] = pick(open.get("model"), &old["openai_model"]);
    result["openai_temperature"] = json!(n(
        open.get("temperature"),
        old["openai_temperature"].as_f64().unwrap_or(0.2)
    ));
    result["openai_timeout_seconds"] = json!(n(
        open.get("timeout_seconds"),
        old["openai_timeout_seconds"].as_f64().unwrap_or(180.)
    ));
    result["openai_max_retries"] = json!(i(
        present(open.get("max_retries")).or_else(|| present(raw.get("openai_max_retries"))),
        3
    ));
    result["openai_enable_thinking"] = json!(b(open.get("enable_thinking"), false));
    for (key, nested, default) in [
        ("openai_api_type", "api_type", "openai"),
        (
            "cerebras_reasoning_effort",
            "cerebras_reasoning_effort",
            "medium",
        ),
        (
            "cerebras_reasoning_format",
            "cerebras_reasoning_format",
            "parsed",
        ),
    ] {
        result[key] = json!(
            open.get(nested)
                .and_then(Value::as_str)
                .filter(|x| !x.is_empty())
                .unwrap_or(default)
        );
    }
    result["openai_api_key_set"] = json!(
        open.get("api_key_enc")
            .and_then(Value::as_str)
            .is_some_and(|x| !x.is_empty())
            || std::env::var("OPENAI_API_KEY").is_ok_and(|x| !x.is_empty())
    );
    result["rag_embedding_api_key_set"] = json!(
        embed
            .get("api_key_enc")
            .and_then(Value::as_str)
            .is_some_and(|x| !x.is_empty())
    );
    result["rag_embedding_base_url"] = json!(providers::normalize_openai_url(
        pick(embed.get("base_url"), &old["rag_embedding_base_url"])
            .as_str()
            .unwrap_or("https://api.openai.com/v1")
    ));
    result["rag_embedding_timeout_seconds"] = json!(n(
        present(embed.get("timeout_seconds"))
            .or_else(|| present(raw.get("rag_embedding_timeout_seconds"))),
        old["rag_embedding_timeout_seconds"].as_f64().unwrap_or(60.)
    ));
    result
}
fn int_bounds(key: &str) -> Option<(i64, i64)> {
    match key {
        "index_max_tokens" => Some((128, 16384)),
        "index_batch_size" => Some((1, 30)),
        "index_context_chars" => Some((4000, 100000)),
        "openai_max_retries" => Some((1, 10)),
        "default_batch_size" => Some((1, i64::MAX)),
        "default_max_retries" => Some((0, 10)),
        "rag_top_k" | "rag_dictionary_top_k" => Some((0, 30)),
        "rag_embedding_dimensions" => Some((1, 4096)),
        "rag_agent_parallelism" => Some((1, 8)),
        "rag_search_pageno" => Some((1, 100)),
        "rag_search_safesearch" => Some((0, 2)),
        _ => None,
    }
}
fn float_bounds(key: &str) -> Option<(f64, f64)> {
    match key {
        "index_temperature" => Some((0., 2.)),
        "index_timeout_seconds" => Some((15., 300.)),
        "rag_min_score" | "rag_dictionary_min_quality" => Some((0., 1.)),
        "rag_embedding_timeout_seconds" => Some((1., 600.)),
        "rag_agent_timeout_seconds" => Some((10., 900.)),
        _ => None,
    }
}
fn set_secret(map: &mut Map<String, Value>, value: &Value) -> AppResult<()> {
    let token = value
        .as_str()
        .ok_or_else(|| bad("API key must be string"))?
        .trim();
    if token.is_empty() {
        map.remove("api_key_enc");
    } else {
        map.insert("api_key_enc".into(), json!(format!("dbplain:v1:{token}")));
    }
    Ok(())
}
pub(crate) fn patch(data: &mut Map<String, Value>, body: &Value) -> AppResult<()> {
    let body = body
        .as_object()
        .ok_or_else(|| bad("translation settings requires object"))?;
    let baseline = defaults();
    let mut open = data
        .get("openai")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut embed = data
        .get("rag_embedding_openai")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    for (key, value) in body {
        if value.is_null() {
            continue;
        }
        if let Some((low, high)) = int_bounds(key) {
            let x = value
                .as_i64()
                .ok_or_else(|| bad("translation settings requires integer"))?;
            if !(low..=high).contains(&x) {
                return Err(bad("translation integer out of range"));
            }
        }
        if let Some((low, high)) = float_bounds(key) {
            let x = value
                .as_f64()
                .ok_or_else(|| bad("translation settings requires number"))?;
            if !x.is_finite() || !(low..=high).contains(&x) {
                return Err(bad("translation number out of range"));
            }
        }
        match key.as_str() {
            "openai_api_key" => set_secret(&mut open, value)?,
            "rag_embedding_api_key" => set_secret(&mut embed, value)?,
            "openai_base_url"
            | "openai_model"
            | "openai_temperature"
            | "openai_timeout_seconds"
            | "openai_max_retries"
            | "openai_api_type"
            | "openai_enable_thinking"
            | "cerebras_reasoning_effort"
            | "cerebras_reasoning_format" => {
                let inner = key.strip_prefix("openai_").unwrap_or(key);
                let v = if key == "openai_base_url" {
                    json!(providers::normalize_openai_url(
                        value
                            .as_str()
                            .ok_or_else(|| bad("OpenAI URL must be string"))?
                    ))
                } else {
                    value.clone()
                };
                open.insert(inner.into(), v);
            }
            "rag_embedding_base_url" => {
                embed.insert(
                    "base_url".into(),
                    json!(providers::normalize_openai_url(
                        value
                            .as_str()
                            .ok_or_else(|| bad("embedding URL must be string"))?
                    )),
                );
            }
            "rag_embedding_timeout_seconds" => {
                embed.insert("timeout_seconds".into(), value.clone());
                data.insert(key.clone(), value.clone());
            }
            "openai_api_key_set" | "rag_embedding_api_key_set" => {}
            _ if baseline.get(key).is_some() => {
                data.insert(key.clone(), value.clone());
            }
            _ => {}
        }
    }
    data.insert("rag_embedding_openai".into(), json!(embed));
    let api = open
        .get("api_type")
        .and_then(Value::as_str)
        .unwrap_or("openai")
        .trim()
        .to_lowercase();
    open.insert(
        "api_type".into(),
        json!(if matches!(api.as_str(), "openai" | "cerebras") {
            api
        } else {
            "openai".into()
        }),
    );
    let effort = open
        .get("cerebras_reasoning_effort")
        .and_then(Value::as_str)
        .unwrap_or("medium")
        .trim()
        .to_lowercase();
    open.insert(
        "cerebras_reasoning_effort".into(),
        json!(if matches!(effort.as_str(), "low" | "medium" | "high") {
            effort
        } else {
            "medium".into()
        }),
    );
    let format = open
        .get("cerebras_reasoning_format")
        .and_then(Value::as_str)
        .unwrap_or("parsed")
        .trim()
        .to_lowercase();
    open.insert(
        "cerebras_reasoning_format".into(),
        json!(if matches!(format.as_str(), "parsed" | "raw" | "hidden") {
            format
        } else {
            "parsed".into()
        }),
    );
    data.insert("openai".into(), json!(open));
    if data
        .get("default_provider")
        .and_then(Value::as_str)
        .is_some_and(|x| !matches!(x, "openai" | "index_translate" | "mock" | "noop"))
    {
        return Err(bad("unsupported default translation provider"));
    }
    for (key, max) in [
        ("index_domain", 90),
        ("index_glossary", 4000),
        ("index_instruction", 2000),
    ] {
        if let Some(v) = data.get(key).and_then(Value::as_str) {
            data.insert(key.into(), json!(v.chars().take(max).collect::<String>()));
        }
    }
    for (key, min, max) in [
        ("default_batch_size", 1, i64::MAX),
        ("default_max_retries", 0, 10),
        ("rag_top_k", 0, 30),
        ("rag_embedding_dimensions", 1, 4096),
        ("rag_dictionary_top_k", 0, 30),
        ("rag_agent_parallelism", 1, 8),
        ("rag_search_safesearch", 0, 2),
        ("rag_search_pageno", 1, 100),
    ] {
        let base = baseline[key].as_i64().unwrap_or(min);
        data.insert(key.into(), json!(i(data.get(key), base).clamp(min, max)));
    }
    for (key, min, max) in [
        ("rag_min_score", 0., 1.),
        ("rag_dictionary_min_quality", 0., 1.),
        ("rag_agent_timeout_seconds", 10., 900.),
        ("rag_embedding_timeout_seconds", 1., 600.),
    ] {
        let base = baseline[key].as_f64().unwrap_or(min);
        let x = if key == "rag_dictionary_min_quality" {
            data.get(key).and_then(Value::as_f64).unwrap_or(base)
        } else {
            n(data.get(key), base)
        };
        data.insert(key.into(), json!(x.clamp(min, max)));
    }
    let provider = data
        .get("rag_embedding_provider")
        .and_then(Value::as_str)
        .unwrap_or("openai")
        .trim()
        .to_lowercase();
    data.insert(
        "rag_embedding_provider".into(),
        json!(if matches!(provider.as_str(), "openai" | "local") {
            provider
        } else {
            "openai".into()
        }),
    );
    for key in [
        "rag_embedding_model",
        "rag_embedding_model_dir",
        "rag_embedding_device",
        "rag_search_url",
        "rag_search_language",
        "rag_search_time_range",
        "rag_domain",
    ] {
        if let Some(v) = data.get(key).and_then(Value::as_str) {
            data.insert(key.into(), json!(v.trim()));
        }
    }
    for (key, default) in [
        ("rag_search_categories", "general"),
        ("rag_search_engines", ""),
        ("rag_search_fallback_engines", "bing,baidu"),
    ] {
        let value = data.get(key).and_then(Value::as_str).unwrap_or("");
        data.insert(key.into(), json!(csv(value, default)));
    }
    let language = data
        .get("rag_search_language")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .unwrap_or("all")
        .trim()
        .chars()
        .take(32)
        .collect::<String>();
    data.insert("rag_search_language".into(), json!(language));
    let range = data
        .get("rag_search_time_range")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    data.insert(
        "rag_search_time_range".into(),
        json!(if matches!(range.as_str(), "day" | "month" | "year") {
            range
        } else {
            String::new()
        }),
    );
    Ok(())
}
async fn read(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let raw: Option<Value> = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(KEY)
        .fetch_optional(&state.db.pool)
        .await?;
    Ok(Json(view(&raw.unwrap_or_else(|| json!({})))))
}
async fn write(State(state): State<AppState>, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("1" | "true")
    ) {
        return Err(AppError::Conflict(
            "V1 translation settings writes disabled".into(),
        ));
    }
    let mut tx: Transaction<'_, sqlx::Postgres> = state.db.pool.begin().await?;
    let before: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(KEY)
            .fetch_optional(&mut *tx)
            .await?;
    let mut stored = before
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    patch(&mut stored, &body)?;
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
      ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=now()",
    )
    .bind(KEY)
    .bind(json!(stored))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(view(&json!(stored))))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/translate/settings", get(read).put(write))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn normalizes() {
        assert_eq!(view(&json!({})).as_object().unwrap().len(), 57);
        let mut m = Map::new();
        patch(
            &mut m,
            &json!({
              "index_max_tokens":5000,
              "openai_model":"local-test",
              "rag_search_categories":"general, general, code"
            }),
        )
        .unwrap();
        let view = view(&json!(m));
        assert_eq!(view["index_max_tokens"], 5000);
        assert_eq!(view["openai_model"], "local-test");
        assert_eq!(view["rag_search_categories"], "general,code");
    }
}
