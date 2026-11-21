use anyhow::{Context, Result, bail};
use std::{net::SocketAddr, path::PathBuf, time::Duration};

#[derive(Clone, Debug)]
pub struct Config {
    pub bind: SocketAddr,
    pub database_url: String,
    pub storage_root: PathBuf,
    pub internal_token: String,
    pub legacy_internal_secret: Option<String>,
    pub bootstrap_secret: Option<String>,
    pub secure_cookies: bool,
    pub worker_gateway: Option<String>,
    pub download_binary: PathBuf,
    pub worker_count: usize,
    pub worker_poll: Duration,
    pub job_lease: Duration,
    pub cleanup_grace: Duration,
}
impl Config {
    pub fn from_env() -> Result<Self> {
        let bind = std::env::var("VIDEOROLL_V2_BIND")
            .unwrap_or_else(|_| "127.0.0.1:18080".to_string())
            .parse()
            .context("VIDEOROLL_V2_BIND must be a valid socket address")?;
        let database_url = std::env::var("DATABASE_URL").context("DATABASE_URL is required")?;
        let storage_root = std::env::var_os("VIDEOROLL_V2_STORAGE_ROOT")
            .map(PathBuf::from)
            .context("VIDEOROLL_V2_STORAGE_ROOT is required")?;
        let internal_token = std::env::var("VIDEOROLL_V2_INTERNAL_TOKEN")
            .context("VIDEOROLL_V2_INTERNAL_TOKEN is required")?;
        if internal_token.len() < 32 {
            bail!("VIDEOROLL_V2_INTERNAL_TOKEN must be at least 32 bytes");
        }
        let worker_gateway = std::env::var("VIDEOROLL_V2_WORKER_GATEWAY")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let worker_count = std::env::var("VIDEOROLL_V2_WORKERS")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(2usize)
            .clamp(1, 32);
        Ok(Self {
            bind,
            database_url,
            storage_root,
            internal_token,
            legacy_internal_secret: std::env::var("INTERNAL_API_SECRET")
                .ok()
                .filter(|s| !s.is_empty()),
            bootstrap_secret: std::env::var("ADMIN_BOOTSTRAP_SECRET")
                .ok()
                .filter(|s| !s.is_empty()),
            secure_cookies: !matches!(
                std::env::var("VIDEOROLL_V2_SECURE_COOKIES").ok().as_deref(),
                Some("false" | "0")
            ),
            worker_gateway,
            download_binary: std::env::var_os("VIDEOROLL_V2_YT_DLP")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("yt-dlp")),
            worker_count,
            worker_poll: Duration::from_secs(3),
            job_lease: Duration::from_secs(180),
            cleanup_grace: Duration::from_secs(86_400),
        })
    }
}
