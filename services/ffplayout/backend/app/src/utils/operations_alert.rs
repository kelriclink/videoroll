use std::{
    env,
    fmt::Write,
    sync::LazyLock,
    time::Duration,
};

use hmac::{Hmac, Mac};
use log::warn;
use serde_json::json;
use sha2::Sha256;

const INTERNAL_TOKEN_HEADER: &str = "X-Videoroll-Internal-Token";
const SERVICE_TOKEN_CONTEXT: &[u8] = b"videoroll-internal-service:v1";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(2);
const CONNECT_TIMEOUT: Duration = Duration::from_millis(500);

static ALERT_CLIENT: LazyLock<reqwest::Client> = LazyLock::new(|| {
    let _ = rustls::crypto::ring::default_provider().install_default();
    reqwest::Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .build()
        .expect("operations alert HTTP client configuration must be valid")
});

fn service_token(secret: &str) -> Option<String> {
    let secret = secret.trim();
    if secret.is_empty() {
        return None;
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).ok()?;
    mac.update(SERVICE_TOKEN_CONTEXT);
    let digest = mac.finalize().into_bytes();
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(&mut hex, "{byte:02x}");
    }
    Some(format!("v1.{hex}"))
}

fn safe_output_target(stream_url: &str) -> String {
    let Ok(url) = reqwest::Url::parse(stream_url) else {
        return "rtmp-target".to_string();
    };
    let host = url.host_str().unwrap_or("unknown");
    match url.port() {
        Some(port) => format!("{}://{host}:{port}", url.scheme()),
        None => format!("{}://{host}", url.scheme()),
    }
}

pub fn is_rtmp_output(stream_url: &str) -> bool {
    reqwest::Url::parse(stream_url)
        .map(|url| matches!(url.scheme(), "rtmp" | "rtmps"))
        .unwrap_or(false)
}

/// Best-effort direct alert bridge from ffplayout to videoroll.
///
/// No RTMP path, query string, credentials, or stream key are included in the
/// payload. A successful reconnect resolves an existing alert but does not
/// create a synthetic "resolved" event when no failure existed. Reporting is
/// intentionally detached from the playout hot path: operations telemetry must
/// never delay opening, failing, or restarting an output.
pub fn report_rtmp_output_state(channel: i32, stream_url: &str, healthy: bool) {
    if !is_rtmp_output(stream_url) {
        return;
    }
    let stream_url = stream_url.to_string();
    tokio::spawn(async move {
        report_rtmp_output_state_inner(channel, &stream_url, healthy).await;
    });
}

async fn report_rtmp_output_state_inner(channel: i32, stream_url: &str, healthy: bool) {
    let Ok(secret) = env::var("INTERNAL_API_SECRET") else {
        return;
    };
    let Some(token) = service_token(&secret) else {
        return;
    };
    let orchestrator = env::var("ORCHESTRATOR_URL")
        .unwrap_or_else(|_| "http://orchestrator:8000".to_string());
    let endpoint = format!(
        "{}/operations/alerts/report",
        orchestrator.trim_end_matches('/')
    );
    let target = safe_output_target(stream_url);
    let body = json!({
        "fingerprint": format!("playout:rtmp:{channel}"),
        "source": "playout",
        "severity": "critical",
        "title": if healthy { "RTMP 输出已恢复" } else { "RTMP 输出断流" },
        "message": if healthy {
            format!("频道 {channel} 到 {target} 的 RTMP 输出已恢复。")
        } else {
            format!("频道 {channel} 到 {target} 的 RTMP 输出链路失败；ffplayout 将按现有监督逻辑重启/重试。")
        },
        "details": {
            "channel": channel,
            "target": target,
            "protocol": "rtmp"
        },
        "resolved": healthy
    });

    let result = ALERT_CLIENT
        .post(endpoint)
        .header(INTERNAL_TOKEN_HEADER, token)
        .json(&body)
        .send()
        .await
        .and_then(reqwest::Response::error_for_status);

    if let Err(error) = result {
        warn!(
            target: "operations_alert",
            "Failed to report RTMP output state for channel {channel}: {error}"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::{is_rtmp_output, safe_output_target, service_token};

    #[test]
    fn derives_the_same_internal_service_token_as_videoroll() {
        assert_eq!(
            service_token("test-secret").as_deref(),
            Some("v1.dffdef2654f9a14ca67e1c143f3a3eeea201acad7cc638b129f7436a59142c04")
        );
    }

    #[test]
    fn strips_stream_keys_and_paths_from_alert_targets() {
        assert_eq!(
            safe_output_target("rtmps://live.example.com:443/app/SECRET?token=also-secret"),
            "rtmps://live.example.com:443"
        );
    }

    #[test]
    fn only_rtmp_schemes_are_reportable() {
        assert!(is_rtmp_output("rtmp://example.com/live/key"));
        assert!(is_rtmp_output("rtmps://example.com/live/key"));
        assert!(!is_rtmp_output("srt://example.com:9000"));
        assert!(!is_rtmp_output("https://example.com/live"));
    }
}
