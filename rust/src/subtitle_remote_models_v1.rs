//! Original Python /subtitle/translate/model-providers/{id}/remote-models
//! read-only model discovery with stricter network-origin controls.
//!
//! Unlike Python's unrestricted httpx fetch, all resolved destinations must
//! be public, HTTPS, and pinned to vetted DNS answers. Loopback HTTP is
//! only available in explicitly opted-in *isolated tests*.
use crate::{AppError, AppResult, api::AppState, providers};
use axum::{
    Json,
    extract::{Path, State},
};
use futures_util::StreamExt;
use reqwest::{Client, Url, redirect::Policy};
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    time::Duration,
};

const MAX_JSON: u64 = 2_097_152;
fn bad(s: &str) -> AppError {
    AppError::Invalid(s.into())
}
fn test_loopback() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_TEST_MODEL_CATALOG_LOOPBACK")
            .ok()
            .as_deref(),
        Some("1" | "true")
    )
}
fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let o = ip.octets();
            !ip.is_private()
                && !ip.is_loopback()
                && !ip.is_link_local()
                && !ip.is_multicast()
                && !ip.is_broadcast()
                && !ip.is_unspecified()
                && !ip.is_documentation()
                && o[0] != 0
                && o[0] < 224
                && !(o[0] == 100 && (64..=127).contains(&o[1]))
                && !(o[0] == 192 && o[1] == 0 && o[2] == 0)
                && !(o[0] == 192 && o[1] == 88 && o[2] == 99)
                && !(o[0] == 198 && (o[1] == 18 || o[1] == 19))
        }
        IpAddr::V6(ip) => {
            let seg = ip.segments();
            (seg[0] & 0xe000) == 0x2000
                && !(seg[0] == 0x2001 && seg[1] == 0x0db8)
                && !ip.is_loopback()
                && !ip.is_multicast()
                && !ip.is_unspecified()
        }
    }
}
fn allowed_ip(ip: IpAddr) -> bool {
    public_ip(ip) || (test_loopback() && ip.is_loopback())
}
fn remote_endpoint(base: &str) -> AppResult<Url> {
    if base.len() > 2048 || base.trim().is_empty() {
        return Err(bad("remote provider URL invalid"));
    }
    let base = base.trim_end_matches('/');
    let raw = if base.ends_with("/v1") {
        format!("{base}/models")
    } else {
        format!("{base}/v1/models")
    };
    let parsed = Url::parse(&raw).map_err(|_| bad("remote model URL invalid"))?;
    if !matches!(parsed.scheme(), "https" | "http")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.fragment().is_some()
        || parsed.query().is_some()
    {
        return Err(bad(
            "remote model URL must not contain credentials, query or fragment",
        ));
    }
    if parsed.scheme() == "http" {
        let host = parsed.host_str().unwrap_or("");
        let loopback = host
            .parse::<IpAddr>()
            .ok()
            .is_some_and(|ip| ip.is_loopback());
        if !test_loopback() || !loopback {
            return Err(bad("remote model catalog requires HTTPS public origin"));
        }
    }
    Ok(parsed)
}
async fn pinned_client(url: &Url) -> AppResult<Client> {
    let host = url
        .host_str()
        .ok_or_else(|| bad("remote model host required"))?;
    let port = url
        .port_or_known_default()
        .ok_or_else(|| bad("remote model port invalid"))?;
    let mut resolved = Vec::<SocketAddr>::new();
    if let Ok(ip) = host.parse::<IpAddr>() {
        if !allowed_ip(ip) {
            return Err(bad("remote model address is not permitted"));
        }
        resolved.push(SocketAddr::new(ip, port));
    } else {
        let answers = tokio::net::lookup_host((host, port))
            .await
            .map_err(|_| AppError::Unavailable("remote model DNS resolution failed".into()))?;
        for addr in answers {
            if !allowed_ip(addr.ip()) {
                return Err(bad("remote model DNS resolves to a restricted IP"));
            }
            if !resolved.contains(&addr) {
                resolved.push(addr);
            }
            if resolved.len() > 16 {
                return Err(bad("remote model DNS returned too many addresses"));
            }
        }
        if resolved.is_empty() {
            return Err(bad("remote model DNS returned no addresses"));
        }
    }
    Client::builder()
        .no_proxy()
        .redirect(Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(15))
        .resolve_to_addrs(host, &resolved)
        .build()
        .map_err(|_| AppError::Unavailable("remote model client creation failed".into()))
}
fn limit(value: Option<&Value>) -> Option<u64> {
    match value? {
        Value::Number(n) => n
            .as_u64()
            .filter(|n| (1..=9_007_199_254_740_991).contains(n)),
        Value::String(s)
            if s.is_ascii()
                && s.bytes().all(|b| b.is_ascii_digit())
                && s.len() <= 16
                && !s.is_empty() =>
        {
            s.parse::<u64>()
                .ok()
                .filter(|n| (1..=9_007_199_254_740_991).contains(n))
        }
        _ => None,
    }
}
fn first_positive(values: &[Option<&Value>]) -> Option<u64> {
    values.iter().find_map(|v| limit(*v))
}
fn model_details(item: &Value) -> Value {
    let meta = item
        .get("top_provider")
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null);
    let ctx = first_positive(&[
        meta.get("context_length"),
        item.get("context_length"),
        item.get("context_window"),
        item.get("max_model_len"),
    ]);
    let mut out = first_positive(&[
        meta.get("max_completion_tokens"),
        item.get("max_completion_tokens"),
        item.get("max_output_tokens"),
    ]);
    if ctx.is_some() && out.is_some() && out >= ctx {
        out = None;
    }
    json!({"context_window":ctx,"max_output_tokens":out})
}
fn parse_models(json: &Value) -> AppResult<Value> {
    let rows = json
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| bad("remote model API did not return OpenAI /models format"))?;
    let mut discovered = BTreeMap::<String, Value>::new();
    for item in rows.iter().take(500) {
        let Some(id) = item.get("id").and_then(Value::as_str) else {
            continue;
        };
        if id.is_empty() || id.chars().count() > 256 {
            continue;
        }
        discovered
            .entry(id.to_string())
            .or_insert_with(|| model_details(item));
    }
    let mut names = Vec::new();
    let mut details = Map::new();
    for (name, detail) in discovered.into_iter().take(200) {
        if !detail["context_window"].is_null() || !detail["max_output_tokens"].is_null() {
            details.insert(name.clone(), detail);
        }
        names.push(name);
    }
    Ok(json!({"models":names,"details":details}))
}
pub(crate) async fn remote_models(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> AppResult<Json<Value>> {
    if id.is_empty() || id.len() > 128 {
        return Err(bad("remote provider ID invalid"));
    }
    let rows = providers::read_catalog(&state.db).await?;
    let entry = rows
        .iter()
        .find(|v| v.get("id").and_then(Value::as_str) == Some(id.as_str()))
        .ok_or_else(|| bad("save the provider before fetching remote models"))?;
    let base = entry
        .get("base_url")
        .and_then(Value::as_str)
        .ok_or_else(|| bad("remote provider URL missing"))?;
    let url = remote_endpoint(base)?;
    let client = pinned_client(&url).await?;
    let token = entry
        .get("api_key_enc")
        .and_then(Value::as_str)
        .unwrap_or("");
    if token.starts_with("gAAAA") {
        return Err(AppError::Unavailable(
            "legacy encrypted model provider credential requires migration".into(),
        ));
    }
    let token = token.strip_prefix("dbplain:v1:").unwrap_or(token);
    if token.contains('\n') || token.contains('\r') || token.len() > 8192 {
        return Err(bad("stored provider token invalid"));
    }
    let mut request = client.get(url);
    if !token.is_empty() {
        request = request.bearer_auth(token);
    }
    let response = request
        .send()
        .await
        .map_err(|_| AppError::Unavailable("remote model catalog request failed".into()))?;
    if !response.status().is_success() {
        return Err(AppError::Unavailable(
            "remote model catalog HTTP error".into(),
        ));
    }
    if response.content_length().is_some_and(|n| n > MAX_JSON) {
        return Err(bad("remote model catalog exceeds response budget"));
    }
    let mut stream = response.bytes_stream();
    let mut bytes = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk =
            chunk.map_err(|_| AppError::Unavailable("remote model body read failed".into()))?;
        if bytes.len() as u64 + chunk.len() as u64 > MAX_JSON {
            return Err(bad("remote model catalog exceeds response budget"));
        }
        bytes.extend_from_slice(&chunk);
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Unavailable("remote model catalog JSON invalid".into()))?;
    Ok(Json(parse_models(&value)?))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejection_and_model_metadata() {
        assert!(remote_endpoint("http://169.254.169.254/latest").is_err());
        assert!(!public_ip("10.0.0.2".parse().unwrap()));
        assert!(!public_ip("127.0.0.1".parse().unwrap()));
        assert!(!public_ip("2001:db8::2".parse().unwrap()));
        assert!(public_ip("8.8.8.8".parse().unwrap()));
        let value = json!({"data":[
            {"id":"z","top_provider":{"context_length":1000,"max_completion_tokens":200}},
            {"id":"a","context_window":1000,"max_output_tokens":1000},
            {"id":"a","context_length":500},
            {"id":"b","max_output_tokens":"800"}
        ]});
        let result = parse_models(&value).unwrap();
        assert_eq!(result["models"], json!(["a", "b", "z"]));
        assert_eq!(result["details"]["a"]["max_output_tokens"], Value::Null);
        assert_eq!(result["details"]["z"]["context_window"], 1000);
        assert_eq!(result["details"]["b"]["max_output_tokens"], 800);
    }
}
