//! Original V1 social-account and browser-login contract, implemented by the
//! Rust orchestrator against the separately deployed SAU browser sidecar.
//! SAU continues owning encrypted storage_state, browser logins, and account
//! validation; never copy credentials into the control-plane database.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Multipart, Path, Query, State},
    routing::get,
};
use hmac::{Hmac, Mac};
use reqwest::{Client, Method, Url};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::Sha256;
use std::time::Duration;
use uuid::Uuid;

fn social_platform(value: &str) -> AppResult<&str> {
    match value {
        "douyin" | "xiaohongshu" | "kuaishou" => Ok(value),
        _ => Err(AppError::Invalid("unsupported SAU platform".into())),
    }
}

fn trusted_sidecar(raw: &str) -> AppResult<Url> {
    let mut url =
        Url::parse(raw).map_err(|_| AppError::Invalid("invalid SAU sidecar endpoint".into()))?;
    let loopback = matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
    if !(url.scheme() == "https" || (url.scheme() == "http" && loopback))
        || url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(AppError::Invalid(
            "SAU sidecar must be an HTTPS origin or a local HTTP origin".into(),
        ));
    }
    url.set_path("/");
    Ok(url)
}

fn service_token(secret: &str) -> AppResult<String> {
    if secret.len() < 16 {
        return Err(AppError::Unavailable(
            "SAU requires a configured non-default internal service secret".into(),
        ));
    }
    let mut hmac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .map_err(|_| AppError::Unavailable("invalid SAU internal secret".into()))?;
    hmac.update(b"videoroll-internal-service:v1");
    Ok(format!("v1.{}", hex::encode(hmac.finalize().into_bytes())))
}

async fn call(
    state: &AppState,
    method: Method,
    path: &str,
    query: Option<&[(&str, &str)]>,
    body: Option<Value>,
    multipart: Option<reqwest::multipart::Form>,
) -> AppResult<Value> {
    let base = state
        .config
        .social_publisher_url
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("SAU sidecar not configured in V2".into()))?;
    let base = trusted_sidecar(base)?;
    let secret = state
        .config
        .legacy_internal_secret
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("SAU service secret unavailable".into()))?;
    let url = base
        .join(path.trim_start_matches('/'))
        .map_err(|_| AppError::Invalid("invalid SAU resource path".into()))?;
    let client = Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let mut request = client
        .request(method, url)
        .header("x-videoroll-internal-token", service_token(secret)?);
    if let Some(query) = query {
        request = request.query(query);
    }
    if let Some(body) = body {
        request = request.json(&body);
    }
    if let Some(form) = multipart {
        request = request.multipart(form);
    }
    let response = request.send().await?;
    let status = response.status();
    let raw = response.bytes().await?;
    if raw.len() > 256 * 1024 {
        return Err(AppError::Unavailable(
            "SAU sidecar response exceeds size limit".into(),
        ));
    }
    let parsed: Value = serde_json::from_slice(&raw)
        .map_err(|_| AppError::Unavailable("SAU returned invalid JSON".into()))?;
    if !status.is_success() {
        let detail = parsed
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or("SAU rejected the request")
            .chars()
            .take(256)
            .collect::<String>();
        return Err(match status.as_u16() {
            400 | 422 => AppError::Invalid(detail),
            401 | 403 => AppError::Unauthorized,
            404 => AppError::NotFound(detail),
            409 => AppError::Conflict(detail),
            _ => AppError::Unavailable("SAU sidecar request failed".into()),
        });
    }
    Ok(parsed)
}

#[derive(Debug, Deserialize)]
struct PlatformFilter {
    platform: Option<String>,
}
async fn accounts(
    State(state): State<AppState>,
    Query(filter): Query<PlatformFilter>,
) -> AppResult<Json<Value>> {
    let query = match filter.platform.as_deref() {
        Some(platform) => Some(vec![("platform", social_platform(platform)?)]),
        None => None,
    };
    Ok(Json(
        call(
            &state,
            Method::GET,
            "accounts",
            query.as_deref(),
            None,
            None,
        )
        .await?,
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LoginRequest {
    account_name: String,
}
fn account_name(name: &str) -> AppResult<&str> {
    let name = name.trim();
    if name.is_empty() || name.chars().count() > 100 || name.chars().any(char::is_control) {
        return Err(AppError::Invalid("invalid SAU account name".into()));
    }
    Ok(name)
}
async fn login(
    State(state): State<AppState>,
    Path(platform): Path<String>,
    Json(body): Json<LoginRequest>,
) -> AppResult<Json<Value>> {
    let platform = social_platform(&platform)?;
    let name = account_name(&body.account_name)?;
    Ok(Json(
        call(
            &state,
            Method::POST,
            &format!("login-sessions/{platform}"),
            None,
            Some(json!({"account_name":name})),
            None,
        )
        .await?,
    ))
}
async fn session(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    Ok(Json(
        call(
            &state,
            Method::GET,
            &format!("login-sessions/{id}"),
            None,
            None,
            None,
        )
        .await?,
    ))
}
async fn cancel_session(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        call(
            &state,
            Method::DELETE,
            &format!("login-sessions/{id}"),
            None,
            None,
            None,
        )
        .await?,
    ))
}
async fn import_account(
    State(state): State<AppState>,
    Path(platform): Path<String>,
    mut form: Multipart,
) -> AppResult<Json<Value>> {
    let platform = social_platform(&platform)?;
    let mut name = None;
    let mut storage_state = None;
    while let Some(field) = form
        .next_field()
        .await
        .map_err(|_| AppError::Invalid("malformed SAU multipart form".into()))?
    {
        match field.name().unwrap_or("") {
            "account_name" if name.is_none() => {
                let text = field
                    .text()
                    .await
                    .map_err(|_| AppError::Invalid("SAU account name is not UTF-8".into()))?;
                name = Some(account_name(&text)?.to_owned());
            }
            "file" if storage_state.is_none() => {
                let bytes = field
                    .bytes()
                    .await
                    .map_err(|_| AppError::Invalid("invalid SAU storage_state".into()))?;
                if bytes.len() > 1024 * 1024 {
                    return Err(AppError::Invalid("SAU storage_state exceeds 1 MiB".into()));
                }
                let value: Value = serde_json::from_slice(&bytes)
                    .map_err(|_| AppError::Invalid("SAU storage_state is not JSON".into()))?;
                if !value.is_object() {
                    return Err(AppError::Invalid(
                        "SAU storage_state must be JSON object".into(),
                    ));
                }
                storage_state = Some(bytes);
            }
            _ => return Err(AppError::Invalid("unexpected SAU multipart field".into())),
        }
    }
    let name = name.ok_or_else(|| AppError::Invalid("SAU account_name missing".into()))?;
    let storage_state =
        storage_state.ok_or_else(|| AppError::Invalid("SAU storage_state file missing".into()))?;
    let part = reqwest::multipart::Part::bytes(storage_state.to_vec())
        .file_name("storage_state.json")
        .mime_str("application/json")
        .map_err(|_| AppError::Invalid("invalid SAU media type".into()))?;
    let form = reqwest::multipart::Form::new()
        .text("account_name", name)
        .part("file", part);
    Ok(Json(
        call(
            &state,
            Method::POST,
            &format!("accounts/{platform}"),
            None,
            None,
            Some(form),
        )
        .await?,
    ))
}
async fn check_account(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        call(
            &state,
            Method::POST,
            &format!("accounts/{id}/check"),
            None,
            None,
            None,
        )
        .await?,
    ))
}
async fn delete_account(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        call(
            &state,
            Method::DELETE,
            &format!("accounts/{id}"),
            None,
            None,
            None,
        )
        .await?,
    ))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/publish/social/accounts", get(accounts))
        .route(
            "/settings/publish/social/accounts/{handle}",
            axum::routing::post(import_account).delete(delete_account),
        )
        .route(
            "/settings/publish/social/accounts/{id}/check",
            axum::routing::post(check_account),
        )
        .route(
            "/settings/publish/social/login-sessions/{handle}",
            get(session).post(login).delete(cancel_session),
        )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn social_proxy_rejects_untrusted_hosts_and_platforms() {
        assert!(trusted_sidecar("https://sau.internal.example").is_ok());
        assert!(trusted_sidecar("http://127.0.0.1:8010").is_ok());
        assert!(trusted_sidecar("http://192.168.1.2:8010").is_err());
        assert!(trusted_sidecar("http://evil.example").is_err());
        assert!(trusted_sidecar("https://user:password@evil.example").is_err());
        assert!(trusted_sidecar("https://sau.internal.example/base").is_err());
        assert!(social_platform("douyin").is_ok());
        assert!(social_platform("kuaishou").is_ok());
        assert!(social_platform("bilibilievil").is_err());
    }
    #[test]
    fn native_service_auth_matches_original_service_token() {
        let token = service_token("development_secret_at_least_16").unwrap();
        assert!(token.starts_with("v1."));
        assert_eq!(token.len(), 67);
    }
    #[test]
    fn reject_bad_account_names() {
        assert!(account_name("  ").is_err());
        assert!(account_name("hello\nworld").is_err());
        assert_eq!(account_name(" Hello ").unwrap(), "Hello");
    }
}
