//! Temporary legacy API compatibility boundary. It is explicitly opt-in,
//! read-only by default, and must NOT be presented as Rust implementation of
//! the original 217+ endpoints. Migrate each route before removing this bridge.
use crate::{AppError, AppResult, Config, api::AppState};
use axum::{
    body::Body,
    extract::{Request, State},
    http::{HeaderName, HeaderValue, Method, Response, StatusCode},
};
use futures_util::StreamExt;
use reqwest::{Client, Url};
use std::time::Duration;

const MAX_FORWARD_BODY_BYTES: u64 = 100 * 1024 * 1024 * 1024;
fn trusted_url(config: &Config) -> AppResult<Option<Url>> {
    let Some(raw) = config.compat_orchestrator_url.as_deref() else {
        return Ok(None);
    };
    let url = Url::parse(raw)
        .map_err(|_| AppError::Invalid("legacy compatibility address is not a valid URL".into()))?;
    let local =
        url.scheme() == "http" && matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
    if !local
        || url.username() != ""
        || url.password().is_some()
        || url.port_or_known_default().is_none()
    {
        return Err(AppError::Invalid(
            "legacy API compatibility only accepts explicit loopback HTTP".into(),
        ));
    }
    Ok(Some(url))
}
fn allowed_header(name: &HeaderName) -> bool {
    !matches!(
        name.as_str(),
        "host"
            | "connection"
            | "proxy-connection"
            | "proxy-authorization"
            | "proxy-authenticate"
            | "keep-alive"
            | "transfer-encoding"
            | "te"
            | "trailer"
            | "upgrade"
            | "content-length"
            | "x-videoroll-internal-token"
            | "x-videoroll-v2-internal-token"
    )
}
pub async fn fallback(
    State(state): State<AppState>,
    request: Request,
) -> AppResult<Response<Body>> {
    let url = trusted_url(&state.config)?
        .ok_or_else(|| AppError::NotFound("legacy API compatibility is disabled".into()))?;
    let path = request.uri().path();
    if !path.starts_with("/api/")
        || path.starts_with("/api/v2/")
        || path.starts_with("/api/v1/")
        || path == "/api/v1"
    {
        return Err(AppError::NotFound("unsupported route".into()));
    }
    let method = request.method().clone();
    if !matches!(method, Method::GET | Method::HEAD | Method::OPTIONS)
        && !state.config.compat_writes_allowed
    {
        return Err(AppError::Conflict(
            "legacy API is mounted read-only to avoid double-executing side effects".into(),
        ));
    }
    let headers = request.headers();
    let presented = headers
        .get("x-videoroll-v2-internal-token")
        .or_else(|| headers.get("authorization"))
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let presented = presented.strip_prefix("Bearer ").unwrap_or(presented);
    use subtle::ConstantTimeEq;
    let privileged = bool::from(
        state
            .config
            .internal_token
            .as_bytes()
            .ct_eq(presented.as_bytes()),
    );
    let browser = crate::auth::request_authenticated(headers, &state.db, &state.config).await;
    if !privileged && !browser {
        return Err(AppError::Unauthorized);
    }
    let mut dest = url;
    dest.set_path(path);
    dest.set_query(request.uri().query());
    let method = reqwest::Method::from_bytes(method.as_str().as_bytes())
        .map_err(|_| AppError::Invalid("invalid downstream HTTP method".into()))?;
    let client = Client::builder()
        .timeout(Duration::from_secs(3600))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let mut target = client.request(method, dest);
    for (name, value) in headers {
        if allowed_header(name) {
            target = target.header(name, value);
        }
    }
    // Never forward V2 client token as a V1 privileged secret. A separate,
    // explicitly configured V1-to-V2 trust key is required for that.
    if let Some(secret) = state.config.compat_internal_token.as_ref() {
        target = target.header("x-videoroll-internal-token", secret);
    }
    if headers
        .get(axum::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .is_some_and(|size| size > MAX_FORWARD_BODY_BYTES)
    {
        return Err(AppError::Invalid(
            "compat forwarded upload exceeds maximum size".into(),
        ));
    }
    let stream = request.into_body().into_data_stream();
    let mut transferred = 0u64;
    let bounded = stream.map(move |chunk| match chunk {
        Ok(bytes) => {
            transferred = transferred.saturating_add(bytes.len() as u64);
            if transferred > MAX_FORWARD_BODY_BYTES {
                Err(std::io::Error::other("legacy streaming body exceeds limit"))
            } else {
                Ok(bytes)
            }
        }
        Err(error) => Err(std::io::Error::other(error)),
    });
    let result = target
        .body(reqwest::Body::wrap_stream(bounded))
        .send()
        .await?;
    // No automatic redirects. Browser sees original Location and handles it
    // through the known proxy if it is an internal relative location.
    let status = StatusCode::from_u16(result.status().as_u16())
        .map_err(|_| AppError::Unavailable("unknown upstream HTTP status".into()))?;
    let response_headers = result.headers().clone();
    let mut response = Response::new(Body::from_stream(result.bytes_stream()));
    *response.status_mut() = status;
    for (name, value) in response_headers.iter() {
        if allowed_header(name) {
            response.headers_mut().append(name, value.clone());
        }
    }
    response.headers_mut().insert(
        "x-videoroll-compatibility",
        HeaderValue::from_static("legacy-readonly-proxy"),
    );
    Ok(response)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn does_not_forward_connection_or_privilege_hop_headers() {
        for name in [
            "host",
            "connection",
            "transfer-encoding",
            "content-length",
            "x-videoroll-internal-token",
            "x-videoroll-v2-internal-token",
        ] {
            assert!(!allowed_header(&name.parse::<HeaderName>().unwrap()));
        }
        assert!(allowed_header(&"cookie".parse().unwrap()));
        assert!(allowed_header(&"range".parse().unwrap()));
    }
}
