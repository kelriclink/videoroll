//! Dedicated internal-only egress gateway; no database or administrator API.
use std::net::SocketAddr;
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();
    let secret = std::env::var("INTERNAL_API_SECRET")?;
    let state = videoroll_v2::egress_gateway_v1::EgressState::from_secret(&secret)
        .map_err(anyhow::Error::msg)?;
    let bind: SocketAddr = std::env::var("VIDEOROLL_V2_EGRESS_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8020".into())
        .parse()?;
    // The original Egress Gateway was internal. Do not accidentally expose
    // the bearer-gated public-web fetcher on a WAN address.
    let container_bind = std::env::var("VIDEOROLL_V2_EGRESS_ALLOW_CONTAINER_BIND")
        .ok()
        .as_deref()
        == Some("true");
    if !bind.ip().is_loopback() && !container_bind {
        anyhow::bail!(
            "VIDEOROLL_V2_EGRESS_BIND must be loopback unless container bind is explicitly allowed"
        );
    }
    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!(address=%bind,"standalone Rust Egress Gateway ready");
    axum::serve(listener, videoroll_v2::egress_gateway_v1::router(state)).await?;
    Ok(())
}
