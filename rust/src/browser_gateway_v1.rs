//! Legacy browser gateway. The Python orchestrator allowed only selected
//! service paths. Dispatch those requests directly to the native service
//! router, with explicit compatibility extensions for existing Rust V1 APIs.
use crate::api::AppState;
use axum::{
    Extension, Json, Router,
    extract::Request,
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::any,
};
use serde_json::json;
use tower::ServiceExt;
use uuid::Uuid;

#[derive(Clone)]
struct Native(Router);

fn valid_provider(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

// Decode once, segment-by-segment. Reject encoded separators, dot segments,
// control characters and malformed UTF-8 rather than let routing disagree.
fn decode_path(raw: &str) -> Option<String> {
    let mut segments = Vec::new();
    for segment in raw.split('/') {
        let mut bytes = Vec::new();
        let mut chars = segment.bytes();
        while let Some(c) = chars.next() {
            if c == b'%' {
                let a = (chars.next()? as char).to_digit(16)?;
                let b = (chars.next()? as char).to_digit(16)?;
                bytes.push((a * 16 + b) as u8);
            } else {
                bytes.push(c);
            }
        }
        let decoded = String::from_utf8(bytes).ok()?;
        if decoded == "."
            || decoded == ".."
            || decoded.contains(['/', '\\', '%'])
            || decoded.chars().any(char::is_control)
        {
            return None;
        }
        segments.push(decoded);
    }
    Some(segments.join("/"))
}

fn legacy_allowed(method: &Method, path: &str) -> bool {
    use Method as M;
    let pieces: Vec<_> = path.trim_start_matches('/').split('/').collect();
    match (method, pieces.as_slice()) {
        (
            &M::GET,
            ["bilibili", "archive", "types"]
            | ["bilibili", "publish", "settings"]
            | ["bilibili", "auth", "settings" | "me"],
        ) => true,
        (&M::POST, ["bilibili", "archive", "type", "recommend"]) => true,
        (&M::PUT, ["bilibili", "publish" | "auth", "settings"]) => true,
        (&M::GET | &M::POST, ["youtube", "sources"]) => true,
        (&M::POST, ["youtube", "ingest"]) => true,
        (&M::PATCH | &M::DELETE, ["youtube", "sources", id]) => !id.is_empty(),
        (
            &M::POST,
            ["youtube", "sources", id, "scan"] | ["youtube", "sources", id, "scan", "start"],
        ) => !id.is_empty(),
        (
            &M::GET,
            ["subtitle", "settings" | "models" | "task_queue"]
            | ["subtitle", "asr" | "translate", "settings"]
            | ["subtitle", "auto", "profile"]
            | ["subtitle", "translate", "model-providers"]
            | ["subtitle", "hardware", "intel"]
            | ["subtitle", "embedding", "runtime"]
            | ["subtitle", "agents", "runs"]
            | ["subtitle", "agent", "skills"]
            | [
                "subtitle",
                "dictionaries",
                "sources" | "entries" | "import-presets",
            ]
            | ["subtitle", "knowledge", "items"]
            | ["subtitle", "task_queue", "broker" | "settings"],
        ) => true,
        (
            &M::POST,
            ["subtitle", "translate", "model-providers" | "test"]
            | ["subtitle", "models", "download" | "upload"]
            | ["subtitle", "models", "proxy", "test"]
            | [
                "subtitle",
                "asr",
                "external" | "groq" | "cloudflare",
                "test",
            ]
            | ["subtitle", "embedding", "models", "list" | "download"]
            | ["subtitle", "embedding", "test"]
            | ["subtitle", "knowledge", "items" | "rebuild-embeddings"]
            | ["subtitle", "dictionaries", "import" | "lookup" | "promote"],
        ) => true,
        (
            &M::PUT,
            ["subtitle", "asr" | "translate" | "task_queue", "settings"]
            | ["subtitle", "auto", "profile"],
        ) => true,
        (
            &M::GET,
            [
                "subtitle",
                "translate",
                "model-providers",
                id,
                "remote-models",
            ],
        )
        | (&M::PUT | &M::DELETE, ["subtitle", "translate", "model-providers", id]) => {
            valid_provider(id)
        }
        (&M::GET, ["subtitle", "agents", "runs", id]) => Uuid::parse_str(id).is_ok(),
        (&M::GET, ["subtitle", "tasks", id, "quality" | "translation-context"])
        | (&M::PUT, ["subtitle", "tasks", id, "translation-context"]) => {
            Uuid::parse_str(id).is_ok()
        }
        (
            &M::PUT | &M::DELETE,
            ["subtitle", "models", name]
            | ["subtitle", "dictionaries", "sources" | "entries", name]
            | ["subtitle", "knowledge", "items", name],
        ) => !name.is_empty(),
        _ => false,
    }
}

// These concrete native APIs were introduced in earlier migration batches.
// Keep them available, but never treat the original wildcard as unrestricted.
fn native_extension(method: &Method, path: &str) -> bool {
    use Method as M;
    let pieces: Vec<_> = path.trim_start_matches('/').split('/').collect();
    match (method, pieces.as_slice()) {
        (&M::GET, ["bilibili", "discovery" | "accounts"])
        | (&M::POST, ["bilibili", "publish" | "accounts"])
        | (&M::POST, ["youtube", "scan"])
        | (&M::GET, ["subtitle", "embedding", "models"])
        | (&M::POST, ["subtitle", "jobs"] | ["subtitle", "knowledge", "import"]) => true,
        (&M::GET, ["bilibili", "publish", "jobs", id])
        | (&M::GET, ["subtitle", "jobs", id])
        | (&M::DELETE, ["bilibili", "accounts", id])
        | (&M::POST, ["bilibili", "accounts", id, "check"]) => !id.is_empty(),
        (&M::DELETE, ["subtitle", "embedding", "models", name]) => !name.is_empty(),
        _ => false,
    }
}

fn refusal(path: &str) -> Response {
    let detail = if path.starts_with("/subtitle/") {
        "subtitle browser operation not found"
    } else if path.starts_with("/youtube/") {
        "youtube browser operation not found"
    } else {
        "bilibili browser operation not found"
    };
    (StatusCode::NOT_FOUND, Json(json!({"detail":detail}))).into_response()
}

async fn dispatch(Extension(native): Extension<Native>, request: Request) -> Response {
    let Some(path) = decode_path(request.uri().path()) else {
        return refusal(request.uri().path());
    };
    if !legacy_allowed(request.method(), &path) && !native_extension(request.method(), &path) {
        return refusal(&path);
    }
    // Outer wildcard capture and MatchedPath cannot leak into the inner
    // concrete router: Axum would otherwise combine their path parameters.
    // Keep the original method, URI/query, headers and unconsumed body.
    let (mut parts, body) = request.into_parts();
    parts.extensions.clear();
    let response = native
        .0
        .oneshot(Request::from_parts(parts, body))
        .await
        .expect("Axum Router has an infallible service error");
    if response.status() == StatusCode::METHOD_NOT_ALLOWED {
        let allow = response.headers().get(header::ALLOW).cloned();
        let mut error = (
            StatusCode::METHOD_NOT_ALLOWED,
            Json(json!({"detail":"Method Not Allowed"})),
        )
            .into_response();
        if let Some(allow) = allow {
            error.headers_mut().insert(header::ALLOW, allow);
        }
        return error;
    }
    response
}

fn routes(native: Router) -> Router<AppState> {
    Router::new()
        .route("/bilibili/{*service_path}", any(dispatch))
        .route("/subtitle/{*service_path}", any(dispatch))
        .route("/youtube/{*service_path}", any(dispatch))
        .layer(Extension(Native(native)))
}
pub fn router(state: AppState) -> Router<AppState> {
    routes(crate::legacy_contracts::service_router().with_state(state))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{Body, to_bytes},
        extract::{Path, Query},
        routing::{get, post},
    };
    use std::collections::HashMap;

    #[test]
    fn legacy_allowlist_and_path_fences() {
        for (method, path) in [
            (Method::GET, "/bilibili/auth/me"),
            (Method::POST, "/bilibili/archive/type/recommend"),
            (Method::PUT, "/bilibili/publish/settings"),
            (Method::GET, "/subtitle/task_queue/settings"),
            (Method::POST, "/subtitle/models/upload"),
            (Method::PUT, "/subtitle/translate/model-providers/openai_1"),
            (Method::DELETE, "/subtitle/models/model"),
            (Method::GET, "/youtube/sources"),
            (Method::POST, "/youtube/sources/1/scan/start"),
            (Method::PATCH, "/youtube/sources/1"),
            (Method::DELETE, "/youtube/sources/1"),
        ] {
            assert!(legacy_allowed(&method, path), "{method} {path}");
        }
        for (method, path) in [
            (Method::POST, "/bilibili/auth/me"),
            (Method::GET, "/subtitle/agents/runs/not-a-uuid"),
            (Method::PUT, "/subtitle/translate/model-providers/a.b"),
            (Method::DELETE, "/subtitle/translate/model-providers/a/b"),
            (Method::POST, "/youtube/sources/1/scan/extra"),
            (Method::GET, "/youtube/private"),
        ] {
            assert!(!legacy_allowed(&method, path), "{method} {path}");
            assert!(!native_extension(&method, path));
        }
        for path in [
            "/subtitle/models/%2Fetc",
            "/youtube/sources/%2e%2e",
            "/subtitle/models/%5cetc",
            "/subtitle/models/%00",
            "/subtitle/models/%",
            "/subtitle/models/%ff",
            "/subtitle/models/%252f",
        ] {
            assert!(decode_path(path).is_none(), "{path}");
        }
        assert_eq!(
            decode_path("/subtitle/models/%E6%A8%A1%E5%9E%8B").as_deref(),
            Some("/subtitle/models/模型")
        );
    }

    #[tokio::test]
    async fn actual_orchestrator_protects_all_gateway_methods_and_dispatches_native_routes() {
        let (_temp, state) = crate::legacy_service_v1::test_state();
        let token = state.config.internal_token.clone();
        let app = crate::api::router(state);
        for (prefix, methods) in [
            ("bilibili", vec![Method::GET, Method::POST, Method::PUT]),
            (
                "subtitle",
                vec![Method::GET, Method::POST, Method::PUT, Method::DELETE],
            ),
            (
                "youtube",
                vec![Method::GET, Method::POST, Method::PATCH, Method::DELETE],
            ),
        ] {
            for method in methods {
                let path = format!("/api/v1/{prefix}/not-an-allowed-operation");
                for (authorized, expected) in [
                    (false, StatusCode::UNAUTHORIZED),
                    (true, StatusCode::NOT_FOUND),
                ] {
                    let mut req = Request::builder().method(method.clone()).uri(&path);
                    if authorized {
                        req = req.header("x-videoroll-v2-internal-token", &token);
                    }
                    let response = app
                        .clone()
                        .oneshot(req.body(Body::empty()).unwrap())
                        .await
                        .unwrap();
                    assert_eq!(response.status(), expected, "{method} {path}");
                    if authorized {
                        let value: serde_json::Value = serde_json::from_slice(
                            &to_bytes(response.into_body(), 1024).await.unwrap(),
                        )
                        .unwrap();
                        assert_eq!(
                            value["detail"],
                            format!("{prefix} browser operation not found")
                        );
                    }
                }
            }
        }
        for (method, path, body, status) in [
            (
                Method::GET,
                "/api/v1/subtitle/hardware/intel",
                "",
                StatusCode::OK,
            ),
            (
                Method::GET,
                "/api/v1/subtitle/task_queue/settings",
                "",
                StatusCode::METHOD_NOT_ALLOWED,
            ),
            (
                Method::POST,
                "/api/v1/bilibili/archive/type/recommend",
                "{",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::PUT,
                "/api/v1/bilibili/auth/settings",
                "{",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::POST,
                "/api/v1/subtitle/translate/test",
                "{",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::POST,
                "/api/v1/subtitle/jobs",
                "{",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::DELETE,
                "/api/v1/subtitle/models/invalid%20name",
                "",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::PATCH,
                "/api/v1/youtube/sources/not-a-uuid",
                "{}",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::DELETE,
                "/api/v1/youtube/sources/not-a-uuid",
                "",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::POST,
                "/api/v1/youtube/ingest",
                "{",
                StatusCode::BAD_REQUEST,
            ),
            (
                Method::DELETE,
                "/api/v1/subtitle/models/%2Fetc",
                "",
                StatusCode::NOT_FOUND,
            ),
        ] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method.clone())
                        .uri(path)
                        .header("x-videoroll-v2-internal-token", &token)
                        .header("content-type", "application/json")
                        .body(Body::from(body))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), status, "{method} {path}");
        }
    }

    #[tokio::test]
    async fn native_dispatch_preserves_body_query_and_concrete_path_parameters() {
        let native = Router::new()
            .route(
                "/youtube/sources/{id}/scan",
                post(
                    |Path(id): Path<String>,
                     Query(query): Query<HashMap<String, String>>,
                     body: String| async move {
                        Json(json!({"id":id,"query":query,"body":body}))
                    },
                ),
            )
            .route(
                "/subtitle/task_queue/settings",
                post(|| async { StatusCode::OK }),
            )
            .route(
                "/bilibili/auth/me",
                get(|| async { Json(json!({"ok":true})) }),
            );
        // No AppState is needed for the fake native router; erase the gateway
        // state via an independently provided test fixture in the actual-router test.
        let gateway = routes(native).with_state(crate::legacy_service_v1::test_state().1);
        let app = Router::new().nest("/api/v1", gateway);
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/youtube/sources/42/scan?force=true")
                    .header("content-type", "application/json")
                    .body(Body::from("{\"force\":true}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let value: serde_json::Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 1024).await.unwrap()).unwrap();
        assert_eq!(
            value,
            json!({"id":"42","query":{"force":"true"},"body":"{\"force\":true}"})
        );
        for (method, path, status, detail) in [
            (
                Method::GET,
                "/api/v1/subtitle/task_queue/settings",
                StatusCode::METHOD_NOT_ALLOWED,
                "Method Not Allowed",
            ),
            (
                Method::POST,
                "/api/v1/bilibili/auth/me",
                StatusCode::NOT_FOUND,
                "bilibili browser operation not found",
            ),
            (
                Method::DELETE,
                "/api/v1/youtube/private",
                StatusCode::NOT_FOUND,
                "youtube browser operation not found",
            ),
        ] {
            let res = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(path)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(res.status(), status);
            let value: serde_json::Value =
                serde_json::from_slice(&to_bytes(res.into_body(), 1024).await.unwrap()).unwrap();
            assert_eq!(value["detail"], detail);
        }
    }
}
