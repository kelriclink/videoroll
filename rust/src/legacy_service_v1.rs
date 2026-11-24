//! Original internal service surface backed by native V1 business handlers.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Request, State},
    http::{Method, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
};
use clap::ValueEnum;
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;
use subtle::ConstantTimeEq;

#[derive(Clone, Copy, Debug, ValueEnum)]
pub enum LegacyService {
    BilibiliPublisher,
    SubtitleService,
    YoutubeIngest,
}
impl LegacyService {
    fn name(self) -> &'static str {
        match self {
            Self::BilibiliPublisher => "bilibili_publisher",
            Self::SubtitleService => "subtitle_service",
            Self::YoutubeIngest => "youtube_ingest",
        }
    }
}

#[derive(Clone)]
struct Boundary {
    token: String,
    routes: Vec<(String, String)>,
}
impl Boundary {
    fn new(service: LegacyService, secret: &str) -> AppResult<Self> {
        let secret = secret.trim();
        if secret.len() < 16
            || matches!(
                secret.to_ascii_lowercase().as_str(),
                "change-me"
                    | "changeme"
                    | "videoroll-development-internal-secret"
                    | "videoroll-development-bootstrap-secret"
            )
        {
            return Err(AppError::Invalid(
                "non-default INTERNAL_API_SECRET required".into(),
            ));
        }
        let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
            .map_err(|_| AppError::Invalid("invalid service secret".into()))?;
        mac.update(b"videoroll-internal-service:v1");
        let catalog: Value =
            serde_json::from_str(include_str!("../docs/LEGACY_ROUTE_CATALOG.json"))
                .map_err(|_| AppError::Invalid("invalid legacy route catalog".into()))?;
        let routes = catalog["routes"]
            .as_array()
            .ok_or_else(|| AppError::Invalid("missing legacy service routes".into()))?
            .iter()
            .filter(|row| row["service"] == service.name())
            .filter_map(|row| Some((row["method"].as_str()?, row["path"].as_str()?)))
            .filter(|(method, path)| *path == "/health" || crate::parity::is_adapter(method, path))
            .map(|(method, path)| (method.to_owned(), path.to_owned()))
            .collect();
        Ok(Self {
            token: format!("v1.{}", hex::encode(mac.finalize().into_bytes())),
            routes,
        })
    }
    fn permits(&self, method: &Method, path: &str) -> bool {
        self.routes
            .iter()
            .any(|(verb, pattern)| verb == method.as_str() && matches_path(pattern, path))
    }
}
fn matches_path(pattern: &str, path: &str) -> bool {
    let mut actual = path.split('/');
    for expected in pattern.split('/') {
        let Some(segment) = actual.next() else {
            return false;
        };
        if expected.starts_with('{') && expected.ends_with('}') {
            if segment.is_empty() {
                return false;
            }
        } else if segment != expected {
            return false;
        }
    }
    actual.next().is_none()
}
fn reject(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}
async fn guard(State(boundary): State<Boundary>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if path != "/health" {
        let token = request
            .headers()
            .get("x-videoroll-internal-token")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .trim();
        if token.is_empty() {
            return reject(
                StatusCode::UNAUTHORIZED,
                "internal service credentials required",
            );
        }
        if !bool::from(token.as_bytes().ct_eq(boundary.token.as_bytes())) {
            return reject(
                StatusCode::FORBIDDEN,
                "invalid internal service credentials",
            );
        }
    }
    if !boundary.permits(request.method(), path) {
        return reject(StatusCode::NOT_FOUND, "Not Found");
    }
    next.run(request).await
}
pub fn router(service: LegacyService, state: AppState) -> AppResult<Router> {
    let boundary = Boundary::new(
        service,
        state.config.legacy_internal_secret.as_deref().unwrap_or(""),
    )?;
    Ok(crate::legacy_contracts::router()
        .route("/health", get(crate::legacy_contracts::health))
        .fallback(|| async { reject(StatusCode::NOT_FOUND, "Not Found") })
        .layer(middleware::from_fn_with_state(boundary, guard))
        .with_state(state))
}
#[cfg(test)]
pub(crate) fn test_state() -> (tempfile::TempDir, AppState) {
    use crate::{Config, Database, scheduler::Scheduler, storage::AssetRegistry};
    use std::{sync::Arc, time::Duration};
    let temp = tempfile::tempdir().unwrap();
    let secret = "isolated-internal-service-secret";
    let database_url = "postgresql://test:test@127.0.0.1:1/unused".to_owned();
    let db = Database {
        pool: sqlx::PgPool::connect_lazy(&database_url).unwrap(),
    };
    let duration = Duration::from_secs(60);
    let config = Arc::new(Config {
        bind: "127.0.0.1:0".parse().unwrap(),
        database_url,
        storage_root: temp.path().into(),
        internal_token: "unused-isolated-v2-internal-token".into(),
        legacy_internal_secret: Some(secret.into()),
        bootstrap_secret: None,
        secure_cookies: true,
        worker_gateway: None,
        download_binary: "yt-dlp".into(),
        ffmpeg_binary: "ffmpeg".into(),
        ffprobe_binary: "ffprobe".into(),
        asr_endpoint: None,
        asr_api_key: None,
        asr_model: "".into(),
        translate_endpoint: None,
        translate_api_key: None,
        translate_model: None,
        youdub_python: None,
        youdub_bridge: None,
        youdub_runtime: None,
        legacy_orchestrator_url: None,
        legacy_internal_token: None,
        legacy_publish_allowed: false,
        native_bilibili_allowed: false,
        social_publisher_url: None,
        social_publish_allowed: false,
        social_storage_root: None,
        native_bilibili_cookie: None,
        native_bilibili_csrf: None,
        native_bilibili_api_base: None,
        native_bilibili_subtitles: false,
        source_scanner_enabled: false,
        remote_renderer_enabled: false,
        embedding_endpoint: None,
        embedding_model: None,
        embedding_api_key: None,
        compat_orchestrator_url: None,
        compat_internal_token: None,
        compat_writes_allowed: false,
        playout_root: None,
        worker_count: 1,
        worker_poll: duration,
        job_lease: duration,
        cleanup_grace: duration,
    });
    let state = AppState {
        config,
        db: db.clone(),
        scheduler: Scheduler::new(db.clone(), duration),
        storage: AssetRegistry::new(db, temp.path().into()),
    };
    (temp, state)
}

#[cfg(test)]
mod tests {
    use super::*;
    // Construct the actual standalone router using an unconnected pool.
    // These requests must never touch the database, external platform or disk.
    #[tokio::test]
    async fn actual_service_routers_health_and_isolation() {
        use axum::body::{Body, to_bytes};
        use tower::ServiceExt;
        let (_temp, state) = test_state();
        let secret = "isolated-internal-service-secret";
        for (service, own, other) in [
            (
                LegacyService::BilibiliPublisher,
                "/bilibili/auth/me",
                "/youtube/sources",
            ),
            (
                LegacyService::SubtitleService,
                "/subtitle/settings",
                "/bilibili/auth/me",
            ),
            (
                LegacyService::YoutubeIngest,
                "/youtube/sources",
                "/subtitle/settings",
            ),
        ] {
            let token = Boundary::new(service, secret).unwrap().token;
            let app = router(service, state.clone()).unwrap();
            for (path, auth, status) in [
                ("/health", None, StatusCode::OK),
                (own, None, StatusCode::UNAUTHORIZED),
                (own, Some("invalid"), StatusCode::FORBIDDEN),
                (other, Some(token.as_str()), StatusCode::NOT_FOUND),
                ("/tasks", Some(token.as_str()), StatusCode::NOT_FOUND),
                ("/health/extra", Some(token.as_str()), StatusCode::NOT_FOUND),
            ] {
                let mut req = Request::builder().uri(path);
                if let Some(auth) = auth {
                    req = req.header("x-videoroll-internal-token", auth);
                }
                let res = app
                    .clone()
                    .oneshot(req.body(Body::empty()).unwrap())
                    .await
                    .unwrap();
                assert_eq!(res.status(), status, "{service:?} {path}");
                if path == "/health" {
                    let bytes = to_bytes(res.into_body(), 1024).await.unwrap();
                    assert_eq!(
                        serde_json::from_slice::<Value>(&bytes).unwrap(),
                        json!({"status":"ok"})
                    );
                }
            }
        }
    }

    #[tokio::test]
    async fn health_auth_and_scope() {
        use axum::body::{Body, to_bytes};
        use tower::ServiceExt;
        let secret = "isolated-internal-service-secret";
        for service in [
            LegacyService::BilibiliPublisher,
            LegacyService::SubtitleService,
            LegacyService::YoutubeIngest,
        ] {
            let boundary = Boundary::new(service, secret).unwrap();
            assert!(boundary.permits(&Method::GET, "/health"));
            assert!(!boundary.permits(&Method::GET, "/tasks"));
            assert!(!boundary.permits(&Method::POST, "/health"));
        }
        let boundary = Boundary::new(LegacyService::YoutubeIngest, secret).unwrap();
        assert!(boundary.permits(&Method::PATCH, "/youtube/sources/abc"));
        assert!(!boundary.permits(&Method::PATCH, "/youtube/sources/abc/extra"));
        let token = boundary.token.clone();
        let app = Router::new()
            .route("/health", get(crate::legacy_contracts::health))
            .route("/youtube/sources", get(|| async { StatusCode::OK }))
            .route("/bilibili/auth/me", get(|| async { StatusCode::OK }))
            .layer(middleware::from_fn_with_state(boundary, guard));
        for (path, auth, expected) in [
            ("/health", None, StatusCode::OK),
            ("/youtube/sources", None, StatusCode::UNAUTHORIZED),
            ("/youtube/sources", Some("wrong"), StatusCode::FORBIDDEN),
            ("/youtube/sources", Some(token.as_str()), StatusCode::OK),
            (
                "/bilibili/auth/me",
                Some(token.as_str()),
                StatusCode::NOT_FOUND,
            ),
        ] {
            let mut request = Request::builder().uri(path);
            if let Some(auth) = auth {
                request = request.header("x-videoroll-internal-token", auth);
            }
            let response = app
                .clone()
                .oneshot(request.body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), expected);
            if path == "/health" {
                let body = to_bytes(response.into_body(), 1024).await.unwrap();
                assert_eq!(
                    serde_json::from_slice::<Value>(&body).unwrap(),
                    json!({"status":"ok"})
                );
            }
        }
    }
}
