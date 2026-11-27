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
    pub ffmpeg_binary: PathBuf,
    pub ffprobe_binary: PathBuf,
    pub asr_endpoint: Option<String>,
    pub asr_api_key: Option<String>,
    pub asr_model: String,
    pub local_asr: Option<serde_json::Value>,
    pub asr_request_options: Option<serde_json::Value>,
    pub cloudflare_asr: bool,
    pub translate_endpoint: Option<String>,
    pub translate_api_key: Option<String>,
    pub translate_model: Option<String>,
    pub youdub_python: Option<PathBuf>,
    pub youdub_bridge: Option<PathBuf>,
    pub youdub_runtime: Option<PathBuf>,
    pub legacy_orchestrator_url: Option<String>,
    pub legacy_internal_token: Option<String>,
    pub legacy_publish_allowed: bool,
    pub native_bilibili_allowed: bool,
    pub social_publisher_url: Option<String>,
    pub social_publish_allowed: bool,
    pub social_storage_root: Option<PathBuf>,
    pub native_bilibili_cookie: Option<String>,
    pub native_bilibili_csrf: Option<String>,
    pub native_bilibili_api_base: Option<String>,
    pub native_bilibili_subtitles: bool,
    pub source_scanner_enabled: bool,
    pub remote_renderer_enabled: bool,
    pub embedding_endpoint: Option<String>,
    pub embedding_model: Option<String>,
    pub embedding_api_key: Option<String>,
    pub compat_orchestrator_url: Option<String>,
    pub compat_internal_token: Option<String>,
    pub compat_writes_allowed: bool,
    pub playout_root: Option<std::path::PathBuf>,
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
            ffmpeg_binary: std::env::var_os("VIDEOROLL_V2_FFMPEG")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("ffmpeg")),
            ffprobe_binary: std::env::var_os("VIDEOROLL_V2_FFPROBE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("ffprobe")),
            asr_endpoint: std::env::var("VIDEOROLL_V2_ASR_ENDPOINT")
                .ok()
                .filter(|s| !s.is_empty()),
            asr_api_key: std::env::var("VIDEOROLL_V2_ASR_API_KEY")
                .ok()
                .filter(|s| !s.is_empty()),
            local_asr: None,
            asr_request_options: None,
            cloudflare_asr: false,
            asr_model: std::env::var("VIDEOROLL_V2_ASR_MODEL")
                .unwrap_or_else(|_| "whisper-large-v3-turbo".into()),
            translate_endpoint: std::env::var("VIDEOROLL_V2_TRANSLATE_ENDPOINT")
                .ok()
                .filter(|s| !s.is_empty()),
            translate_api_key: std::env::var("VIDEOROLL_V2_TRANSLATE_API_KEY")
                .ok()
                .filter(|s| !s.is_empty()),
            translate_model: std::env::var("VIDEOROLL_V2_TRANSLATE_MODEL")
                .ok()
                .filter(|s| !s.is_empty()),
            youdub_python: std::env::var_os("VIDEOROLL_V2_YOUDUB_PYTHON").map(PathBuf::from),
            youdub_bridge: std::env::var_os("VIDEOROLL_V2_YOUDUB_BRIDGE").map(PathBuf::from),
            youdub_runtime: std::env::var_os("VIDEOROLL_V2_YOUDUB_RUNTIME").map(PathBuf::from),
            legacy_orchestrator_url: std::env::var("VIDEOROLL_V2_LEGACY_ORCHESTRATOR_URL")
                .ok()
                .filter(|s| !s.is_empty()),
            legacy_internal_token: std::env::var("VIDEOROLL_V2_LEGACY_INTERNAL_TOKEN")
                .ok()
                .filter(|s| !s.is_empty()),
            legacy_publish_allowed: std::env::var("VIDEOROLL_V2_ENABLE_LEGACY_PUBLISH")
                .ok()
                .as_deref()
                == Some("true"),
            social_publisher_url: std::env::var("VIDEOROLL_V2_SOCIAL_PUBLISHER_URL")
                .ok()
                .filter(|v| !v.trim().is_empty()),
            social_publish_allowed: std::env::var("VIDEOROLL_V2_ENABLE_SAU_PUBLISH")
                .ok()
                .as_deref()
                == Some("true"),
            social_storage_root: std::env::var_os("VIDEOROLL_V2_SAU_STORAGE_ROOT")
                .map(PathBuf::from),
            native_bilibili_allowed: std::env::var("VIDEOROLL_V2_ENABLE_NATIVE_BILIBILI")
                .ok()
                .as_deref()
                == Some("true"),
            native_bilibili_cookie: std::env::var("VIDEOROLL_V2_BILIBILI_COOKIE")
                .ok()
                .filter(|s| !s.is_empty()),
            native_bilibili_csrf: std::env::var("VIDEOROLL_V2_BILIBILI_CSRF")
                .ok()
                .filter(|s| !s.is_empty()),
            native_bilibili_api_base: std::env::var("VIDEOROLL_V2_BILIBILI_API_BASE")
                .ok()
                .filter(|s| !s.is_empty()),
            native_bilibili_subtitles: !matches!(
                std::env::var("VIDEOROLL_V2_BILIBILI_SUBTITLES")
                    .ok()
                    .as_deref(),
                Some("false" | "0")
            ),
            source_scanner_enabled: std::env::var("VIDEOROLL_V2_SOURCE_SCANNER").ok().as_deref()
                == Some("true"),
            remote_renderer_enabled: std::env::var("VIDEOROLL_V2_REMOTE_RENDER").ok().as_deref()
                == Some("true"),
            embedding_endpoint: std::env::var("VIDEOROLL_V2_EMBEDDING_ENDPOINT")
                .ok()
                .filter(|v| !v.is_empty()),
            embedding_model: std::env::var("VIDEOROLL_V2_EMBEDDING_MODEL")
                .ok()
                .filter(|v| !v.is_empty()),
            embedding_api_key: std::env::var("VIDEOROLL_V2_EMBEDDING_KEY")
                .ok()
                .filter(|v| !v.is_empty()),
            compat_orchestrator_url: std::env::var("VIDEOROLL_V2_COMPAT_URL")
                .ok()
                .filter(|v| !v.is_empty()),
            compat_internal_token: std::env::var("VIDEOROLL_V2_COMPAT_INTERNAL_TOKEN")
                .ok()
                .filter(|v| !v.is_empty()),
            compat_writes_allowed: std::env::var("VIDEOROLL_V2_COMPAT_WRITE").ok().as_deref()
                == Some("true"),
            playout_root: std::env::var_os("VIDEOROLL_V2_PLAYOUT_ROOT")
                .map(std::path::PathBuf::from),
            worker_count,
            worker_poll: Duration::from_secs(3),
            job_lease: Duration::from_secs(180),
            // Preserve the Python failed-task retention configuration.
            cleanup_grace: Duration::from_secs(
                std::env::var("FAILED_TASK_RESOURCE_RETENTION_HOURS")
                    .ok().and_then(|v| v.parse::<i64>().ok())
                    .unwrap_or(48).clamp(1, 3650 * 24) as u64 * 3600,
            ),
        })
    }
}
