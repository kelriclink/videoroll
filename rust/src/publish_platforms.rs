//! Native, isolated platform enablement preferences matching V1 semantics.
//! Do not claim SAU support just because a toggle can be saved: until the
//! complete account/session/publish adapter is migrated, social enablement
//! is rejected explicitly rather than silently selecting a dead backend.
use crate::{AppError, AppResult, Config, Database, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use serde::Deserialize;
use serde_json::{Value, json};

const KEY: &str = "publish.platforms";
const PLATFORMS: [&str; 4] = ["bilibili", "douyin", "xiaohongshu", "kuaishou"];

fn safe_platform(s: &str) -> AppResult<&str> {
    if !PLATFORMS.contains(&s) {
        return Err(AppError::Invalid("unsupported publishing platform".into()));
    }
    Ok(s)
}
fn normalized(raw: &Value) -> Value {
    let mut out = json!({"bilibili":true,"douyin":false,"xiaohongshu":false,"kuaishou":false});
    // Preserve the original Bilibili preference, but never expose social
    // platforms as enabled until the complete SAU adapter is present.
    if let Some(v) = raw.get("bilibili").and_then(Value::as_bool) {
        out["bilibili"] = json!(v);
    }
    out
}
fn sau_configured(config: &Config) -> bool {
    config.social_publish_allowed
        && config.social_publisher_url.is_some()
        && config
            .legacy_internal_secret
            .as_deref()
            .is_some_and(|v| v.len() >= 16)
        && config.social_storage_root.is_some()
}
pub async fn social_enabled(config: &Config, db: &Database, platform: &str) -> AppResult<bool> {
    safe_platform(platform)?;
    if platform == "bilibili" || !sau_configured(config) {
        return Ok(false);
    }
    let value: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(KEY)
            .fetch_optional(&db.pool)
            .await?;
    Ok(value
        .as_ref()
        .and_then(|v| v.get(platform))
        .and_then(Value::as_bool)
        == Some(true))
}
pub async fn enabled(db: &Database, platform: &str) -> AppResult<bool> {
    safe_platform(platform)?;
    let raw = crate::settings::effective(db, KEY).await?;
    Ok(normalized(&raw)[platform] == true)
}
async fn read(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let v = normalized(&crate::settings::effective(&state.db, KEY).await?);
    let mut v = v;
    if sau_configured(&state.config) {
        let override_setting: Option<Value> =
            sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
                .bind(KEY)
                .fetch_optional(&state.db.pool)
                .await?;
        if let Some(o) = override_setting {
            for p in ["douyin", "xiaohongshu", "kuaishou"] {
                if o[p] == true {
                    v[p] = json!(true);
                }
            }
        }
    }
    Ok(Json(json!({"platforms":v})))
}
#[derive(Debug, Deserialize)]
struct Update {
    enabled: bool,
}
async fn update(
    State(state): State<AppState>,
    Path(platform): Path<String>,
    Json(payload): Json<Update>,
) -> AppResult<Json<Value>> {
    let platform = safe_platform(&platform)?;
    if platform != "bilibili" && payload.enabled && !sau_configured(&state.config) {
        return Err(AppError::Conflict(
            "SAU publishing requires explicit opt-in, a sidecar, internal secret and shared media root".into(),
        ));
    }
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('vr2.publish.platforms',0))")
        .execute(&mut *tx)
        .await?;
    let raw: Option<Value> = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(KEY)
        .fetch_optional(&mut *tx)
        .await?;
    let from_original = raw.is_some();
    let source = if let Some(v) = raw {
        v
    } else {
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(KEY)
            .fetch_optional(&mut *tx)
            .await?
            .unwrap_or(json!({}))
    };
    let mut next = normalized(&source);
    for key in ["douyin", "xiaohongshu", "kuaishou"] {
        if next[key] == true {
            next[key] = json!(false);
        }
    }
    // Existing social choices may be persisted only when all SAU
    // safety preconditions have been configured on this deployment.
    if from_original
        && sau_configured(&state.config)
        && let Some(existing) = source.as_object()
    {
        for p in ["douyin", "xiaohongshu", "kuaishou"] {
            if existing.get(p).and_then(Value::as_bool) == Some(true) {
                next[p] = json!(true);
            }
        }
    }
    next[platform] = json!(payload.enabled);
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=$2,version=app_settings.version+1,updated_at=now()"
    ).bind(KEY).bind(&next).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(json!({"platforms":next})))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/publish/platforms", get(read))
        .route(
            "/settings/publish/platforms/{platform}",
            axum::routing::put(update),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_defaults_and_platform_validation() {
        assert_eq!(normalized(&json!({}))["bilibili"], true);
        assert_eq!(normalized(&json!({"bilibili":false}))["bilibili"], false);
        assert!(safe_platform("ftp").is_err());
    }
}
