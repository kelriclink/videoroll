//! Native Rust submission outbox for the existing isolated Social Automation
//! Unit (SAU) browser worker. The worker continues owning encrypted sessions
//! and platform-specific automation. V2 owns intent, authorization, asset
//! references, idempotency and reconciliation; no V1 job is mutated by Rust.
use crate::{
    AppError, AppResult, Config, Database, api::AppState, legacy_contracts::read_task,
    storage::AssetRegistry,
};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::{get as route_get, post},
};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, Row};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

const OWNER: &str = "v2_sau_publish";
const PROJECTION: &str =
    "id,task_id,platform,account_id,video_key,cover_key,metadata,platform_options,
    fingerprint,state,sau_job_id,external_id,external_url,last_error,attempt,
    dispatched_at,accepted_at,finished_at,created_at,updated_at";

#[derive(Debug, Clone, Serialize, FromRow)]
pub struct SocialIntent {
    id: Uuid,
    task_id: Uuid,
    platform: String,
    account_id: Uuid,
    video_key: String,
    cover_key: Option<String>,
    metadata: Value,
    platform_options: Value,
    fingerprint: String,
    state: String,
    sau_job_id: Option<Uuid>,
    external_id: Option<String>,
    external_url: Option<String>,
    last_error: Option<String>,
    attempt: i32,
    dispatched_at: Option<chrono::DateTime<chrono::Utc>>,
    accepted_at: Option<chrono::DateTime<chrono::Utc>>,
    finished_at: Option<chrono::DateTime<chrono::Utc>>,
    created_at: chrono::DateTime<chrono::Utc>,
    updated_at: chrono::DateTime<chrono::Utc>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SocialPayload {
    platform: String,
    account_id: String,
    video_key: Option<String>,
    #[serde(default)]
    video_parts: Option<Vec<Value>>,
    cover_key: Option<String>,
    typeid_mode: Option<String>,
    meta: Option<Value>,
    #[serde(default)]
    platform_options: Value,
    #[serde(default)]
    skip_review: bool,
    #[serde(default)]
    force_retry: bool,
}
fn platform(value: &str) -> AppResult<&str> {
    match value {
        "douyin" | "xiaohongshu" | "kuaishou" => Ok(value),
        _ => Err(AppError::Invalid("unsupported SAU publish platform".into())),
    }
}
fn validate(body: &SocialPayload) -> AppResult<(Uuid, Value, Value)> {
    platform(&body.platform)?;
    let account = Uuid::parse_str(&body.account_id)
        .map_err(|_| AppError::Invalid("SAU account_id must be a UUID".into()))?;
    if body.skip_review
        || body.force_retry
        || body.typeid_mode.is_some()
        || body.video_parts.is_some()
    {
        return Err(AppError::Invalid(
            "SAU V2 does not accept review bypass, forced retry, Bilibili-only typeid or video_parts".into()
        ));
    }
    let meta = body.meta.clone().ok_or_else(|| {
        AppError::Invalid("SAU publication requires explicit platform metadata".into())
    })?;
    if meta.as_object().is_none_or(|v| v.is_empty())
        || serde_json::to_vec(&meta).map_or(true, |v| v.len() > 64 * 1024)
    {
        return Err(AppError::Invalid(
            "SAU metadata must be a nonempty JSON object <=64 KiB".into(),
        ));
    }
    let options = if body.platform_options.is_null() {
        json!({})
    } else {
        body.platform_options.clone()
    };
    if !options.is_object() || serde_json::to_vec(&options).map_or(true, |v| v.len() > 16 * 1024) {
        return Err(AppError::Invalid(
            "SAU platform options must be JSON object <=16 KiB".into(),
        ));
    }
    Ok((account, meta, options))
}
pub(crate) async fn shared_media_root(config: &Config) -> AppResult<()> {
    if !config.social_publish_allowed {
        return Err(AppError::Unavailable(
            "SAU publishing has not been explicitly enabled".into(),
        ));
    }
    let configured = config
        .social_storage_root
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("SAU shared media root not configured".into()))?;
    let source = tokio::fs::canonicalize(&config.storage_root).await?;
    let target = tokio::fs::canonicalize(configured).await?;
    if source != target {
        return Err(AppError::Conflict(
            "SAU and V2 media roots are not identical".into(),
        ));
    }
    if config.social_publisher_url.is_none()
        || config
            .legacy_internal_secret
            .as_deref()
            .is_none_or(|v| v.len() < 16)
    {
        return Err(AppError::Unavailable(
            "SAU sidecar or HMAC secret not configured".into(),
        ));
    }
    Ok(())
}
async fn asset_key(
    db: &Database,
    task: Uuid,
    kind: &str,
    chosen: Option<&str>,
) -> AppResult<String> {
    let key = if let Some(chosen) = chosen {
        AssetRegistry::checked_key(chosen)?;
        let key: Option<String> = sqlx::query_scalar(
            "SELECT a.storage_key FROM assets a
             JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live'
             WHERE a.task_id=$1 AND a.kind::text=$2 AND a.storage_key=$3
               AND a.storage_key LIKE 'v2/%' LIMIT 1",
        )
        .bind(task)
        .bind(kind)
        .bind(chosen)
        .fetch_optional(&db.pool)
        .await?;
        key
    } else {
        sqlx::query_scalar(
            "SELECT a.storage_key FROM assets a
             JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live'
             WHERE a.task_id=$1 AND a.kind::text=$2
               AND a.storage_key LIKE 'v2/%'
             ORDER BY a.created_at DESC,a.id DESC LIMIT 1",
        )
        .bind(task)
        .bind(kind)
        .fetch_optional(&db.pool)
        .await?
    };
    key.ok_or_else(|| {
        AppError::NotFound(format!(
            "verified V2 {kind} video asset missing for SAU publication"
        ))
    })
}
fn digest(
    account: Uuid,
    platform: &str,
    video: &str,
    cover: Option<&str>,
    meta: &Value,
    opts: &Value,
) -> AppResult<String> {
    let input = json!({"account":account,"platform":platform,"video":video,
        "cover":cover,"meta":meta,"options":opts});
    let bytes = serde_json::to_vec(&input)
        .map_err(|_| AppError::Invalid("SAU fingerprint cannot be serialized".into()))?;
    Ok(hex::encode(Sha256::digest(&bytes)))
}
fn visible(intent: &SocialIntent) -> Value {
    json!({"job_id":intent.id,"platform":intent.platform,"state":intent.state,
        "account_id":intent.account_id,"external_id":intent.external_id,
        "external_url":intent.external_url,"sau_job_id":intent.sau_job_id,
        "response":null,"attempt":intent.attempt})
}
async fn account_checked(config: &Config, platform: &str, id: Uuid) -> AppResult<()> {
    let result = crate::social_accounts::call(
        config,
        Method::GET,
        "accounts",
        Some(&[("platform", platform)]),
        None,
        None,
    )
    .await?;
    let accounts = result
        .as_array()
        .ok_or_else(|| AppError::Unavailable("SAU account list response invalid".into()))?;
    let wanted_id = id.to_string();
    let found = accounts.iter().any(|a| {
        a.get("id").and_then(Value::as_str) == Some(wanted_id.as_str())
            && a.get("platform").and_then(Value::as_str) == Some(platform)
            && a.get("is_active").and_then(Value::as_bool) == Some(true)
            && a.get("check_state").and_then(Value::as_str) == Some("valid")
    });
    if !found {
        return Err(AppError::Conflict(
            "SAU account is not active, validated or matched to platform".into(),
        ));
    }
    Ok(())
}
pub async fn manual(state: &AppState, task_id: Uuid, raw: Value) -> AppResult<Value> {
    let body: SocialPayload = serde_json::from_value(raw)
        .map_err(|_| AppError::Invalid("invalid SAU manual publish fields".into()))?;
    let platform = platform(&body.platform)?;
    let (account, meta, options) = validate(&body)?;
    let task = read_task(&state.db, task_id).await?;
    if task.source_license == "unknown" || task.status == "CANCELED" {
        return Err(AppError::Conflict(
            "SAU publication requires authorized and active source".into(),
        ));
    }
    if !crate::publish_platforms::social_enabled(&state.config, &state.db, platform).await? {
        return Err(AppError::Conflict(
            "SAU platform disabled or not configured in native V2".into(),
        ));
    }
    shared_media_root(&state.config).await?;
    account_checked(&state.config, platform, account).await?;
    crate::publish_review::ensure_approved(
        &state.db,
        &state.storage,
        &state.config,
        task_id,
        &meta,
    )
    .await?;
    let video = asset_key(&state.db, task_id, "video_final", body.video_key.as_deref()).await?;
    let cover = if let Some(cover) = body.cover_key.as_deref() {
        Some(asset_key(&state.db, task_id, "cover_image", Some(cover)).await?)
    } else {
        None
    };
    // This is a physical shared filesystem, not a forged storage:// ref.
    let video_path = state.storage.resolve_existing(&video).await?;
    let metadata = tokio::fs::metadata(&video_path).await?;
    if !metadata.is_file() || metadata.len() == 0 {
        return Err(AppError::Conflict(
            "SAU selected video is empty or unavailable".into(),
        ));
    }
    crate::media::ffmpeg::probe_video(&state.config.ffprobe_binary, &video_path).await?;
    if let Some(ref cover_key) = cover {
        let cover_path = state.storage.resolve_existing(cover_key).await?;
        let metadata = tokio::fs::metadata(&cover_path).await?;
        if !metadata.is_file() || metadata.len() == 0 {
            return Err(AppError::Conflict("SAU cover asset is unavailable".into()));
        }
    }
    let fingerprint = digest(account, platform, &video, cover.as_deref(), &meta, &options)?;
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
        .bind(format!("v2:sau:{}:{}:{}", task_id, platform, account))
        .execute(&mut *tx)
        .await?;
    let existing = sqlx::query_as::<_, SocialIntent>(&format!(
        "SELECT {PROJECTION} FROM vr2_social_publish_intents
         WHERE task_id=$1 AND platform=$2 AND account_id=$3 FOR UPDATE"
    ))
    .bind(task_id)
    .bind(platform)
    .bind(account)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(existing) = existing {
        if existing.fingerprint == fingerprint {
            return Ok(visible(&existing));
        }
        return Err(AppError::Conflict(
            "this SAU account already has a distinct publish intent for the task".into(),
        ));
    }
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO operation_inbox
        (id,operation_key,runtime_engine,task_id,stage,generation,status,
         sau_platform,sau_account_id,sau_video_key,sau_cover_key,
         request_json,sau_platform_options,sau_fingerprint,attempt_count,max_attempts)
        VALUES($1,('v2:sau:intent:'||$1::text),'rust-v2',$2,'sau_publish',0,
               'queued',$3,$4,$5,$6,$7,$8,$9,1,1)",
    )
    .bind(id)
    .bind(task_id)
    .bind(platform)
    .bind(account)
    .bind(&video)
    .bind(&cover)
    .bind(&meta)
    .bind(&options)
    .bind(&fingerprint)
    .execute(&mut *tx)
    .await?;
    for (key, role) in [
        (Some(video.as_str()), "social_video"),
        (cover.as_deref(), "social_cover"),
    ] {
        if let Some(key) = key {
            sqlx::query(
                "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
                VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            )
            .bind(key)
            .bind(OWNER)
            .bind(id)
            .bind(role)
            .execute(&mut *tx)
            .await?;
        }
    }
    tx.commit().await?;
    Ok(
        json!({"job_id":id,"platform":platform,"state":"queued","account_id":account,
        "external_id":null,"external_url":null,"sau_job_id":null,"response":null,"attempt":1}),
    )
}
async fn list(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Json<Vec<Value>>> {
    read_task(&state.db, task_id).await?;
    let records = sqlx::query_as::<_, SocialIntent>(&format!(
        "SELECT {PROJECTION} FROM vr2_social_publish_intents
         WHERE task_id=$1 ORDER BY created_at DESC LIMIT 100"
    ))
    .bind(task_id)
    .fetch_all(&state.db.pool)
    .await?;
    Ok(Json(records.iter().map(visible).collect()))
}
async fn get(
    State(state): State<AppState>,
    Path((task_id, id)): Path<(Uuid, Uuid)>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    let record = sqlx::query_as::<_, SocialIntent>(&format!(
        "SELECT {PROJECTION} FROM vr2_social_publish_intents WHERE id=$1 AND task_id=$2"
    ))
    .bind(id)
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("SAU native intent missing".into()))?;
    Ok(Json(visible(&record)))
}
fn accepted_state(raw: &str) -> AppResult<&'static str> {
    match raw {
        "submitting" | "queued" | "processing" => Ok("accepted"),
        "published" => Ok("published"),
        "submitted" => Ok("submitted"),
        "unknown" => Ok("unknown"),
        "failed" => Ok("failed"),
        _ => Err(AppError::Unavailable(
            "SAU returned unrecognized submission state".into(),
        )),
    }
}
async fn claim(db: &Database) -> AppResult<Option<SocialIntent>> {
    let mut tx = db.pool.begin().await?;
    let record = sqlx::query_as::<_, SocialIntent>(&format!(
        "SELECT {PROJECTION} FROM vr2_social_publish_intents
         WHERE state='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
    ))
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(ref record) = record {
        sqlx::query(
            "UPDATE vr2_social_publish_intents
            SET state='dispatching',dispatched_at=now(),updated_at=now()
            WHERE id=$1 AND state='queued'",
        )
        .bind(record.id)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(record)
}
fn error_summary() -> &'static str {
    "SAU submission outcome may be unknown; inspect worker and platform receipt"
}
async fn release_refs(db: &Database, intent: Uuid) -> AppResult<()> {
    sqlx::query("DELETE FROM storage_object_refs WHERE owner_type=$1 AND owner_id=$2")
        .bind(OWNER)
        .bind(intent)
        .execute(&db.pool)
        .await?;
    Ok(())
}
async fn dispatch(config: &Config, db: &Database, record: &SocialIntent) -> AppResult<()> {
    shared_media_root(config).await?;
    if !crate::publish_platforms::social_enabled(config, db, &record.platform).await? {
        return Err(AppError::Conflict(
            "SAU platform was disabled after intent creation".into(),
        ));
    }
    // No retry after this boundary, because the sidecar might have persisted a
    // job and begun an irreversible browser submission.
    let result = crate::social_accounts::call(
        config,
        Method::POST,
        &format!("sau/{}/publish", record.platform),
        None,
        Some(json!({
            "platform":record.platform,"task_id":record.task_id,"batch_id":null,
            "account_id":record.account_id,"video":{"type":"storage","key":record.video_key},
            "cover":record.cover_key.as_ref().map(|v|json!({"type":"storage","key":v})),
            "meta":record.metadata,"platform_options":record.platform_options,
            "force_retry":false,
        })),
        None,
    )
    .await?;
    let id = result
        .get("job_id")
        .and_then(Value::as_str)
        .and_then(|id| Uuid::parse_str(id).ok())
        .ok_or_else(|| {
            AppError::Unavailable(
                "SAU did not return a durable publish job id; external outcome uncertain".into(),
            )
        })?;
    if result.get("platform").and_then(Value::as_str) != Some(record.platform.as_str()) {
        return Err(AppError::Unavailable(
            "SAU accepted response platform does not match request".into(),
        ));
    }
    let state = accepted_state(result.get("state").and_then(Value::as_str).unwrap_or(""))?;
    // Submitted must carry actual platform ID to be counted as proven.
    let external_id = result.get("external_id").and_then(Value::as_str);
    let external_url = result.get("external_url").and_then(Value::as_str);
    let verified = if matches!(state, "submitted" | "published")
        && external_id.is_none()
        && external_url.is_none()
    {
        "accepted"
    } else {
        state
    };
    let changed = sqlx::query(
        "UPDATE vr2_social_publish_intents
         SET state=$2,sau_job_id=$3,external_id=$4,external_url=$5,
             accepted_at=now(),updated_at=now(),
             finished_at=CASE WHEN $2 IN ('submitted','published','failed','unknown')
                              THEN now() ELSE finished_at END
         WHERE id=$1 AND state='dispatching'",
    )
    .bind(record.id)
    .bind(verified)
    .bind(id)
    .bind(external_id)
    .bind(external_url)
    .execute(&db.pool)
    .await?
    .rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict(
            "SAU intent no longer owns dispatch state".into(),
        ));
    }
    if matches!(verified, "submitted" | "published" | "failed") {
        release_refs(db, record.id).await?;
    }
    Ok(())
}
pub async fn tick(config: &Config, db: &Database) -> AppResult<bool> {
    let Some(record) = claim(db).await? else {
        return Ok(false);
    };
    if let Err(error) = dispatch(config, db, &record).await {
        tracing::warn!(intent_id=%record.id,error=%error,
            "SAU handoff outcome uncertain; request will not be retried automatically");
        // Once the durable dispatch boundary has been crossed, even an HTTP
        // 409/400 can be a proxy error AFTER the browser worker accepted the
        // intent. Never classify sidecar errors as safe-to-retry failures.
        let status = "unknown";
        sqlx::query(
            "UPDATE vr2_social_publish_intents SET state=$2,last_error=$3,
             finished_at=now(),updated_at=now()
             WHERE id=$1 AND state='dispatching'",
        )
        .bind(record.id)
        .bind(status)
        .bind(error_summary())
        .execute(&db.pool)
        .await?;
    }
    Ok(true)
}
/// A crash during dispatch makes remote outcome unknown. Never automatically
/// invoke the sidecar a second time.
pub async fn reconcile_stale(db: &Database) -> AppResult<u64> {
    let changed=sqlx::query(
        "UPDATE vr2_social_publish_intents
         SET state='unknown',last_error='SAU handoff may have reached the browser worker; verify before retry',
             updated_at=now() WHERE state='dispatching'
             AND dispatched_at<now()-interval '2 minutes'"
    ).execute(&db.pool).await?.rows_affected();
    Ok(changed)
}
/// Read completed SAU publish jobs from the shared original schema (read-only).
/// A dispatch response is not proof of platform acceptance.
pub async fn reconcile_receipts(db: &Database) -> AppResult<u64> {
    let records = sqlx::query_as::<_, SocialIntent>(&format!(
        "SELECT {PROJECTION} FROM vr2_social_publish_intents
         WHERE state IN ('accepted','unknown') AND sau_job_id IS NOT NULL
         AND EXISTS (SELECT 1 FROM publish_jobs j WHERE j.id=vr2_social_publish_intents.sau_job_id
           AND j.task_id=vr2_social_publish_intents.task_id AND j.platform::text=vr2_social_publish_intents.platform
           AND j.account_id=vr2_social_publish_intents.account_id
           AND ((j.state::text IN ('submitted','published') AND (NULLIF(j.external_id,'') IS NOT NULL OR NULLIF(j.external_url,'') IS NOT NULL))
                OR (j.state::text='failed')
                OR (vr2_social_publish_intents.state='accepted' AND
                    (j.state::text='unknown' OR (j.state::text='submitted' AND j.response_json->>'runtime'='rust'
                                                 AND j.response_json->>'receipt_verified'='false')))))
         ORDER BY updated_at LIMIT 100"
    ))
    .fetch_all(&db.pool)
    .await?;
    let mut updated = 0;
    for record in records {
        let job = sqlx::query(
            "SELECT state::text AS state,external_id,external_url,response_json
             FROM publish_jobs WHERE id=$1 AND task_id=$2 AND platform::text=$3
               AND account_id=$4",
        )
        .bind(record.sau_job_id)
        .bind(record.task_id)
        .bind(&record.platform)
        .bind(record.account_id)
        .fetch_optional(&db.pool)
        .await?;
        let Some(job) = job else { continue };
        let external_state: String = job.get("state");
        let external_id: Option<String> = job
            .try_get::<Option<String>, _>("external_id")?
            .filter(|s| !s.trim().is_empty());
        let external_url: Option<String> = job
            .try_get::<Option<String>, _>("external_url")?
            .filter(|s| !s.trim().is_empty());
        let response: Option<Value> = job.try_get("response_json")?;
        let unverified_native_submission = response
            .as_ref()
            .is_some_and(|r| r["runtime"] == "rust" && r["receipt_verified"] == false);
        let state = match external_state.as_str() {
            "submitted" | "published" if external_id.is_some() || external_url.is_some() => {
                external_state.as_str()
            }
            "submitted" if unverified_native_submission => "unknown",
            "failed" => "failed",
            "unknown" => "unknown",
            _ => continue,
        };
        let changed = sqlx::query(
            "UPDATE vr2_social_publish_intents SET state=$2,external_id=$3,
             external_url=$4,finished_at=now(),updated_at=now(),
             last_error=CASE WHEN $2='unknown' THEN COALESCE(last_error,'SAU platform receipt not independently verified; check platform before retry') ELSE NULL END
             WHERE id=$1 AND state IN ('accepted','unknown') AND state IS DISTINCT FROM $2",
        )
        .bind(record.id)
        .bind(state)
        .bind(external_id)
        .bind(external_url)
        .execute(&db.pool)
        .await?
        .rows_affected();
        if changed != 0 {
            if matches!(state, "published" | "submitted" | "failed") {
                release_refs(db, record.id).await?;
            }
            updated += 1;
        }
    }
    Ok(updated)
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PublishAllRequest {
    account_id: Option<String>,
    platforms: Option<Vec<String>>,
    #[serde(default)]
    account_ids: HashMap<String, String>,
    video_key: Option<String>,
    video_parts: Option<Vec<Value>>,
    cover_key: Option<String>,
    typeid_mode: Option<String>,
    meta: Option<Value>,
    #[serde(default)]
    platform_meta: HashMap<String, Value>,
    #[serde(default)]
    platform_options: HashMap<String, Value>,
    #[serde(default)]
    skip_review: bool,
    #[serde(default)]
    force_retry: bool,
    #[serde(default)]
    fresh_batch: bool,
}
fn batch_platforms(input: &PublishAllRequest) -> AppResult<Vec<String>> {
    if input.skip_review
        || input.force_retry
        || input.fresh_batch
        || input.video_parts.is_some()
        || input.typeid_mode.is_some()
    {
        return Err(AppError::Invalid(
            "multi-platform V2 SAU does not support review bypass, forced retry, fresh batch or video_parts".into()
        ));
    }
    let platforms = input.platforms.as_ref().ok_or_else(|| {
        AppError::Invalid(
            "publish_all requires an explicit platform list; no hidden targets are selected".into(),
        )
    })?;
    if platforms.is_empty() || platforms.len() > 3 {
        return Err(AppError::Invalid(
            "publish_all requires 1–3 SAU platforms".into(),
        ));
    }
    let mut unique = HashSet::new();
    for platform in platforms {
        self::platform(platform)?;
        if !unique.insert(platform.as_str()) {
            return Err(AppError::Invalid("duplicate publish_all target".into()));
        }
    }
    for key in input
        .account_ids
        .keys()
        .chain(input.platform_meta.keys())
        .chain(input.platform_options.keys())
    {
        if !unique.contains(key.as_str()) {
            return Err(AppError::Invalid(
                "publish_all has settings for an unrequested platform".into(),
            ));
        }
    }
    Ok(platforms.clone())
}
async fn publish_all(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Json(input): Json<PublishAllRequest>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    let platforms = batch_platforms(&input)?;
    // The old contract permits partial per-target outcomes. Native V2 returns
    // errors explicitly and never reports a partially accepted batch as done.
    // No fake PublishBatch is invented: each target has a durable V2 intent.
    let mut results = serde_json::Map::new();
    let mut errors = serde_json::Map::new();
    let mut ok = 0usize;
    let mut published = 0usize;
    for platform in &platforms {
        let account = input
            .account_ids
            .get(platform)
            .or(input.account_id.as_ref());
        let meta = input.platform_meta.get(platform).or(input.meta.as_ref());
        let request = json!({
            "platform":platform,"account_id":account,"video_key":input.video_key,
            "video_parts":null,"cover_key":input.cover_key,
            "typeid_mode":null,"meta":meta,
            "platform_options":input.platform_options.get(platform).cloned().unwrap_or_else(||json!({})),
            "skip_review":false,"force_retry":false,
        });
        match manual(&state, task_id, request).await {
            Ok(result) => {
                let status = result.get("state").and_then(Value::as_str).unwrap_or("");
                if matches!(status, "unknown" | "failed") {
                    errors.insert(
                        platform.clone(),
                        json!("existing delivery requires manual verification"),
                    );
                } else {
                    ok += 1;
                    if matches!(status, "submitted" | "published") {
                        published += 1;
                    }
                }
                results.insert(platform.clone(), result);
            }
            Err(error) => {
                errors.insert(platform.clone(), json!(format!("{error}")));
            }
        }
    }
    let n = platforms.len();
    let err = errors.len();
    Ok(Json(json!({
        "batch_id":null,"results":results,
        "all_accepted":ok==n && err==0,
        "has_any_accepted":ok>0,
        "all_published":published==n && err==0,
        "all_succeeded":published==n && err==0,
        "platform_count":n,"ok_count":ok,"error_count":err,"errors":errors
    })))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{id}/actions/publish_all", post(publish_all))
        .route("/tasks/{id}/social_publish_jobs", route_get(list))
        .route("/tasks/{id}/social_publish_jobs/{job_id}", route_get(get))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn multi_platform_batch_rejects_hidden_or_duplicate_targets() {
        let input: PublishAllRequest = serde_json::from_value(json!({
            "platforms":["douyin","kuaishou"],"account_ids":{"douyin":Uuid::new_v4().to_string()}
        }))
        .unwrap();
        assert_eq!(batch_platforms(&input).unwrap(), ["douyin", "kuaishou"]);
        for invalid in [
            json!({"platforms":["douyin","douyin"]}),
            json!({"platforms":["douyin","bilibili"]}),
            json!({"platforms":["douyin"],"fresh_batch":true}),
            json!({"platforms":["douyin"],"platform_meta":{"kuaishou":{"title":"bad"}}}),
            json!({}),
        ] {
            assert!(batch_platforms(&serde_json::from_value(invalid).unwrap()).is_err());
        }
    }
    #[test]
    fn safety_contract_never_accepts_skip_review_or_forced_retry() {
        let good = json!({"platform":"douyin","account_id":Uuid::new_v4().to_string(),
            "meta":{"title":"short test"},"platform_options":{}});
        let parsed: SocialPayload = serde_json::from_value(good.clone()).unwrap();
        assert!(validate(&parsed).is_ok());
        let mut bad = good.clone();
        bad["force_retry"] = json!(true);
        assert!(validate(&serde_json::from_value(bad).unwrap()).is_err());
        let mut bad = good.clone();
        bad["skip_review"] = json!(true);
        assert!(validate(&serde_json::from_value(bad).unwrap()).is_err());
        let mut bad = good;
        bad["platform"] = json!("tiktok");
        assert!(validate(&serde_json::from_value(bad).unwrap()).is_err());
    }
    #[test]
    fn no_sau_success_without_external_receipt() {
        assert_eq!(accepted_state("submitting").unwrap(), "accepted");
        assert_eq!(accepted_state("published").unwrap(), "published");
        assert!(accepted_state("ok").is_err());
        let a = Uuid::new_v4();
        let fp = digest(
            a,
            "douyin",
            "v2/demo",
            None,
            &json!({"title":"a"}),
            &json!({}),
        )
        .unwrap();
        assert_ne!(
            fp,
            digest(
                a,
                "douyin",
                "v2/demo",
                None,
                &json!({"title":"b"}),
                &json!({})
            )
            .unwrap()
        );
    }
}
