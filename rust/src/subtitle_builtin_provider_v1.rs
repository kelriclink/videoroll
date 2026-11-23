//! Python-compatible built-in translation model provider updates.
//! The Python source stores these in subtitle.translate, not the custom
//! subtitle.model_providers catalog. Mutations are explicitly opt-in and
//! rejected when a V2 overlay would hide the result.
use crate::{
    AppError, AppResult, api::AppState, providers, settings, subtitle_provider_write_v1 as write,
    subtitle_providers_v1 as read,
};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};

fn bad(msg: &str) -> AppError {
    AppError::Invalid(msg.into())
}
fn required_model(payload: &Value) -> AppResult<Value> {
    let items = payload
        .get("models")
        .ok_or_else(|| bad("built-in provider requires exactly one model"))?;
    let checked = write::models(items)?;
    let models = checked
        .as_array()
        .ok_or_else(|| bad("models must be an array"))?;
    if models.len() != 1 {
        return Err(bad("built-in provider requires exactly one model"));
    }
    Ok(models[0].clone())
}
fn positive_int(
    value: Option<&Value>,
    default: i64,
    min: i64,
    max: i64,
    key: &str,
) -> AppResult<i64> {
    let number = match value {
        None | Some(Value::Null) => default,
        Some(v) => v
            .as_i64()
            .ok_or_else(|| bad("integer configuration expected"))?,
    };
    if !(min..=max).contains(&number) {
        return Err(AppError::Invalid(format!("{key} out of supported range")));
    }
    Ok(number)
}
fn text_choice<'a>(
    value: Option<&'a Value>,
    fallback: &'a str,
    allowed: &[&str],
) -> AppResult<&'a str> {
    let name = value.and_then(Value::as_str).unwrap_or(fallback);
    if allowed.contains(&name) {
        Ok(name)
    } else {
        Err(bad("unsupported model reasoning or API type"))
    }
}
fn named_text(
    options: &Map<String, Value>,
    key: &str,
    old: &Value,
    default: &str,
    max_chars: usize,
) -> AppResult<String> {
    let name = options
        .get(key)
        .and_then(Value::as_str)
        .or_else(|| old.get(key).and_then(Value::as_str))
        .unwrap_or(default);
    Ok(name.chars().take(max_chars).collect::<String>())
}
async fn locked_translate(tx: &mut Transaction<'_, Postgres>) -> AppResult<Map<String, Value>> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext($1))")
        .bind("subtitle.translate")
        .execute(&mut **tx)
        .await?;
    let data: Option<Value> = sqlx::query_scalar(
        "SELECT value_json FROM app_settings WHERE key='subtitle.translate' FOR UPDATE",
    )
    .fetch_optional(&mut **tx)
    .await?;
    Ok(data
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default())
}
fn patch_legacy(stored: &mut Map<String, Value>, body: &Value, model: &Value) -> AppResult<()> {
    if model.get("context_window").is_some_and(|v| !v.is_null())
        || model.get("max_output_tokens").is_some_and(|v| !v.is_null())
    {
        return Err(bad(
            "built-in compatibility model does not store token capacity",
        ));
    }
    let api = text_choice(body.get("api_type"), "openai", &["openai", "cerebras"])?;
    let base = body
        .get("base_url")
        .and_then(Value::as_str)
        .ok_or_else(|| bad("base_url required"))?;
    let base = crate::subtitle_providers_v1::normalize_openai_url(base);
    let parsed = reqwest::Url::parse(&base).map_err(|_| bad("base_url invalid"))?;
    if !matches!(parsed.scheme(), "http" | "https")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err(bad("built-in translation service URL invalid"));
    }
    let timeout = positive_int(body.get("timeout_seconds"), 180, 5, 1200, "timeout_seconds")?;
    let retries = positive_int(body.get("max_retries"), 3, 1, 10, "max_retries")?;
    let mut openai = stored
        .get("openai")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let temperature = model
        .get("temperature")
        .filter(|v| !v.is_null())
        .cloned()
        .or_else(|| openai.get("temperature").cloned())
        .unwrap_or(json!(0.2));
    let model_id = model.get("id").and_then(Value::as_str).unwrap_or("");
    let effort = text_choice(
        model.get("reasoning_effort"),
        "medium",
        &["low", "medium", "high"],
    )?;
    let format = text_choice(
        model.get("reasoning_format"),
        "parsed",
        &["parsed", "raw", "hidden"],
    )?;
    openai.insert("base_url".into(), json!(base));
    openai.insert("model".into(), json!(model_id));
    openai.insert("temperature".into(), temperature);
    openai.insert("timeout_seconds".into(), json!(timeout));
    openai.insert("max_retries".into(), json!(retries));
    openai.insert("api_type".into(), json!(api));
    openai.insert(
        "enable_thinking".into(),
        model
            .get("enable_thinking")
            .cloned()
            .unwrap_or(json!(false)),
    );
    openai.insert("cerebras_reasoning_effort".into(), json!(effort));
    openai.insert("cerebras_reasoning_format".into(), json!(format));
    match body.get("api_key") {
        None | Some(Value::Null) => {}
        Some(Value::String(secret)) if secret.trim().is_empty() => {
            openai.remove("api_key_enc");
        }
        Some(Value::String(secret)) if secret.chars().count() <= 8192 => {
            openai.insert(
                "api_key_enc".into(),
                json!(format!("dbplain:v1:{}", secret.trim())),
            );
        }
        _ => return Err(bad("api_key invalid")),
    }
    stored.insert("openai".into(), Value::Object(openai));
    Ok(())
}
fn patch_index(stored: &mut Map<String, Value>, body: &Value, model: &Value) -> AppResult<()> {
    let timeout = positive_int(
        body.get("timeout_seconds"),
        180,
        15,
        300,
        "index_timeout_seconds",
    )?;
    let temp = model
        .get("temperature")
        .filter(|v| !v.is_null())
        .cloned()
        .or_else(|| stored.get("index_temperature").cloned())
        .unwrap_or(json!(0.0));
    let num = temp
        .as_f64()
        .ok_or_else(|| bad("index_temperature invalid"))?;
    if !num.is_finite() || !(0.0..=2.0).contains(&num) {
        return Err(bad("index_temperature outside supported range"));
    }
    let max_tokens = positive_int(
        model.get("max_output_tokens"),
        stored
            .get("index_max_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(4096),
        128,
        16384,
        "index_max_tokens",
    )?;
    let default_options = Map::new();
    let options = match body.get("options") {
        None | Some(Value::Null) => &default_options,
        Some(Value::Object(o)) => o,
        _ => return Err(bad("index options must be an object")),
    };
    let batch = positive_int(
        options.get("batch_size"),
        stored
            .get("index_batch_size")
            .and_then(Value::as_i64)
            .unwrap_or(12),
        1,
        30,
        "index_batch_size",
    )?;
    let chars = positive_int(
        options.get("context_chars"),
        stored
            .get("index_context_chars")
            .and_then(Value::as_i64)
            .unwrap_or(6000),
        4000,
        100000,
        "index_context_chars",
    )?;
    let original = json!({
        "domain":stored.get("index_domain"),
        "glossary":stored.get("index_glossary"),
        "instruction":stored.get("index_instruction")
    });
    let domain = named_text(options, "domain", &original, "视频字幕", 90)?;
    let glossary = named_text(options, "glossary", &original, "", 4000)?;
    let instruction = named_text(options, "instruction", &original, "", 2000)?;
    stored.insert("index_temperature".into(), temp);
    stored.insert("index_timeout_seconds".into(), json!(timeout as f64));
    stored.insert("index_max_tokens".into(), json!(max_tokens));
    stored.insert("index_batch_size".into(), json!(batch));
    stored.insert("index_context_chars".into(), json!(chars));
    stored.insert("index_domain".into(), json!(domain));
    stored.insert("index_glossary".into(), json!(glossary));
    stored.insert("index_instruction".into(), json!(instruction));
    Ok(())
}
pub(crate) async fn update(state: &AppState, provider_id: &str, body: Value) -> AppResult<Value> {
    write::writable()?;
    let model = required_model(&body)?;
    let mut tx = state.db.pool.begin().await?;
    let mut stored = locked_translate(&mut tx).await?;
    match provider_id {
        "legacy" => patch_legacy(&mut stored, &body, &model)?,
        "index_translate" => patch_index(&mut stored, &body, &model)?,
        _ => return Err(bad("not a built-in provider")),
    }
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version)
         VALUES('subtitle.translate',$1,1)
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=now()",
    )
    .bind(json!(stored))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    let translate = settings::effective(&state.db, "subtitle.translate").await?;
    let customs = providers::read_catalog(&state.db).await?;
    let rows = read::catalog(&translate, customs)?;
    rows.into_iter()
        .find(|v| v["id"] == provider_id)
        .ok_or_else(|| bad("builtin translation provider projection unavailable"))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn builtin_limits() {
        let mut row = Map::new();
        let model = json!({"id":"m","temperature":0.7,
           "context_window":null,"max_output_tokens":null,
           "enable_thinking":true,"reasoning_effort":"high","reasoning_format":"parsed"});
        let body = json!({"base_url":"https://example.test","api_type":"cerebras",
          "timeout_seconds":150,"max_retries":2});
        patch_legacy(&mut row, &body, &model).unwrap();
        assert_eq!(row["openai"]["model"], "m");
        assert_eq!(row["openai"]["api_type"], "cerebras");
        assert!(patch_legacy(&mut row, &body, &json!({"context_window":1024})).is_err());
        let mut ix = Map::new();
        patch_index(
            &mut ix,
            &json!({"timeout_seconds":200,"options":{"batch_size":20}}),
            &json!({"id":"Index","temperature":0.3,"max_output_tokens":8192}),
        )
        .unwrap();
        assert_eq!(ix["index_max_tokens"], 8192);
        assert!(patch_index(&mut ix, &json!({"timeout_seconds":5}), &model).is_err());
    }
}
