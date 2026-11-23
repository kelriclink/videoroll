//! Python-compatible custom translation provider CRUD, opt-in and transactional.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use reqwest::Url;
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use std::collections::HashSet;
use uuid::Uuid;

const KEY: &str = "subtitle.model_providers";
fn bad(s: &str) -> AppError {
    AppError::Invalid(s.to_owned())
}
pub(crate) fn writable() -> AppResult<()> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_PROVIDER_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        return Err(AppError::Conflict(
            "V1 translation provider writes are not enabled".into(),
        ));
    }
    Ok(())
}
fn field<'a>(v: &'a Value, key: &str, default: &'a str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or(default)
}
fn int_field(v: &Value, key: &str, default: i64) -> AppResult<i64> {
    match v.get(key) {
        None => Ok(default),
        Some(Value::Number(n)) => n.as_i64().ok_or_else(|| bad("integer field invalid")),
        _ => Err(bad("integer field invalid")),
    }
}
fn url(raw: &str) -> AppResult<String> {
    if raw.trim().is_empty() || raw.chars().count() > 2048 {
        return Err(bad("provider URL length invalid"));
    }
    let out = crate::subtitle_providers_v1::normalize_openai_url(raw);
    let parsed = Url::parse(&out).map_err(|_| bad("provider URL invalid"))?;
    if !matches!(parsed.scheme(), "http" | "https")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err(bad("provider URL requires HTTP(S) with no credentials"));
    }
    Ok(out)
}
fn token_budget(v: &Value, key: &str) -> AppResult<Option<u64>> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n))
            if n.as_u64()
                .is_some_and(|v| (1..=9_007_199_254_740_991).contains(&v)) =>
        {
            Ok(n.as_u64())
        }
        _ => Err(bad("token budget must be positive JSON-safe integer")),
    }
}
pub(crate) fn models(raw: &Value) -> AppResult<Value> {
    let entries = raw
        .as_array()
        .ok_or_else(|| bad("models must be an array"))?;
    if entries.len() > 200 {
        return Err(bad("too many models"));
    }
    let mut ids = HashSet::new();
    let mut normalized = Vec::new();
    for model in entries {
        let name = model
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| bad("invalid model ID"))?;
        let id = name.trim();
        if id.is_empty() || id.chars().count() > 256 || !ids.insert(id.to_string()) {
            return Err(bad("model ID invalid or duplicate"));
        }
        let ctx = token_budget(model, "context_window")?;
        let out = token_budget(model, "max_output_tokens")?;
        if ctx.is_some() && out.is_some() && out >= ctx {
            return Err(bad("model output exceeds context"));
        }
        let temperature = model.get("temperature").cloned().unwrap_or(json!(0.2));
        if !temperature.is_null()
            && !temperature
                .as_f64()
                .is_some_and(|v| v.is_finite() && (0.0..=2.0).contains(&v))
        {
            return Err(bad("model temperature invalid"));
        }
        let thinking = model
            .get("enable_thinking")
            .cloned()
            .unwrap_or(json!(false));
        if !thinking.is_boolean() {
            return Err(bad("model thinking must be boolean"));
        }
        let effort = field(model, "reasoning_effort", "medium");
        let format = field(model, "reasoning_format", "parsed");
        if !matches!(effort, "low" | "medium" | "high")
            || !matches!(format, "parsed" | "raw" | "hidden")
        {
            return Err(bad("model reasoning mode invalid"));
        }
        normalized.push(json!({
            "id":id,"context_window":ctx,"max_output_tokens":out,
            "temperature":temperature,"enable_thinking":thinking,
            "reasoning_effort":effort,"reasoning_format":format
        }));
    }
    Ok(json!(normalized))
}
fn normalize(payload: &Value, id: &str, previous: Option<&Value>) -> AppResult<Value> {
    if !payload.is_object() {
        return Err(bad("provider must be a JSON object"));
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| bad("missing name"))?;
    if name.trim().is_empty() || name.chars().count() > 80 {
        return Err(bad("provider name invalid"));
    }
    let base = payload
        .get("base_url")
        .and_then(Value::as_str)
        .ok_or_else(|| bad("missing base_url"))?;
    let base = url(base)?;
    let api_type = field(payload, "api_type", "openai");
    if !matches!(api_type, "openai" | "cerebras") {
        return Err(bad("provider API type invalid"));
    }
    let timeout = int_field(payload, "timeout_seconds", 180)?;
    let retries = int_field(payload, "max_retries", 3)?;
    if !(5..=1200).contains(&timeout) || !(1..=10).contains(&retries) {
        return Err(bad("timeout or retries invalid"));
    }
    let default_models = json!([]);
    let models = models(payload.get("models").unwrap_or(&default_models))?;
    let secret = match payload.get("api_key") {
        None | Some(Value::Null) => previous
            .and_then(|v| v.get("api_key_enc"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        Some(Value::String(s)) => {
            if s.chars().count() > 8192 {
                return Err(bad("API key too long"));
            }
            if s.trim().is_empty() {
                String::new()
            } else {
                format!("dbplain:v1:{}", s.trim())
            }
        }
        _ => return Err(bad("API key must be string or null")),
    };
    Ok(json!({
        "id":id,"name":name.trim(),"base_url":base,"api_key_enc":secret,
        "api_type":api_type,"timeout_seconds":timeout,"max_retries":retries,
        "models":models
    }))
}
async fn lock_and_read(tx: &mut Transaction<'_, Postgres>) -> AppResult<Vec<Value>> {
    // Serializes Rust writers, including creation of a missing app_settings row.
    // Python old code does not take this advisory lock: cross-version concurrent
    // changes still require operational sequencing.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext($1))")
        .bind(KEY)
        .execute(&mut **tx)
        .await?;
    let json: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(KEY)
            .fetch_optional(&mut **tx)
            .await?;
    let entries = json
        .as_ref()
        .and_then(|v| v.get("providers"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if entries.len() > 200 {
        return Err(bad("too many stored providers"));
    }
    Ok(entries)
}
async fn persist(tx: &mut Transaction<'_, Postgres>, rows: Vec<Value>) -> AppResult<()> {
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=now()",
    )
    .bind(KEY)
    .bind(json!({"providers":rows}))
    .execute(&mut **tx)
    .await?;
    Ok(())
}
fn safe(item: &Value) -> AppResult<Value> {
    let list = crate::subtitle_providers_v1::catalog(&json!({}), vec![item.clone()])?;
    list.last()
        .cloned()
        .ok_or_else(|| bad("invalid provider projection"))
}
async fn save(state: &AppState, id: Option<String>, payload: Value) -> AppResult<Value> {
    writable()?;
    let id = id.unwrap_or_else(|| Uuid::new_v4().to_string());
    if matches!(id.as_str(), "legacy" | "index_translate") {
        return Err(bad("built-in providers require translation settings"));
    }
    if id.is_empty() || id.len() > 128 {
        return Err(bad("invalid provider ID"));
    }
    let mut tx = state.db.pool.begin().await?;
    let rows = lock_and_read(&mut tx).await?;
    let previous = rows
        .iter()
        .find(|v| v.get("id").and_then(Value::as_str) == Some(id.as_str()));
    let new = normalize(&payload, &id, previous)?;
    let mut updated = rows
        .into_iter()
        .filter(|v| v.get("id").and_then(Value::as_str) != Some(id.as_str()))
        .collect::<Vec<_>>();
    updated.push(new.clone());
    persist(&mut tx, updated).await?;
    tx.commit().await?;
    safe(&new)
}
async fn remove(state: &AppState, id: String) -> AppResult<Value> {
    writable()?;
    if matches!(id.as_str(), "legacy" | "index_translate") {
        return Err(bad("built-in provider cannot be deleted"));
    }
    let mut tx = state.db.pool.begin().await?;
    let rows = lock_and_read(&mut tx).await?;
    if !rows
        .iter()
        .any(|v| v.get("id").and_then(Value::as_str) == Some(id.as_str()))
    {
        return Err(bad("provider does not exist"));
    }
    persist(
        &mut tx,
        rows.into_iter()
            .filter(|v| v.get("id").and_then(Value::as_str) != Some(id.as_str()))
            .collect(),
    )
    .await?;
    tx.commit().await?;
    Ok(json!({"status":"deleted"}))
}
fn error_response(err: AppError) -> Response {
    match err {
        AppError::Invalid(message) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":message})),
        )
            .into_response(),
        other => other.into_response(),
    }
}
pub(crate) async fn create(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, Response> {
    save(&state, None, body)
        .await
        .map(Json)
        .map_err(error_response)
}
pub(crate) async fn update(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Json<Value>, Response> {
    if matches!(id.as_str(), "legacy" | "index_translate") {
        return crate::subtitle_builtin_provider_v1::update(&state, &id, body)
            .await
            .map(Json)
            .map_err(error_response);
    }
    save(&state, Some(id), body)
        .await
        .map(Json)
        .map_err(error_response)
}
pub(crate) async fn delete(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    remove(&state, id).await.map(Json).map_err(error_response)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn budget_and_secret_semantics() {
        let payload = json!({"name":"Example","base_url":"https://example.com","api_key":"secret",
            "models":[{"id":"m1","context_window":1000,"max_output_tokens":900}]});
        let row = normalize(&payload, "id", None).unwrap();
        assert_eq!(row["api_key_enc"], "dbplain:v1:secret");
        assert_eq!(row["base_url"], "https://example.com/v1");
        assert_eq!(safe(&row).unwrap()["api_key_set"], true);
        assert!(!safe(&row).unwrap().to_string().contains("secret"));
        let mut invalid = payload.clone();
        invalid["models"][0]["max_output_tokens"] = json!(1000);
        assert!(normalize(&invalid, "id", None).is_err());
        let mut invalid = payload.clone();
        let dup = invalid["models"][0].clone();
        invalid["models"].as_array_mut().unwrap().push(dup);
        assert!(normalize(&invalid, "id", None).is_err());
        assert_eq!(
            normalize(
                &json!({"name":"X","base_url":"https://example.com","api_key":""}),
                "id",
                Some(&row)
            )
            .unwrap()["api_key_enc"],
            ""
        );
    }
}
