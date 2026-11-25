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
    let owner: Option<String> =
        sqlx::query_scalar("SELECT runtime_owner FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_optional(&mut *tx)
            .await?;
    if owner.as_deref() != Some("rust") {
        return Err(AppError::Conflict("Rust task ownership changed".into()));
    }
    let mixed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1 AND request_json->>'runtime'='rust-mixed-v1')")
        .bind(task_id).fetch_one(&mut *tx).await?;
    if mixed {
        return Err(AppError::Conflict(
            "mixed batch owns admission; use publish_all".into(),
        ));
    }
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
#[derive(Debug, Deserialize, Serialize)]
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
// Native mixed admission is deliberately opt-in: both consumers must be
// configured against the same original DB and media root, with Python stopped.
fn mixed_enabled() -> bool {
    crate::outbox_dispatcher::native_only()
        && std::env::var("VIDEOROLL_V2_ENABLE_NATIVE_MIXED_PUBLISH")
            .ok()
            .as_deref()
            == Some("true")
        && std::env::var("VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH")
            .ok()
            .as_deref()
            == Some("true")
}
async fn mixed_receipt(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    task: Uuid,
    fingerprint: &str,
) -> AppResult<Option<Value>> {
    let row=sqlx::query("SELECT id,request_json,expected_targets FROM publish_batches WHERE task_id=$1 AND request_json->>'runtime'='rust-mixed-v1' FOR UPDATE")
        .bind(task).fetch_optional(&mut **tx).await?;
    let Some(row) = row else { return Ok(None) };
    let request: Value = row.try_get("request_json")?;
    if request["fingerprint"].as_str() != Some(fingerprint) {
        return Err(AppError::Conflict(
            "mixed batch request differs from durable admission; reconcile before changing targets"
                .into(),
        ));
    }
    let batch: Uuid = row.try_get("id")?;
    let expected: Value = row.try_get("expected_targets")?;
    let keys = crate::bilibili_batch_reconcile_v1::keys(&expected)?;
    let jobs=sqlx::query("SELECT id,platform::text AS platform,account_id,state::text AS state,aid,bvid,external_id,external_url,response_json FROM publish_jobs WHERE task_id=$1 AND batch_id=$2")
        .bind(task).bind(batch).fetch_all(&mut **tx).await?;
    let mut results = serde_json::Map::new();
    let mut errors = serde_json::Map::new();
    let mut ok = 0;
    let mut published = 0;
    let mut seen = HashSet::new();
    for j in jobs {
        let p: String = j.try_get("platform")?;
        let a: Option<Uuid> = j.try_get("account_id")?;
        let key = format!(
            "{p}:{}",
            a.map(|v| v.to_string()).unwrap_or("default".into())
        );
        if !keys.contains(&key) || !seen.insert(key) {
            return Err(AppError::Conflict(
                "mixed batch receipt identity mismatch".into(),
            ));
        }
        let status: String = j.try_get("state")?;
        let response: Option<Value> = j.try_get("response_json")?;
        let external_id: Option<String> = j.try_get("external_id")?;
        let external_url: Option<String> = j.try_get("external_url")?;
        let confirmed = matches!(status.as_str(), "published" | "submitted")
            && (external_id.as_ref().is_some_and(|v| !v.is_empty())
                || external_url.as_ref().is_some_and(|v| !v.is_empty()));
        let uncertain = matches!(status.as_str(), "failed" | "unknown")
            || (matches!(status.as_str(), "published" | "submitted") && !confirmed);
        if uncertain {
            errors.insert(
                p.clone(),
                json!("publication failed or remains unverified; reconcile before retry"),
            );
        } else {
            ok += 1;
        }
        if confirmed {
            published += 1;
        }
        results.insert(p.clone(),json!({"platform":p,"job_id":j.try_get::<Uuid,_>("id")?,"state":status,"account_id":a,"aid":j.try_get::<Option<String>,_>("aid")?,"bvid":j.try_get::<Option<String>,_>("bvid")?,"external_id":external_id,"external_url":external_url,"response":response}));
    }
    let n = keys.len();
    if seen.len() != n {
        return Err(AppError::Conflict(
            "mixed batch has missing durable targets".into(),
        ));
    }
    Ok(Some(
        json!({"batch_id":batch,"results":results,"errors":errors,"platform_count":n,"ok_count":ok,"error_count":errors.len(),"all_accepted":ok==n,"has_any_accepted":ok>0,"all_published":published==n,"all_succeeded":published==n}),
    ))
}
async fn mixed_publish(state: &AppState, task: Uuid, input: PublishAllRequest) -> AppResult<Value> {
    if !mixed_enabled() {
        return Err(AppError::Conflict("native mixed publication requires native-only outbox and explicit native SAU/mixed opt-in".into()));
    }
    let platforms = input
        .platforms
        .as_ref()
        .ok_or_else(|| AppError::Invalid("explicit platforms required".into()))?;
    if platforms.len() < 2
        || platforms.len() > 4
        || input.skip_review
        || input.force_retry
        || input.fresh_batch
    {
        return Err(AppError::Invalid(
            "mixed publication requires 2–4 explicit targets without bypass or forced retry".into(),
        ));
    }
    let mut unique = HashSet::new();
    for p in platforms {
        if p != "bilibili" {
            platform(p)?;
        }
        if !unique.insert(p.as_str()) {
            return Err(AppError::Invalid("duplicate platform".into()));
        }
    }
    for k in input
        .account_ids
        .keys()
        .chain(input.platform_meta.keys())
        .chain(input.platform_options.keys())
    {
        if !unique.contains(k.as_str()) {
            return Err(AppError::Invalid("options for unrequested platform".into()));
        }
    }
    let canonical = serde_json::to_value(&input)
        .map_err(|_| AppError::Invalid("invalid mixed request".into()))?;
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(
            serde_json::to_vec(&canonical)
                .map_err(|_| AppError::Invalid("invalid mixed request".into()))?
        )
    );
    // Fast replay works after media retention or account rotation; no side effect.
    {
        let mut tx = state.db.pool.begin().await?;
        sqlx::query("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust' FOR UPDATE")
            .bind(task)
            .fetch_one(&mut *tx)
            .await?;
        if let Some(result) = mixed_receipt(&mut tx, task, &fingerprint).await? {
            tx.commit().await?;
            return Ok(result);
        }
    }
    shared_media_root(&state.config).await?;
    let video = asset_key(&state.db, task, "video_final", input.video_key.as_deref()).await?;
    let cover = match input.cover_key.as_deref() {
        Some(k) => Some(asset_key(&state.db, task, "cover_image", Some(k)).await?),
        None => None,
    };
    let path = state.storage.resolve_existing(&video).await?;
    crate::media::ffmpeg::probe_video(&state.config.ffprobe_binary, &path).await?;
    let mut expected = Vec::new();
    let mut socials = Vec::new();
    let mut bili = None;
    for p in platforms {
        let account = input
            .account_ids
            .get(p)
            .or(input.account_id.as_ref())
            .map(|v| {
                Uuid::parse_str(v).map_err(|_| AppError::Invalid("invalid account UUID".into()))
            })
            .transpose()?;
        let meta = input.platform_meta.get(p).or(input.meta.as_ref()).cloned();
        let options = input
            .platform_options
            .get(p)
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !options.is_object() || serde_json::to_vec(&options).map_or(true, |v| v.len() > 16384) {
            return Err(AppError::Invalid("invalid platform options".into()));
        }
        expected.push(json!({"platform":p,"account_id":account,"key":format!("{p}:{}",account.map(|v|v.to_string()).unwrap_or("default".into()))}));
        if p == "bilibili" {
            let saved = crate::publish_meta::saved(&state.db, task).await?;
            let supplied = meta.map(crate::publish_meta::normalized).transpose()?;
            if saved
                .as_ref()
                .zip(supplied.as_ref())
                .is_some_and(|(a, b)| a != b)
            {
                return Err(AppError::Conflict(
                    "save changed Bilibili draft first".into(),
                ));
            }
            let meta = saved
                .or(supplied)
                .ok_or_else(|| AppError::Invalid("Bilibili metadata missing".into()))?;
            if options
                .as_object()
                .unwrap()
                .keys()
                .any(|k| k != "typeid_mode")
            {
                return Err(AppError::Invalid("unsupported Bilibili option".into()));
            }
            let mode = options
                .get("typeid_mode")
                .map(|v| {
                    v.as_str()
                        .ok_or_else(|| AppError::Invalid("typeid_mode must be a string".into()))
                })
                .transpose()?
                .or(input.typeid_mode.as_deref())
                .unwrap_or("meta");
            bili=Some(crate::bilibili_publish_v1::prepare_mixed(state,json!({"task_id":task,"account_id":account,"video":{"type":"storage","key":video},"cover":cover.as_ref().map(|k|json!({"type":"storage","key":k})),"video_parts":input.video_parts.clone().unwrap_or_default(),"meta":meta,"typeid_mode":mode})).await?);
        } else {
            if !crate::publish_platforms::social_enabled(&state.config, &state.db, p).await? {
                return Err(AppError::Conflict("SAU target disabled".into()));
            }
            let account =
                account.ok_or_else(|| AppError::Invalid("explicit SAU account required".into()))?;
            let meta = crate::sau_publish_native_v1::normalize_meta(
                &meta.ok_or_else(|| AppError::Invalid("explicit SAU metadata required".into()))?,
                p,
            )?;
            crate::publish_review::ensure_approved(
                &state.db,
                &state.storage,
                &state.config,
                task,
                &meta,
            )
            .await?;
            socials.push((p.clone(), account, meta, options));
        }
    }
    let mut tx = state.db.pool.begin().await?;
    let status:Option<String>=sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 AND runtime_owner='rust' AND source_license::text<>'unknown' FOR UPDATE").bind(task).fetch_optional(&mut *tx).await?;
    if let Some(result) = mixed_receipt(&mut tx, task, &fingerprint).await? {
        tx.commit().await?;
        return Ok(result);
    }
    if !matches!(
        status.as_deref(),
        Some("rendered" | "approved" | "ready_for_review" | "subtitle_ready")
    ) {
        return Err(AppError::Conflict(
            "mixed admission requires a licensed, ready Rust task".into(),
        ));
    }
    let occupied:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1) OR EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1) OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1) OR EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown')) OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND stage='publish' AND state IN ('pending','running','awaiting','unknown','succeeded'))").bind(task).fetch_one(&mut *tx).await?;
    if occupied {
        return Err(AppError::Conflict("existing publication or active workflow requires reconciliation before mixed admission".into()));
    }
    let batch = Uuid::new_v4();
    sqlx::query("INSERT INTO publish_batches(id,task_id,state,expected_targets,request_json) VALUES($1,$2,'active',$3,$4)").bind(batch).bind(task).bind(json!(expected)).bind(json!({"runtime":"rust-mixed-v1","fingerprint":fingerprint,"platforms":platforms})).execute(&mut *tx).await?;
    // Shared original Bilibili admission validates P1/P2, file and ownership in
    // the SAME transaction. No commit or HTTP handoff until all targets exist.
    let mut bili = bili.ok_or_else(|| AppError::Invalid("Bilibili target required".into()))?;
    bili.bind_batch(batch);
    crate::bilibili_publish_v1::enqueue_prepared(state, &mut tx, bili, false).await?;
    // The shared native SAU enqueue persists original outbox + inbox atomically.
    for (p, account, meta, options) in socials {
        let name:Option<String>=sqlx::query_scalar("SELECT name FROM accounts WHERE id=$1 AND platform::text=$2 AND is_active AND check_state='valid' FOR UPDATE").bind(account).bind(&p).fetch_optional(&mut *tx).await?;
        let name = name.ok_or_else(|| {
            AppError::Conflict("SAU account missing, inactive or unvalidated".into())
        })?;
        crate::sau_login_native_v1::ensure_not_logging_in(&mut tx, &p, &name).await?;
        let id = Uuid::new_v4();
        let operation = format!("publish-job:{id}");
        let saved = json!({"platform":p,"account_id":account,"account_name":name,"video":{"type":"storage","key":video},"cover":cover.as_ref().map(|k|json!({"type":"storage","key":k})),"meta":meta,"platform_options":options});
        sqlx::query("INSERT INTO publish_jobs(id,task_id,batch_id,platform,account_id,cover_key,meta_json,state,operation_key) SELECT $1,$2,$3,platform,id,$4,$5,'submitting',$6 FROM accounts WHERE id=$7").bind(id).bind(task).bind(batch).bind(&cover).bind(saved).bind(&operation).bind(account).execute(&mut *tx).await?;
        crate::sau_worker_native_v1::enqueue(&mut tx, id, &operation).await?;
    }
    sqlx::query("UPDATE tasks SET active_publish_batch_id=$2,status='publishing',error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1").bind(task).bind(batch).execute(&mut *tx).await?;
    let result = mixed_receipt(&mut tx, task, &fingerprint)
        .await?
        .ok_or_else(|| AppError::Conflict("mixed admission missing batch".into()))?;
    tx.commit().await?;
    Ok(result)
}

async fn publish_all(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Json(input): Json<PublishAllRequest>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    if input
        .platforms
        .as_ref()
        .is_some_and(|p| p.iter().any(|v| v == "bilibili"))
    {
        return mixed_publish(&state, task_id, input).await.map(Json);
    }
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
