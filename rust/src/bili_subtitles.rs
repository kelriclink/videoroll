//! Durable independent Bilibili native-subtitle syncing.
//! A successful archive is NEVER un-published because subtitle syncing fails.
//! preSave is a non-idempotent boundary; uncertain outcomes require evidence.
use crate::{
    AppError, AppResult, Config, Database, api::AppState, domain::WorkItem, storage::AssetRegistry,
};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::{get, post},
};
use reqwest::{Client, Url, header};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::Digest;
use sqlx::{Postgres, Row, Transaction};
use std::{collections::HashSet, time::Duration};
use uuid::Uuid;

const MAX_SUBTITLE_SIZE: u64 = 12 * 1024 * 1024;
const MAX_ATTEMPTS: i32 = 8;
const LANGUAGE: &str = "zh";
const OWNER: &str = "bilibili_subtitle";

#[derive(Clone, Debug, Serialize, sqlx::FromRow)]
pub struct SyncRecord {
    pub publish_job_id: Uuid,
    pub task_id: Uuid,
    pub workflow_id: Uuid,
    pub source_key: String,
    pub source_checksum: Option<String>,
    pub aid: i64,
    pub bvid: String,
    pub expected_parts: i16,
    pub state: String,
    pub submitted: Value,
    pub attempts: i32,
    pub next_attempt_at: chrono::DateTime<chrono::Utc>,
    pub inflight_cid: Option<i64>,
    pub last_error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    limit: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct ResolveRequest {
    cid: i64,
    submitted: bool,
    evidence: String,
    location: Option<String>,
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/bilibili/subtitles", get(list))
        .route("/bilibili/subtitles/{id}", get(detail))
        .route("/bilibili/subtitles/{id}/resolve", post(resolve))
}
async fn list(
    State(state): State<AppState>,
    Query(query): Query<ListQuery>,
) -> AppResult<Json<Vec<SyncRecord>>> {
    let rows = sqlx::query_as::<_, SyncRecord>(
        "SELECT publish_job_id,task_id,workflow_id,source_key,source_checksum,aid,bvid,expected_parts,state,submitted,attempts,next_attempt_at,inflight_cid,last_error FROM vr2_bilibili_subtitle_syncs ORDER BY created_at DESC LIMIT $1"
    ).bind(query.limit.unwrap_or(50).clamp(1,200))
        .fetch_all(&state.db.pool).await?;
    Ok(Json(rows))
}
async fn detail(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<SyncRecord>> {
    let record=sqlx::query_as::<_,SyncRecord>(
        "SELECT publish_job_id,task_id,workflow_id,source_key,source_checksum,aid,bvid,expected_parts,state,submitted,attempts,next_attempt_at,inflight_cid,last_error FROM vr2_bilibili_subtitle_syncs WHERE publish_job_id=$1"
    ).bind(id).fetch_optional(&state.db.pool).await?
    .ok_or_else(||AppError::NotFound("subtitle sync job not found".into()))?;
    Ok(Json(record))
}

/// Only verified native Bilibili archives enter the outbox.
/// Used by the independent, post-publication outbox reconciler.
pub async fn enqueue_locked(
    tx: &mut Transaction<'_, Postgres>,
    item: &WorkItem,
    receipt: &Value,
) -> AppResult<bool> {
    if receipt.get("native_bilibili").and_then(Value::as_bool) != Some(true) {
        return Ok(false);
    }
    let aid = receipt
        .get("aid")
        .and_then(Value::as_i64)
        .filter(|v| *v > 0)
        .ok_or_else(|| {
            AppError::Invalid(
                "native Bilibili receipt must have a positive aid for subtitle sync".into(),
            )
        })?;
    let bvid = receipt
        .get("bvid")
        .and_then(Value::as_str)
        .filter(|v| v.starts_with("BV") && v.len() <= 32)
        .ok_or_else(|| AppError::Invalid("invalid native Bilibili bvid".into()))?;
    let expected_parts = receipt
        .get("parts")
        .and_then(Value::as_i64)
        .filter(|v| (1..=2).contains(v))
        .ok_or_else(|| AppError::Invalid("invalid Bilibili archive parts".into()))?;
    let source:Option<String>=sqlx::query_scalar(
        "SELECT a.storage_key FROM assets a JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live' WHERE a.task_id=$1 AND a.kind::text='subtitle_srt' AND a.storage_key LIKE 'v2/%' ORDER BY a.created_at DESC,a.id DESC LIMIT 1"
    ).bind(item.task_id).fetch_optional(&mut **tx).await?;
    let Some(source) = source else {
        // The source had no credible speech, or no verified SRT was generated.
        return Ok(false);
    };
    let inserted = sqlx::query(
        "UPDATE operation_inbox SET
           bili_sync_source_key=$4,bili_sync_aid=$5,bili_sync_bvid=$6,
           bili_sync_parts=$7,bili_sync_state='pending',bili_sync_created_at=now(),
           bili_sync_next_attempt_at=now(),bili_sync_reference_deadline=now()+interval '7 days',
           updated_at=now()
           WHERE id=$1 AND task_id=$2 AND workflow_id=$3
            AND stage='publish' AND runtime_engine='rust-v2' AND bili_sync_state IS NULL",
    )
    .bind(item.id)
    .bind(item.task_id)
    .bind(item.workflow_id)
    .bind(&source)
    .bind(aid)
    .bind(bvid)
    .bind(expected_parts as i16)
    .execute(&mut **tx)
    .await?
    .rows_affected()
        == 1;
    if inserted {
        // Retain only the SRT; regular workflow resource release is unaffected.
        sqlx::query(
            "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,$2,$3,'native_srt') ON CONFLICT DO NOTHING"
        ).bind(&source).bind(OWNER).bind(item.id).execute(&mut **tx).await?;
    }
    Ok(inserted)
}

/// Reconciliation also repairs a crash between external publication and outbox
/// discovery. It reads only confirmed Rust-native receipts, never v1 jobs.
pub async fn recover_confirmed(db: &Database, limit: i64) -> AppResult<u64> {
    let rows=sqlx::query(
        "SELECT j.id,j.workflow_id,j.task_id,j.generation,j.operation_key,j.result FROM vr2_jobs j WHERE j.stage='publish' AND j.state='succeeded' AND j.result->>'native_bilibili'='true' AND NOT EXISTS (SELECT 1 FROM vr2_bilibili_subtitle_syncs s WHERE s.publish_job_id=j.id) AND EXISTS (SELECT 1 FROM assets a JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live' WHERE a.task_id=j.task_id AND a.kind::text='subtitle_srt' AND a.storage_key LIKE 'v2/%') ORDER BY j.finished_at DESC LIMIT $1"
    ).bind(limit.clamp(1,40)).fetch_all(&db.pool).await?;
    let mut n = 0;
    for row in rows {
        let result: Value = row.try_get("result")?;
        let job = WorkItem {
            id: row.try_get("id")?,
            workflow_id: row.try_get("workflow_id")?,
            task_id: row.try_get("task_id")?,
            stage: crate::domain::Stage::Publish,
            generation: row.try_get("generation")?,
            operation_key: row.try_get("operation_key")?,
            lease_token: Uuid::nil(),
            attempts: 0,
            max_attempts: 1,
            payload: json!({}),
        };
        let mut tx = db.pool.begin().await?;
        match enqueue_locked(&mut tx, &job, &result).await {
            Ok(created) => {
                tx.commit().await?;
                if created {
                    n += 1;
                }
            }
            Err(error) => tracing::warn!(job_id=%job.id,error=%error,
                "native subtitle outbox reconciliation failed; published archive remains confirmed"),
        }
    }
    Ok(n)
}

fn api_base(config: &Config) -> AppResult<Url> {
    let raw = config
        .native_bilibili_api_base
        .as_deref()
        .unwrap_or("https://api.bilibili.com");
    let url = Url::parse(raw)
        .map_err(|_| AppError::Invalid("Bilibili subtitle API URL invalid".into()))?;
    validate_api_origin(&url)?;
    Ok(url)
}
fn validate_api_origin(url: &Url) -> AppResult<()> {
    let host = url.host_str().unwrap_or("");
    let official = url.scheme() == "https" && host == "api.bilibili.com" && url.port().is_none();
    let test = url.scheme() == "http"
        && matches!(host, "127.0.0.1" | "localhost" | "::1")
        && url.port().is_some();
    if !(official || test)
        || url.username() != ""
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(AppError::Invalid(
            "Bilibili API must be official HTTPS or isolated loopback test".into(),
        ));
    }
    Ok(())
}

fn endpoint(base: &Url, path: &str) -> AppResult<Url> {
    base.join(path)
        .map_err(|_| AppError::Invalid("invalid Bilibili endpoint".into()))
}
fn cookie_client(cookie: &str) -> AppResult<Client> {
    if cookie.trim().is_empty() || cookie.contains('\n') || cookie.contains('\r') {
        return Err(AppError::Invalid("Bilibili cookie invalid".into()));
    }
    let mut headers = header::HeaderMap::new();
    headers.insert(
        header::COOKIE,
        header::HeaderValue::from_str(cookie)
            .map_err(|_| AppError::Invalid("Bilibili cookie header invalid".into()))?,
    );
    headers.insert(
        header::REFERER,
        header::HeaderValue::from_static("https://member.bilibili.com/"),
    );
    Client::builder()
        .default_headers(headers)
        .user_agent("Mozilla/5.0 VideoRoll-Rust-V2")
        .timeout(Duration::from_secs(45))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(Into::into)
}
async fn bili_json(mut response: reqwest::Response) -> AppResult<Value> {
    if !response.status().is_success() {
        return Err(AppError::Unavailable(format!(
            "Bilibili subtitle API HTTP {}",
            response.status()
        )));
    }
    if response
        .content_length()
        .is_some_and(|len| len > 2 * 1024 * 1024)
    {
        return Err(AppError::Unavailable("Bilibili response too large".into()));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        if chunk.len() > (2 * 1024 * 1024usize).saturating_sub(bytes.len()) {
            return Err(AppError::Unavailable("Bilibili response too large".into()));
        }
        bytes.extend_from_slice(&chunk);
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Unavailable("invalid Bilibili response".into()))?;
    if value.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(AppError::Unavailable(format!(
            "Bilibili subtitle API rejected operation (code {})",
            value.get("code").and_then(Value::as_i64).unwrap_or(-1)
        )));
    }
    Ok(value)
}
fn archive_cids(value: &Value, expected: i16) -> AppResult<Vec<i64>> {
    let pages = value
        .pointer("/data/pages")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Unavailable("Bilibili archive pages not ready".into()))?;
    let ids = pages
        .iter()
        .map(|page| page.get("cid").and_then(Value::as_i64))
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| AppError::Unavailable("Bilibili pages have invalid cid".into()))?;
    let distinct = ids.iter().copied().collect::<HashSet<_>>();
    if ids.len() != expected as usize || ids.iter().any(|v| *v <= 0) || distinct.len() != ids.len()
    {
        return Err(AppError::Unavailable(
            "Bilibili pages not ready or do not match published parts".into(),
        ));
    }
    Ok(ids)
}
fn completed_token(cid: i64, sha: &str) -> String {
    format!("{cid}:{LANGUAGE}:{sha}")
}
async fn upload_srt(client: &Client, base: &Url, csrf: &str, raw: Vec<u8>) -> AppResult<String> {
    let file = reqwest::multipart::Part::bytes(raw)
        .file_name("videoroll.zh-Hans.srt")
        .mime_str("application/x-subrip")
        .map_err(|_| AppError::Invalid("invalid SRT content-type".into()))?;
    let form = reqwest::multipart::Form::new()
        .text("bucket", "subtitle")
        .text("csrf", csrf.to_string())
        .text("content_type", "application/x-subrip")
        .part("file", file);
    let url = endpoint(base, "x/upload/web/image")?;
    let response = client
        .post(url)
        .query(&[
            ("csrf", csrf),
            ("t", &chrono::Utc::now().timestamp_millis().to_string()),
        ])
        .multipart(form)
        .send()
        .await?;
    let parsed = bili_json(response).await?;
    let location = parsed
        .pointer("/data/location")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty() && s.len() < 4096)
        .ok_or_else(|| AppError::Unavailable("Bilibili subtitle upload has no location".into()))?;
    Ok(location.to_string())
}
async fn pre_save(
    client: &Client,
    base: &Url,
    csrf: &str,
    aid: i64,
    cid: i64,
    location: &str,
) -> AppResult<()> {
    let files = json!([{"url":location,"lan":LANGUAGE,"subtitle_id":0}]).to_string();
    let response = client
        .post(endpoint(base, "x/v2/dm/subtitle/draft/preSave")?)
        .query(&[
            ("csrf", csrf),
            ("t", &chrono::Utc::now().timestamp_millis().to_string()),
        ])
        .form(&[
            ("oid", cid.to_string()),
            ("aid", aid.to_string()),
            ("type", "1".into()),
            ("csrf", csrf.to_string()),
            ("files", files),
        ])
        .send()
        .await?;
    bili_json(response).await?;
    Ok(())
}
fn backoff_seconds(attempt: i32) -> i64 {
    (60_i64.saturating_mul(1i64 << attempt.saturating_sub(1).clamp(0, 5))).min(1800)
}

async fn mark_retry(db: &Database, record: &SyncRecord, token: Uuid, error: &str) -> AppResult<()> {
    let failed = record.attempts >= MAX_ATTEMPTS;
    let next = backoff_seconds(record.attempts);
    let mut tx = db.pool.begin().await?;
    sqlx::query(
        "UPDATE vr2_bilibili_subtitle_syncs SET state=$1,last_error=$2,next_attempt_at=now()+($3*interval '1 second'),lease_token=NULL,lease_until=NULL,inflight_cid=NULL,updated_at=now() WHERE publish_job_id=$4 AND lease_token=$5 AND state='running'"
    ).bind(if failed{"failed"} else {"retry"})
        .bind(error.chars().take(390).collect::<String>())
        .bind(next).bind(record.publish_job_id).bind(token)
        .execute(&mut *tx).await?;
    if failed {
        release_ref(&mut tx, record).await?;
    }
    tx.commit().await?;
    Ok(())
}
async fn release_ref(tx: &mut Transaction<'_, Postgres>, record: &SyncRecord) -> AppResult<()> {
    sqlx::query(
        "DELETE FROM storage_object_refs WHERE owner_type=$1 AND owner_id=$2 AND role='native_srt'",
    )
    .bind(OWNER)
    .bind(record.publish_job_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}
async fn mark_unknown(
    db: &Database,
    record: &SyncRecord,
    token: Uuid,
    error: &str,
) -> AppResult<()> {
    sqlx::query(
        "UPDATE vr2_bilibili_subtitle_syncs SET state='unknown',last_error=$1,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE publish_job_id=$2 AND lease_token=$3 AND state='submitting'"
    ).bind(error.chars().take(390).collect::<String>()).bind(record.publish_job_id)
        .bind(token).execute(&db.pool).await?;
    Ok(())
}

pub async fn reconcile_expired(db: &Database) -> AppResult<()> {
    sqlx::query(
        "UPDATE vr2_bilibili_subtitle_syncs SET state='unknown',lease_token=NULL,lease_until=NULL,last_error='subtitle draft preSave ownership expired; external status requires verification',updated_at=now() WHERE state='submitting' AND lease_until < now()"
    ).execute(&db.pool).await?;
    sqlx::query(
        "UPDATE vr2_bilibili_subtitle_syncs SET state='retry',lease_token=NULL,lease_until=NULL,next_attempt_at=now()+interval '60 seconds',last_error='subtitle preparation ownership expired',updated_at=now() WHERE state='running' AND lease_until < now()"
    ).execute(&db.pool).await?;
    // Prevent permanent multi-GB retention in indefinitely unreviewed states.
    // The database audit row remains intact.
    sqlx::query(
        "DELETE FROM storage_object_refs WHERE owner_type=$1 AND role='native_srt' AND owner_id IN (SELECT publish_job_id FROM vr2_bilibili_subtitle_syncs WHERE reference_deadline<now())"
    ).bind(OWNER).execute(&db.pool).await?;
    Ok(())
}
async fn claim(db: &Database) -> AppResult<Option<(SyncRecord, Uuid)>> {
    let token = Uuid::new_v4();
    let record=sqlx::query_as::<_,SyncRecord>(
        "WITH next AS (SELECT publish_job_id FROM vr2_bilibili_subtitle_syncs WHERE state IN ('pending','retry') AND next_attempt_at<=now() FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE vr2_bilibili_subtitle_syncs s SET state='running',attempts=attempts+1,lease_token=$1,lease_until=now()+interval '5 minutes',updated_at=now() FROM next WHERE s.publish_job_id=next.publish_job_id RETURNING s.publish_job_id,s.task_id,s.workflow_id,s.source_key,s.source_checksum,s.aid,s.bvid,s.expected_parts,s.state,s.submitted,s.attempts,s.next_attempt_at,s.inflight_cid,s.last_error"
    ).bind(token).fetch_optional(&db.pool).await?;
    Ok(record.map(|r| (r, token)))
}

async fn run_claimed(
    config: &Config,
    db: &Database,
    store: &AssetRegistry,
    record: &SyncRecord,
    token: Uuid,
) -> AppResult<()> {
    let base = api_base(config)?;
    let (cookie, csrf) = crate::bilibili_account_v1::confirmed_credentials(
        db,
        config,
        record.publish_job_id,
        record.task_id,
    )
    .await?;
    let (sha, size) = store.sha256(&record.source_key).await?;
    if size <= 0 || size as u64 > MAX_SUBTITLE_SIZE {
        return Err(AppError::Invalid("SRT source is empty or oversized".into()));
    }
    if record
        .source_checksum
        .as_ref()
        .is_some_and(|saved| saved != &sha)
    {
        return Err(AppError::Conflict(
            "SRT changed during Bilibili sync; manual review required".into(),
        ));
    }
    let source = store.resolve_existing(&record.source_key).await?;
    let raw = tokio::fs::read(source).await?;
    if raw.len() as i64 != size || hex::encode(sha2::Sha256::digest(&raw)) != sha {
        return Err(AppError::Conflict("SRT data changed during sync".into()));
    }
    sqlx::query(
        "UPDATE vr2_bilibili_subtitle_syncs SET source_checksum=$1 WHERE publish_job_id=$2 AND lease_token=$3 AND state='running' AND lease_until>now()"
    ).bind(&sha).bind(record.publish_job_id).bind(token).execute(&db.pool).await?;
    let client = cookie_client(&cookie)?;
    let page_data = bili_json(
        client
            .get(endpoint(&base, "x/web-interface/view")?)
            .query(&[("aid", record.aid)])
            .send()
            .await?,
    )
    .await?;
    if page_data
        .pointer("/data/aid")
        .and_then(Value::as_i64)
        .is_some_and(|aid| aid != record.aid)
        || page_data
            .pointer("/data/bvid")
            .and_then(Value::as_str)
            .is_some_and(|bvid| bvid != record.bvid)
    {
        return Err(AppError::Conflict(
            "subtitle archive identity mismatch".into(),
        ));
    }
    let cids = archive_cids(&page_data, record.expected_parts)?;
    for cid in cids {
        let token_key = completed_token(cid, &sha);
        if record.submitted.get(&token_key).is_some() {
            continue;
        }
        // Refresh the lease before every preparatory upload and refuse stale
        // claims before an external side effect.
        let renewed=sqlx::query("UPDATE vr2_bilibili_subtitle_syncs SET lease_until=now()+interval '5 minutes' WHERE publish_job_id=$1 AND lease_token=$2 AND state='running' AND lease_until>now()").bind(record.publish_job_id).bind(token).execute(&db.pool).await?.rows_affected();
        if renewed != 1 {
            return Err(AppError::Conflict("subtitle sync lease expired".into()));
        }
        let location = upload_srt(&client, &base, &csrf, raw.clone()).await?;
        // Durable irreversible boundary. Crash/timeout from here is UNKNOWN;
        // the worker must not create a second draft automatically.
        let updated=sqlx::query(
            "UPDATE vr2_bilibili_subtitle_syncs SET state='submitting',inflight_cid=$1,lease_until=now()+interval '5 minutes',updated_at=now() WHERE publish_job_id=$2 AND lease_token=$3 AND state='running' AND lease_until>now()"
        ).bind(cid).bind(record.publish_job_id).bind(token)
            .execute(&db.pool).await?.rows_affected();
        if updated != 1 {
            return Err(AppError::Conflict(
                "subtitle draft lease lost before submission".into(),
            ));
        }
        if let Err(error) = pre_save(&client, &base, &csrf, record.aid, cid, &location).await {
            mark_unknown(db, record, token, &error.to_string()).await?;
            return Ok(());
        }
        sqlx::query(
            "UPDATE vr2_bilibili_subtitle_syncs SET state='running',inflight_cid=NULL,submitted=jsonb_set(submitted,ARRAY[$1],$2::jsonb,true),updated_at=now() WHERE publish_job_id=$3 AND lease_token=$4 AND state='submitting'"
        ).bind(&token_key)
            .bind(json!({"cid":cid,"lan":LANGUAGE,"location":location}).to_string())
            .bind(record.publish_job_id).bind(token)
            .execute(&db.pool).await?;
    }
    let mut tx = db.pool.begin().await?;
    let completed=sqlx::query(
        "UPDATE vr2_bilibili_subtitle_syncs SET state='submitted',inflight_cid=NULL,lease_token=NULL,lease_until=NULL,last_error=NULL,updated_at=now() WHERE publish_job_id=$1 AND lease_token=$2 AND state='running'"
    ).bind(record.publish_job_id).bind(token).execute(&mut *tx).await?.rows_affected();
    if completed != 1 {
        return Err(AppError::Conflict("subtitle completion lease lost".into()));
    }
    release_ref(&mut tx, record).await?;
    tx.commit().await?;
    Ok(())
}
pub async fn tick(config: &Config, db: &Database, store: &AssetRegistry) -> AppResult<bool> {
    if !config.native_bilibili_allowed {
        return Ok(false);
    }
    reconcile_expired(db).await?;
    recover_confirmed(db, 30).await?;
    let Some((record, token)) = claim(db).await? else {
        return Ok(false);
    };
    if let Err(error) = run_claimed(config, db, store, &record, token).await {
        tracing::warn!(publish_job_id=%record.publish_job_id,error=%error,
            "native subtitle synchronization failed (publication unchanged)");
        mark_retry(db, &record, token, &error.to_string()).await?;
    }
    Ok(true)
}
async fn resolve(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(body): Json<ResolveRequest>,
) -> AppResult<Json<Value>> {
    let evidence = body.evidence.trim();
    if evidence.chars().count() < 12 || evidence.chars().count() > 1000 {
        return Err(AppError::Invalid(
            "documented manual verification evidence required".into(),
        ));
    }
    let mut tx = state.db.pool.begin().await?;
    let row=sqlx::query(
        "SELECT state,inflight_cid,source_checksum FROM vr2_bilibili_subtitle_syncs WHERE publish_job_id=$1 FOR UPDATE"
    ).bind(id).fetch_optional(&mut *tx).await?
        .ok_or_else(||AppError::NotFound("subtitle sync not found".into()))?;
    let status: String = row.try_get("state")?;
    let inflight: Option<i64> = row.try_get("inflight_cid")?;
    if status != "unknown" || inflight != Some(body.cid) {
        return Err(AppError::Conflict(
            "only matching uncertain subtitle draft can be reconciled".into(),
        ));
    }
    let sha: Option<String> = row.try_get("source_checksum")?;
    if body.submitted {
        let hash = sha.ok_or_else(|| AppError::Conflict("missing subtitle checksum".into()))?;
        let token = completed_token(body.cid, &hash);
        let location = body.location.unwrap_or_default();
        sqlx::query(
            "UPDATE vr2_bilibili_subtitle_syncs SET submitted=jsonb_set(submitted,ARRAY[$1],$2::jsonb,true),state='retry',inflight_cid=NULL,last_error=NULL,next_attempt_at=now(),updated_at=now() WHERE publish_job_id=$3"
        ).bind(token).bind(json!({"cid":body.cid,"lan":LANGUAGE,
            "location":location,"reconciled":true,"evidence":evidence}).to_string())
        .bind(id).execute(&mut *tx).await?;
    } else {
        sqlx::query(
            "UPDATE vr2_bilibili_subtitle_syncs SET state='retry',inflight_cid=NULL,last_error=NULL,next_attempt_at=now(),updated_at=now() WHERE publish_job_id=$1"
        ).bind(id).execute(&mut *tx).await?;
    }
    sqlx::query(
        "INSERT INTO workflow_events(id,workflow_id,kind,details) SELECT $1,workflow_id,'bilibili.subtitle_reconciled',$2 FROM vr2_bilibili_subtitle_syncs WHERE publish_job_id=$3"
    ).bind(Uuid::new_v4()).bind(json!({"cid":body.cid,
        "submitted":body.submitted,"evidence":evidence})).bind(id)
        .execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(json!({"status":"retry","reconciled":true})))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cids_match_archive_part_count() {
        let data = json!({"data":{"pages":[{"cid":12},{"cid":13}]}});
        assert_eq!(archive_cids(&data, 2).unwrap(), vec![12, 13]);
        assert!(archive_cids(&data, 1).is_err());
        assert!(archive_cids(&json!({"data":{"pages":[{"cid":12},{"cid":12}]}}), 2).is_err());
    }
    #[test]
    fn api_endpoint_is_not_arbitrary_ssrf() {
        for good in ["https://api.bilibili.com/", "http://127.0.0.1:18084/"] {
            assert!(
                validate_api_origin(&Url::parse(good).unwrap()).is_ok(),
                "{good}"
            );
        }
        for bad in [
            "http://api.bilibili.com/",
            "https://api.bilibili.com.evil.invalid/",
            "http://10.0.0.21:18084/",
            "http://127.0.0.1:18084/secret",
            "https://user@api.bilibili.com/",
            "https://api.bilibili.com/?redirect=https://evil.invalid",
        ] {
            assert!(
                validate_api_origin(&Url::parse(bad).unwrap()).is_err(),
                "{bad}"
            );
        }
        assert_eq!(backoff_seconds(1), 60);
        assert_eq!(backoff_seconds(9), 1800);
        assert_eq!(completed_token(12, "ab"), "12:zh:ab");
    }
}
