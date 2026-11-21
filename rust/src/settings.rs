//! Explicit, versioned settings overlay. Never mutate V1 config rows.
use crate::{AppError, AppResult, Database, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

const PUBLIC_SECTIONS: &[(&str, &str)] = &[
    ("auto", "subtitle.auto_profile"),
    ("asr", "subtitle.asr"),
    ("translate", "subtitle.translate"),
    ("youtube", "youtube.settings"),
    ("render", "subtitle.render"),
    ("publish", "publish.settings"),
    ("dubbing", "dubbing.settings"),
    ("review", "review.settings"),
    ("storage", "storage.settings"),
    ("agent", "translate.agent"),
];
pub fn resolve_key(section: &str) -> AppResult<&'static str> {
    PUBLIC_SECTIONS
        .iter()
        .find(|(name, _)| *name == section)
        .map(|(_, key)| *key)
        .ok_or_else(|| AppError::Invalid("settings section is not supported by v2".into()))
}
fn secure_fields(key: &str) -> bool {
    [
        "password",
        "api_key",
        "secret",
        "token",
        "cookie",
        "authorization",
        "credential",
        "private_key",
    ]
    .iter()
    .any(|needle| key.to_ascii_lowercase().contains(needle))
}
pub fn mask_secrets(value: &Value) -> Value {
    match value {
        Value::Object(obj) => {
            let mut output = serde_json::Map::new();
            for (k, v) in obj {
                if secure_fields(k) {
                    output.insert(k.clone(), json!({"configured":!v.is_null() && v!= ""}));
                } else {
                    output.insert(k.clone(), mask_secrets(v));
                }
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(mask_secrets).collect()),
        _ => value.clone(),
    }
}
#[derive(Debug, Serialize)]
pub struct SettingsView {
    pub section: String,
    pub key: String,
    pub version: i32,
    pub source: String,
    pub value: Value,
}
#[derive(Debug, Deserialize)]
pub struct PatchSettings {
    pub expected_version: i32,
    pub value: Value,
}
fn validate_keys(value: &Value) -> AppResult<()> {
    let Some(obj) = value.as_object() else {
        return Err(AppError::Invalid(
            "settings payload must be a JSON object".into(),
        ));
    };
    if obj.len() > 256 {
        return Err(AppError::Invalid("too many settings fields".into()));
    }
    for (k, v) in obj {
        if k.len() > 128 || k.is_empty() || secure_fields(k) {
            return Err(AppError::Invalid(format!(
                "settings key {k:?} cannot be edited here"
            )));
        }
        let bytes =
            serde_json::to_vec(v).map_err(|_| AppError::Invalid("invalid settings JSON".into()))?;
        if bytes.len() > 100_000 {
            return Err(AppError::Invalid("settings value exceeds budget".into()));
        }
    }
    Ok(())
}
async fn current(db: &Database, section: &str) -> AppResult<SettingsView> {
    let key = resolve_key(section)?;
    let overlay: Option<(Value, i32)> =
        sqlx::query_as("SELECT value_json,version FROM vr2_settings WHERE key=$1")
            .bind(key)
            .fetch_optional(&db.pool)
            .await?;
    let (value, version, source) = if let Some((value, version)) = overlay {
        (value, version, "v2")
    } else {
        let fallback: Option<(Value, i32)> =
            sqlx::query_as("SELECT value_json,version FROM app_settings WHERE key=$1")
                .bind(key)
                .fetch_optional(&db.pool)
                .await?;
        let (value, version) = fallback.unwrap_or((json!({}), 0));
        (value, version, "v1_readonly")
    };
    Ok(SettingsView {
        section: section.into(),
        key: key.into(),
        version,
        source: source.into(),
        value: mask_secrets(&value),
    })
}
pub async fn effective(db: &Database, key: &str) -> AppResult<Value> {
    let v: Option<(Value,)> = sqlx::query_as("SELECT value_json FROM vr2_settings WHERE key=$1")
        .bind(key)
        .fetch_optional(&db.pool)
        .await?;
    if let Some((v,)) = v {
        return Ok(v);
    }
    let (v,): (Option<Value>,) =
        sqlx::query_as("SELECT (SELECT value_json FROM app_settings WHERE key=$1)")
            .bind(key)
            .fetch_one(&db.pool)
            .await?;
    Ok(v.unwrap_or(json!({})))
}
async fn read(
    State(state): State<AppState>,
    Path(section): Path<String>,
) -> AppResult<Json<SettingsView>> {
    Ok(Json(current(&state.db, &section).await?))
}
async fn list(State(state): State<AppState>) -> AppResult<Json<Vec<SettingsView>>> {
    let mut values = Vec::with_capacity(PUBLIC_SECTIONS.len());
    for (section, _) in PUBLIC_SECTIONS {
        values.push(current(&state.db, section).await?);
    }
    Ok(Json(values))
}
async fn patch(
    State(state): State<AppState>,
    Path(section): Path<String>,
    Json(payload): Json<PatchSettings>,
) -> AppResult<Json<SettingsView>> {
    let key = resolve_key(&section)?;
    let value = payload.value;
    validate_keys(&value)?;
    let mut tx = state.db.pool.begin().await?;
    // Serialize first-touch writes even without an existing V2 row.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))")
        .bind(format!("vr2.settings:{key}"))
        .execute(&mut *tx)
        .await?;
    let active: Option<i32> =
        sqlx::query_scalar("SELECT version FROM vr2_settings WHERE key=$1 FOR UPDATE")
            .bind(key)
            .fetch_optional(&mut *tx)
            .await?;
    let version = if let Some(v) = active {
        v
    } else {
        sqlx::query_scalar::<_, i32>(
            "SELECT COALESCE((SELECT version FROM app_settings WHERE key=$1),0)",
        )
        .bind(key)
        .fetch_one(&mut *tx)
        .await?
    };
    if version != payload.expected_version {
        return Err(AppError::Conflict(format!(
            "settings version changed from {} to {}; reload before saving",
            payload.expected_version, version
        )));
    }
    sqlx::query(
        "INSERT INTO vr2_settings(key,value_json,version) VALUES($1,$2,$3) ON CONFLICT(key) DO UPDATE SET value_json=$2,version=$3,updated_at=now()"
    ).bind(key).bind(value).bind(version+1).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(current(&state.db, &section).await?))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings", get(list))
        .route("/settings/{section}", get(read).put(patch))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn deny_leaking_or_setting_credentials() {
        let redacted =
            mask_secrets(&json!({"nested":{"api_key":"abc","model":"qwen"},"token":null}));
        assert_eq!(redacted["nested"]["api_key"]["configured"], true);
        assert_eq!(redacted["nested"]["model"], "qwen");
        assert_eq!(redacted["token"]["configured"], false);
        assert!(validate_keys(&json!({"cookie":"secret"})).is_err());
        assert!(validate_keys(&json!({"asr_model":"whisper"})).is_ok());
    }
    #[test]
    fn reject_unregistered_sections() {
        assert!(resolve_key("auto").is_ok());
        assert!(resolve_key("root").is_err());
    }
}
