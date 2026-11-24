//! Native original desktop_access_grants protocol with cookie-bound grants.
use crate::{AppError, AppResult, api::AppState, auth};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use reqwest::Url;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use subtle::ConstantTimeEq;
use uuid::Uuid;

const TTL: i64 = 300;
const RECONNECT: i64 = 8;
#[derive(Deserialize)]
struct GrantRequest {
    desktop_type: String,
    resource_id: String,
}
fn hash(raw: &str) -> String {
    hex::encode(Sha256::digest(raw.as_bytes()))
}
fn fingerprint(cookie: &str) -> String {
    hash(&format!("videoroll-desktop-session:v1:{}", cookie.trim()))
}
fn normalize_uuid(raw: &str) -> AppResult<String> {
    Uuid::parse_str(raw.trim())
        .map(|id| id.to_string())
        .map_err(|_| AppError::Invalid("desktop resource_id must be a UUID".into()))
}
fn kind(raw: &str) -> AppResult<&'static str> {
    match raw.trim().to_lowercase().as_str() {
        "login" => Ok("login"),
        "publish" => Ok("publish"),
        _ => Err(AppError::Invalid("unsupported desktop type".into())),
    }
}
async fn cookie(state: &AppState, headers: &HeaderMap) -> AppResult<String> {
    // Service tokens never grant access to desktop resources.
    let value = auth::cookie_from_headers(headers).ok_or(AppError::Unauthorized)?;
    if !auth::request_authenticated(headers, &state.db, &state.config).await {
        return Err(AppError::Unauthorized);
    }
    Ok(value)
}
fn token() -> String {
    let a = Uuid::new_v4();
    let b = Uuid::new_v4();
    let mut bytes = [0u8; 32];
    bytes[..16].copy_from_slice(a.as_bytes());
    bytes[16..].copy_from_slice(b.as_bytes());
    URL_SAFE_NO_PAD.encode(bytes)
}
async fn create(
    State(state): State<AppState>,
    headers: HeaderMap,
    request: Request<Body>,
) -> AppResult<Json<Value>> {
    let session = cookie(&state, &headers).await?;
    let raw = to_bytes(request.into_body(), 4096)
        .await
        .map_err(|_| AppError::TooLarge("desktop grant payload too large".into()))?;
    let body: GrantRequest = serde_json::from_slice(&raw)
        .map_err(|_| AppError::Invalid("invalid desktop grant payload".into()))?;
    if !(1..=128).contains(&body.resource_id.chars().count()) {
        return Err(AppError::Invalid(
            "desktop resource_id length invalid".into(),
        ));
    }
    let desktop_type = kind(&body.desktop_type)?;
    let resource_id = normalize_uuid(&body.resource_id)?;
    let owner = fingerprint(&session);
    let expires = Utc::now() + ChronoDuration::seconds(TTL);
    let scope = json!({
        "desktop_type":desktop_type,
        "resource_id":resource_id,
        "session_fingerprint":owner,
        "reconnect_limit":RECONNECT,
        "reconnect_count":0,
    });
    for _ in 0..3 {
        let raw = token();
        let inserted = sqlx::query(
            "INSERT INTO desktop_access_grants
               (id,token_hash,subject,scope_json,status,expires_at)
             VALUES($1,$2,$3,$4,'active',$5)
             ON CONFLICT(token_hash) DO NOTHING",
        )
        .bind(Uuid::new_v4())
        .bind(hash(&raw))
        .bind(&owner)
        .bind(&scope)
        .bind(expires)
        .execute(&state.db.pool)
        .await?;
        if inserted.rows_affected() == 1 {
            return Ok(Json(json!({
                "token":raw,"desktop_type":desktop_type,
                "resource_id":resource_id,"expires_at":expires,
                "reconnect_limit":RECONNECT,
            })));
        }
    }
    Err(AppError::Unavailable(
        "unable to create desktop grant".into(),
    ))
}
struct Scope {
    desktop_type: &'static str,
    resource_id: String,
    token: String,
    consume: bool,
}
fn parse_scope(headers: &HeaderMap) -> AppResult<Scope> {
    let source = headers
        .get("x-original-uri")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .trim();
    // The only permitted routes are the static noVNC namespace and WebSocket.
    // URI normalisation must not convert traversal into an accepted path.
    if !source.starts_with('/')
        || source.starts_with("//")
        || source.contains('%')
        || source.contains('\\')
        || source.contains("/../")
        || source.ends_with("/..")
        || source.contains("/./")
        || source.ends_with("/.")
    {
        return Err(AppError::Forbidden("desktop path is not authorized".into()));
    }
    let url = Url::parse(&format!("http://internal.nginx{source}"))
        .map_err(|_| AppError::Forbidden("desktop path is not authorized".into()))?;
    let path = url.path().trim_end_matches('/');
    let desktop_type = if path.starts_with("/social-login/") {
        "login"
    } else if path.starts_with("/social-publish/") {
        "publish"
    } else {
        return Err(AppError::Forbidden("desktop path is not authorized".into()));
    };
    let pairs = url.query_pairs().collect::<Vec<_>>();
    let parameter = |name: &str, header: &str| -> String {
        pairs
            .iter()
            .find(|(k, v)| k == name && !v.is_empty())
            .map(|(_, v)| v.to_string())
            .or_else(|| {
                headers
                    .get(header)
                    .and_then(|v| v.to_str().ok())
                    .map(str::to_owned)
            })
            .unwrap_or_default()
            .trim()
            .to_owned()
    };
    let grant = parameter("grant", "x-desktop-grant");
    let resource = parameter("resource", "x-desktop-resource");
    if grant.is_empty() || resource.is_empty() {
        return Err(AppError::Forbidden("desktop grant is required".into()));
    }
    Ok(Scope {
        desktop_type,
        resource_id: normalize_uuid(&resource)
            .map_err(|_| AppError::Forbidden("desktop resource is invalid".into()))?,
        token: grant,
        consume: path.ends_with("/websockify"),
    })
}
async fn check(state: &AppState, headers: &HeaderMap) -> AppResult<()> {
    let session = cookie(state, headers).await?;
    let scope = parse_scope(headers)?;
    if scope.token.len() > 1024 {
        return Err(AppError::Forbidden(
            "desktop grant is invalid or expired".into(),
        ));
    }
    let token_hash = hash(&scope.token);
    let owner = fingerprint(&session);
    let mut tx = state.db.pool.begin().await?;
    let row = sqlx::query(
        "SELECT subject,scope_json,status,expires_at FROM desktop_access_grants
         WHERE token_hash=$1 FOR UPDATE",
    )
    .bind(&token_hash)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(row) = row else {
        return Err(AppError::Forbidden(
            "desktop grant is invalid or expired".into(),
        ));
    };
    let subject: String = row.try_get("subject")?;
    let status: String = row.try_get("status")?;
    if status != "active" || !bool::from(subject.as_bytes().ct_eq(owner.as_bytes())) {
        return Err(AppError::Forbidden(
            "desktop grant is invalid or expired".into(),
        ));
    }
    let exp: DateTime<Utc> = row.try_get("expires_at")?;
    if exp <= Utc::now() {
        sqlx::query(
            "UPDATE desktop_access_grants SET status='expired',
             last_error='expired',updated_at=now() WHERE token_hash=$1",
        )
        .bind(&token_hash)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        return Err(AppError::Forbidden(
            "desktop grant is invalid or expired".into(),
        ));
    }
    let mut data: Value = row.try_get("scope_json")?;
    if data.get("desktop_type").and_then(Value::as_str) != Some(scope.desktop_type)
        || data.get("resource_id").and_then(Value::as_str) != Some(scope.resource_id.as_str())
        || data.get("session_fingerprint").and_then(Value::as_str) != Some(owner.as_str())
    {
        return Err(AppError::Forbidden(
            "desktop grant is invalid or expired".into(),
        ));
    }
    if scope.consume {
        let limit = data
            .get("reconnect_limit")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let count = data
            .get("reconnect_count")
            .and_then(Value::as_i64)
            .unwrap_or(-1);
        if !(1..=12).contains(&limit) || count < 0 || count >= limit {
            let (status, error) = if count < 0 || !(1..=12).contains(&limit) {
                ("revoked", "invalid_scope")
            } else {
                ("consumed", "reconnect_limit_reached")
            };
            sqlx::query(
                "UPDATE desktop_access_grants SET status=$2,last_error=$3,
                 updated_at=now() WHERE token_hash=$1",
            )
            .bind(&token_hash)
            .bind(status)
            .bind(error)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
            return Err(AppError::Forbidden(
                "desktop grant is invalid or expired".into(),
            ));
        }
        data["reconnect_count"] = json!(count + 1);
        let status = if count + 1 >= limit {
            "consumed"
        } else {
            "active"
        };
        sqlx::query(
            "UPDATE desktop_access_grants SET scope_json=$2,status=$3,
             consumed_at=now(),updated_at=now() WHERE token_hash=$1",
        )
        .bind(&token_hash)
        .bind(&data)
        .bind(status)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(())
}
pub(crate) async fn authorize(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<StatusCode> {
    check(&state, &headers).await?;
    Ok(StatusCode::NO_CONTENT)
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/desktop/grants", post(create))
        .route("/desktop/authorize", get(authorize))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn scope_and_token_are_bounded_and_cookie_bound() {
        let mut h = HeaderMap::new();
        for uri in [
            "/admin/",
            "/social-login/../admin",
            "//evil/social-login/vnc.html",
            "/social-login/%2e%2e/admin",
            "https://evil/social-login/vnc.html",
        ] {
            h.insert("x-original-uri", uri.parse().unwrap());
            assert!(parse_scope(&h).is_err(), "{uri}");
        }
        h.insert(
            "x-original-uri",
            "/social-login/websockify?grant=abc&resource=00000000-0000-0000-0000-000000000001"
                .parse()
                .unwrap(),
        );
        let req = parse_scope(&h).unwrap();
        assert_eq!(req.desktop_type, "login");
        assert!(req.consume);
        assert_eq!(req.resource_id, "00000000-0000-0000-0000-000000000001");
        assert_eq!(token().len(), 43);
        assert_ne!(fingerprint("first"), fingerprint("second"));
    }
}
