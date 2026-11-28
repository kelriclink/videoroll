//! Independent, at-most-once Bilibili append-after-publish operation.
//! Owner archive is read immediately before the irreversible edit. A crash
//! while uploading/submitting is NEVER silently replayed, and a confirmed P1
//! remains published independent of P2 success.
use crate::{
    AppError, AppResult, Config, Database, api::AppState, bilibili::NativeP2Client,
    legacy_contracts::read_task, storage::AssetRegistry,
};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::{get as route_get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

const OWNER: &str = "native_bilibili_p2";
const LOOKUP: &str = "SELECT publish_job_id,task_id,dubbing_job_id,aid,output_video_key,title,state,attempt,
    uploaded_filename,uploaded_cid,last_error,operator_evidence,claimed_at,finished_at,created_at,updated_at
    FROM vr2_bilibili_p2_appends";

#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct AppendRecord {
    publish_job_id: Uuid,
    task_id: Uuid,
    dubbing_job_id: Uuid,
    aid: i64,
    output_video_key: String,
    title: String,
    state: String,
    attempt: i32,
    uploaded_filename: Option<String>,
    uploaded_cid: Option<i64>,
    last_error: Option<String>,
    operator_evidence: Option<String>,
    claimed_at: Option<chrono::DateTime<chrono::Utc>>,
    finished_at: Option<chrono::DateTime<chrono::Utc>>,
    created_at: chrono::DateTime<chrono::Utc>,
    updated_at: chrono::DateTime<chrono::Utc>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AppendRequest {
    dubbing_job_id: Uuid,
    title: Option<String>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResolveRequest {
    /// Only mark failed on verified evidence that edit did not succeed.
    /// Setting published requires an owner-side CID match.
    published: bool,
    evidence: String,
}

pub(crate) fn trim_title(raw: Option<&str>, p1: &str) -> AppResult<String> {
    let result = raw
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| {
            let title = p1.trim();
            if let Some(rest) = title.strip_prefix("【熟肉】") {
                format!("【中配】{rest}")
            } else {
                format!("【中配】{title}")
            }
        });
    if result.is_empty() || result.chars().count() > 80 || result.chars().any(char::is_control) {
        return Err(AppError::Invalid(
            "P2 title must contain 1–80 printable characters".into(),
        ));
    }
    Ok(result)
}
fn receipt_aid(value: &Value) -> AppResult<i64> {
    if value.get("native_bilibili").and_then(Value::as_bool) != Some(true)
        || value.get("confirmed").and_then(Value::as_bool) != Some(true)
        || value.get("parts").and_then(Value::as_i64) != Some(1)
        || !value
            .get("bvid")
            .and_then(Value::as_str)
            .is_some_and(|v| v.starts_with("BV") && v.len() <= 32)
    {
        return Err(AppError::Conflict(
            "P2 append requires a confirmed native Bilibili single-part P1".into(),
        ));
    }
    value
        .get("aid")
        .and_then(Value::as_i64)
        .filter(|v| *v > 0)
        .ok_or_else(|| AppError::Conflict("confirmed P1 has no Bilibili aid".into()))
}
async fn verify_dubbing(db: &Database, task_id: Uuid, job_id: Uuid) -> AppResult<String> {
    let row = sqlx::query(
        "SELECT r.output_video_key FROM vr2_dubbing_runs r
         JOIN vr2_jobs j ON j.id=r.job_id AND j.stage='dubbing' AND j.state='succeeded'
         JOIN tasks m ON m.id=r.task_id AND m.runtime_owner='rust'
         JOIN assets a ON a.task_id=r.task_id AND a.kind='video_dubbed'::asset_kind
           AND a.storage_key=r.output_video_key
         JOIN storage_objects o ON o.storage_key=a.storage_key AND o.state='live'
         WHERE r.task_id=$1 AND r.job_id=$2 AND r.model_status='succeeded'
           AND r.source_video_key=(
             SELECT a0.storage_key FROM assets a0
             WHERE a0.task_id=$1 AND a0.kind='video_raw'::asset_kind
             ORDER BY a0.created_at DESC,a0.id DESC LIMIT 1)
           AND r.source_srt_key=(
             SELECT a1.storage_key FROM assets a1
             WHERE a1.task_id=$1 AND a1.kind='subtitle_srt'::asset_kind
             ORDER BY a1.created_at DESC,a1.id DESC LIMIT 1)
         LIMIT 1",
    )
    .bind(task_id)
    .bind(job_id)
    .fetch_optional(&db.pool)
    .await?
    .ok_or_else(|| {
        AppError::Conflict(
            "P2 requires verified same-task YouDub, current original video and current SRT".into(),
        )
    })?;
    let key:String=row.get("output_video_key");
    if !crate::storage::task_asset_key(task_id,&key){return Err(AppError::Conflict("P2 asset does not belong to task".into()));}
    Ok(key)
}
async fn get(
    State(state): State<AppState>,
    Path((task_id, publish_job_id)): Path<(Uuid, Uuid)>,
) -> AppResult<Json<AppendRecord>> {
    crate::legacy_contracts::read_visible_task(&state.db, task_id).await?;
    let mut record = sqlx::query_as::<_, AppendRecord>(&format!(
        "{LOOKUP} WHERE publish_job_id=$1 AND task_id=$2"
    ))
    .bind(publish_job_id)
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("P2 append not found".into()))?;
    if let Some(dub)=sqlx::query_scalar::<_,Uuid>("SELECT (request_json->>'historical_dubbing_id')::uuid FROM operation_inbox WHERE id=$1 AND runtime_engine='rust-historical-p2-append'").bind(publish_job_id).fetch_optional(&state.db.pool).await? {record.dubbing_job_id=dub;}
    Ok(Json(record))
}
async fn enqueue(
    State(state): State<AppState>,
    Path((task_id, publish_job_id)): Path<(Uuid, Uuid)>,
    Json(request): Json<AppendRequest>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, task_id).await?;
    if !state.config.native_bilibili_allowed
        || !crate::publish_platforms::enabled(&state.db, "bilibili").await?
    {
        return Err(AppError::Conflict(
            "native Bilibili publishing disabled".into(),
        ));
    }
    let historical: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE id=$1 AND task_id=$2)")
            .bind(publish_job_id)
            .bind(task_id)
            .fetch_one(&state.db.pool)
            .await?;
    if historical {
        return Ok(Json(
            crate::historical_p2_append::enqueue(
                &state.db,
                task_id,
                publish_job_id,
                request.dubbing_job_id,
                request.title.as_deref(),
            )
            .await?,
        ));
    }
    let video_key = verify_dubbing(&state.db, task_id, request.dubbing_job_id).await?;
    // Forbid enqueuing a different P2 when an existing intent is unresolved.
    let mut tx = state.db.pool.begin().await?;
    let owner:Option<Uuid>=sqlx::query_scalar("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust' FOR UPDATE").bind(task_id).fetch_optional(&mut *tx).await?;
    if owner.is_none(){return Err(AppError::Conflict("P2 task ownership changed".into()));}
    crate::storage::verify_original_read(&state.storage,&mut tx,task_id,"video_dubbed",&video_key,true).await?;
    let original = sqlx::query(
        "SELECT result,state FROM vr2_jobs j
         JOIN tasks m ON m.id=j.task_id AND m.runtime_owner='rust'
         WHERE j.id=$1 AND j.task_id=$2 AND j.stage='publish' FOR UPDATE OF j",
    )
    .bind(publish_job_id)
    .bind(task_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("native publish job not found".into()))?;
    let published_state: String = original.get("state");
    if published_state != "succeeded" {
        return Err(AppError::Conflict(
            "original archive must be confirmed published".into(),
        ));
    }
    let receipt: Value = original
        .try_get::<Option<Value>, _>("result")?
        .unwrap_or(Value::Null);
    let aid = receipt_aid(&receipt)?;
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(aid)
        .execute(&mut *tx)
        .await?;
    let other: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE aid=$1 AND publish_job_id<>$2)",
    )
    .bind(aid)
    .bind(publish_job_id)
    .fetch_one(&mut *tx)
    .await?;
    if other {
        return Err(AppError::Conflict(
            "another P2 intent owns the archive".into(),
        ));
    }
    let p1 = crate::publish_meta::saved(&state.db,&state.storage,task_id)
        .await?
        .and_then(|meta| meta.get("title").and_then(Value::as_str).map(str::to_owned))
        .unwrap_or_else(|| "已发布视频".into());
    let title = trim_title(request.title.as_deref(), &p1)?;
    let prior =
        sqlx::query_as::<_, AppendRecord>(&format!("{LOOKUP} WHERE publish_job_id=$1 FOR UPDATE"))
            .bind(publish_job_id)
            .fetch_optional(&mut *tx)
            .await?;
    if let Some(ref prior) = prior {
        if prior.task_id != task_id {
            return Err(AppError::Conflict("P2 append task mismatch".into()));
        }
        if prior.state != "failed" {
            if prior.dubbing_job_id == request.dubbing_job_id
                && prior.title == title
                && matches!(
                    prior.state.as_str(),
                    "queued" | "uploaded" | "uploading" | "submitting"
                )
            {
                return Ok(Json(json!({"publish_job_id":publish_job_id,"aid":aid,
                    "dubbing_job_id":request.dubbing_job_id,"state":prior.state,"attempt":prior.attempt})));
            }
            return Err(AppError::Conflict(
                "P2 append already exists; unknown/published edits must not be repeated".into(),
            ));
        }
        if prior.attempt >= 32 {
            return Err(AppError::Conflict("P2 attempt limit exceeded".into()));
        }
        sqlx::query(
            "UPDATE vr2_bilibili_p2_appends SET dubbing_job_id=$2,
            output_video_key=$3,title=$4,state='queued',attempt=attempt+1,
            uploaded_filename=NULL,uploaded_cid=NULL,last_error=NULL,operator_evidence=NULL,
            claimed_at=NULL,finished_at=NULL,updated_at=now() WHERE publish_job_id=$1",
        )
        .bind(publish_job_id)
        .bind(request.dubbing_job_id)
        .bind(&video_key)
        .bind(&title)
        .execute(&mut *tx)
        .await?;
        // The old asset reference must not survive a failed attempt with a
        // different P2 output, and must be replaced inside the same tx.
        sqlx::query("DELETE FROM storage_object_refs WHERE owner_type=$1 AND owner_id=$2")
            .bind(OWNER)
            .bind(publish_job_id)
            .execute(&mut *tx)
            .await?;
    } else {
        let changed = sqlx::query(
            "UPDATE operation_inbox SET
              p2_dubbing_job_id=$3,p2_aid=$4,p2_output_video_key=$5,
              p2_title=$6,p2_state='queued',p2_attempt=1,
              p2_created_at=now(),updated_at=now()
             WHERE id=$1 AND task_id=$2 AND stage='publish'
               AND runtime_engine='rust-v2' AND p2_state IS NULL",
        )
        .bind(publish_job_id)
        .bind(task_id)
        .bind(request.dubbing_job_id)
        .bind(aid)
        .bind(&video_key)
        .bind(&title)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        if changed != 1 {
            return Err(AppError::Conflict(
                "original publish stage no longer owns the Bilibili P2 append".into(),
            ));
        }
    }
    sqlx::query(
        "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
        VALUES($1,$2,$3,'p2_video') ON CONFLICT DO NOTHING",
    )
    .bind(&video_key)
    .bind(OWNER)
    .bind(publish_job_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"publish_job_id":publish_job_id,"aid":aid,
        "dubbing_job_id":request.dubbing_job_id,"state":"queued","attempt":
        prior.as_ref().map(|v|v.attempt+1).unwrap_or(1)})))
}
fn format_error(error: &impl std::fmt::Display) -> String {
    // Upstream responses may contain credentials or private URLs; neither are
    // persisted in the UI error field. Logs may include the opaque error type.
    let _ = error;
    "P2 upload or creator-center verification failed; inspect redacted worker log".into()
}
async fn update(
    db: &Database,
    id: Uuid,
    state: &str,
    filename: Option<&str>,
    cid: Option<i64>,
    error: Option<&str>,
) -> AppResult<()> {
    let changed=sqlx::query(
        "UPDATE vr2_bilibili_p2_appends SET state=$2,
          uploaded_filename=COALESCE($3,uploaded_filename),uploaded_cid=COALESCE($4,uploaded_cid),
          last_error=$5,updated_at=now(),
          finished_at=CASE WHEN $2 IN ('published','failed','unknown') THEN now() ELSE finished_at END
          WHERE publish_job_id=$1 AND state IN ('uploading','uploaded','submitting')"
    ).bind(id).bind(state).bind(filename).bind(cid).bind(error)
    .execute(&db.pool).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict(
            "P2 operation lost durable state ownership".into(),
        ));
    }
    if state == "published" || state == "failed" {
        sqlx::query("DELETE FROM storage_object_refs WHERE owner_type=$1 AND owner_id=$2")
            .bind(OWNER)
            .bind(id)
            .execute(&db.pool)
            .await?;
    }
    Ok(())
}
fn owned_p1(owner: &Value, aid: i64, recorded_cid: Option<i64>) -> AppResult<bool> {
    if owner.pointer("/archive/aid").and_then(Value::as_i64) != Some(aid) {
        return Err(AppError::Conflict("owner archive aid mismatch".into()));
    }
    let pages = owner
        .get("videos")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Conflict("owner archive video list missing".into()))?;
    if pages.len() == 1 {
        return Ok(false);
    }
    if pages.len() == 2
        && recorded_cid.is_some()
        && pages[1].get("cid").and_then(Value::as_i64) == recorded_cid
    {
        // Safe reconciliation: only the CID generated by THIS durable intent
        // can prove that our previous edit succeeded.
        return Ok(true);
    }
    Err(AppError::Conflict(
        "owner archive has unexpected parts; do not overwrite or append P3".into(),
    ))
}
async fn claim(db: &Database) -> AppResult<Option<AppendRecord>> {
    let mut tx = db.pool.begin().await?;
    let row = sqlx::query_as::<_, AppendRecord>(&format!(
        "{LOOKUP} WHERE state IN ('queued','uploaded') ORDER BY created_at
         FOR UPDATE SKIP LOCKED LIMIT 1"
    ))
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(ref row) = row {
        let next = if row.state == "uploaded" {
            "submitting"
        } else {
            "uploading"
        };
        sqlx::query(
            "UPDATE vr2_bilibili_p2_appends SET state=$2,
          claimed_at=now(),updated_at=now() WHERE publish_job_id=$1",
        )
        .bind(row.publish_job_id)
        .bind(next)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(row)
}
fn check_historical_p1(
    owner: &Value,
    evidence: Option<&crate::historical_p2_append::Evidence>,
) -> AppResult<()> {
    if let Some(ev) = evidence {
        if owner.pointer("/videos/0/cid").and_then(Value::as_i64) != Some(ev.cid)
            || owner
                .pointer("/archive/bvid")
                .and_then(Value::as_str)
                .is_some_and(|b| b != ev.bvid)
        {
            return Err(AppError::Conflict(
                "historical P1 owner-side identity mismatch".into(),
            ));
        }
    }
    Ok(())
}
async fn process(
    config: &Config,
    db: &Database,
    store: &AssetRegistry,
    row: &AppendRecord,
) -> AppResult<()> {
    let historical = crate::historical_p2_append::execution_evidence(
        db,
        store,
        row.publish_job_id,
        row.task_id,
        row.dubbing_job_id,
    )
    .await?;
    if historical
        .as_ref()
        .is_some_and(|e| e.aid != row.aid || e.key != row.output_video_key)
    {
        return Err(AppError::Conflict("historical P2 intent changed".into()));
    }
    let client = if historical.is_some() {
        NativeP2Client::new(config, db).await?
    } else {
        NativeP2Client::for_confirmed(config, db, row.publish_job_id, row.task_id).await?
    };
    if !crate::publish_platforms::enabled(db, "bilibili").await? {
        return Err(AppError::Conflict(
            "Bilibili disabled between enqueue and execution".into(),
        ));
    }
    let owner = client.owner(row.aid).await?;
    check_historical_p1(&owner, historical.as_ref())?;
    if owned_p1(&owner, row.aid, row.uploaded_cid)? {
        update(db, row.publish_job_id, "published", None, None, None).await?;
        return Ok(());
    }
    let (filename, cid) = if row.state == "uploaded" {
        (
            row.uploaded_filename
                .clone()
                .ok_or_else(|| AppError::Conflict("uploaded P2 is missing filename".into()))?,
            row.uploaded_cid
                .ok_or_else(|| AppError::Conflict("uploaded P2 is missing CID".into()))?,
        )
    } else {
        let path = store.resolve_existing(&row.output_video_key).await?;
        crate::media::ffmpeg::probe_video(&config.ffprobe_binary, &path).await?;
        crate::bilibili::append_admission(db, row.publish_job_id, true).await?;
        let receipt = match client.upload(&path).await {
            Ok(v) => v,
            Err(error) => {
                crate::bilibili::append_rate_limit(db, true, row.attempt, &error).await;
                return Err(error);
            }
        };
        // Persist immutable upload receipt separately from the irreversible edit.
        update(
            db,
            row.publish_job_id,
            "submitting",
            Some(&receipt.0),
            Some(receipt.1),
            None,
        )
        .await?;
        receipt
    };
    // Fetch again before changing external archive. Its metadata may have been
    // edited during the potentially large upload.
    crate::bilibili::append_admission(db, row.publish_job_id, false).await?;
    let current = client.owner(row.aid).await?;
    check_historical_p1(&current, historical.as_ref())?;
    if owned_p1(&current, row.aid, Some(cid))? {
        update(db, row.publish_job_id, "published", None, None, None).await?;
        return Ok(());
    }
    // No network call until the transition is durably committed; any crash
    // after this point is unknown and must not be replayed automatically.
    update(db, row.publish_job_id, "submitting", None, None, None).await?;
    if let Err(error) = client
        .edit(&current, row.aid, &filename, cid, &row.title)
        .await
    {
        crate::bilibili::append_rate_limit(db, false, row.attempt, &error).await;
        tracing::warn!(publish_job_id=%row.publish_job_id,error=%error,
            "Bilibili P2 archive edit outcome uncertain; do not retry");
        update(
            db,
            row.publish_job_id,
            "unknown",
            None,
            None,
            Some("P2 edit may have succeeded; verify author-side CID before resolving"),
        )
        .await?;
        return Ok(());
    }
    update(db, row.publish_job_id, "published", None, None, None).await
}
/// A post-publication P2 introduces another CID. If P1's independent native
/// subtitle sync already completed, re-open its outbox while retaining the
/// P1 per-CID proof. We do not rewrite uncertain/inflight subtitle drafts.
async fn reconcile_p2_subtitles(db: &Database, limit: i64) -> AppResult<u64> {
    let records = sqlx::query(
        "SELECT a.publish_job_id,a.task_id,a.dubbing_job_id
         FROM vr2_bilibili_p2_appends a
         JOIN vr2_bilibili_subtitle_syncs s ON s.publish_job_id=a.publish_job_id
         WHERE a.state='published' AND s.expected_parts=1
           AND s.state IN ('submitted','pending','retry')
         ORDER BY a.finished_at LIMIT $1",
    )
    .bind(limit.clamp(1, 100))
    .fetch_all(&db.pool)
    .await?;
    let mut reopened = 0;
    for row in records {
        let id: Uuid = row.get("publish_job_id");
        let task: Uuid = row.get("task_id");
        let dubbing: Uuid = row.get("dubbing_job_id");
        let mut tx = db.pool.begin().await?;
        // Serialize with the V2 garbage collector. The reference cannot be
        // added after the object has entered the 'deleting' state.
        let source: Option<String> = sqlx::query_scalar(
            "SELECT o.storage_key FROM storage_objects o
             JOIN vr2_bilibili_subtitle_syncs s ON s.source_key=o.storage_key
             JOIN vr2_dubbing_runs r ON r.source_srt_key=s.source_key
             WHERE s.publish_job_id=$1 AND s.task_id=$2
               AND r.job_id=$3 AND r.task_id=$2 AND o.state='live'
             FOR UPDATE OF o LIMIT 1",
        )
        .bind(id)
        .bind(task)
        .bind(dubbing)
        .fetch_optional(&mut *tx)
        .await?;
        if let Some(source) = source {
            let changed = sqlx::query(
                "UPDATE vr2_bilibili_subtitle_syncs
                 SET expected_parts=2,
                   state=CASE WHEN state='submitted' THEN 'pending' ELSE state END,
                   attempts=0,next_attempt_at=now(),lease_token=NULL,lease_until=NULL,
                   reference_deadline=now()+interval '7 days',updated_at=now()
                 WHERE publish_job_id=$1 AND task_id=$2 AND source_key=$3
                   AND expected_parts=1 AND state IN ('submitted','pending','retry')",
            )
            .bind(id)
            .bind(task)
            .bind(&source)
            .execute(&mut *tx)
            .await?
            .rows_affected();
            if changed == 1 {
                sqlx::query(
                    "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
                     VALUES($1,'bilibili_subtitle',$2,'native_srt') ON CONFLICT DO NOTHING",
                )
                .bind(&source)
                .bind(id)
                .execute(&mut *tx)
                .await?;
                reopened += 1;
            }
        }
        tx.commit().await?;
    }
    Ok(reopened)
}

/// Process at most one independent P2 intent. The main publisher never waits
/// for this secondary edit, and P1 remains confirmed.
pub async fn tick(config: &Config, db: &Database, store: &AssetRegistry) -> AppResult<bool> {
    let Some(row) = claim(db).await? else {
        return Ok(false);
    };
    if let Err(error) = process(config, db, store, &row).await {
        tracing::warn!(publish_job_id=%row.publish_job_id,error=%error,
            "P2 append pre-edit operation failed");
        // If post-edit state has already been persisted to 'unknown',
        // it will never be automatically reset.
        let current: Option<String> =
            sqlx::query_scalar("SELECT state FROM vr2_bilibili_p2_appends WHERE publish_job_id=$1")
                .bind(row.publish_job_id)
                .fetch_optional(&db.pool)
                .await?;
        if matches!(current.as_deref(), Some("uploading" | "uploaded")) {
            update(
                db,
                row.publish_job_id,
                "failed",
                None,
                None,
                Some(&format_error(&error)),
            )
            .await?;
        } else if current.as_deref() == Some("submitting") {
            update(
                db,
                row.publish_job_id,
                "unknown",
                None,
                None,
                Some("P2 edit outcome unknown; manual verification required"),
            )
            .await?;
        }
    }
    if let Err(error) = reconcile_p2_subtitles(db, 30).await {
        tracing::warn!(error=%error,"P2 native subtitle extension deferred, archive remains published");
    }
    Ok(true)
}
/// A restart after a process crash must never silently rerun a P2 whose
/// uploaded file or archive edit may have reached Bilibili.
pub async fn reconcile_stale(db: &Database) -> AppResult<u64> {
    if let Err(error) = reconcile_p2_subtitles(db, 30).await {
        tracing::warn!(error=%error,"P2 subtitle outbox extension deferred");
    }
    let count = sqlx::query(
        "UPDATE vr2_bilibili_p2_appends SET state='unknown',
          last_error='P2 worker stopped during external side effect; manual verification required',
          updated_at=now()
         WHERE state IN ('uploading','submitting')
           AND claimed_at<now()-interval '30 minutes'",
    )
    .execute(&db.pool)
    .await?
    .rows_affected();
    Ok(count)
}
async fn resolve(
    State(state): State<AppState>,
    Path((task_id, publish_job_id)): Path<(Uuid, Uuid)>,
    Json(request): Json<ResolveRequest>,
) -> AppResult<Json<AppendRecord>> {
    read_task(&state.db, task_id).await?;
    if request.evidence.trim().chars().count() < 20 || request.evidence.chars().count() > 4096 {
        return Err(AppError::Invalid(
            "P2 resolution requires 20–4096 characters of evidence".into(),
        ));
    }
    let row = sqlx::query_as::<_, AppendRecord>(&format!(
        "{LOOKUP} WHERE publish_job_id=$1 AND task_id=$2"
    ))
    .bind(publish_job_id)
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("P2 append not found".into()))?;
    if row.state != "unknown" {
        return Err(AppError::Conflict(
            "only unknown P2 outcome can be manually resolved".into(),
        ));
    }
    let historical = crate::historical_p2_append::execution_evidence(
        &state.db,
        &state.storage,
        row.publish_job_id,
        row.task_id,
        row.dubbing_job_id,
    )
    .await?;
    if historical.is_some() && !request.published {
        return Err(AppError::Conflict("historical unknown P2 requires verified owner-side publication; failed replay remains disabled".into()));
    }
    if request.published {
        let cid = row.uploaded_cid.ok_or_else(|| {
            AppError::Conflict("P2 CID unavailable; cannot prove publication".into())
        })?;
        let client = if historical.is_some() {
            NativeP2Client::new(&state.config, &state.db).await?
        } else {
            NativeP2Client::for_confirmed(&state.config, &state.db, row.publish_job_id, row.task_id)
                .await?
        };
        let owner = client.owner(row.aid).await?;
        check_historical_p1(&owner, historical.as_ref())?;
        if !owned_p1(&owner, row.aid, Some(cid))? {
            return Err(AppError::Conflict(
                "author-side archive does not have the expected P2 CID".into(),
            ));
        }
    }
    let next = if request.published {
        "published"
    } else {
        "failed"
    };
    let changed = sqlx::query(
        "UPDATE vr2_bilibili_p2_appends SET state=$2,operator_evidence=$3,
         finished_at=now(),last_error=NULL,updated_at=now()
         WHERE publish_job_id=$1 AND task_id=$4 AND state='unknown'",
    )
    .bind(publish_job_id)
    .bind(next)
    .bind(&request.evidence)
    .bind(task_id)
    .execute(&state.db.pool)
    .await?
    .rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict(
            "P2 resolution was concurrently modified".into(),
        ));
    }
    if !request.published {
        // Only operator-certified failure permits a subsequent distinct P2
        // request; this decision and its evidence remain in the row.
        sqlx::query("DELETE FROM storage_object_refs WHERE owner_type=$1 AND owner_id=$2")
            .bind(OWNER)
            .bind(publish_job_id)
            .execute(&state.db.pool)
            .await?;
    } else {
        sqlx::query("DELETE FROM storage_object_refs WHERE owner_type=$1 AND owner_id=$2")
            .bind(OWNER)
            .bind(publish_job_id)
            .execute(&state.db.pool)
            .await?;
    }
    get(State(state), Path((task_id, publish_job_id))).await
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/tasks/{id}/publish_jobs/{publish_job_id}/append_p2",
            post(enqueue),
        )
        .route(
            "/tasks/{id}/publish_jobs/{publish_job_id}/p2",
            route_get(get),
        )
        .route(
            "/tasks/{id}/publish_jobs/{publish_job_id}/p2/resolve",
            post(resolve),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn p2_title_and_native_receipt_are_strict() {
        assert_eq!(trim_title(None, "【熟肉】hello").unwrap(), "【中配】hello");
        assert_eq!(trim_title(None, "hello").unwrap(), "【中配】hello");
        assert!(trim_title(Some(&"x".repeat(81)), "").is_err());
        assert!(
            receipt_aid(&json!({"confirmed":true,"native_bilibili":true,"parts":1,"aid":42,"bvid":"BVmockTestId"}))
                .is_ok()
        );
        assert!(
            receipt_aid(&json!({"confirmed":true,"native_bilibili":true,"parts":2,"aid":42,"bvid":"BVmockTestId"}))
                .is_err()
        );
        assert!(receipt_aid(&json!({"confirmed":true,"parts":1,"aid":42})).is_err());
    }
    #[test]
    fn only_recorded_cid_can_prove_prior_append() {
        let single = json!({"archive":{"aid":25},"videos":[{"cid":7}]});
        assert!(!owned_p1(&single, 25, None).unwrap());
        let double = json!({"archive":{"aid":25},"videos":[{"cid":7},{"cid":9}]});
        assert!(owned_p1(&double, 25, Some(9)).unwrap());
        assert!(owned_p1(&double, 25, Some(11)).is_err());
        assert!(owned_p1(&double, 25, None).is_err());
    }
}
