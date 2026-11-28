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
             WHERE a.task_id=$1 AND a.kind::text=$2 AND a.storage_key=$3
             AND (a.storage_key NOT LIKE 'v2/%' OR EXISTS(SELECT 1 FROM storage_objects o WHERE o.storage_key=a.storage_key AND o.state='live')) LIMIT 1",
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
             WHERE a.task_id=$1 AND a.kind::text=$2
             AND (a.storage_key NOT LIKE 'v2/%' OR EXISTS(SELECT 1 FROM storage_objects o WHERE o.storage_key=a.storage_key AND o.state='live'))
             ORDER BY a.created_at DESC,a.id DESC LIMIT 1",
        )
        .bind(task)
        .bind(kind)
        .fetch_optional(&db.pool)
        .await?
    };
    let key=key.ok_or_else(|| AppError::NotFound(format!("task {kind} asset missing for SAU publication")))?;
    if !crate::storage::task_asset_key(task,&key){return Err(AppError::Conflict("SAU asset key does not match task".into()));}
    Ok(key)
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
    if mixed_enabled() {
        let request: PublishAllRequest = serde_json::from_value(json!({
            "platforms":[platform],"account_ids":{(platform):account.to_string()},
            "platform_meta":{(platform):meta},"platform_options":{(platform):options},
            "video_key":body.video_key,"cover_key":body.cover_key
        }))
        .map_err(|_| AppError::Invalid("invalid single SAU batch request".into()))?;
        let result = mixed_publish(state, task_id, request).await?;
        let mut job = result["results"][platform].clone();
        job["batch_id"] = result["batch_id"].clone();
        return Ok(job);
    }

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
    crate::storage::verify_original_read(&state.storage,&mut tx,task_id,"video_final",&video,true).await?;
    if let Some(k)=cover.as_deref(){crate::storage::verify_original_read(&state.storage,&mut tx,task_id,"cover_image",k,true).await?;}
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
// Original batch-backed SAU jobs are visible through the older facade list/detail as well.
const NATIVE_SOCIAL_VIEW: &str = "SELECT jsonb_build_object('job_id',p.id,'sau_job_id',p.id,'batch_id',p.batch_id,'platform',p.platform::text,'account_id',p.account_id,'state',CASE WHEN p.state::text='submitted' AND p.response_json->>'receipt_verified'='false' AND NULLIF(p.external_id,'') IS NULL AND NULLIF(p.external_url,'') IS NULL THEN 'unknown' ELSE p.state::text END,'external_id',p.external_id,'external_url',p.external_url,'response',NULL,'attempt',1) FROM publish_jobs p JOIN publish_batches b ON b.id=p.batch_id AND b.task_id=p.task_id WHERE p.task_id=$1 AND b.request_json->>'runtime'='rust-mixed-v1' AND p.platform::text IN ('douyin','xiaohongshu','kuaishou')";
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
    let native = sqlx::query_scalar::<_, Value>(&format!(
        "{NATIVE_SOCIAL_VIEW} ORDER BY p.created_at DESC,p.id DESC LIMIT 100"
    ))
    .bind(task_id)
    .fetch_all(&state.db.pool)
    .await?;
    let mut result: Vec<Value> = records.iter().map(visible).collect();
    result.extend(native);
    result.truncate(100);
    Ok(Json(result))
}
async fn get(
    State(state): State<AppState>,
    Path((task_id, id)): Path<(Uuid, Uuid)>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    if let Some(native) =
        sqlx::query_scalar::<_, Value>(&format!("{NATIVE_SOCIAL_VIEW} AND p.id=$2"))
            .bind(task_id)
            .bind(id)
            .fetch_optional(&state.db.pool)
            .await?
    {
        return Ok(Json(native));
    }
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
    let store=AssetRegistry::new(db.clone(),config.storage_root.clone());
    let mut tx=db.pool.begin().await?;
    crate::storage::verify_original_read(&store,&mut tx,record.task_id,"video_final",&record.video_key,false).await?;
    if let Some(k)=record.cover_key.as_deref(){crate::storage::verify_original_read(&store,&mut tx,record.task_id,"cover_image",k,false).await?;}
    tx.commit().await?;
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
pub(crate) fn mixed_enabled() -> bool {
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
    admit_batch(state, task, input, None).await
}
async fn admit_batch(
    state: &AppState,
    task: Uuid,
    input: PublishAllRequest,
    parent: Option<&crate::domain::WorkItem>,
) -> AppResult<Value> {
    if !mixed_enabled() {
        return Err(AppError::Conflict("native mixed publication requires native-only outbox and explicit native SAU/mixed opt-in".into()));
    }
    let platforms = input
        .platforms
        .as_ref()
        .ok_or_else(|| AppError::Invalid("explicit platforms required".into()))?;
    let has_bili = platforms.iter().any(|p| p == "bilibili");
    if !has_bili {
        batch_platforms(&input)?;
    }
    if platforms.len() < if has_bili && parent.is_none() { 2 } else { 1 }
        || platforms.len() > 4
        || input.skip_review
        || input.force_retry
        || input.fresh_batch
    {
        return Err(AppError::Invalid(
            "native publication requires explicit targets without bypass or forced retry".into(),
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
    if parent.is_none() {
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
            if parent.is_none()
                && saved
                    .as_ref()
                    .zip(supplied.as_ref())
                    .is_some_and(|(a, b)| a != b)
            {
                return Err(AppError::Conflict(
                    "save changed Bilibili draft first".into(),
                ));
            }
            let meta = (if parent.is_some() {
                supplied.or(saved)
            } else {
                saved.or(supplied)
            })
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
    if parent.is_none() {
        if let Some(result) = mixed_receipt(&mut tx, task, &fingerprint).await? {
            tx.commit().await?;
            return Ok(result);
        }
    }
    if let Some(item) = parent {
        if item.task_id != task || item.stage != crate::domain::Stage::Publish {
            return Err(AppError::Conflict(
                "invalid automatic publication parent".into(),
            ));
        }
        let flow:Option<String>=sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 AND task_id=$2 AND generation=$3 AND current_stage='publish' FOR UPDATE").bind(item.workflow_id).bind(task).bind(item.generation).fetch_optional(&mut *tx).await?;
        let job:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_jobs WHERE id=$1 AND workflow_id=$2 AND task_id=$3 AND stage='publish' AND state='running' AND lease_token=$4 AND lease_until>now() FOR UPDATE").bind(item.id).bind(item.workflow_id).bind(task).bind(item.lease_token).fetch_optional(&mut *tx).await?;
        if flow.as_deref() != Some("running") || job != Some(item.id) {
            return Err(AppError::Conflict(
                "automatic publish stage lease lost".into(),
            ));
        }
    }

    if !matches!(
        status.as_deref(),
        Some("rendered" | "approved" | "ready_for_review" | "subtitle_ready")
    ) {
        return Err(AppError::Conflict(
            "mixed admission requires a licensed, ready Rust task".into(),
        ));
    }
    let occupied:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1) OR EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1) OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1) OR EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND ($2::uuid IS NULL OR id<>$2) AND state IN ('running','paused','unknown')) OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND ($3::uuid IS NULL OR id<>$3) AND stage='publish' AND state IN ('pending','running','awaiting','unknown','succeeded'))").bind(task).bind(parent.map(|p|p.workflow_id)).bind(parent.map(|p|p.id)).fetch_one(&mut *tx).await?;
    if occupied {
        return Err(AppError::Conflict("existing publication or active workflow requires reconciliation before mixed admission".into()));
    }
    let batch = Uuid::new_v4();
    sqlx::query("INSERT INTO publish_batches(id,task_id,state,expected_targets,request_json) VALUES($1,$2,'active',$3,$4)").bind(batch).bind(task).bind(json!(expected)).bind(json!({"runtime":"rust-mixed-v1","fingerprint":fingerprint,"platforms":platforms,"auto_parent":parent.map(|p|json!({"workflow_id":p.workflow_id,"job_id":p.id,"generation":p.generation}))})).execute(&mut *tx).await?;
    // Shared original Bilibili admission validates P1/P2, file and ownership in
    // the SAME transaction. No commit or HTTP handoff until all targets exist.
    if let Some(mut bili) = bili {
        bili.bind_batch(batch);
        crate::bilibili_publish_v1::enqueue_prepared(
            state,
            &mut tx,
            bili,
            false,
            parent.map(|p| p.workflow_id),
        )
        .await?;
    }
    // Recheck ownership under the same task lock, after any concurrent asset
    // deletion/GC. Do not admit a dangling file or a foreign storage key.
    for (kind, key) in std::iter::once(("video_final", video.as_str()))
        .chain(cover.as_deref().map(|k| ("cover_image", k)))
    {
        crate::storage::verify_original_read(&state.storage,&mut tx,task,kind,key,true).await?;
        let valid:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets a JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live' WHERE a.task_id=$1 AND a.kind::text=$2 AND a.storage_key=$3)").bind(task).bind(kind).bind(key).fetch_one(&mut *tx).await?;
        if !valid {
            return Err(AppError::Conflict(
                "native publication asset no longer available".into(),
            ));
        }
        let p = state.storage.resolve_existing(key).await?;
        let m = tokio::fs::metadata(p).await?;
        if !m.is_file() || m.len() == 0 {
            return Err(AppError::Conflict(
                "native publication file missing or empty".into(),
            ));
        }
    }

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
    if let Some(item) = parent {
        let ticket = format!("native-batch:{batch}");
        sqlx::query("UPDATE vr2_jobs SET state='awaiting',external_ticket=$2,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1").bind(item.id).bind(&ticket).execute(&mut *tx).await?;
        crate::scheduler::Scheduler::event(
            &mut tx,
            item.workflow_id,
            Some(item.id),
            "job.awaiting",
            json!({"ticket":ticket}),
        )
        .await?;
    }
    let result = mixed_receipt(&mut tx, task, &fingerprint)
        .await?
        .ok_or_else(|| AppError::Conflict("mixed admission missing batch".into()))?;
    tx.commit().await?;
    Ok(result)
}

/// Freeze the original explicit after_render request. Unlike automatic
/// profile selection, missing/invalid targets are never silently expanded.
pub(crate) fn validate_after_render(raw: Value) -> AppResult<Value> {
    let mut input: PublishAllRequest = serde_json::from_value(raw)
        .map_err(|_| AppError::Invalid("invalid after_render PublishAllRequest".into()))?;
    let platforms = input
        .platforms
        .as_ref()
        .ok_or_else(|| AppError::Invalid("explicit after_render platforms required".into()))?;
    if platforms.is_empty()
        || platforms.len() > 4
        || input.skip_review
        || input.force_retry
        || input.fresh_batch
        || input.video_parts.as_ref().is_some_and(|v| !v.is_empty())
    {
        return Err(AppError::Invalid("after_render requires explicit targets without bypass, forced retry, fresh batch or preselected P2".into()));
    }
    input.video_parts = None;
    let mut unique = HashSet::new();
    for p in platforms {
        if p != "bilibili" {
            platform(p)?;
        }
        if !unique.insert(p.as_str()) {
            return Err(AppError::Invalid("duplicate after_render platform".into()));
        }
        let account = input.account_ids.get(p).or(input.account_id.as_ref());
        if let Some(a) = account {
            Uuid::parse_str(a)
                .map_err(|_| AppError::Invalid("invalid after_render account UUID".into()))?;
        }
        if p != "bilibili" && account.is_none() {
            return Err(AppError::Invalid(
                "explicit SAU account required for after_render".into(),
            ));
        }
        if let Some(meta) = input.platform_meta.get(p).or(input.meta.as_ref()) {
            if p == "bilibili" {
                crate::publish_meta::normalized(meta.clone())?;
            } else {
                crate::sau_publish_native_v1::normalize_meta(meta, p)?;
            }
        } else if p != "bilibili" {
            return Err(AppError::Invalid(
                "explicit SAU metadata required for after_render".into(),
            ));
        }
    }
    for k in input
        .account_ids
        .keys()
        .chain(input.platform_meta.keys())
        .chain(input.platform_options.keys())
    {
        if !unique.contains(k.as_str()) {
            return Err(AppError::Invalid(
                "after_render settings for unrequested platform".into(),
            ));
        }
    }
    if !platforms.iter().any(|v| v == "bilibili") && input.typeid_mode.is_some() {
        return Err(AppError::Invalid("typeid_mode requires Bilibili".into()));
    }
    if let Some(mode) = input.typeid_mode.as_deref() {
        if !matches!(mode, "meta" | "bilibili_predict" | "ai_summary") {
            return Err(AppError::Invalid(
                "unsupported after_render typeid_mode".into(),
            ));
        }
    }
    for options in input.platform_options.values() {
        if !options.is_object() || serde_json::to_vec(options).map_or(true, |v| v.len() > 16384) {
            return Err(AppError::Invalid(
                "invalid after_render platform options".into(),
            ));
        }
    }
    if input.video_key.as_deref() == Some("") {
        input.video_key = None;
    }
    if input.cover_key.as_deref() == Some("") {
        input.cover_key = None;
    }
    for key in input.video_key.iter().chain(input.cover_key.iter()) {
        AssetRegistry::checked_key(key)?;
    }
    serde_json::to_value(input)
        .map_err(|_| AppError::Invalid("after_render payload encode failed".into()))
}
/// Pin the original input separately from the output publication request.
pub(crate) async fn explicit_input(
    db: &Database,
    registry: &AssetRegistry,
    task: Uuid,
    flow: Uuid,
) -> AppResult<Option<String>> {
    let config: Value =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1 AND task_id=$2")
            .bind(flow)
            .bind(task)
            .fetch_one(&db.pool)
            .await?;
    if config.get("after_render_publish").is_none() {
        return Ok(None);
    }
    let pinned: crate::subtitle_resume_v1::Artifact =
        serde_json::from_value(config["after_render_input"].clone()).map_err(|_| {
            AppError::Conflict("explicit publication missing original input digest".into())
        })?;
    let current = crate::media::ffmpeg::latest_asset_key(db, task, "video_raw").await?;
    if current.as_deref() != Some(pinned.key.as_str())
        || crate::subtitle_resume_v1::asset_digest(db, registry, task, "video_raw", &pinned.key)
            .await?
            != pinned
    {
        return Err(AppError::Conflict(
            "explicit after_render original video changed".into(),
        ));
    }
    Ok(Some(pinned.key))
}
pub(crate) async fn explicit_render_inputs(
    db: &Database,
    registry: &AssetRegistry,
    task: Uuid,
    flow: Uuid,
) -> AppResult<Option<(String, Option<String>, Option<String>)>> {
    let Some(raw) = explicit_input(db, registry, task, flow).await? else {
        return Ok(None);
    };
    let row=sqlx::query("SELECT id,result FROM vr2_jobs WHERE workflow_id=$1 AND task_id=$2 AND stage='subtitle' AND state='succeeded'").bind(flow).bind(task).fetch_optional(&db.pool).await?.ok_or_else(||AppError::Conflict("explicit publication render lacks current subtitle receipt".into()))?;
    let job: Uuid = row.try_get("id")?;
    let result: Value = row.try_get("result")?;
    if result["no_speech"].as_bool() == Some(true) {
        return Ok(Some((raw, None, None)));
    }
    let ass = format!("v2/sub/{task}/{job}/subtitle.ass");
    let srt = format!("v2/sub/{task}/{job}/subtitle.srt");
    if (!result["ass_key"].is_null() && result["ass_key"].as_str()!=Some(ass.as_str()))
        || result["srt_key"].as_str() != Some(srt.as_str())
    {
        return Err(AppError::Conflict(
            "explicit publication requires this generation's subtitle assets".into(),
        ));
    }
    let ass=if result["ass_key"].is_null(){None}else{crate::subtitle_resume_v1::asset_digest(db,registry,task,"subtitle_ass",&ass).await?;Some(ass)};
    crate::subtitle_resume_v1::asset_digest(db, registry, task, "subtitle_srt", &srt).await?;
    Ok(Some((raw, ass, Some(srt))))
}
pub(crate) async fn explicit_after_render(
    state: &AppState,
    item: &crate::domain::WorkItem,
    raw: Value,
) -> AppResult<crate::domain::StageOutcome> {
    explicit_input(&state.db, &state.storage, item.task_id, item.workflow_id)
        .await?
        .ok_or_else(|| AppError::Conflict("explicit publication input missing".into()))?;
    let mut input: PublishAllRequest = serde_json::from_value(validate_after_render(raw)?)
        .map_err(|_| AppError::Invalid("invalid stored after_render payload".into()))?;
    let receipt:Option<Value>=sqlx::query_scalar("SELECT result FROM vr2_jobs WHERE workflow_id=$1 AND task_id=$2 AND generation=$3 AND stage='render' AND state='succeeded'").bind(item.workflow_id).bind(item.task_id).bind(item.generation).fetch_optional(&state.db.pool).await?.flatten();
    let receipt = receipt.ok_or_else(|| {
        AppError::Conflict(
            "after_render requires this generation's successful render receipt".into(),
        )
    })?;
    let key = receipt["storage_key"]
        .as_str()
        .ok_or_else(|| AppError::Conflict("render receipt missing storage key".into()))?;
    if receipt["reused"].as_bool() == Some(true)
        || input.video_key.as_deref().is_some_and(|v| v != key)
    {
        return Err(AppError::Conflict(
            "after_render video must match this generation's render receipt".into(),
        ));
    }
    let key = asset_key(&state.db, item.task_id, "video_final", Some(key)).await?;
    let (sha, size) = state.storage.sha256(&key).await?;
    // Local render results include SHA/size; remote results name the
    // original execution and uploaded asset. Validate that exact receipt.
    if receipt["remote_completed"].as_bool() == Some(true) {
        let execution = receipt["remote_execution_id"]
            .as_str()
            .and_then(|v| Uuid::parse_str(v).ok())
            .ok_or_else(|| AppError::Conflict("remote render execution missing".into()))?;
        let asset = receipt["asset_id"]
            .as_str()
            .and_then(|v| Uuid::parse_str(v).ok())
            .ok_or_else(|| AppError::Conflict("remote render asset receipt missing".into()))?;
        let exact:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM render_executions e JOIN assets a ON a.id=$2 JOIN vr2_jobs j ON j.id=e.job_id WHERE e.id=$1 AND e.task_id=$3 AND e.workflow_id=$4 AND e.runtime_engine='rust-v2' AND e.state='completed' AND j.stage='render' AND j.state='succeeded' AND j.result=$5 AND e.output_json=$5 AND a.task_id=$3 AND a.kind='video_final' AND a.storage_key=$6 AND a.sha256=$7 AND a.size_bytes=$8)").bind(execution).bind(asset).bind(item.task_id).bind(item.workflow_id).bind(&receipt).bind(&key).bind(&sha).bind(size).fetch_one(&state.db.pool).await?;
        if !exact {
            return Err(AppError::Conflict(
                "remote render receipt or asset digest mismatch".into(),
            ));
        }
    } else if receipt["sha256"].as_str() != Some(sha.as_str())
        || receipt["size_bytes"].as_i64() != Some(size)
    {
        return Err(AppError::Conflict(
            "after_render receipt media digest mismatch".into(),
        ));
    }
    let owned:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets a JOIN storage_objects o ON o.storage_key=a.storage_key JOIN storage_object_refs r ON r.storage_key=a.storage_key WHERE a.task_id=$1 AND a.kind='video_final' AND a.storage_key=$2 AND a.sha256=$3 AND a.size_bytes=$4 AND o.state='live' AND o.checksum_sha256=$3 AND o.size_bytes=$4 AND r.owner_type='workflow' AND r.owner_id=$5 AND r.role='video_final')").bind(item.task_id).bind(&key).bind(&sha).bind(size).bind(item.workflow_id).fetch_one(&state.db.pool).await?;
    if !owned {
        return Err(AppError::Conflict(
            "after_render rendered asset ownership mismatch".into(),
        ));
    }
    input.video_key = Some(key);
    let result = admit_batch(state, item.task_id, input, Some(item)).await?;
    let batch = result["batch_id"]
        .as_str()
        .ok_or_else(|| AppError::Conflict("after_render batch receipt missing".into()))?;
    Ok(crate::domain::StageOutcome::Awaiting {
        ticket: format!("native-batch:{batch}"),
    })
}

/// Runtime selection follows the original auto box. All target parameters
/// freeze in the original batch transaction, under the exact parent lease.
pub(crate) async fn automatic(
    state: &AppState,
    item: &crate::domain::WorkItem,
    platforms: Vec<String>,
) -> AppResult<crate::domain::StageOutcome> {
    let profile = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
    let mut meta = crate::publish_meta::automatic_draft(state, item.task_id).await?;
    let has_bili = platforms.iter().any(|p| p == "bilibili");
    let mut account_ids = HashMap::new();
    let mut platform_meta = HashMap::new();
    let source_meta =
        crate::media::ffmpeg::latest_asset_key(&state.db, item.task_id, "metadata_json").await?;
    let author = if let Some(key) = source_meta {
        let path = state.storage.resolve_existing(&key).await?;
        if tokio::fs::metadata(&path).await?.len() > 2 * 1024 * 1024 {
            return Err(AppError::Invalid("source metadata oversized".into()));
        }
        let raw: Value = serde_json::from_slice(&tokio::fs::read(path).await?)
            .map_err(|_| AppError::Invalid("source metadata invalid".into()))?;
        raw["uploader"].as_str().unwrap_or("").to_owned()
    } else {
        String::new()
    };
    let no_speech:bool=sqlx::query_scalar("SELECT COALESCE((SELECT result->>'no_speech'='true' FROM vr2_jobs WHERE workflow_id=$1 AND stage='subtitle' AND state='succeeded' ORDER BY created_at DESC LIMIT 1),false)").bind(item.workflow_id).fetch_one(&state.db.pool).await?;
    if no_speech {
        let prefix = profile["publish_title_prefix"].as_str().unwrap_or("");
        if !prefix.is_empty() {
            if let Some(title) = meta["title"].as_str().and_then(|t| t.strip_prefix(prefix)) {
                meta["title"] = json!(title.trim());
            }
        }
    }
    if has_bili {
        let external: bool =
            sqlx::query_scalar("SELECT source_type::text='youtube' FROM tasks WHERE id=$1")
                .bind(item.task_id)
                .fetch_optional(&state.db.pool)
                .await?
                .unwrap_or(false);
        let explicit = meta["tags"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        // The old draft's generic fallback is not a user tag and must not
        // suppress creator-center recommendations.
        let explicit = if explicit.len() == 1 && explicit[0] == "视频分享" {
            Vec::new()
        } else {
            explicit
        };
        meta["tags"] = json!(
            crate::bilibili_account_v1::resolve_publish_tags(
                &state.db,
                &state.config,
                item.task_id,
                meta["typeid"].as_u64().unwrap_or(0) as u32,
                meta["title"].as_str().unwrap_or(""),
                meta["desc"].as_str().unwrap_or(""),
                external,
                &explicit
            )
            .await
        );
    }
    for p in &platforms {
        if p == "bilibili" {
            platform_meta.insert(p.clone(), crate::publish_meta::normalized(meta.clone())?);
            continue;
        }
        platform(p)?;
        let account:Option<Uuid>=sqlx::query_scalar("SELECT id FROM accounts WHERE platform::text=$1 AND is_active AND check_state='valid' ORDER BY created_at DESC,id DESC LIMIT 1").bind(p).fetch_optional(&state.db.pool).await?;
        let account = account
            .ok_or_else(|| AppError::Conflict(format!("no active validated account for {p}")))?;
        account_ids.insert(p.clone(), account.to_string());
        let mut m = meta.clone();
        if p == "douyin" && !author.trim().is_empty() {
            m["desc"] = json!(format!("原作者：{}", author.trim()));
        }
        platform_meta.insert(
            p.clone(),
            crate::sau_publish_native_v1::normalize_meta(&m, p)?,
        );
    }
    let video = asset_key(&state.db, item.task_id, "video_final", None).await?;
    let cover = if profile["publish_use_youtube_cover"].as_bool() == Some(true) {
        crate::media::ffmpeg::latest_asset_key(&state.db, item.task_id, "cover_image").await?
    } else {
        None
    };
    let config: Value = sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
        .bind(item.workflow_id)
        .fetch_one(&state.db.pool)
        .await?;
    let selected = config
        .get("auto_publish_dubbing_job_id")
        .filter(|v| !v.is_null())
        .map(|v| {
            v.as_str()
                .and_then(|s| Uuid::parse_str(s).ok())
                .ok_or_else(|| AppError::Invalid("invalid selected automatic P2 job".into()))
        })
        .transpose()?;
    if selected.is_some() && (!has_bili || no_speech) {
        return Err(AppError::Conflict(
            "selected P2 incompatible with current targets or no-speech source".into(),
        ));
    }
    let mut parts = None;
    if has_bili
        && !no_speech
        && (selected.is_some() || profile["auto_dub_p2"].as_bool() == Some(true))
    {
        let dub:Option<String>=sqlx::query_scalar("SELECT r.output_video_key FROM vr2_dubbing_runs r JOIN vr2_jobs j ON j.id=r.job_id WHERE (($3::uuid IS NULL AND j.workflow_id=$1) OR j.id=$3) AND j.task_id=$2 AND j.stage='dubbing' AND j.state='succeeded' AND r.model_status='succeeded' ORDER BY j.created_at DESC LIMIT 1").bind(item.workflow_id).bind(item.task_id).bind(selected).fetch_optional(&state.db.pool).await?.flatten();
        if selected.is_some() && dub.is_none() {
            return Err(AppError::Conflict(
                "selected P2 receipt no longer succeeded".into(),
            ));
        }
        if let Some(dub) = dub {
            let main = meta["title"].as_str().unwrap_or("").trim();
            if main.is_empty() {
                return Err(AppError::Invalid(
                    "automatic P2 requires original title".into(),
                ));
            }
            let p1_prefix = profile["publish_title_prefix"]
                .as_str()
                .unwrap_or("【熟肉】")
                .trim();
            let raw = profile["auto_dub_p2_prefix"]
                .as_str()
                .unwrap_or("【中配】")
                .trim();
            let marker = if raw.is_empty() {
                "中配"
            } else {
                raw.strip_prefix('【')
                    .and_then(|v| v.strip_suffix('】'))
                    .unwrap_or(raw)
                    .trim()
            };
            if marker.is_empty()
                || marker.chars().count() > 20
                || marker
                    .chars()
                    .any(|c| c.is_control() || matches!(c, '【' | '】' | '/' | '\\'))
            {
                return Err(AppError::Invalid("invalid P2 title prefix".into()));
            }
            let stripped = if p1_prefix.is_empty() {
                main
            } else {
                main.strip_prefix(p1_prefix).unwrap_or(main).trim_start()
            };
            let p2 = format!(
                "【{marker}】{}",
                if stripped.is_empty() {
                    "中文配音"
                } else {
                    stripped
                }
            );
            let combined = if main.starts_with("【熟肉&中配】") {
                main.to_owned()
            } else {
                let original = if !p1_prefix.is_empty() && main.starts_with(p1_prefix) {
                    stripped
                } else {
                    main.strip_prefix("【熟肉】").unwrap_or(main).trim_start()
                };
                format!("【熟肉&中配】{original}")
            };
            let title = if combined.chars().count() > 80 {
                format!("{}…", combined.chars().take(79).collect::<String>())
            } else {
                combined
            };
            platform_meta
                .get_mut("bilibili")
                .ok_or_else(|| AppError::Conflict("Bilibili metadata missing".into()))?["title"] =
                json!(title);
            parts = Some(vec![
                json!({"video":{"type":"storage","key":video},"title":main.chars().take(80).collect::<String>()}),
                json!({"video":{"type":"storage","key":dub},"title":p2.chars().take(80).collect::<String>()}),
            ]);
        }
    }
    let input = PublishAllRequest {
        account_id: None,
        platforms: Some(platforms),
        account_ids,
        video_key: Some(video),
        video_parts: parts,
        cover_key: cover,
        typeid_mode: if has_bili {
            Some(
                profile["publish_typeid_mode"]
                    .as_str()
                    .unwrap_or("meta")
                    .into(),
            )
        } else {
            None
        },
        meta: None,
        platform_meta,
        platform_options: HashMap::new(),
        skip_review: false,
        force_retry: false,
        fresh_batch: false,
    };
    let result = admit_batch(state, item.task_id, input, Some(item)).await?;
    let batch = result["batch_id"]
        .as_str()
        .ok_or_else(|| AppError::Conflict("automatic batch receipt missing".into()))?;
    Ok(crate::domain::StageOutcome::Awaiting {
        ticket: format!("native-batch:{batch}"),
    })
}
async fn publish_all(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Json(input): Json<PublishAllRequest>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    if mixed_enabled()
        || input
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
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManualReceipt {
    platform: String,
    account_id: Uuid,
    external_id: String,
    evidence: String,
    /// Explicit operator attestation, not an automated platform API check.
    confirmed_in_creator_center: bool,
}
fn receipt_id(p: &str, raw: &str) -> AppResult<String> {
    let s = raw.trim();
    let valid = match p {
        "douyin" => (12..=32).contains(&s.len()) && s.bytes().all(|c| c.is_ascii_digit()),
        "xiaohongshu" => s.len() == 24 && s.bytes().all(|c| c.is_ascii_hexdigit()),
        "kuaishou" => {
            (6..=64).contains(&s.len())
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        }
        _ => false,
    };
    if !valid {
        return Err(AppError::Invalid(
            "invalid platform work ID; use the original creator-center ID, not a URL".into(),
        ));
    }
    Ok(if p == "xiaohongshu" {
        s.to_ascii_lowercase()
    } else {
        s.to_owned()
    })
}
async fn resolve_mixed_receipt(
    State(state): State<AppState>,
    Path((task, job)): Path<(Uuid, Uuid)>,
    Json(body): Json<ManualReceipt>,
) -> AppResult<Json<Value>> {
    if !mixed_enabled() {
        return Err(AppError::Conflict(
            "native mixed publication is not enabled".into(),
        ));
    }
    let external_id = receipt_id(&body.platform, &body.external_id)?;
    let evidence = body.evidence.trim();
    if !body.confirmed_in_creator_center
        || !(12..=2000).contains(&evidence.chars().count())
        || evidence.chars().any(|c| c.is_control() && c != '\n')
    {
        return Err(AppError::Invalid(
            "explicit creator-center confirmation and 12–2000 characters of evidence required"
                .into(),
        ));
    }
    let mut tx = state.db.pool.begin().await?;
    let parent=sqlx::query("SELECT status::text AS status,runtime_owner,active_publish_batch_id FROM tasks WHERE id=$1 FOR UPDATE").bind(task).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::NotFound("task not found".into()))?;
    if parent.try_get::<String, _>("runtime_owner")? != "rust"
        || parent.try_get::<String, _>("status")? == "canceled"
    {
        return Err(AppError::Conflict(
            "task is not an active Rust-owned publication".into(),
        ));
    }
    // Same Task -> Account -> Job -> Inbox/Outbox -> Batch order as worker.
    let account: Option<Uuid> =
        sqlx::query_scalar("SELECT id FROM accounts WHERE id=$1 AND platform::text=$2 FOR UPDATE")
            .bind(body.account_id)
            .bind(&body.platform)
            .fetch_optional(&mut *tx)
            .await?;
    if account.is_none() {
        return Err(AppError::Conflict(
            "receipt account/platform mismatch".into(),
        ));
    }
    let row=sqlx::query("SELECT id,batch_id,platform::text AS platform,account_id,state::text AS state,operation_key,started_at,lease_owner,lease_until,response_json,external_id,external_url FROM publish_jobs WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(job).bind(task).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::NotFound("original publish job not found".into()))?;
    if row.try_get::<String, _>("platform")? != body.platform
        || row.try_get::<Option<Uuid>, _>("account_id")? != Some(body.account_id)
    {
        return Err(AppError::Conflict("receipt job identity mismatch".into()));
    }
    let batch = row.try_get::<Option<Uuid>, _>("batch_id")?.ok_or_else(|| {
        AppError::Conflict("only original native mixed batches are supported".into())
    })?;
    if parent.try_get::<Option<Uuid>, _>("active_publish_batch_id")? != Some(batch) {
        return Err(AppError::Conflict(
            "publication batch no longer owns this task".into(),
        ));
    }
    let key: Option<String> = row.try_get("operation_key")?;
    if key.as_deref() != Some(format!("publish-job:{job}").as_str()) {
        return Err(AppError::Conflict(
            "original SAU operation key mismatch".into(),
        ));
    }
    let key = key.unwrap();
    let proof:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key=$1 AND runtime_engine='rust-sau-publish' AND status='completed' AND lease_owner IS NULL AND lease_until IS NULL AND request_json->>'job_id'=$2 AND result_json->>'job_id'=$2 AND result_json->>'status' IN ('submitted','unknown')) AND (SELECT count(*) FROM outbox_events WHERE operation_key=$1)=1 AND EXISTS(SELECT 1 FROM outbox_events WHERE operation_key=$1 AND aggregate_id=$2 AND task_name='social_publisher.process_job' AND status='native_completed' AND lease_owner IS NULL AND lease_until IS NULL)").bind(&key).bind(job.to_string()).fetch_one(&mut *tx).await?;
    if !proof
        || row
            .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("started_at")?
            .is_none()
        || row.try_get::<Option<String>, _>("lease_owner")?.is_some()
        || row
            .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("lease_until")?
            .is_some()
    {
        return Err(AppError::Conflict(
            "original browser execution has not settled or lacks start evidence".into(),
        ));
    }
    let batch_row=sqlx::query("SELECT expected_targets,request_json FROM publish_batches WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(batch).bind(task).fetch_one(&mut *tx).await?;
    let req: Value = batch_row.try_get("request_json")?;
    let expected: Value = batch_row.try_get("expected_targets")?;
    let keys = crate::bilibili_batch_reconcile_v1::keys(&expected)?;
    if req["runtime"] != "rust-mixed-v1"
        || !keys.contains(&format!("{}:{}", body.platform, body.account_id))
    {
        return Err(AppError::Conflict(
            "receipt is not an expected native mixed target".into(),
        ));
    }
    let current: Vec<(String, Option<Uuid>)> = sqlx::query_as(
        "SELECT platform::text,account_id FROM publish_jobs WHERE task_id=$1 AND batch_id=$2",
    )
    .bind(task)
    .bind(batch)
    .fetch_all(&mut *tx)
    .await?;
    let identities: HashSet<String> = current
        .iter()
        .map(|(p, a)| {
            format!(
                "{p}:{}",
                a.map(|v| v.to_string()).unwrap_or("default".into())
            )
        })
        .collect();
    if current.len() != keys.len()
        || identities.len() != keys.len()
        || keys.iter().any(|k| !identities.contains(k))
    {
        return Err(AppError::Conflict(
            "mixed target ledger incomplete or duplicated".into(),
        ));
    }
    let status: String = row.try_get("state")?;
    let response: Option<Value> = row.try_get("response_json")?;
    let mut response = response.unwrap_or(Value::Null);
    let prior: Option<String> = row.try_get("external_id")?;
    let prior_url: Option<String> = row.try_get("external_url")?;
    let attest = json!({"source":"operator_creator_center","platform":body.platform,"account_id":body.account_id,"external_id":external_id,"evidence":evidence,"confirmed_in_creator_center":true});
    if status == "published"
        && response.get("operator_receipt") == Some(&attest)
        && prior.as_deref() == Some(external_id.as_str())
    {
        tx.commit().await?;
        return Ok(Json(
            json!({"job_id":job,"batch_id":batch,"state":"published","external_id":external_id,"verification":"operator_creator_center"}),
        ));
    }
    if !matches!(status.as_str(), "submitted" | "unknown")
        || response["runtime"] != "rust"
        || response["driver"] != "sau"
        || response["receipt_verified"] != false
        || response.get("operator_receipt").is_some()
        || prior.as_deref().is_some_and(|s| !s.is_empty())
        || prior_url.as_deref().is_some_and(|s| !s.is_empty())
    {
        return Err(AppError::Conflict(
            "only an unresolved native SAU receipt can be attested once".into(),
        ));
    }
    // Serializes identical work IDs even across different tasks. No external
    // URL fetching, browser action, credential use or submission replay.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
        .bind(format!(
            "videoroll:sau-receipt:{}:{external_id}",
            body.platform
        ))
        .execute(&mut *tx)
        .await?;
    let duplicate:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE platform::text=$1 AND external_id=$2 AND id<>$3)").bind(&body.platform).bind(&external_id).bind(job).fetch_one(&mut *tx).await?;
    if duplicate {
        return Err(AppError::Conflict(
            "platform work ID already belongs to another original publish job".into(),
        ));
    }
    // Preserve original worker response, including receipt_verified=false.
    // Manual attestation is explicitly different from automated verification.
    response["operator_receipt"] = attest;
    response["operator_confirmed_at"] = json!(chrono::Utc::now());
    sqlx::query("UPDATE publish_jobs SET state='published',external_id=$2,response_json=$3,finished_at=now(),updated_at=now() WHERE id=$1").bind(job).bind(&external_id).bind(response).execute(&mut *tx).await?;
    crate::bilibili_batch_reconcile_v1::reconcile_native_sau(&mut tx, task, batch).await?;
    tx.commit().await?;
    Ok(Json(
        json!({"job_id":job,"batch_id":batch,"state":"published","external_id":external_id,"verification":"operator_creator_center"}),
    ))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{id}/actions/publish_all", post(publish_all))
        .route(
            "/tasks/{id}/publish_jobs/{job_id}/sau/resolve",
            post(resolve_mixed_receipt),
        )
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
