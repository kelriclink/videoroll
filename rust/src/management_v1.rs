//! Management compatibility for Python YouDub job summaries and render connection.
//! The existing YouDub/RenderWorker engines remain separate services.
//! No synthetic GPU residency or fabricated executor state is reported.
use crate::{AppError, AppResult, Database, api::AppState, settings};
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, header::HOST},
    routing::get,
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

async fn dubbing_summary(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let rows = sqlx::query(
        "SELECT id,task_id,status,progress,output_json->'youdub'->>'stage' AS stage,
        error_message,created_at
        FROM dubbing_jobs
        WHERE request_json->>'model_path'='youdub:voxcpm2'
        ORDER BY created_at DESC LIMIT 40",
    )
    .fetch_all(&state.db.pool)
    .await?;
    let active: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT FROM dubbing_jobs
        WHERE request_json->>'model_path'='youdub:voxcpm2'
        AND status IN ('queued','preparing','synthesizing','aligning','mixing',
                        'rendering','pause_requested','cancel_requested')",
    )
    .fetch_one(&state.db.pool)
    .await?;
    let queued: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT FROM dubbing_jobs
        WHERE request_json->>'model_path'='youdub:voxcpm2' AND status='queued'",
    )
    .fetch_one(&state.db.pool)
    .await?;
    let failures = rows
        .iter()
        .filter(|r| r.try_get::<String, _>("status").ok().as_deref() == Some("failed"))
        .count();
    let mut recent = Vec::new();
    for row in rows.iter().take(12) {
        recent.push(json!({
          "id":row.try_get::<Uuid,_>("id")?.to_string(),
          "task_id":row.try_get::<Uuid,_>("task_id")?.to_string(),
          "status":row.try_get::<String,_>("status")?,
          "progress":row.try_get::<i32,_>("progress")?,
          "stage":row.try_get::<Option<String>,_>("stage")?,
          "error":row.try_get::<Option<String>,_>("error_message")?,
          "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?
        }));
    }
    Ok(Json(json!({
      "queued":queued,"running":active.saturating_sub(queued),
      "failed":failures,"recent":recent
    })))
}
const CONNECTION_KEY: &str = "render_worker_connection";
const WORKER_PATH: &str = "/api/render-workers/v1";
async fn connection_config(db: &Database) -> AppResult<Option<String>> {
    let value = settings::effective(db, CONNECTION_KEY).await?;
    Ok(value
        .get("server_url")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string))
}
fn request_origin(headers: &HeaderMap) -> String {
    let host = headers
        .get("x-forwarded-host")
        .or_else(|| headers.get(HOST))
        .and_then(|v| v.to_str().ok())
        .unwrap_or("127.0.0.1")
        .trim();
    let safe = if host.len() <= 255
        && !host.is_empty()
        && host.bytes().all(|b| {
            b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_' | b':' | b'[' | b']')
        }) {
        host
    } else {
        "127.0.0.1"
    };
    let proto = headers
        .get("x-forwarded-proto")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("http")
        .split(',')
        .next()
        .unwrap_or("http")
        .trim();
    format!(
        "{}://{}",
        if proto == "https" { "https" } else { "http" },
        safe
    )
}
pub(crate) async fn render_server_url(db: &Database, headers: &HeaderMap) -> AppResult<String> {
    Ok(connection_config(db)
        .await?
        .unwrap_or_else(|| request_origin(headers)))
}
fn response(url: &str) -> Value {
    json!({"server_url":url,"worker_api_path":WORKER_PATH,"protocol_version":1})
}
async fn connection_get(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    let fallback = request_origin(&headers);
    let url = connection_config(&state.db).await?.unwrap_or(fallback);
    Ok(Json(response(&url)))
}
#[derive(Debug, Deserialize)]
struct ConnectionUpdate {
    server_url: String,
}
fn checked_url(raw: &str) -> AppResult<String> {
    let cleaned = raw.trim().trim_end_matches('/');
    if cleaned.is_empty() || cleaned.len() > 512 {
        return Err(AppError::Invalid(
            "server_url must be an absolute http(s) URL without credentials".into(),
        ));
    }
    let url =
        reqwest::Url::parse(cleaned).map_err(|_| AppError::Invalid("invalid server_url".into()))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(AppError::Invalid(
            "server_url must be an absolute http(s) URL without credentials".into(),
        ));
    }
    Ok(cleaned.to_string())
}
async fn connection_put(
    State(state): State<AppState>,
    Json(payload): Json<ConnectionUpdate>,
) -> AppResult<Json<Value>> {
    let url = checked_url(&payload.server_url)?;
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
        ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,
        version=app_settings.version+1,updated_at=now()",
    )
    .bind(CONNECTION_KEY)
    .bind(json!({"server_url":url}))
    .execute(&state.db.pool)
    .await?;
    Ok(Json(response(&url)))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/dubbing/jobs/summary", get(dubbing_summary))
        .route(
            "/render-management/connection",
            get(connection_get).put(connection_put),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn connection_rejects_credentials_and_invalid() {
        assert!(checked_url("https://render.example.net/").is_ok());
        assert!(checked_url("https://abc:secret@host.com").is_err());
        assert!(checked_url("ftp://host.com").is_err());
        assert!(checked_url("http://").is_err());
    }
}
