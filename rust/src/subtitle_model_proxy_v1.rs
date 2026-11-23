//! Original /subtitle/models/proxy/test API.
//! A real HTTP HEAD/body-free GET through the selected HTTP(S)/SOCKS proxy.
//! The Python contract does not follow redirects and reports errors in a
//! successful JSON response rather than returning fabricated connectivity.
use crate::{AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::post};
use reqwest::{Client, Proxy, Url, redirect::Policy};
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::{Duration, Instant};

#[derive(Debug, Deserialize)]
struct Request {
    proxy: Option<String>,
    url: Option<String>,
}
fn host_allowed(input: &str) -> bool {
    Url::parse(input).ok().is_some_and(|parsed| {
        matches!(parsed.scheme(), "http" | "https")
            && parsed.username().is_empty()
            && parsed.password().is_none()
            && parsed.host_str().is_some_and(|hostname| {
                let host = hostname.trim_end_matches('.').to_lowercase();
                host == "huggingface.co" || host.ends_with(".huggingface.co")
            })
    })
}
fn error_view(url: &str, proxy: Option<&str>, message: &str, elapsed: u128) -> Value {
    json!({"ok":false,"url":url,"used_proxy":proxy,
         "status_code":null,"elapsed_ms":elapsed.min(u64::MAX as u128) as u64,
         "error":message})
}
async fn test_proxy(
    State(state): State<AppState>,
    Json(request): Json<Request>,
) -> AppResult<Json<Value>> {
    let url = request.url.as_deref().unwrap_or("").trim();
    let url = if url.is_empty() {
        "https://huggingface.co/robots.txt"
    } else {
        url
    };
    let selected = request.proxy.as_deref().unwrap_or("").trim();
    if !host_allowed(url) {
        return Ok(Json(error_view(
            url,
            (!selected.is_empty()).then_some(selected),
            "model proxy test URL must be an HTTP(S) huggingface.co URL",
            0,
        )));
    }
    let proxy = if let Some(value) = request.proxy {
        value.trim().to_owned()
    } else {
        let raw: Option<Value> =
            sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.asr'")
                .fetch_optional(&state.db.pool)
                .await?;
        crate::subtitle_asr_settings_v1::view(&raw.unwrap_or(Value::Null))
            .get("model_download_proxy")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned()
    };
    let used_proxy = (!proxy.is_empty()).then_some(proxy.as_str());
    let now = Instant::now();
    let mut builder = Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(20))
        .redirect(Policy::none());
    if !proxy.is_empty() {
        let proxy_client = match Proxy::all(&proxy) {
            Ok(p) => p,
            Err(e) => {
                return Ok(Json(error_view(
                    url,
                    used_proxy,
                    &e.to_string(),
                    now.elapsed().as_millis(),
                )));
            }
        };
        builder = builder.proxy(proxy_client);
    }
    let client = match builder.build() {
        Ok(client) => client,
        Err(e) => {
            return Ok(Json(error_view(
                url,
                used_proxy,
                &e.to_string(),
                now.elapsed().as_millis(),
            )));
        }
    };
    let response = client.get(url).send().await;
    match response {
        Ok(response) => Ok(Json(json!({
            "ok":response.status().as_u16()<400,
            "url":url,"used_proxy":used_proxy,
            "status_code":response.status().as_u16(),
            "elapsed_ms":now.elapsed().as_millis().min(u64::MAX as u128) as u64,
            "error":null
        }))),
        Err(e) => Ok(Json(error_view(
            url,
            used_proxy,
            &e.to_string(),
            now.elapsed().as_millis(),
        ))),
    }
}
pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/models/proxy/test", post(test_proxy))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn model_proxy_url_allowlist() {
        assert!(host_allowed("https://huggingface.co/robots.txt"));
        assert!(host_allowed("http://datasets.huggingface.co/a"));
        assert!(!host_allowed("http://127.0.0.1/private"));
        assert!(!host_allowed(
            "https://huggingface.co.attacker.invalid/file"
        ));
        assert!(!host_allowed("https://foo@huggingface.co/robots.txt"));
        assert!(!host_allowed("file:///etc/passwd"));
    }
}
