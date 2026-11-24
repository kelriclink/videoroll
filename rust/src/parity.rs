//! Source-backed Rust v2 migration inventory.
//! Counts distinguish declarations, native adapters, and *verified parity*.
//! No path match is enough to claim a response/behavior contract is equivalent.
use crate::{AppError, AppResult, api::AppState};
use axum::{Json, Router, routing::get};
use serde_json::{Value, json};
use std::collections::BTreeMap;

const LEGACY_JSON: &str = include_str!("../docs/LEGACY_ROUTE_CATALOG.json");
const FRONTEND_JSON: &str = include_str!("../docs/FRONTEND_API_REFERENCES.json");

/// Verified native handler exists, but full original business equivalence
/// remains pending (title fallback, historical assets, manual triggers, etc).
const V1_ADAPTERS: &[(&str, &str)] = &[
    ("GET", "/health"),
    // Original Bilibili standalone settings now served natively at the
    // orchestrator's /api/v1 boundary, after DB/Pydantic differential tests.
    ("GET", "/bilibili/publish/settings"),
    ("PUT", "/bilibili/publish/settings"),
    ("GET", "/bilibili/auth/settings"),
    ("PUT", "/bilibili/auth/settings"),
    ("GET", "/bilibili/auth/me"),
    ("GET", "/bilibili/archive/types"),
    ("POST", "/bilibili/archive/type/recommend"),
    ("GET", "/bilibili/publish/jobs/{job_id}"),
    ("GET", "/settings/api"),
    ("PUT", "/settings/api"),
    ("POST", "/tasks/{task_id}/actions/youtube_download_enqueue"),
    ("POST", "/tasks/{task_id}/actions/youtube_download"),
    ("POST", "/tasks/{task_id}/actions/auto_dubbing"),
    ("POST", "/tasks/{task_id}/assets/{asset_id}/playout"),
    ("DELETE", "/tasks/{task_id}/assets/{asset_id}"),
    // Python youtube_ingest source operations are exposed through /api/v1
    // because the original React page uses the orchestrator proxy boundary.
    ("GET", "/youtube/sources"),
    ("POST", "/youtube/sources"),
    ("PATCH", "/youtube/sources/{source_pk}"),
    ("DELETE", "/youtube/sources/{source_pk}"),
    ("POST", "/youtube/sources/{source_pk}/scan"),
    ("POST", "/youtube/sources/{source_pk}/scan/start"),
    ("POST", "/youtube/scan"),
    ("GET", "/system/resources"),
    ("GET", "/settings/storage"),
    ("PUT", "/settings/storage"),
    ("GET", "/operations/ai-usage"),
    ("GET", "/operations/ai-usage/pricing"),
    ("PUT", "/operations/ai-usage/pricing"),
    ("GET", "/settings/dubbing/jobs/summary"),
    ("GET", "/settings/dubbing"),
    ("GET", "/settings/dubbing/native"),
    ("GET", "/render-management/connection"),
    ("PUT", "/render-management/connection"),
    ("GET", "/render-management/enrollments"),
    ("GET", "/render-management/workers"),
    ("GET", "/render-management/executions"),
    ("GET", "/render-management/executions/{execution_id}"),
    ("GET", "/render-workers/v1/protocol"),
    ("POST", "/render-workers/v1/enroll"),
    ("POST", "/render-workers/v1/local-enroll"),
    ("POST", "/render-workers/v1/{worker_id}/heartbeat"),
    ("POST", "/render-workers/v1/{worker_id}/claim"),
    (
        "GET",
        "/render-workers/v1/executions/{execution_id}/artifacts/{role}",
    ),
    (
        "POST",
        "/render-workers/v1/executions/{execution_id}/output",
    ),
    (
        "POST",
        "/render-workers/v1/executions/{execution_id}/complete",
    ),
    ("POST", "/render-workers/v1/executions/{execution_id}/fail"),
    (
        "POST",
        "/render-workers/v1/executions/{execution_id}/heartbeat",
    ),
    (
        "POST",
        "/render-workers/v1/executions/{execution_id}/progress",
    ),
    ("POST", "/render-workers/v1/executions/{execution_id}/logs"),
    ("GET", "/render-workers/v1/executions/{execution_id}/cancel"),
    ("GET", "/render-workers/v1/executions/{execution_id}/output"),
    ("POST", "/render-management/enrollments"),
    ("DELETE", "/render-management/enrollments/{enrollment_id}"),
    ("PATCH", "/render-management/workers/{worker_id}"),
    (
        "PATCH",
        "/render-management/workers/{worker_id}/devices/{device_id:path}",
    ),
    (
        "POST",
        "/render-management/workers/{worker_id}/revoke-credential",
    ),
    (
        "POST",
        "/render-management/executions/{execution_id}/cancel",
    ),
    (
        "POST",
        "/render-management/executions/{execution_id}/requeue",
    ),
    ("GET", "/subtitle/models"),
    ("POST", "/subtitle/models/download"),
    ("POST", "/subtitle/models/proxy/test"),
    ("POST", "/subtitle/models/upload"),
    ("DELETE", "/subtitle/models/{name}"),
    ("POST", "/subtitle/asr/external/test"),
    ("POST", "/subtitle/asr/groq/test"),
    ("POST", "/subtitle/asr/cloudflare/test"),
    ("GET", "/subtitle/dictionaries/sources"),
    ("PUT", "/subtitle/dictionaries/sources/{source_id}"),
    ("DELETE", "/subtitle/dictionaries/sources/{source_id}"),
    ("GET", "/subtitle/dictionaries/entries"),
    ("PUT", "/subtitle/dictionaries/entries/{entry_id}"),
    ("DELETE", "/subtitle/dictionaries/entries/{entry_id}"),
    ("POST", "/subtitle/dictionaries/lookup"),
    ("GET", "/subtitle/dictionaries/import-presets"),
    ("POST", "/subtitle/dictionaries/import"),
    ("POST", "/subtitle/dictionaries/promote"),
    ("GET", "/subtitle/knowledge/items"),
    ("POST", "/subtitle/knowledge/items"),
    ("POST", "/subtitle/knowledge/import"),
    ("POST", "/subtitle/knowledge/rebuild-embeddings"),
    ("DELETE", "/subtitle/knowledge/items/{item_id}"),
    ("GET", "/subtitle/embedding/models"),
    ("POST", "/subtitle/embedding/models/list"),
    ("POST", "/subtitle/embedding/models/download"),
    ("DELETE", "/subtitle/embedding/models/{name}"),
    ("GET", "/subtitle/embedding/runtime"),
    ("POST", "/subtitle/embedding/test"),
    ("GET", "/subtitle/hardware/intel"),
    ("GET", "/subtitle/settings"),
    ("GET", "/subtitle/agents/runs"),
    ("GET", "/subtitle/agent/skills"),
    ("GET", "/subtitle/agents/runs/{run_id}"),
    ("GET", "/subtitle/asr/settings"),
    ("PUT", "/subtitle/asr/settings"),
    ("GET", "/subtitle/auto/profile"),
    ("PUT", "/subtitle/auto/profile"),
    ("GET", "/subtitle/jobs/{job_id}"),
    ("POST", "/subtitle/jobs"),
    ("GET", "/subtitle/tasks/{task_id}/quality"),
    ("GET", "/subtitle/tasks/{task_id}/translation-context"),
    ("PUT", "/subtitle/tasks/{task_id}/translation-context"),
    ("GET", "/subtitle/task_queue"),
    ("GET", "/subtitle/task_queue/broker"),
    ("PUT", "/subtitle/task_queue/settings"),
    ("GET", "/subtitle/translate/settings"),
    ("PUT", "/subtitle/translate/settings"),
    ("POST", "/subtitle/translate/test"),
    ("GET", "/subtitle/translate/model-providers"),
    ("POST", "/subtitle/translate/model-providers"),
    ("PUT", "/subtitle/translate/model-providers/{provider_id}"),
    (
        "DELETE",
        "/subtitle/translate/model-providers/{provider_id}",
    ),
    (
        "GET",
        "/subtitle/translate/model-providers/{provider_id}/remote-models",
    ),
    ("WEBSOCKET", "/ws/events"),
    ("GET", "/auth/status"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/setup"),
    ("GET", "/tasks"),
    ("GET", "/tasks/{task_id}"),
    ("GET", "/videos/converted"),
    ("GET", "/tasks/{task_id}/assets"),
    ("GET", "/tasks/{task_id}/assets/{asset_id}/download"),
    ("GET", "/tasks/{task_id}/assets/{asset_id}/stream"),
    ("GET", "/tasks/{task_id}/subtitle_jobs"),
    ("GET", "/tasks/{task_id}/dubbing_jobs"),
    ("GET", "/tasks/{task_id}/publish_jobs"),
    ("GET", "/tasks/{task_id}/playout-assets"),
    ("POST", "/tasks"),
    ("POST", "/youtube/ingest"),
    ("POST", "/auto/youtube"),
    ("POST", "/tasks/{task_id}/actions/auto_youtube_start"),
    ("POST", "/tasks/{task_id}/upload/video"),
    ("POST", "/tasks/{task_id}/upload/cover"),
    ("GET", "/tasks/{task_id}/publish_batches"),
    ("POST", "/tasks/{task_id}/dubbing_jobs/{job_id}/confirm"),
    ("POST", "/tasks/{task_id}/dubbing_jobs/{job_id}/rerun"),
    ("POST", "/tasks/{task_id}/actions/stop"),
    ("POST", "/tasks/{task_id}/actions/resume"),
    ("POST", "/tasks/actions/stop_all"),
    ("POST", "/tasks/actions/resume_stopped"),
    ("GET", "/tasks/{task_id}/publish_meta"),
    ("PUT", "/tasks/{task_id}/publish_meta"),
    ("GET", "/tasks/{task_id}/publish_meta/draft"),
    ("POST", "/tasks/{task_id}/publish_meta/draft"),
    ("GET", "/tasks/{task_id}/youtube_meta"),
    ("POST", "/tasks/{task_id}/actions/youtube_meta"),
    ("GET", "/tasks/{task_id}/youtube_download_progress"),
    ("GET", "/settings/youtube"),
    ("PUT", "/settings/youtube"),
    ("POST", "/settings/youtube/test"),
    ("GET", "/settings/review"),
    ("PUT", "/settings/review"),
    ("GET", "/tasks/{task_id}/publish_review"),
    ("POST", "/tasks/{task_id}/actions/publish_review"),
    ("POST", "/tasks/{task_id}/actions/subtitle"),
    ("POST", "/tasks/{task_id}/actions/dubbing"),
    ("POST", "/tasks/{task_id}/dubbing_jobs/{job_id}/retry"),
    ("POST", "/tasks/{task_id}/dubbing_jobs/{job_id}/control"),
    ("POST", "/tasks/{task_id}/actions/subtitle_resume"),
    ("POST", "/tasks/{task_id}/actions/auto_subtitle_handoff"),
    ("POST", "/tasks/{task_id}/actions/auto_publish"),
    (
        "POST",
        "/tasks/{task_id}/publish_jobs/{publish_job_id}/append_p2",
    ),
    ("POST", "/tasks/{task_id}/actions/publish_all"),
    ("POST", "/tasks/{task_id}/actions/publish"),
    ("POST", "/tasks/actions/resume_failed_recent"),
    ("GET", "/settings/publish/platforms"),
    ("PUT", "/settings/publish/platforms/{platform}"),
    ("GET", "/settings/publish/social/accounts"),
    ("POST", "/settings/publish/social/accounts/{platform}"),
    (
        "POST",
        "/settings/publish/social/accounts/{account_id}/check",
    ),
    ("DELETE", "/settings/publish/social/accounts/{account_id}"),
    ("POST", "/settings/publish/social/login-sessions/{platform}"),
    (
        "GET",
        "/settings/publish/social/login-sessions/{session_id}",
    ),
    (
        "DELETE",
        "/settings/publish/social/login-sessions/{session_id}",
    ),
    ("GET", "/operations/alerts"),
    ("POST", "/operations/alerts/report"),
    ("POST", "/operations/alerts/scan"),
    ("POST", "/operations/alerts/{alert_id}/ack"),
    ("POST", "/operations/alerts/{alert_id}/resolve"),
];

fn from_sources() -> AppResult<(Value, Value)> {
    let routes: Value = serde_json::from_str(LEGACY_JSON)
        .map_err(|e| AppError::Invalid(format!("corrupted legacy audit: {e}")))?;
    let frontend: Value = serde_json::from_str(FRONTEND_JSON)
        .map_err(|e| AppError::Invalid(format!("corrupted frontend audit: {e}")))?;
    Ok((routes, frontend))
}

fn is_adapter(method: &str, path: &str) -> bool {
    V1_ADAPTERS
        .iter()
        .any(|&(verb, url)| verb == method && url == path)
}

pub fn report() -> AppResult<Value> {
    let (routes, frontend) = from_sources()?;
    let legacy = routes
        .get("routes")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Invalid("legacy audit is missing routes".into()))?;
    let usages = frontend
        .get("references")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Invalid("frontend audit is missing references".into()))?;
    let mut services = BTreeMap::<String, (u64, u64)>::new();
    let mut mapped = Vec::new();
    for row in legacy {
        let service = row
            .get("service")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let path = row.get("path").and_then(Value::as_str).unwrap_or("");
        let method = row.get("method").and_then(Value::as_str).unwrap_or("");
        // The V1 frontend consumes YouTube source management as well as
        // ingest through the orchestrator. These original declarations live
        // in the separate youtube_ingest service.
        let adapter = (service == "orchestrator_api"
            || (service == "youtube_ingest" && path.starts_with("/youtube/"))
            || (service == "subtitle_service" && path.starts_with("/subtitle/"))
            || (service == "bilibili_publisher"
                && matches!(
                    (method, path),
                    ("GET" | "PUT", "/bilibili/publish/settings")
                        | ("GET" | "PUT", "/bilibili/auth/settings")
                        | ("GET", "/bilibili/auth/me")
                        | ("GET", "/bilibili/archive/types")
                        | ("POST", "/bilibili/archive/type/recommend")
                        | ("GET", "/bilibili/publish/jobs/{job_id}")
                )))
            && is_adapter(method, path);
        let entry = services.entry(service.into()).or_default();
        entry.0 += 1;
        if adapter {
            entry.1 += 1;
            mapped.push(json!({"method":method,"path":path,"state":"native_adapter_partial"}));
        }
    }
    let breakdown = services
        .into_iter()
        .map(|(service, (declared, adapted))| {
            json!({
                "service":service,"declared_routes":declared,"native_adapters":adapted,
                "requires_contract_verification":declared.saturating_sub(adapted),
            })
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "legacy_route_declarations":legacy.len(),
        "frontend_url_references":usages.len(),
        "native_v1_adapters":mapped.len(),
        "fully_verified_legacy_routes":0,
        "all_features_migrated":false,
        "services":breakdown,
        "native_adapters":mapped,
        "warnings":[
            "Runtime introspection and external service endpoints require separate contract checks",
            "V1 adapter status means a native handler exists, NOT full behavioral equivalence",
            "The full original React UI remains experimental and is NOT enabled in production",
        ],
    }))
}
async fn summary() -> AppResult<Json<Value>> {
    Ok(Json(report()?))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/parity", get(summary))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_audit_has_legacy_paths_and_partial_count() {
        let value = report().unwrap();
        assert_eq!(value["all_features_migrated"], false);
        assert!(value["legacy_route_declarations"].as_u64().unwrap() >= 217);
        assert_eq!(value["native_v1_adapters"], V1_ADAPTERS.len());
        assert!(V1_ADAPTERS.len() >= 147);
        assert_eq!(value["fully_verified_legacy_routes"], 0);
        assert!(value["frontend_url_references"].as_u64().unwrap() >= 190);
        assert!(is_adapter("GET", "/tasks/{task_id}"));
        assert!(is_adapter("POST", "/tasks/{task_id}/actions/publish"));
    }
}
