//! Original /settings/api contract. The actual remote intake must continue to
//! authenticate with the PBKDF2 token, never with an admin cookie or an
//! unvalidated query token. This module only edits the original app_settings.
use crate::{AppError, AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::get};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use pbkdf2::pbkdf2_hmac;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::Sha256;
use sqlx::Row;
use uuid::Uuid;

const KEY: &str = "remote.api";

fn public(value: &Value) -> Value {
    json!({
        "token_set":value.get("token_hash").and_then(Value::as_str).is_some_and(|s| !s.trim().is_empty()),
        "token_updated_at":value.get("token_updated_at").and_then(Value::as_str).filter(|s| !s.is_empty()),
        "endpoint_path":"/remote/auto/youtube",
        "http_method":"POST",
        "authorization_header":"Authorization",
        "idempotency_header":"Idempotency-Key",
    })
}
async fn get_config(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(public(
        &crate::settings::effective(&state.db, KEY).await?,
    )))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigUpdate {
    token: Option<String>,
}
async fn put_config(
    State(state): State<AppState>,
    Json(update): Json<ConfigUpdate>,
) -> AppResult<Json<Value>> {
    // None means unchanged; empty string means explicitly revoke the token.
    let change = update.token.map(|t| t.trim().to_owned());
    if let Some(ref token) = change
        && !token.is_empty()
        && !(8..=512).contains(&token.chars().count())
    {
        return Err(AppError::Invalid(
            "remote API token must contain 8–512 characters".into(),
        ));
    }
    let encrypted = change.as_deref().filter(|s| !s.is_empty()).map(|token| {
        let salt = Uuid::new_v4().into_bytes();
        let mut result = [0u8; 32];
        pbkdf2_hmac::<Sha256>(token.as_bytes(), &salt, 200_000, &mut result);
        format!(
            "pbkdf2_sha256$200000${}${}",
            URL_SAFE_NO_PAD.encode(salt),
            URL_SAFE_NO_PAD.encode(result)
        )
    });
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('app.settings:remote.api',0))")
        .execute(&mut *tx)
        .await?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES($1,'{}'::jsonb,1) ON CONFLICT(key) DO NOTHING")
        .bind(KEY).execute(&mut *tx).await?;
    let row = sqlx::query("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
        .bind(KEY)
        .fetch_one(&mut *tx)
        .await?;
    let mut stored: Value = row.try_get("value_json")?;
    if !stored.is_object() {
        return Err(AppError::Conflict(
            "remote API settings are not a JSON object".into(),
        ));
    }
    if let Some(token) = change {
        if token.is_empty() {
            stored.as_object_mut().unwrap().remove("token_hash");
            stored.as_object_mut().unwrap().remove("token_updated_at");
        } else {
            stored["token_hash"] = json!(encrypted.expect("hashed nonempty token"));
            stored["token_updated_at"] = json!(chrono::Utc::now().to_rfc3339());
        }
        sqlx::query(
            "UPDATE app_settings SET value_json=$2,version=version+1,updated_at=now() WHERE key=$1",
        )
        .bind(KEY)
        .bind(&stored)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(Json(public(&stored)))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/settings/api", get(get_config).put(put_config))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn secrets_never_leak_from_settings() {
        let v = public(&json!({"token_hash":"plain-secret","token_updated_at":"some-time"}));
        assert_eq!(v["token_set"], true);
        assert!(!v.to_string().contains("plain-secret"));
    }
}
