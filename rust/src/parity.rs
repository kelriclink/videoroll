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
    ("GET", "/system/resources"),
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
        let adapter = service == "orchestrator_api" && is_adapter(method, path);
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
        assert_eq!(value["native_v1_adapters"], 38);
        assert_eq!(value["fully_verified_legacy_routes"], 0);
        assert!(value["frontend_url_references"].as_u64().unwrap() >= 190);
        assert!(is_adapter("GET", "/tasks/{task_id}"));
        assert!(!is_adapter("POST", "/tasks/{task_id}/actions/publish"));
    }
}
