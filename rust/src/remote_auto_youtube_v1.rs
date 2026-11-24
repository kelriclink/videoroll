//! Original public remote YouTube intake. It has its own Bearer PBKDF2
//! authentication, not the administrator cookie or Rust internal API token.
//! Original remote.api settings and remote_api_requests ledger are authoritative.
use crate::{AppError, AppResult, api::AppState, auth, ingest, legacy_contracts};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{HeaderMap, StatusCode, header},
    routing::get,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use std::sync::{Arc, OnceLock};
use subtle::ConstantTimeEq;
use tokio::sync::Semaphore;
use uuid::Uuid;

const CONFIG: &str = "remote.api";
const MAX_KEY: usize = 255;
const MAX_BEARER: usize = 512;
const MAX_ACTIVE: i64 = 4;
const MAX_PER_MINUTE: i64 = 60;

#[derive(Clone, Debug, Deserialize, Serialize)]
// Original Pydantic BaseModel ignores unrecognized request properties.
struct RemoteRequest {
    url: String,
    #[serde(default = "authorized")]
    license: String,
    #[serde(default)]
    proof_url: Option<String>,
    #[serde(default)]
    auto_publish: Option<bool>,
}
fn authorized() -> String {
    "authorized".into()
}

fn sha256(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}
fn canonical_hash(payload: &RemoteRequest) -> AppResult<String> {
    // serde_json::Map uses sorted keys by default, matching Python
    // model_dump(mode=json), json.dumps(sort_keys=True,separators=(',',':')).
    let data = serde_json::to_string(payload)
        .map_err(|_| AppError::Invalid("remote request payload invalid".into()))?;
    let value: Value = serde_json::from_str(&data)
        .map_err(|_| AppError::Invalid("remote request payload invalid".into()))?;
    let canonical = serde_json::to_string(&value)
        .map_err(|_| AppError::Invalid("remote request payload invalid".into()))?;
    Ok(sha256(&canonical))
}
fn bearer(headers: &HeaderMap) -> AppResult<String> {
    let value = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let (scheme, token) = value.split_once(' ').unwrap_or(("", ""));
    if !bool::from(scheme.to_ascii_lowercase().as_bytes().ct_eq(b"bearer"))
        || token.is_empty()
        || token.trim() != token
        || token.len() > MAX_BEARER
        || token.chars().any(char::is_control)
    {
        return Err(AppError::Unauthorized);
    }
    Ok(token.to_owned())
}
fn key(headers: &HeaderMap) -> AppResult<String> {
    let key = headers
        .get("idempotency-key")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .trim();
    if key.is_empty() || key.len() > MAX_KEY || key.chars().any(char::is_control) {
        return Err(AppError::Invalid(
            "Idempotency-Key header is required (1-255 bytes)".into(),
        ));
    }
    Ok(key.into())
}
async fn authenticate(state: &AppState, headers: &HeaderMap) -> AppResult<String> {
    let stored: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(CONFIG)
            .fetch_optional(&state.db.pool)
            .await?;
    let hash = stored
        .as_ref()
        .and_then(|v| v.get("token_hash"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if hash.is_empty() {
        return Err(AppError::Forbidden("remote api token is not set".into()));
    }
    let token = bearer(headers)?;
    // PBKDF2 uses 200k rounds. Never block the async HTTP reactor or allow
    // unbounded unauthenticated hash jobs to exhaust API worker threads.
    static HASH_SLOTS: OnceLock<Arc<Semaphore>> = OnceLock::new();
    let slots = HASH_SLOTS.get_or_init(|| Arc::new(Semaphore::new(8)));
    let permit = slots
        .clone()
        .try_acquire_owned()
        .map_err(|_| AppError::RateLimited)?;
    let digest = sha256(&token);
    let hash = hash.to_owned();
    let valid = tokio::task::spawn_blocking(move || {
        let _guard = permit;
        auth::verify_password(&token, &hash)
    })
    .await
    .map_err(|_| AppError::Unavailable("remote authentication unavailable".into()))?;
    if !valid {
        return Err(AppError::Unauthorized);
    }
    Ok(digest)
}
fn proof(proof: Option<&str>) -> AppResult<Option<String>> {
    let Some(value) = proof.filter(|p| !p.trim().is_empty()) else {
        return Ok(None);
    };
    if value.len() > 2048 {
        return Err(AppError::Invalid("proof URL too long".into()));
    }
    let url =
        reqwest::Url::parse(value).map_err(|_| AppError::Invalid("invalid proof URL".into()))?;
    if !["https", "http"].contains(&url.scheme())
        || url.username() != ""
        || url.password().is_some()
        || url.host().is_none()
    {
        return Err(AppError::Invalid("invalid proof URL".into()));
    }
    Ok(Some(value.into()))
}
fn replay(row: &sqlx::postgres::PgRow, hash: &str) -> AppResult<Value> {
    let previous: String = row.try_get("request_hash")?;
    if !bool::from(previous.as_bytes().ct_eq(hash.as_bytes())) {
        return Err(AppError::Conflict(
            "Idempotency-Key was already used with a different payload".into(),
        ));
    }
    let status: String = row.try_get("status")?;
    if status == "failed" {
        return Err(AppError::Conflict(
            "remote request previously failed; use a new Idempotency-Key to retry".into(),
        ));
    }
    if status != "completed" {
        return Err(AppError::Conflict(
            "remote request is already being processed".into(),
        ));
    }
    let stored: Option<Value> = row.try_get("response_json")?;
    let value = stored
        .filter(|v| v.get("task_id").is_some())
        .ok_or_else(|| AppError::Unavailable("remote request result is invalid".into()))?;
    Ok(value)
}
async fn reserve(
    state: &AppState,
    token_hash: &str,
    idempotency: &str,
    request_hash: &str,
) -> AppResult<Option<Value>> {
    let mut tx = state.db.pool.begin().await?;
    // Row lock is transaction-scoped and interoperates with Python's unique
    // constraint; the reservation is committed before the workflow side effect.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))")
        .bind(format!("remote-api:{token_hash}:{idempotency}"))
        .execute(&mut *tx)
        .await?;
    // Serialize per-token quota checks across distinct idempotency keys too:
    // without this second lock, simultaneous valid clients can all observe
    // the same pending count and exceed the original four-dispatch ceiling.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))")
        .bind(format!("remote-api-quota:{token_hash}"))
        .execute(&mut *tx)
        .await?;
    let existing = sqlx::query(
        "SELECT request_hash,status,response_json FROM remote_api_requests
         WHERE token_hash=$1 AND idempotency_key=$2 FOR UPDATE",
    )
    .bind(token_hash)
    .bind(idempotency)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(row) = existing {
        return replay(&row, request_hash).map(Some);
    }
    // No Redis dependency required for correctness. Bounded original-ledger
    // token quota prevents unlimited remote intake if Redis is unavailable.
    let recent: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM remote_api_requests
         WHERE token_hash=$1 AND created_at>now()-interval '1 minute'",
    )
    .bind(token_hash)
    .fetch_one(&mut *tx)
    .await?;
    let pending: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM remote_api_requests
         WHERE token_hash=$1 AND status='pending' AND lease_until>now()",
    )
    .bind(token_hash)
    .fetch_one(&mut *tx)
    .await?;
    if recent >= MAX_PER_MINUTE || pending >= MAX_ACTIVE {
        return Err(AppError::RateLimited);
    }
    let inserted = sqlx::query(
        "INSERT INTO remote_api_requests
         (id,token_hash,idempotency_key,request_hash,request_json,status,lease_owner,lease_until)
         VALUES($1,$2,$3,$4,'{}'::jsonb,'pending','remote-api:rust',now()+interval '24 hours')
         ON CONFLICT(token_hash,idempotency_key) DO NOTHING",
    )
    .bind(Uuid::new_v4())
    .bind(token_hash)
    .bind(idempotency)
    .bind(request_hash)
    .execute(&mut *tx)
    .await?;
    if inserted.rows_affected() != 1 {
        // A Python process raced with this reservation; the record is
        // authoritative whether it is completed, failed or pending.
        let row = sqlx::query(
            "SELECT request_hash,status,response_json FROM remote_api_requests
             WHERE token_hash=$1 AND idempotency_key=$2",
        )
        .bind(token_hash)
        .bind(idempotency)
        .fetch_one(&mut *tx)
        .await?;
        return replay(&row, request_hash).map(Some);
    }
    tx.commit().await?;
    Ok(None)
}
async fn settle(
    state: &AppState,
    token_hash: &str,
    idempotency: &str,
    response: Option<Value>,
) -> AppResult<()> {
    let completed = response.is_some();
    let changed = sqlx::query(
        "UPDATE remote_api_requests
         SET status=$3,response_json=$4,lease_owner=NULL,completed_at=now(),updated_at=now()
         WHERE token_hash=$1 AND idempotency_key=$2
          AND status='pending' AND lease_owner='remote-api:rust'",
    )
    .bind(token_hash)
    .bind(idempotency)
    .bind(if completed { "completed" } else { "failed" })
    .bind(response.unwrap_or_else(|| json!({"detail":"pipeline dispatch failed"})))
    .execute(&state.db.pool)
    .await?;
    if changed.rows_affected() != 1 {
        return Err(AppError::Unavailable(
            "remote request result could not be persisted".into(),
        ));
    }
    Ok(())
}
async fn post_remote(
    State(state): State<AppState>,
    headers: HeaderMap,
    request: Request<Body>,
) -> AppResult<Json<Value>> {
    // Authentication before key validation and URL/body dispatch; never use
    // cookies, query string tokens, x-internal-token or the admin session.
    let token_hash = authenticate(&state, &headers).await?;
    let idempotency = key(&headers)?;
    // Authenticate before consuming or validating body JSON. Axum's Json
    // extractor otherwise returns 422 for unauthenticated empty requests,
    // creating an authentication bypass in the public error contract.
    let body = to_bytes(request.into_body(), 32 * 1024)
        .await
        .map_err(|_| AppError::TooLarge("remote YouTube request body exceeds 32 KiB".into()))?;
    let payload: RemoteRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::Invalid("remote YouTube request must be valid JSON".into()))?;
    if !legacy_contracts::valid_license(&payload.license) {
        return Err(AppError::Invalid("invalid YouTube license".into()));
    }
    let id = ingest::youtube_video_id(&payload.url)?;
    let source_proof = proof(payload.proof_url.as_deref())?;
    let request_hash = canonical_hash(&payload)?;
    if let Some(previous) = reserve(&state, &token_hash, &idempotency, &request_hash).await? {
        return Ok(Json(previous));
    }
    // No uncertain 24h pending reservation may be retried automatically. A
    // later new idempotency key still hits ingested_videos original uniqueness.
    let dispatch = state
        .scheduler
        .create_auto_youtube_with_proof(
            &payload.url,
            &payload.license,
            0,
            json!({"auto_publish":false,"auto_publish_platforms":[]}),
            source_proof.as_deref(),
        )
        .await;
    let (task, flow, deduped) = match dispatch {
        Ok(value) => value,
        Err(error) => {
            settle(&state, &token_hash, &idempotency, None).await?;
            return Err(error);
        }
    };
    let answer = json!({
        "task_id":task.id,
        "pipeline_job_id":if deduped {None} else {flow.map(|v|v.id.to_string())},
        "deduped":deduped,
        "source_id":id
    });
    settle(&state, &token_hash, &idempotency, Some(answer.clone())).await?;
    Ok(Json(answer))
}
async fn legacy_query_removed() -> (StatusCode, Json<Value>) {
    (
        StatusCode::GONE,
        Json(json!({
            "detail":"Remote API query-token requests were removed; use POST JSON with Authorization: Bearer and Idempotency-Key."
        })),
    )
}
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/remote/auto/youtube",
        get(legacy_query_removed).post(post_remote),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_canonical_payload_is_stable_with_default_nulls() {
        let v: RemoteRequest = serde_json::from_value(json!({
            "url":"https://www.youtube.com/watch?v=abcdef12345"
        }))
        .unwrap();
        assert_eq!(v.license, "authorized");
        assert_eq!(v.proof_url, None);
        let normalized: Value = serde_json::from_str(&serde_json::to_string(&v).unwrap()).unwrap();
        assert_eq!(
            normalized,
            json!({"url":"https://www.youtube.com/watch?v=abcdef12345",
            "license":"authorized","proof_url":null,"auto_publish":null})
        );
        assert_eq!(canonical_hash(&v).unwrap().len(), 64);
    }
    #[test]
    fn malformed_bearer_is_rejected() {
        let mut headers = HeaderMap::new();
        assert!(bearer(&headers).is_err());
        headers.insert(header::AUTHORIZATION, "Basic abc".parse().unwrap());
        assert!(bearer(&headers).is_err());
        headers.insert(header::AUTHORIZATION, "Bearer abc".parse().unwrap());
        assert_eq!(bearer(&headers).unwrap(), "abc");
    }
}
