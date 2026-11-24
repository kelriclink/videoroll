//! Original Python `POST /bilibili/publish` contract, backed by native Rust
//! publisher Stage and original publish_jobs/operation_inbox tables.
//! No Celery dispatch and no claim of completed upload at enqueue time.
use crate::{AppError, AppResult, api::AppState, storage::AssetRegistry};
use axum::{Json, Router, extract::State, routing::post};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

#[derive(Debug, Deserialize, Serialize, Clone)]
struct MediaRef {
    #[serde(default = "storage_type")]
    r#type: String,
    key: String,
}
fn storage_type() -> String {
    "storage".into()
}
#[derive(Debug, Deserialize, Serialize, Clone)]
struct VideoPart {
    video: MediaRef,
    title: String,
}
#[derive(Debug, Deserialize)]
struct Request {
    task_id: Uuid,
    #[serde(default)]
    batch_id: Option<Uuid>,
    #[serde(default)]
    account_id: Option<Uuid>,
    #[serde(default)]
    force_retry: bool,
    #[serde(default = "predict_mode")]
    typeid_mode: String,
    video: MediaRef,
    #[serde(default)]
    video_parts: Vec<VideoPart>,
    #[serde(default)]
    cover: Option<MediaRef>,
    meta: Value,
}
fn predict_mode() -> String {
    "bilibili_predict".into()
}
fn normalize_meta(value: &Value) -> AppResult<crate::bilibili::ArchiveMeta> {
    let original = value
        .as_object()
        .ok_or_else(|| AppError::Invalid("Bilibili meta must be an object".into()))?;
    if serde_json::to_vec(original)
        .map_err(|_| AppError::Invalid("invalid Bilibili metadata".into()))?
        .len()
        > 64 * 1024
    {
        return Err(AppError::Invalid("Bilibili meta exceeds 64 KB".into()));
    }
    let mut map: Map<String, Value> = original.clone();
    map.entry("copyright").or_insert(json!(1));
    if !map.contains_key("typeid") {
        if let Some(v) = map.remove("tid") {
            map.insert("typeid".into(), v);
        }
    }
    if !map.contains_key("tags") {
        if let Some(v) = map.remove("tag").or_else(|| map.remove("keywords")) {
            map.insert("tags".into(), v);
        }
    }
    // The original Pydantic input normalizes both string and array tags.
    if let Some(Value::String(tags)) = map.get("tags") {
        let tags = tags
            .replace('，', ",")
            .split(',')
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .map(str::to_owned)
            .collect::<Vec<_>>();
        map.insert("tags".into(), json!(tags));
    }
    if map.get("tags").and_then(Value::as_array).is_some() {
        let mut dedup = Vec::<String>::new();
        for raw in map.get("tags").and_then(Value::as_array).unwrap() {
            let value = raw
                .as_str()
                .ok_or_else(|| AppError::Invalid("Bilibili tags must be strings".into()))?
                .trim();
            if !value.is_empty() && !dedup.iter().any(|s| s == value) {
                dedup.push(value.to_owned());
            }
        }
        map.insert("tags".into(), json!(dedup));
    }
    // Do not pass aliases or unknown user metadata into Bilibili API.
    map.remove("tid");
    map.remove("tag");
    map.remove("keywords");
    let meta: crate::bilibili::ArchiveMeta = serde_json::from_value(Value::Object(map))
        .map_err(|_| AppError::Invalid("Bilibili publish metadata fields invalid".into()))?;
    meta.validate()?;
    Ok(meta)
}
fn check_ref(input: &MediaRef) -> AppResult<&str> {
    if input.r#type != "storage" {
        return Err(AppError::Invalid(
            "native Bilibili requires storage inputs; S3 bridge not configured".into(),
        ));
    }
    if !input.key.starts_with("v2/") {
        return Err(AppError::Invalid(
            "native Bilibili requires a Rust-managed V2 asset".into(),
        ));
    }
    AssetRegistry::checked_key(&input.key)?;
    Ok(&input.key)
}
fn check_parts(body: &Request) -> AppResult<Vec<(String, String, &'static str)>> {
    let primary = check_ref(&body.video)?.to_owned();
    if body.video_parts.is_empty() {
        return Ok(vec![(primary, String::new(), "video_final")]);
    }
    if body.video_parts.len() != 2 {
        return Err(AppError::Invalid(
            "video_parts must contain P1 and P2".into(),
        ));
    }
    let p1 = check_ref(&body.video_parts[0].video)?;
    let p2 = check_ref(&body.video_parts[1].video)?;
    if p1 != primary || p1 == p2 {
        return Err(AppError::Invalid(
            "P1 must match video.key and P2 must be distinct".into(),
        ));
    }
    let mut out = Vec::new();
    for (index, part) in body.video_parts.iter().enumerate() {
        if part.title.trim().is_empty() || part.title.chars().count() > 80 {
            return Err(AppError::Invalid(
                "part title must contain 1–80 characters".into(),
            ));
        }
        out.push((
            part.video.key.clone(),
            part.title.clone(),
            if index == 0 {
                "video_final"
            } else {
                "video_dubbed"
            },
        ));
    }
    Ok(out)
}
fn view(row: &sqlx::postgres::PgRow) -> AppResult<Value> {
    let response: Option<Value> = row.try_get("response_json")?;
    let response = response.filter(|v| v.is_object() && !v.as_object().unwrap().is_empty());
    Ok(json!({"job_id":row.try_get::<Uuid,_>("id")?,
        "state":row.try_get::<String,_>("state")?,
        "aid":row.try_get::<Option<String>,_>("aid")?,
        "bvid":row.try_get::<Option<String>,_>("bvid")?,"response":response}))
}
async fn owned_asset(
    tx: &mut Transaction<'_, Postgres>,
    task_id: Uuid,
    kind: &str,
    key: &str,
) -> AppResult<()> {
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets
       WHERE task_id=$1 AND kind::text=$2 AND storage_key=$3)",
    )
    .bind(task_id)
    .bind(kind)
    .bind(key)
    .fetch_one(&mut **tx)
    .await?;
    if !exists {
        return Err(AppError::Conflict(format!(
            "Bilibili {kind} asset does not belong to the task"
        )));
    }
    Ok(())
}
async fn create(
    State(state): State<AppState>,
    Json(body): Json<Request>,
) -> AppResult<Json<Value>> {
    if !state.config.native_bilibili_allowed
        || !crate::publish_platforms::enabled(&state.db, "bilibili").await?
    {
        return Err(AppError::Conflict(
            "native Bilibili publisher not enabled".into(),
        ));
    }
    if body.account_id.is_some() {
        return Err(AppError::Conflict("account_id requires a matching account-specific Bilibili Cookie; not implemented in Rust yet".into()));
    }
    if body.force_retry {
        return Err(AppError::Conflict(
            "force_retry risks duplicate external archives; reconcile before retry".into(),
        ));
    }
    if !matches!(
        body.typeid_mode.as_str(),
        "meta" | "bilibili_predict" | "ai_summary"
    ) {
        return Err(AppError::Conflict(
            "unsupported Bilibili typeid_mode".into(),
        ));
    }
    let meta = normalize_meta(&body.meta)?;
    let parts = check_parts(&body)?;
    let cover = body
        .cover
        .as_ref()
        .map(check_ref)
        .transpose()?
        .map(str::to_owned);
    // Require an actual live account and approved metadata, not only settings.
    crate::bilibili_account_v1::publication_credentials(&state.db, &state.config).await?;
    let snapshot = serde_json::to_value(&meta)
        .map_err(|_| AppError::Invalid("Bilibili metadata cannot be serialized".into()))?;
    crate::publish_review::ensure_approved(
        &state.db,
        &state.storage,
        &state.config,
        body.task_id,
        &snapshot,
    )
    .await?;
    // The original Python endpoint uses SELECT ... FOR UPDATE on tasks to
    // serialize duplicate submits across services. Preserve that same lock.
    let mut tx = state.db.pool.begin().await?;
    let status: Option<String> = sqlx::query_scalar(
        "SELECT upper(status::text) FROM tasks
       WHERE id=$1 AND runtime_owner='rust' FOR UPDATE",
    )
    .bind(body.task_id)
    .fetch_optional(&mut *tx)
    .await?;
    let status = status.ok_or_else(|| AppError::NotFound("Rust-owned task not found".into()))?;
    let licensed: bool =
        sqlx::query_scalar("SELECT source_license::text <> 'unknown' FROM tasks WHERE id=$1")
            .bind(body.task_id)
            .fetch_one(&mut *tx)
            .await?;
    if !licensed {
        return Err(AppError::Conflict(
            "Bilibili publication requires a licensed source".into(),
        ));
    }
    // Start with the original Batch entity, under the parent Task row lock.
    // A single Bilibili target can be settled atomically with the original
    // publish_jobs receipt. Multiple targets need independent platform
    // correlation and task-level aggregation; fail closed until that exists.
    let mut batch_is_active = true;
    if let Some(batch_id) = body.batch_id {
        let batch = sqlx::query(
            "SELECT task_id,state,expected_targets FROM publish_batches
             WHERE id=$1 FOR UPDATE",
        )
        .bind(batch_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::Invalid("publish batch does not exist".into()))?;
        if batch.try_get::<Uuid, _>("task_id")? != body.task_id {
            return Err(AppError::Invalid(
                "publish batch does not belong to this task".into(),
            ));
        }
        batch_is_active = batch.try_get::<String, _>("state")? == "active";
        let targets: Value = batch.try_get("expected_targets")?;
        let expected = targets
            .as_array()
            .ok_or_else(|| AppError::Invalid("invalid publish batch target list".into()))?;
        if expected.len() != 1 {
            return Err(AppError::Conflict("Rust direct Bilibili publishing currently requires an isolated single-target batch".into()));
        }
        let target = &expected[0];
        let key = target.get("key").and_then(Value::as_str).unwrap_or("");
        if target.get("platform").and_then(Value::as_str) != Some("bilibili")
            || target.get("account_id").is_some_and(|v| !v.is_null())
            || (!key.is_empty() && key != "bilibili:default")
        {
            return Err(AppError::Invalid(
                "Bilibili default account is not an expected batch target".into(),
            ));
        }
    }
    // Published/active/unknown rows always take precedence over a new action,
    // even when a different HTTP caller generated them (Python or Rust).
    let existing = sqlx::query(
        "SELECT id,state::text AS state,aid,bvid,response_json,batch_id
       FROM publish_jobs WHERE task_id=$1 AND platform='bilibili' AND account_id IS NULL
         AND state::text IN ('published','submitting','submitted','unknown')
         ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT 1",
    )
    .bind(body.task_id)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(row) = existing {
        let previous: Option<Uuid> = row.try_get("batch_id")?;
        if previous != body.batch_id {
            return Err(AppError::Conflict("previous Bilibili publish belongs to a different batch; reconcile before reusing it".into()));
        }
        return Ok(Json(view(&row)?));
    }
    if !batch_is_active {
        return Err(AppError::Conflict(
            "publish batch is no longer active".into(),
        ));
    }
    if let Some(batch_id) = body.batch_id {
        let other_batches: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM publish_batches
             WHERE task_id=$1 AND id<>$2 AND state IN ('active','succeeded','partial_failed'))",
        )
        .bind(body.task_id)
        .bind(batch_id)
        .fetch_one(&mut *tx)
        .await?;
        if other_batches {
            return Err(AppError::Conflict("Bilibili direct batch has other current or completed targets; use batch orchestrator".into()));
        }
    }
    if matches!(status.as_str(), "CANCELED" | "PUBLISHED" | "PUBLISHING") {
        return Err(AppError::Conflict(
            "terminal or already publishing task cannot publish again".into(),
        ));
    }
    let unsafe_other: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs
       WHERE task_id=$1 AND stage='publish' AND
         (state IN ('pending','running','awaiting','unknown','succeeded')))",
    )
    .bind(body.task_id)
    .fetch_one(&mut *tx)
    .await?;
    if unsafe_other {
        return Err(AppError::Conflict(
            "another Rust publisher already owns or completed this task".into(),
        ));
    }
    let busy: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_workflows
       WHERE task_id=$1 AND state IN ('running','paused','unknown'))",
    )
    .bind(body.task_id)
    .fetch_one(&mut *tx)
    .await?;
    if busy {
        return Err(AppError::Conflict("another workflow owns this task".into()));
    }
    for (key, _, kind) in &parts {
        owned_asset(&mut tx, body.task_id, kind, key).await?;
    }
    if let Some(ref key) = cover {
        owned_asset(&mut tx, body.task_id, "cover_image", key).await?;
    }
    if parts.len() == 2 {
        let p2 = &parts[1].0;
        let valid: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM vr2_dubbing_runs r
           JOIN vr2_jobs j ON j.id=r.job_id
           WHERE r.task_id=$1 AND r.output_video_key=$2 AND r.model_status='succeeded'
             AND j.state='succeeded' AND j.stage='dubbing')",
        )
        .bind(body.task_id)
        .bind(p2)
        .fetch_one(&mut *tx)
        .await?;
        if !valid {
            return Err(AppError::Conflict(
                "P2 dubbing job has not succeeded for this task".into(),
            ));
        }
    }
    for (key, _, _) in &parts {
        let path = state.storage.resolve_existing(key).await?;
        let stat = tokio::fs::metadata(&path)
            .await
            .map_err(|_| AppError::Conflict("Bilibili video part file missing".into()))?;
        if !stat.is_file() || stat.len() == 0 {
            return Err(AppError::Conflict("Bilibili video part empty".into()));
        }
    }
    if let Some(ref key) = cover {
        let path = state.storage.resolve_existing(key).await?;
        let stat = tokio::fs::metadata(&path)
            .await
            .map_err(|_| AppError::Conflict("Bilibili cover file missing".into()))?;
        if !stat.is_file() || stat.len() == 0 {
            return Err(AppError::Conflict("Bilibili cover empty".into()));
        }
    }
    let generation: i32 = sqlx::query_scalar(
        "SELECT COALESCE(MAX(generation),0)+1 FROM vr2_workflows WHERE task_id=$1",
    )
    .bind(body.task_id)
    .fetch_one(&mut *tx)
    .await?;
    let workflow_id = Uuid::new_v4();
    let job_id = Uuid::new_v4();
    let stage_id = Uuid::new_v4();
    let original_operation = format!("publish-job:{job_id}");
    let original_meta = json!({"meta":snapshot,"video":body.video,"video_parts":body.video_parts,
        "account_id":null,"platform":"bilibili","typeid_mode":body.typeid_mode});
    let direct = json!({"publish_job_id":job_id,"meta":meta,"parts":parts.iter()
        .map(|(key,title,kind)|json!({"key":key,"title":title,"kind":kind})).collect::<Vec<_>>(),
        "cover_key":cover,"typeid_mode":body.typeid_mode});
    let config = json!({"auto_publish":true,"auto_publish_platforms":["bilibili"],"bilibili_direct_v1":direct});
    // Commit the legacy publish job + native stage + Python-recovery fence as
    // one transaction. Without this, a Python dispatcher can double-upload.
    sqlx::query(
        "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration)
       VALUES($1,$2,$3,'running','publish',$4)",
    )
    .bind(workflow_id)
    .bind(body.task_id)
    .bind(generation)
    .bind(config)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "INSERT INTO operation_inbox(id,workflow_id,task_id,stage,generation,operation_key,
       request_json,max_attempts,runtime_engine)
       VALUES($1,$2,$3,'publish',$4,$5,'{}'::jsonb,1,'rust-v2')",
    )
    .bind(stage_id)
    .bind(workflow_id)
    .bind(body.task_id)
    .bind(generation)
    .bind(format!("vr2:{workflow_id}:publish:{generation}"))
    .execute(&mut *tx)
    .await?;
    sqlx::query("INSERT INTO publish_jobs(id,task_id,batch_id,platform,account_id,bili_account_id,meta_json,cover_key,
       state,operation_key,lease_owner,lease_until)
       VALUES($1,$2,$3,'bilibili',NULL,NULL,$4,$5,'submitting',$6,
          'rust-v2',now()+interval '48 hours')")
        .bind(job_id).bind(body.task_id).bind(body.batch_id).bind(&original_meta).bind(&cover)
        .bind(&original_operation).execute(&mut *tx).await?;
    sqlx::query(
        "INSERT INTO operation_inbox(id,operation_key,status,request_json,lease_owner,
        lease_until,runtime_engine)
        VALUES($1,$2,'processing','{}'::jsonb,'rust-v2',now()+interval '48 hours','python')",
    )
    .bind(Uuid::new_v4())
    .bind(&original_operation)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "UPDATE tasks SET status='publishing'::task_status,
         error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1",
    )
    .bind(body.task_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"job_id":job_id,"state":"submitting","aid":null,"bvid":null,"response":null}),
    ))
}

pub fn router() -> Router<AppState> {
    Router::new().route("/bilibili/publish", post(create))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_metadata_aliases_and_p1_p2_validation() {
        let meta = normalize_meta(&json!({"title":"Video","tid":171,"tag":"A,B,A",
            "copyright":2,"source":"https://youtu.be/example"}))
        .unwrap();
        assert_eq!(meta.typeid, 171);
        assert_eq!(meta.tags, ["A", "B"]);
        let body:Request=serde_json::from_value(json!({"task_id":Uuid::nil(),
            "video":{"key":"v2/video/one.mp4"},"meta":{"title":"Test","typeid":171,"tags":["tag"]}})).unwrap();
        assert_eq!(check_parts(&body).unwrap().len(), 1);
        let mut wrong = body;
        wrong.video_parts = vec![
            VideoPart {
                video: MediaRef {
                    r#type: "storage".into(),
                    key: "v2/video/other.mp4".into(),
                },
                title: "P1".into(),
            },
            VideoPart {
                video: MediaRef {
                    r#type: "storage".into(),
                    key: "v2/video/p2.mp4".into(),
                },
                title: "P2".into(),
            },
        ];
        assert!(check_parts(&wrong).is_err());
    }
}

/// Python's recovery scans original `publish_jobs` and requires an active
/// original inbox ownership fence. It must never enqueue a competing Celery
/// dispatcher for a native Rust archive upload.
pub(crate) async fn mark_started(
    db: &crate::Database,
    job: &crate::domain::WorkItem,
) -> AppResult<()> {
    let id:Option<Uuid>=sqlx::query_scalar("SELECT (configuration->'bilibili_direct_v1'->>'publish_job_id')::uuid FROM vr2_workflows WHERE id=$1")
        .bind(job.workflow_id).fetch_optional(&db.pool).await?.flatten();
    let Some(id) = id else {
        return Ok(());
    };
    let mut tx = db.pool.begin().await?;
    let active: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs
        WHERE id=$1 AND task_id=$2 AND lease_token=$3 AND state='running' AND lease_until>now())",
    )
    .bind(job.id)
    .bind(job.task_id)
    .bind(job.lease_token)
    .fetch_one(&mut *tx)
    .await?;
    if !active {
        return Err(AppError::Conflict("stale Bilibili publisher lease".into()));
    }
    let updated = sqlx::query(
        "UPDATE publish_jobs SET started_at=COALESCE(started_at,now()),
        lease_owner='rust-v2',lease_until=now()+interval '48 hours',heartbeat_at=now(),
        upload_active=true,updated_at=now()
        WHERE id=$1 AND task_id=$2 AND state='submitting'",
    )
    .bind(id)
    .bind(job.task_id)
    .execute(&mut *tx)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(AppError::Conflict(
            "original publish intent no longer submitting".into(),
        ));
    }
    sqlx::query(
        "UPDATE operation_inbox SET lease_until=now()+interval '48 hours',heartbeat_at=now(),
        lease_owner='rust-v2',updated_at=now()
        WHERE operation_key=$1 AND runtime_engine='python' AND status='processing'",
    )
    .bind(format!("publish-job:{id}"))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

/// Commit the original Python visible publication receipt inside the *same*
/// transaction that settles Rust's irreversible publish stage. A crash after
/// Bilibili accepts an archive does not create an internally confirmed Rust
/// success without an original-table receipt.
pub(crate) async fn mark_confirmed(
    tx: &mut Transaction<'_, Postgres>,
    job: &crate::domain::WorkItem,
    result: &Value,
) -> AppResult<()> {
    let id:Option<Uuid>=sqlx::query_scalar("SELECT (configuration->'bilibili_direct_v1'->>'publish_job_id')::uuid FROM vr2_workflows WHERE id=$1")
        .bind(job.workflow_id).fetch_optional(&mut **tx).await?.flatten();
    let Some(id) = id else {
        return Ok(());
    };
    let bvid = result
        .get("bvid")
        .and_then(Value::as_str)
        .filter(|v| v.starts_with("BV") && v.len() <= 32)
        .ok_or_else(|| {
            AppError::Unavailable("confirmed Bilibili receipt lacks BV identifier".into())
        })?;
    let aid = result.get("aid").and_then(|v| {
        v.as_str()
            .map(str::to_owned)
            .or_else(|| v.as_i64().map(|v| v.to_string()))
    });
    let receipt = json!({"bvid":bvid,"aid":aid,"native_bilibili":true,"confirmed":true,
        "parts":result.get("parts")});
    let updated = sqlx::query(
        "UPDATE publish_jobs SET state='published',
        aid=$3,bvid=$4,external_id=$4,external_url=$5,response_json=$6,
        upload_active=false,upload_progress=100,lease_owner=NULL,lease_until=NULL,
        heartbeat_at=now(),finished_at=now(),updated_at=now()
        WHERE id=$1 AND task_id=$2 AND state='submitting'",
    )
    .bind(id)
    .bind(job.task_id)
    .bind(aid)
    .bind(bvid)
    .bind(format!("https://www.bilibili.com/video/{bvid}"))
    .bind(&receipt)
    .execute(&mut **tx)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(AppError::Conflict(
            "original Bilibili receipt ownership lost".into(),
        ));
    }
    let batch_id: Option<Uuid> =
        sqlx::query_scalar("SELECT batch_id FROM publish_jobs WHERE id=$1")
            .bind(id)
            .fetch_one(&mut **tx)
            .await?;
    if let Some(batch_id) = batch_id {
        let result = sqlx::query(
            "UPDATE publish_batches SET state='succeeded',
            finished_at=COALESCE(finished_at,now()),updated_at=now()
            WHERE id=$1 AND task_id=$2 AND state='active'",
        )
        .bind(batch_id)
        .bind(job.task_id)
        .execute(&mut **tx)
        .await?;
        if result.rows_affected() != 1 {
            return Err(AppError::Conflict(
                "original single-target Bilibili batch is not active".into(),
            ));
        }
    }
    sqlx::query(
        "UPDATE operation_inbox SET status='completed',result_json=$2,
        completed_at=now(),lease_owner=NULL,lease_until=NULL,updated_at=now()
        WHERE operation_key=$1 AND runtime_engine='python' AND status='processing'",
    )
    .bind(format!("publish-job:{id}"))
    .bind(&receipt)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// Every unconfirmed external result is quarantined. Never issue another
/// archive submission just because an UPOS or creator-center call timed out.
pub(crate) async fn mark_uncertain(
    tx: &mut Transaction<'_, Postgres>,
    workflow_id: Uuid,
    reason: &str,
) -> AppResult<()> {
    let id:Option<Uuid>=sqlx::query_scalar("SELECT (configuration->'bilibili_direct_v1'->>'publish_job_id')::uuid FROM vr2_workflows WHERE id=$1")
        .bind(workflow_id).fetch_optional(&mut **tx).await?.flatten();
    let Some(id) = id else {
        return Ok(());
    };
    let details = json!({"recovery":"unknown","error":reason.chars().take(400).collect::<String>(),
        "native_bilibili":true,"verify_platform_before_retry":true});
    sqlx::query(
        "UPDATE publish_jobs SET state='unknown',upload_active=false,
        lease_owner=NULL,lease_until=NULL,heartbeat_at=now(),finished_at=now(),
        response_json=$2,updated_at=now() WHERE id=$1 AND state='submitting'",
    )
    .bind(id)
    .bind(&details)
    .execute(&mut **tx)
    .await?;
    sqlx::query(
        "UPDATE operation_inbox SET status='completed',result_json=$2,
        completed_at=now(),lease_owner=NULL,lease_until=NULL,updated_at=now()
        WHERE operation_key=$1 AND runtime_engine='python' AND status='processing'",
    )
    .bind(format!("publish-job:{id}"))
    .bind(&details)
    .execute(&mut **tx)
    .await?;
    let batch_id: Option<Uuid> =
        sqlx::query_scalar("SELECT batch_id FROM publish_jobs WHERE id=$1")
            .bind(id)
            .fetch_one(&mut **tx)
            .await?;
    if let Some(batch_id) = batch_id {
        sqlx::query(
            "UPDATE publish_batches SET state='failed',
           finished_at=COALESCE(finished_at,now()),updated_at=now()
           WHERE id=$1 AND state='active'",
        )
        .bind(batch_id)
        .execute(&mut **tx)
        .await?;
    }
    // Exact original Python failure semantics: do not leave task UI stuck at
    // PUBLISHING while the externally irreversible receipt is UNKNOWN.
    sqlx::query(
        "UPDATE tasks SET status='failed'::task_status,
        error_code='PUBLISH_OUTCOME_UNKNOWN',error_message=$2,updated_at=now()
        WHERE id=(SELECT task_id FROM publish_jobs WHERE id=$1)
          AND runtime_owner='rust' AND status='publishing'::task_status",
    )
    .bind(id)
    .bind("Bilibili outcome uncertain: verify the archive on the platform before retry")
    .execute(&mut **tx)
    .await?;
    Ok(())
}
