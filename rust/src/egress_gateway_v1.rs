//! Standalone Rust replacement for Python videoroll.apps.egress_gateway.main.
//! Never expose a generic downloader on the administrator/API port.
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::Request,
    http::{HeaderMap, HeaderName, HeaderValue, StatusCode},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use hmac::{Hmac, Mac};
use reqwest::{Client, Url, redirect::Policy};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::Sha256;
use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    str::FromStr,
    time::Duration,
};
use subtle::ConstantTimeEq;

const AUTH_HEADER: &str = "x-videoroll-internal-token";
const CONTEXT: &[u8] = b"videoroll-internal-service:v1";
const MAX_TIMEOUT: f64 = 60.0;
const MAX_BYTES: usize = 2_000_000;
const MAX_REDIRECTS: u8 = 5;
const MAX_HEADERS: usize = 16;

#[derive(Clone)]
pub struct EgressState {
    expected: String,
}
impl EgressState {
    pub fn from_secret(secret: &str) -> Result<Self, &'static str> {
        let trimmed = secret.trim();
        if trimmed.is_empty()
            || matches!(
                trimmed.to_ascii_lowercase().as_str(),
                "change-me"
                    | "changeme"
                    | "videoroll-development-internal-secret"
                    | "videoroll-development-bootstrap-secret"
            )
        {
            return Err("INTERNAL_API_SECRET must be a non-default service secret");
        }
        let mut mac = Hmac::<Sha256>::new_from_slice(trimmed.as_bytes())
            .map_err(|_| "INTERNAL_API_SECRET cannot be hashed")?;
        mac.update(CONTEXT);
        Ok(Self {
            expected: format!("v1.{}", hex::encode(mac.finalize().into_bytes())),
        })
    }
    fn check(&self, headers: &HeaderMap) -> Result<(), (StatusCode, &'static str)> {
        let Some(raw) = headers.get(AUTH_HEADER) else {
            return Err((
                StatusCode::UNAUTHORIZED,
                "internal service credentials required",
            ));
        };
        let presented = raw.to_str().unwrap_or("").trim();
        if !bool::from(presented.as_bytes().ct_eq(self.expected.as_bytes())) {
            return Err((
                StatusCode::FORBIDDEN,
                "invalid internal service credentials",
            ));
        }
        Ok(())
    }
}
#[derive(Deserialize)]
struct FetchRequest {
    url: String,
    #[serde(default = "timeout_default")]
    timeout: f64,
    #[serde(default = "limit_default")]
    max_bytes: usize,
    #[serde(default = "redirect_default")]
    redirects: u8,
    #[serde(default)]
    headers: BTreeMap<String, String>,
}
fn timeout_default() -> f64 {
    20.0
}
fn limit_default() -> usize {
    500_000
}
fn redirect_default() -> u8 {
    5
}
#[derive(Serialize)]
struct FetchResponse {
    status_code: u16,
    headers: BTreeMap<String, String>,
    body_base64: String,
    url: String,
    truncated: bool,
}
type Failure = (StatusCode, &'static str);
fn denied(reason: &'static str) -> Failure {
    (StatusCode::FORBIDDEN, reason)
}
fn failed() -> Failure {
    (StatusCode::BAD_GATEWAY, "egress fetch failed")
}

/// Conservative subset of Python ipaddress.ip_address(...).is_global:
/// reserved/unknown ranges are rejected, never guessed to be public.
fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => {
            let o = v.octets();
            let reserved = [
                "0.0.0.0/8",
                "10.0.0.0/8",
                "100.64.0.0/10",
                "127.0.0.0/8",
                "169.254.0.0/16",
                "172.16.0.0/12",
                "192.0.0.0/24",
                "192.0.2.0/24",
                "192.88.99.0/24",
                "192.168.0.0/16",
                "198.18.0.0/15",
                "198.51.100.0/24",
                "203.0.113.0/24",
                "224.0.0.0/4",
                "240.0.0.0/4",
            ];
            !v.is_private()
                && !v.is_loopback()
                && !v.is_link_local()
                && !v.is_multicast()
                && !v.is_unspecified()
                && !v.is_broadcast()
                && !v.is_documentation()
                && o[0] != 255
                && !reserved
                    .iter()
                    .any(|cidr| ipnet::Ipv4Net::from_str(cidr).is_ok_and(|net| net.contains(&v)))
        }
        IpAddr::V6(v) => {
            if let Some(v4) = v.to_ipv4_mapped() {
                return public_ip(IpAddr::V4(v4));
            }
            // Non-2000::/3 addresses and transition technologies are always
            // denied, including NAT64, 6to4 and link-local/ULA addresses.
            let octets = v.octets();
            if octets[0] & 0xe0 != 0x20 {
                return false;
            }
            let reserved = [
                "2001::/32",
                "2001:db8::/32",
                "2001:10::/28",
                "2001:20::/28",
                "2001:2::/48",
                "2002::/16",
            ];
            !v.is_loopback()
                && !v.is_unspecified()
                && !v.is_multicast()
                && !v.is_unique_local()
                && !v.is_unicast_link_local()
                && !reserved
                    .iter()
                    .any(|cidr| ipnet::Ipv6Net::from_str(cidr).is_ok_and(|net| net.contains(&v)))
        }
    }
}
#[cfg(feature = "egress-test-loopback")]
fn isolated_loopback(ip: IpAddr, port: u16) -> bool {
    std::env::var("VIDEOROLL_V2_TEST_EGRESS_LOOPBACK")
        .ok()
        .as_deref()
        == Some("true")
        && std::env::var("VIDEOROLL_V2_TEST_EGRESS_DB").ok().as_deref() == Some("disposable")
        && ip == IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)
        && port == 18098
}
#[cfg(not(feature = "egress-test-loopback"))]
fn isolated_loopback(_ip: IpAddr, _port: u16) -> bool {
    false
}

fn target(url: &str) -> Result<(Url, String, u16), Failure> {
    if url.is_empty() || url.len() > 4096 {
        return Err(denied("egress URL length is invalid"));
    }
    let parsed = Url::parse(url).map_err(|_| denied("egress URL is invalid"))?;
    let port = match parsed.scheme() {
        "http" => 80,
        "https" => 443,
        _ => return Err(denied("egress URL scheme must be HTTP or HTTPS")),
    };
    if !parsed.username().is_empty() || parsed.password().is_some() || parsed.fragment().is_some() {
        return Err(denied("egress URL userinfo or fragment is not allowed"));
    }
    let host = parsed
        .host_str()
        .filter(|h| !h.is_empty())
        .ok_or_else(|| denied("egress URL hostname is required"))?;
    if host.contains('%') {
        return Err(denied("egress URL has an invalid hostname"));
    }
    let host = host.to_owned();
    let explicit_port = parsed.port();
    if explicit_port.is_some_and(|p| p != port) {
        #[cfg(feature = "egress-test-loopback")]
        {
            if explicit_port != Some(18098)
                || host != "127.0.0.1"
                || std::env::var("VIDEOROLL_V2_TEST_EGRESS_LOOPBACK")
                    .ok()
                    .as_deref()
                    != Some("true")
                || std::env::var("VIDEOROLL_V2_TEST_EGRESS_DB").ok().as_deref()
                    != Some("disposable")
            {
                return Err(denied("egress URL port is not allowed"));
            }
        }
        #[cfg(not(feature = "egress-test-loopback"))]
        return Err(denied("egress URL port is not allowed"));
    }
    let port = explicit_port.unwrap_or(port);
    Ok((parsed, host, port))
}
async fn endpoint(host: &str, port: u16) -> Result<IpAddr, Failure> {
    let mut addresses = tokio::net::lookup_host((host, port))
        .await
        .map_err(|_| denied("egress DNS resolution failed"))?
        .map(|x| x.ip())
        .collect::<Vec<_>>();
    addresses.sort_unstable();
    addresses.dedup();
    if addresses.is_empty() {
        return Err(denied("egress DNS resolution returned no addresses"));
    }
    // Mixed public/private DNS is not acceptable: don't select public and
    // silently ignore a rebind attempt containing private answer(s).
    if addresses
        .iter()
        .any(|ip| !public_ip(*ip) && !isolated_loopback(*ip, port))
    {
        return Err(denied(
            "egress DNS resolution returned a non-global address",
        ));
    }
    Ok(addresses[0])
}
fn request_headers(raw: &BTreeMap<String, String>) -> Result<HeaderMap, Failure> {
    let mut headers = HeaderMap::new();
    headers.insert(
        "accept",
        HeaderValue::from_static("application/json, text/html;q=0.9, text/plain;q=0.8, */*;q=0.1"),
    );
    headers.insert(
        "user-agent",
        HeaderValue::from_static("VideoRoll-Egress-Gateway/1.0"),
    );
    let forbidden = [
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "connection",
        "transfer-encoding",
        "te",
        "upgrade",
        "content-length",
        "x-forwarded-for",
        "x-real-ip",
    ];
    for (name, value) in raw.iter().take(MAX_HEADERS) {
        let name = name.trim().to_ascii_lowercase();
        let value = value.trim();
        if name.is_empty() || value.is_empty() || forbidden.contains(&name.as_str()) {
            continue;
        }
        if name.contains(['\r', '\n']) || value.contains(['\r', '\n']) {
            return Err(denied("egress request contains an invalid header"));
        }
        let Ok(key) = HeaderName::from_bytes(name.as_bytes()) else {
            return Err(denied("egress request contains an invalid header"));
        };
        let value = HeaderValue::from_str(&value.chars().take(1024).collect::<String>())
            .map_err(|_| denied("egress request contains an invalid header"))?;
        headers.insert(key, value);
    }
    Ok(headers)
}
fn content_type_ok(headers: &reqwest::header::HeaderMap) -> bool {
    let mime = headers
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|h| h.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    mime.starts_with("text/")
        || mime == "application/json"
        || mime.ends_with("+json")
        || mime == "application/xml"
        || mime == "application/xhtml+xml"
        || mime.ends_with("+xml")
}
fn redirect_status(n: u16) -> bool {
    matches!(n, 301 | 302 | 303 | 307 | 308)
}
async fn fetch(req: FetchRequest) -> Result<FetchResponse, Failure> {
    if !req.timeout.is_finite()
        || !(0.1..=MAX_TIMEOUT).contains(&req.timeout)
        || !(1..=MAX_BYTES).contains(&req.max_bytes)
        || req.redirects > MAX_REDIRECTS
    {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "egress request limits are invalid",
        ));
    }
    let outbound = request_headers(&req.headers)?;
    let mut current = req.url;
    for hop in 0..=req.redirects {
        let (url, host, port) = target(&current)?;
        let ip = endpoint(&host, port).await?;
        // DNS resolution selects the address but the HTTP dial is *pinned*
        // to that verified IP. URL host/SNI/certificate checks stay original.
        // Proxy variables are disabled; they could bypass destination checks.
        let client = Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .timeout(Duration::from_secs_f64(req.timeout))
            .pool_max_idle_per_host(0)
            .resolve(&host, SocketAddr::new(ip, port))
            .build()
            .map_err(|_| failed())?;
        let mut response = client
            .get(url.clone())
            .headers(outbound.clone())
            .send()
            .await
            .map_err(|_| failed())?;
        if let Some(remote) = response.remote_addr() {
            if remote.ip() != ip {
                return Err(denied("egress socket peer does not match verified IP"));
            }
        }
        let code = response.status().as_u16();
        let headers = response
            .headers()
            .iter()
            .filter_map(|(name, value)| {
                Some((name.as_str().to_owned(), value.to_str().ok()?.to_owned()))
            })
            .collect::<BTreeMap<_, _>>();
        if redirect_status(code) {
            let Some(location) = response
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|v| v.to_str().ok())
            else {
                return Ok(FetchResponse {
                    status_code: code,
                    headers,
                    body_base64: String::new(),
                    url: current,
                    truncated: false,
                });
            };
            if hop >= req.redirects {
                return Err(denied("egress redirect limit exceeded"));
            }
            let next = url
                .join(location)
                .map_err(|_| denied("egress redirect URL invalid"))?;
            current = next.into();
            continue;
        }
        if !content_type_ok(response.headers()) {
            return Err(denied("egress response content type is not allowed"));
        }
        let mut bytes = Vec::with_capacity(req.max_bytes.min(8192));
        let mut truncated = false;
        while let Some(chunk) = response.chunk().await.map_err(|_| failed())? {
            let remaining = req.max_bytes.saturating_sub(bytes.len());
            if chunk.len() > remaining {
                bytes.extend_from_slice(&chunk[..remaining]);
                truncated = true;
                break;
            }
            bytes.extend_from_slice(&chunk);
        }
        return Ok(FetchResponse {
            status_code: code,
            headers,
            body_base64: STANDARD.encode(bytes),
            url: current,
            truncated,
        });
    }
    Err(denied("egress redirect limit exceeded"))
}
// Internal research adapter. Reuse the same destination validation and pinned
// transport as /fetch, but never expose arbitrary headers or redirects.
pub(crate) async fn research_plain_text(raw: &str) -> crate::AppResult<Option<String>> {
    let Ok((url, _, _)) = target(raw) else {
        return Ok(None);
    };
    if url.query().is_some() || (url.scheme() != "https" && !research_test_url(&url)) {
        return Ok(None);
    }
    let req = FetchRequest {
        url: url.into(),
        timeout: 12.0,
        max_bytes: 500_000,
        redirects: 0,
        headers: BTreeMap::from([("accept".into(), "text/plain".into())]),
    };
    let response = match tokio::time::timeout(Duration::from_secs(12), fetch(req)).await {
        Ok(Ok(v)) => v,
        Ok(Err((StatusCode::FORBIDDEN, _))) => return Ok(None),
        _ => {
            return Err(crate::AppError::Unavailable(
                "research page fetch unavailable".into(),
            ));
        }
    };
    if response.status_code >= 500 || response.status_code == 429 {
        return Err(crate::AppError::Unavailable(
            "research page temporarily unavailable".into(),
        ));
    }
    if !(200..300).contains(&response.status_code) || response.truncated {
        return Ok(None);
    }
    let mime = response
        .headers
        .get("content-type")
        .map(String::as_str)
        .unwrap_or("");
    if !mime
        .split(';')
        .next()
        .is_some_and(|s| s.trim().eq_ignore_ascii_case("text/plain"))
    {
        return Ok(None);
    }
    let bytes = STANDARD
        .decode(response.body_base64)
        .map_err(|_| crate::AppError::Unavailable("research page response invalid".into()))?;
    Ok(clean_research_text(&bytes))
}
fn research_test_url(url: &Url) -> bool {
    url.host_str() == Some("127.0.0.1")
        && url
            .port()
            .is_some_and(|port| isolated_loopback(IpAddr::V4(std::net::Ipv4Addr::LOCALHOST), port))
}
fn clean_research_text(bytes: &[u8]) -> Option<String> {
    let raw = std::str::from_utf8(bytes).ok()?;
    if raw
        .chars()
        .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
    {
        return None;
    }
    let collapsed = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    let text: String = collapsed.chars().take(5000).collect();
    (!text.is_empty()).then_some(text)
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({"status":"ok"}))
}

async fn fetch_http(
    axum::extract::State(state): axum::extract::State<EgressState>,
    req: Request<Body>,
) -> Result<Json<FetchResponse>, (StatusCode, Json<serde_json::Value>)> {
    let err = |error: (StatusCode, &'static str)| (error.0, Json(json!({"detail":error.1})));
    state.check(req.headers()).map_err(err)?;
    let body = to_bytes(req.into_body(), 32768).await.map_err(|_| {
        err((
            StatusCode::PAYLOAD_TOO_LARGE,
            "egress request body is too large",
        ))
    })?;
    let payload: FetchRequest = serde_json::from_slice(&body).map_err(|_| {
        err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid egress request JSON",
        ))
    })?;
    if payload.url.is_empty() || payload.url.len() > 4096 {
        return Err(err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid egress request URL length",
        )));
    }
    let seconds = payload.timeout;
    let result = tokio::time::timeout(
        Duration::from_secs_f64(seconds.min(60.0).max(0.1)),
        fetch(payload),
    )
    .await
    .map_err(|_| err(failed()))?
    .map_err(err)?;
    Ok(Json(result))
}
pub fn router(state: EgressState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/fetch", post(fetch_http))
        .with_state(state)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn research_text_validation() {
        assert_eq!(
            clean_research_text(b"one\n\t two").as_deref(),
            Some("one two")
        );
        assert!(clean_research_text(b"\0binary").is_none());
        assert!(clean_research_text(&[255, 254]).is_none());
        assert!(clean_research_text(b"  ").is_none());
        assert_eq!(
            clean_research_text("汉".repeat(6000).as_bytes())
                .unwrap()
                .chars()
                .count(),
            5000
        );
    }
    #[tokio::test]
    async fn research_rejects_nonpublic_and_invalid_urls() {
        for raw in [
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://169.254.169.254/",
            "http://example.invalid/",
            "https://example.invalid/?token=x",
            "https://example.invalid:444/",
            "https://u:p@example.invalid/",
        ] {
            assert!(research_plain_text(raw).await.unwrap().is_none(), "{raw}");
        }
    }
    #[cfg(feature = "egress-test-loopback")]
    #[tokio::test]
    async fn research_plain_text_loopback_fixture() {
        assert!(isolated_loopback(
            IpAddr::V4(std::net::Ipv4Addr::LOCALHOST),
            18098
        ));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:18098")
            .await
            .unwrap();
        let app = Router::new()
            .route(
                "/plain",
                get(|| async {
                    (
                        [(
                            axum::http::header::CONTENT_TYPE,
                            "text/plain; charset=utf-8",
                        )],
                        "Fixture\nplain text",
                    )
                }),
            )
            .route(
                "/html",
                get(|| async {
                    (
                        [(axum::http::header::CONTENT_TYPE, "text/html")],
                        "<html>ignored</html>",
                    )
                }),
            )
            .route(
                "/redirect",
                get(|| async { axum::response::Redirect::temporary("/plain") }),
            )
            .route(
                "/large",
                get(|| async {
                    (
                        [(axum::http::header::CONTENT_TYPE, "text/plain")],
                        "x".repeat(500001),
                    )
                }),
            );
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let base = "http://127.0.0.1:18098";
        assert_eq!(
            research_plain_text(&format!("{base}/plain"))
                .await
                .unwrap()
                .as_deref(),
            Some("Fixture plain text")
        );
        for path in ["html", "redirect", "large"] {
            assert!(
                research_plain_text(&format!("{base}/{path}"))
                    .await
                    .unwrap()
                    .is_none(),
                "{path}"
            );
        }
        server.abort();
    }
    #[test]
    fn private_ipv4_ipv6_and_embedded_ip_rejected() {
        for raw in [
            "127.0.0.1",
            "10.2.3.4",
            "100.64.0.1",
            "169.254.169.254",
            "192.168.1.1",
            "172.31.2.5",
            "192.0.2.2",
            "198.18.0.1",
            "203.0.113.2",
            "224.0.0.5",
            "255.255.255.255",
            "::1",
            "fe80::1",
            "fd00::1",
            "2001:db8::1",
            "::ffff:127.0.0.1",
        ] {
            assert!(!public_ip(raw.parse().unwrap()), "{raw}");
        }
        for raw in [
            "8.8.8.8",
            "1.1.1.1",
            "2606:4700:4700::1111",
            "2001:4860:4860::8888",
        ] {
            assert!(public_ip(raw.parse().unwrap()), "{raw}");
        }
    }
    #[test]
    fn url_schemes_ports_credentials_and_fragments() {
        for raw in [
            "file:///etc/passwd",
            "http://127.0.0.1:12345",
            "http://alice:secret@example.com/",
            "https://example.com:444/a",
            "http://example.com/#frag",
            "gopher://example.com/",
        ] {
            assert!(target(raw).is_err(), "{raw}");
        }
        assert!(target("https://example.com/path?q=1").is_ok());
        assert!(target("http://8.8.8.8/").is_ok());
    }
    #[test]
    fn forbidden_headers_stripped_and_newlines_rejected() {
        let headers = BTreeMap::from([
            ("Cookie".into(), "session=abc".into()),
            ("Authorization".into(), "secret".into()),
            ("Accept-Language".into(), "en-US".into()),
        ]);
        let out = request_headers(&headers).unwrap();
        assert!(!out.contains_key("cookie"));
        assert!(!out.contains_key("authorization"));
        assert_eq!(out.get("accept-language").unwrap(), "en-US");
        assert!(
            request_headers(&BTreeMap::from([("X-Test".into(), "one\r\ntwo".into())])).is_err()
        );
    }
    #[test]
    fn python_service_token_derivation_is_repeatable() {
        let x = EgressState::from_secret("isolated-safe-secret-12345").unwrap();
        assert!(x.expected.starts_with("v1."));
        assert_eq!(x.expected.len(), 67);
        assert!(EgressState::from_secret("videoroll-development-internal-secret").is_err());
        let mut h = HeaderMap::new();
        assert_eq!(x.check(&h).unwrap_err().0, StatusCode::UNAUTHORIZED);
        h.insert(AUTH_HEADER, HeaderValue::from_str(&x.expected).unwrap());
        assert!(x.check(&h).is_ok());
    }
}
