//! Native, isolated platform enablement preferences matching V1 semantics.
//! Do not claim SAU support just because a toggle can be saved: until the
//! complete account/session/publish adapter is migrated, social enablement
//! is rejected explicitly rather than silently selecting a dead backend.
use crate::{AppError, AppResult, Database, api::AppState};
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
pub async fn enabled(db: &Database, platform: &str) -> AppResult<bool> {
    safe_platform(platform)?;
    let raw = crate::settings::effective(db, KEY).await?;
    Ok(normalized(&raw)[platform] == true)
}
async fn read(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let v = normalized(&crate::settings::effective(&state.db, KEY).await?);
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
    if platform != "bilibili" && payload.enabled {
        return Err(AppError::Conflict(
            "social SAU account sessions and actual publication are not native in V2 yet".into(),
        ));
    }
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('vr2.publish.platforms',0))")
        .execute(&mut *tx)
        .await?;
    let raw: Option<Value> = sqlx::query_scalar("SELECT value_json FROM vr2_settings WHERE key=$1")
        .bind(KEY)
        .fetch_optional(&mut *tx)
        .await?;
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
    next[platform] = json!(payload.enabled);
    sqlx::query(
        "INSERT INTO vr2_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=$2,version=vr2_settings.version+1,updated_at=now()"
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
