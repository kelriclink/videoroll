//! Local RenderWorker bootstrap, original V1 wire protocol.
//! Kept separate from one-time invitation enrollment. Never allows bootstrap
//! from public IPs, non-bootstrap credentials, or across runtime engines.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json,
    extract::{ConnectInfo, State},
    http::{HeaderMap, header},
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use std::net::{IpAddr, SocketAddr};
use subtle::ConstantTimeEq;
use uuid::Uuid;

fn one() -> i32 {
    1
}
fn spec() -> Vec<i32> {
    vec![1]
}
fn object() -> Value {
    json!({})
}
#[derive(Debug, Deserialize)]
pub(crate) struct LocalEnroll {
    worker_key: String,
    name: String,
    platform: String,
    architecture: Option<String>,
    #[serde(default)]
    version: String,
    #[serde(default = "one")]
    protocol_version: i32,
    #[serde(default = "spec")]
    render_spec_versions: Vec<i32>,
    #[serde(default = "object")]
    capabilities: Value,
    #[serde(default = "object")]
    resources: Value,
    #[serde(default = "object")]
    labels: Value,
    #[serde(default = "one")]
    max_concurrency: i32,
}
fn private_source(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => v.is_private() || v.is_loopback() || v.is_link_local(),
        IpAddr::V6(v) => v.is_unique_local() || v.is_loopback() || v.is_unicast_link_local(),
    }
}
fn authorize(ip: IpAddr, headers: &HeaderMap, secret: Option<&str>) -> AppResult<()> {
    if !private_source(ip) {
        return Err(AppError::Forbidden(
            "local enrollment restricted to private networks".into(),
        ));
    }
    let expected = secret
        .filter(|s| !s.is_empty())
        .ok_or(AppError::Unauthorized)?;
    let raw = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let (scheme, provided) = raw.split_once(' ').unwrap_or(("", ""));
    if !scheme.eq_ignore_ascii_case("bearer")
        || provided.len() != expected.len()
        || !bool::from(provided.as_bytes().ct_eq(expected.as_bytes()))
    {
        return Err(AppError::Unauthorized);
    }
    Ok(())
}
fn validate(p: &LocalEnroll) -> AppResult<()> {
    if !(1..=128).contains(&p.worker_key.len())
        || !(1..=128).contains(&p.name.len())
        || !(1..=32).contains(&p.platform.len())
        || p.architecture.as_deref().is_some_and(|v| v.len() > 32)
        || p.version.len() > 64
        || !(1..=100).contains(&p.protocol_version)
        || p.render_spec_versions.is_empty()
        || p.render_spec_versions.len() > 16
        || p.render_spec_versions.iter().any(|v| *v < 1 || *v > 100)
        || !(1..=32).contains(&p.max_concurrency)
        || !p.capabilities.is_object()
        || !p.resources.is_object()
        || !p.labels.is_object()
    {
        return Err(AppError::Invalid(
            "invalid local RenderWorker request".into(),
        ));
    }
    Ok(())
}
pub(crate) async fn enroll(
    State(s): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(p): Json<LocalEnroll>,
) -> AppResult<Json<Value>> {
    authorize(peer.ip(), &headers, s.config.bootstrap_secret.as_deref())?;
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL")
            .ok()
            .as_deref(),
        Some("1" | "true")
    ) {
        return Err(AppError::Conflict(
            "legacy RenderWorker writes require explicit opt-in".into(),
        ));
    }
    validate(&p)?;
    let mut tx = s.db.pool.begin().await?;
    let original = sqlx::query(
        "SELECT id,max_concurrency,labels,runtime_engine FROM render_workers
        WHERE worker_key=$1 FOR UPDATE",
    )
    .bind(&p.worker_key)
    .fetch_optional(&mut *tx)
    .await?;
    if original
        .as_ref()
        .is_some_and(|r| r.try_get::<String, _>("runtime_engine").ok().as_deref() != Some("python"))
    {
        return Err(AppError::Conflict(
            "worker_key belongs to another runtime".into(),
        ));
    }
    let id = original
        .as_ref()
        .map(|r| r.get::<Uuid, _>("id"))
        .unwrap_or_else(Uuid::new_v4);
    let capacity = original
        .as_ref()
        .map(|r| r.get::<i32, _>("max_concurrency"))
        .unwrap_or(p.max_concurrency);
    let mut labels = p.labels.as_object().cloned().unwrap_or_default();
    if let Some(old) = &original {
        let existing: Value = old.try_get("labels")?;
        if let Some(disabled) = existing.get("disabled_render_device_ids") {
            labels.insert("disabled_render_device_ids".into(), disabled.clone());
        }
    }
    labels.insert("local".into(), json!(true));
    labels.insert("standalone".into(), json!(true));
    use base64::Engine as _;
    let entropy = [Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4()]
        .into_iter()
        .flat_map(|u| u.into_bytes())
        .collect::<Vec<_>>();
    let credential = format!(
        "vrw_{}",
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(entropy)
    );
    let digest = hex::encode(Sha256::digest(credential.as_bytes()));
    let row = sqlx::query(
        "INSERT INTO render_workers(
        id,worker_key,name,platform,architecture,version,protocol_version,
        render_spec_versions,capabilities,resources,labels,max_concurrency,
        credential_hash,credential_issued_at,status,enabled,last_seen_at,runtime_engine)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),'online',true,now(),'python')
       ON CONFLICT(worker_key) DO UPDATE SET name=$3,platform=$4,
        architecture=$5,version=$6,protocol_version=$7,render_spec_versions=$8,
        capabilities=$9,resources=$10,labels=$11,max_concurrency=$12,
        credential_hash=$13,credential_issued_at=now(),credential_revoked_at=NULL,
        status='online',enabled=true,last_seen_at=now(),updated_at=now()
       WHERE render_workers.runtime_engine='python' RETURNING *",
    )
    .bind(id)
    .bind(&p.worker_key)
    .bind(&p.name)
    .bind(&p.platform)
    .bind(&p.architecture)
    .bind(&p.version)
    .bind(p.protocol_version)
    .bind(json!(p.render_spec_versions))
    .bind(&p.capabilities)
    .bind(&p.resources)
    .bind(Value::Object(labels))
    .bind(capacity)
    .bind(&digest)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::Conflict("worker identity belongs to another runtime".into()))?;
    let mut result = serde_json::Map::new();
    for key in ["worker_key", "name", "platform", "version", "status"] {
        result.insert(key.into(), json!(row.try_get::<String, _>(key)?));
    }
    result.insert("id".into(), json!(row.try_get::<Uuid, _>("id")?));
    result.insert(
        "architecture".into(),
        json!(row.try_get::<Option<String>, _>("architecture")?),
    );
    result.insert(
        "protocol_version".into(),
        json!(row.try_get::<i32, _>("protocol_version")?),
    );
    for key in [
        "render_spec_versions",
        "capabilities",
        "resources",
        "labels",
    ] {
        result.insert(key.into(), row.try_get::<Value, _>(key)?);
    }
    result.insert("enabled".into(), json!(row.try_get::<bool, _>("enabled")?));
    result.insert(
        "draining".into(),
        json!(row.try_get::<bool, _>("draining")?),
    );
    result.insert(
        "max_concurrency".into(),
        json!(row.try_get::<i32, _>("max_concurrency")?),
    );
    result.insert(
        "active_jobs".into(),
        json!(row.try_get::<i32, _>("active_jobs")?),
    );
    result.insert(
        "last_seen_at".into(),
        json!(row.try_get::<chrono::DateTime<chrono::Utc>, _>("last_seen_at")?),
    );
    tx.commit().await?;
    Ok(Json(json!({"worker":result,"credential":credential})))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nonlocal_or_bad_bootstrap_rejected() {
        assert!(!private_source("8.8.8.8".parse().unwrap()));
        assert!(private_source("127.0.0.1".parse().unwrap()));
        assert!(private_source("10.10.0.2".parse().unwrap()));
        let mut h = HeaderMap::new();
        h.insert(
            header::AUTHORIZATION,
            "Bearer valid-secrethere".parse().unwrap(),
        );
        assert!(authorize("8.8.8.8".parse().unwrap(), &h, Some("valid-secrethere")).is_err());
        assert!(authorize("127.0.0.1".parse().unwrap(), &h, Some("other-secret")).is_err());
        assert!(authorize("127.0.0.1".parse().unwrap(), &h, Some("valid-secrethere")).is_ok());
    }
}
