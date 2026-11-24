//! Rust SAU original-service account API subset. Not a replacement for the
//! browser login/publish worker until the remaining endpoints are verified.
use std::{net::SocketAddr, sync::Arc};
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();
    let secret = std::env::var("INTERNAL_API_SECRET")?;
    let db = std::env::var("DATABASE_URL")?;
    let pool = sqlx::PgPool::connect(&db).await?;
    let state = Arc::new(videoroll_v2::sau_account_native_v1::SauState::new(
        pool, &secret,
    )?);
    let bind: SocketAddr = std::env::var("VIDEOROLL_V2_SAU_BIND")
        .unwrap_or_else(|_| "127.0.0.1:18087".into())
        .parse()?;
    if !bind.ip().is_loopback()
        && std::env::var("VIDEOROLL_V2_SAU_ALLOW_CONTAINER_BIND")
            .ok()
            .as_deref()
            != Some("true")
    {
        anyhow::bail!("SAU must bind loopback unless container binding explicitly enabled")
    }
    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!(address=%bind,"native Rust SAU account subsystem ready");
    axum::serve(listener, videoroll_v2::sau_account_native_v1::router(state)).await?;
    Ok(())
}
