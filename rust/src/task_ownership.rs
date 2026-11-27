//! Explicit, offline adoption of original tasks. No historical business rows
//! are copied or rewritten as successful native executions.
use crate::{AppError, AppResult, Config, Database, domain::Stage, storage::AssetRegistry};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

const ENGINE: &str = "rust-task-cutover";
const TERMINAL: &str = "('succeeded','failed','canceled','completed','superseded')";
const ADOPTION_LOCK: &str = "LOCK TABLE tasks,pipeline_runs,operation_inbox,assets,subtitles,subtitle_jobs,render_jobs,render_executions,dubbing_jobs,publish_jobs,publish_batches,outbox_events IN SHARE ROW EXCLUSIVE MODE NOWAIT";

// Resolve the original outbox aggregate to its owning task without casting
// untrusted historical strings to UUID. Include inbox evidence by operation key.
const BLOCKERS: &str = r#"SELECT array_remove(ARRAY[
 CASE WHEN EXISTS(SELECT 1 FROM pipeline_runs w WHERE w.task_id=t.id
   AND w.id IS DISTINCT FROM $2::uuid
   AND w.state NOT IN ('succeeded','failed','canceled','completed','superseded')) THEN 'active_workflow' END,
 CASE WHEN EXISTS(SELECT 1 FROM subtitle_jobs j WHERE j.task_id=t.id
   AND (j.status::text NOT IN ('succeeded','failed','canceled','completed') OR j.lease_owner IS NOT NULL OR j.lease_until IS NOT NULL)) THEN 'subtitle_job_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM render_jobs j WHERE j.task_id=t.id
   AND (j.status::text NOT IN ('succeeded','failed','canceled','completed') OR j.lease_owner IS NOT NULL OR j.lease_until IS NOT NULL)) THEN 'render_job_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM render_executions e LEFT JOIN render_jobs j ON j.id=e.render_job_id
   WHERE (e.task_id=t.id OR j.task_id=t.id) AND e.state NOT IN ('completed','failed','canceled','lost')
   AND NOT (e.runtime_engine='python' AND e.state='succeeded' AND e.finished_at IS NOT NULL)) THEN 'render_execution_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM dubbing_jobs j WHERE j.task_id=t.id
   AND (j.status NOT IN ('succeeded','failed','canceled','completed') OR j.lease_owner IS NOT NULL OR j.lease_until IS NOT NULL)) THEN 'dubbing_job_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=t.id
   AND (j.state::text IN ('draft','submitting','unknown') OR j.lease_owner IS NOT NULL OR j.lease_until IS NOT NULL OR j.upload_active
      OR (j.state::text IN ('submitted','published') AND
         (CASE WHEN j.platform::text='bilibili' THEN COALESCE(NULLIF(j.bvid,''),NULLIF(j.aid,''),NULLIF(j.external_id,'')) ELSE COALESCE(NULLIF(j.external_id,''),NULLIF(j.external_url,'')) END) IS NULL)
      OR COALESCE(j.response_json->'auto_dub_p2'->>'state','') NOT IN ('','published','failed')))
   THEN 'publication_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM publish_batches b WHERE b.task_id=t.id AND b.state NOT IN ('succeeded','failed','partial_failed')) THEN 'publish_batch_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM operation_inbox i WHERE
   i.id IS DISTINCT FROM $3::uuid AND
   (i.task_id=t.id OR i.request_json->>'task_id'=t.id::text OR i.operation_key='youtube-download:'||t.id::text
      OR EXISTS(SELECT 1 FROM subtitle_jobs j WHERE j.task_id=t.id AND j.operation_key=i.operation_key)
      OR EXISTS(SELECT 1 FROM render_jobs j WHERE j.task_id=t.id AND j.operation_key=i.operation_key)
      OR EXISTS(SELECT 1 FROM dubbing_jobs j WHERE j.task_id=t.id AND j.operation_key=i.operation_key)
      OR EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=t.id AND j.operation_key=i.operation_key))
   AND (i.status NOT IN ('completed','succeeded','failed','canceled') OR i.lease_owner IS NOT NULL OR i.lease_until IS NOT NULL)
   AND NOT (i.runtime_engine='python' AND i.operation_key='youtube-download:'||t.id::text
       AND i.request_json->>'task_id'=t.id::text AND i.status='pending' AND i.attempt_count=0
       AND i.lease_owner IS NULL AND i.lease_until IS NULL AND i.result_json IS NULL)) THEN 'operation_unsettled' END,
 CASE WHEN EXISTS(SELECT 1 FROM outbox_events e LEFT JOIN operation_inbox i ON i.operation_key=e.operation_key
   WHERE ((e.aggregate_type='task' AND e.aggregate_id=t.id::text)
      OR (e.aggregate_type='pipeline_run' AND EXISTS(SELECT 1 FROM pipeline_runs w WHERE w.task_id=t.id AND w.id::text=e.aggregate_id))
      OR (e.aggregate_type='publish_batch' AND EXISTS(SELECT 1 FROM publish_batches b WHERE b.task_id=t.id AND b.id::text=e.aggregate_id))
      OR (e.aggregate_type='publish_job' AND EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=t.id AND j.id::text=e.aggregate_id))
      OR (e.aggregate_type='render_job' AND EXISTS(SELECT 1 FROM render_jobs j WHERE j.task_id=t.id AND j.id::text=e.aggregate_id))
      OR (e.aggregate_type='dubbing_job' AND EXISTS(SELECT 1 FROM dubbing_jobs j WHERE j.task_id=t.id AND j.id::text=e.aggregate_id))
      OR (e.aggregate_type='subtitle_job' AND EXISTS(SELECT 1 FROM subtitle_jobs j WHERE j.task_id=t.id AND j.id::text=e.aggregate_id)))
   AND NOT (e.status IN ('dispatched','native_completed') AND COALESCE(i.status,'')='completed')
   AND NOT ($4::uuid IS NOT NULL AND e.aggregate_type='publish_batch' AND e.aggregate_id=$4::text
      AND e.status='native_pending' AND e.attempt_count=0 AND e.lease_owner IS NULL
      AND e.lease_until IS NULL AND e.delivered_at IS NULL AND i.id IS NULL
      AND EXISTS(SELECT 1 FROM publish_batches b WHERE b.id=$4 AND b.task_id=t.id
        AND b.state IN ('succeeded','partial_failed','failed')
        AND ((e.task_name='rust.publish.batch_finished' AND e.event_type='workflow.publish.finished'
          AND e.operation_key='workflow-publish-finished:'||b.id::text||':'||b.state
          AND e.args_json=jsonb_build_object('args',jsonb_build_array(b.id,b.state,t.id)))
        OR (b.state='succeeded' AND e.task_name='rust.publish.cleanup' AND e.event_type='publish.cleanup'
          AND e.operation_key='publish-cleanup:'||b.id::text
          AND e.args_json=jsonb_build_object('args',jsonb_build_array(t.id,b.id)))))
      AND NOT EXISTS(SELECT 1 FROM outbox_events dup WHERE dup.operation_key=e.operation_key AND dup.id<>e.id))
   AND NOT (e.status='pending' AND e.attempt_count=0 AND e.lease_owner IS NULL AND e.lease_until IS NULL AND e.delivered_at IS NULL AND i.id IS NULL
      AND e.aggregate_type='publish_batch' AND e.task_name IN ('subtitle_service.cleanup_task','subtitle_service.push_publish_workflow_event')
      AND NOT EXISTS(SELECT 1 FROM outbox_events dup WHERE dup.operation_key=e.operation_key AND dup.id<>e.id))
   ) THEN 'outbox_unsettled' END,
 CASE WHEN t.status::text='publishing' THEN 'task_publishing' END
],NULL)::text[] AS blockers FROM tasks t WHERE t.id=$1"#;

pub(crate) async fn blockers(tx: &mut Transaction<'_, Postgres>, task: Uuid) -> AppResult<Vec<String>> {
    blockers_except_batch_waiter(tx,task,None,None,None).await
}

/// Only the timeout reconciler may exempt a single verified, lease-free
/// native batch waiter. Adoption and all other callers use no exemptions.
pub(crate) async fn blockers_except_batch_waiter(tx:&mut Transaction<'_,Postgres>,task:Uuid,flow:Option<Uuid>,job:Option<Uuid>,terminal_batch:Option<Uuid>)->AppResult<Vec<String>> {
    Ok(sqlx::query_scalar(BLOCKERS)
        .bind(task).bind(flow).bind(job).bind(terminal_batch)
        .fetch_one(&mut **tx)
        .await?)
}
fn stage_for(status: &str, source: &str) -> Option<Stage> {
    match status {
        "created" | "ingested" if source == "youtube" => Some(Stage::Download),
        "downloaded" | "audio_extracted" | "asr_done" | "translated" => Some(Stage::Subtitle),
        "subtitle_ready" => Some(Stage::Render),
        _ => None,
    }
}
pub async fn audit(db: &Database, after: Option<Uuid>, limit: i64) -> AppResult<Value> {
    let mut tx = db.pool.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        .execute(&mut *tx)
        .await?;
    let rows=sqlx::query("SELECT id,status::text AS status,source_type::text AS source_type,source_license::text AS license
      FROM tasks WHERE runtime_owner='python' AND ($1::uuid IS NULL OR id>$1) ORDER BY id LIMIT $2")
        .bind(after).bind(limit.clamp(1,1000)+1).fetch_all(&mut *tx).await?;
    let total: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks WHERE runtime_owner='python'")
        .fetch_one(&mut *tx)
        .await?;
    let mut items = vec![];
    let mut next = None;
    for r in rows.iter().take(limit.clamp(1, 1000) as usize) {
        let task: Uuid = r.try_get("id")?;
        let status: String = r.try_get("status")?;
        let source: String = r.try_get("source_type")?;
        let reasons = blockers(&mut tx, task).await?;
        let history: bool =
            sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1)")
                .bind(task)
                .fetch_one(&mut *tx)
                .await?;
        let resume =
            if reasons.is_empty() && !history && r.try_get::<String, _>("license")? != "unknown" {
                stage_for(&status, &source).map(|s| s.as_str())
            } else {
                None
            };
        items.push(json!({"task_id":task,"status":status,"blockers":reasons,"resume_candidate":resume,"has_publish_history":history}));
        next = Some(task);
    }
    tx.commit().await?;
    Ok(
        json!({"python_owned_total":total,"items":items,"next_after":if rows.len()>limit.clamp(1,1000) as usize{next}else{None},"note":"database preflight only; file verification and stopped-runtime confirmation required for adoption"}),
    )
}
async fn bulk_plan(
    tx: &mut Transaction<'_, Postgres>,
    after: Option<Uuid>,
    limit: i64,
) -> AppResult<Value> {
    let rows=sqlx::query(
        "SELECT id,status::text AS status,source_type::text AS source_type,
        source_license::text AS license,updated_at,
        (to_jsonb(t)->>'lock_owner' IS NOT NULL OR to_jsonb(t)->>'lock_until' IS NOT NULL) AS task_locked,
        EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=t.id) AS publication_history,
        EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=t.id AND j.state::text='published'
            AND (CASE WHEN j.platform::text='bilibili' THEN COALESCE(NULLIF(j.bvid,''),NULLIF(j.aid,''),NULLIF(j.external_id,'')) ELSE COALESCE(NULLIF(j.external_id,''),NULLIF(j.external_url,'')) END) IS NOT NULL) AS published_receipt,
        EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key='youtube-download:'||t.id::text AND i.status='pending') AS pending_download,
        EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key='task-ownership-cutover:'||t.id::text) AS adoption_receipt
        FROM tasks t WHERE runtime_owner='python' AND ($1::uuid IS NULL OR id>$1)
        ORDER BY id LIMIT $2")
        .bind(after).bind(limit+1).fetch_all(&mut **tx).await?;
    let mut items = Vec::new();
    let mut counts = std::collections::BTreeMap::<String, i64>::new();
    let mut next = None;
    let total: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks WHERE runtime_owner='python'")
        .fetch_one(&mut **tx)
        .await?;
    for row in rows.iter().take(limit as usize) {
        let task: Uuid = row.try_get("id")?;
        let status: String = row.try_get("status")?;
        let source: String = row.try_get("source_type")?;
        let license: String = row.try_get("license")?;
        let history: bool = row.try_get("publication_history")?;
        let mut reasons = blockers(tx, task).await?;
        if row.try_get::<bool, _>("task_locked")? {
            reasons.push("task_lease_present".into());
        }
        if row.try_get::<bool, _>("pending_download")? {
            reasons.push("pending_download_requires_explicit_resume".into());
        }
        if row.try_get::<bool, _>("adoption_receipt")? {
            reasons.push("adoption_receipt_already_exists".into());
        }
        if status == "published" && !row.try_get::<bool, _>("published_receipt")? {
            reasons.push("published_without_confirmed_receipt".into());
        }
        let resume = if reasons.is_empty() && !history && license != "unknown" {
            stage_for(&status, &source).map(|s| s.as_str())
        } else {
            None
        };
        // This operation only transfers historical terminal ownership. Any
        // resumable or reviewable task needs the explicit single-task path.
        let disposition = if !reasons.is_empty() {
            "requires_reconciliation"
        } else if matches!(status.as_str(), "published" | "failed" | "canceled") {
            "safe_terminal"
        } else if resume.is_some() {
            "resume_candidate"
        } else {
            "manual_review"
        };
        *counts.entry(disposition.into()).or_default() += 1;
        next = Some(task);
        items.push(json!({"task_id":task,"status":status,"updated_at":row.try_get::<chrono::DateTime<chrono::Utc>,_>("updated_at")?,"disposition":disposition,
            "blockers":reasons,"resume_candidate":resume,"has_publish_history":history}));
    }
    let next_after = if rows.len() > limit as usize {
        next
    } else {
        None
    };
    // This token binds the exact reviewed page, not an authorization secret
    // or a complete snapshot of the database. All eligibility is checked
    // again under the same table locks as single-task adoption.
    let page = json!({"after":after,"limit":limit,"items":items,"next_after":next_after});
    let token =
        hex::encode(Sha256::digest(serde_json::to_vec(&page).map_err(|_| {
            AppError::Invalid("cannot encode adoption plan".into())
        })?));
    Ok(
        json!({"mode":"plan","python_owned_total":total,"after":after,"limit":limit,"items":items,
        "counts":counts,"next_after":next_after,"plan_token":token,"automatic_resume":false,"automatic_publish":false}),
    )
}

/// Review a bounded page and optionally adopt exactly the safe terminal
/// subset atomically. Never invoke worker scheduling or external services.
pub async fn bulk(
    db: &Database,
    cfg: &Config,
    after: Option<Uuid>,
    limit: i64,
    apply: bool,
    quiesced: bool,
    expected_plan: Option<&str>,
) -> AppResult<Value> {
    if !(1..=500).contains(&limit) {
        return Err(AppError::Invalid("bulk page limit must be 1..500".into()));
    }
    if apply {
        if !crate::native_deployment::enabled()
            || !crate::outbox_dispatcher::native_only()
            || !quiesced
        {
            return Err(AppError::Conflict("bulk adoption requires standalone native-only mode and stopped producers/consumers confirmation".into()));
        }
        crate::native_deployment::validate(cfg)?;
        if expected_plan.is_none_or(|s| s.len() != 64 || !s.bytes().all(|c| c.is_ascii_hexdigit()))
        {
            return Err(AppError::Invalid(
                "bulk apply requires the reviewed page plan token".into(),
            ));
        }
        // No schema writes or implicit migrations. A checksum mismatch must
        // not be ignored merely because individual queries happen to work.
        let preflight = crate::native_deployment::preflight(db, cfg, false).await?;
        if preflight.get("missing_migrations").is_some()
            || preflight.get("invalid_migrations").is_some()
        {
            return Err(AppError::Conflict(
                "bulk adoption requires the complete verified migration ledger".into(),
            ));
        }
        if preflight["database_collation"]["compatible"] != true {
            return Err(AppError::Conflict(
                "bulk adoption requires compatible database collation".into(),
            ));
        }
    } else if quiesced || expected_plan.is_some() {
        return Err(AppError::Invalid(
            "quiesced and plan-token are only valid with apply".into(),
        ));
    }
    let mut tx = db.pool.begin().await?;
    if apply {
        sqlx::query(ADOPTION_LOCK).execute(&mut *tx).await?;
    } else {
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .execute(&mut *tx)
            .await?;
    }
    sqlx::query("SET LOCAL statement_timeout='30s'")
        .execute(&mut *tx)
        .await?;
    let mut plan = bulk_plan(&mut tx, after, limit).await?;
    if apply {
        if plan["plan_token"].as_str() != expected_plan {
            return Err(AppError::Conflict(
                "bulk plan changed; re-run the read-only plan before applying".into(),
            ));
        }
        let mut adopted = Vec::new();
        for item in plan["items"].as_array().expect("bulk plan items") {
            if item["disposition"] != "safe_terminal" {
                continue;
            }
            let task = Uuid::parse_str(item["task_id"].as_str().expect("task id"))
                .map_err(|_| AppError::Invalid("invalid bulk task id".into()))?;
            let status = &item["status"];
            let key = format!("task-ownership-cutover:{task}");
            let updated=sqlx::query("UPDATE tasks SET runtime_owner='rust',runtime_origin=COALESCE(runtime_origin,'legacy_adopted'),updated_at=now() WHERE id=$1 AND runtime_owner='python'")
                .bind(task).execute(&mut *tx).await?.rows_affected();
            if updated != 1 {
                return Err(AppError::Conflict("bulk task ownership changed".into()));
            }
            let result = json!({"task_id":task,"previous_owner":"python","previous_status":status,"runtime_owner":"rust",
                "workflow_id":null,"resumed_stage":null,"verified_assets":[],"retired_unattempted_downloads":0,"retired_hatchet_runs":[],"automatic_publish":false});
            sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,task_id,request_json,result_json,completed_at) VALUES($1,$2,$3,'completed',$4,$5,$6,now())")
                .bind(Uuid::new_v4()).bind(key).bind(ENGINE).bind(task)
                .bind(json!({"resume":false,"bulk_plan_token":expected_plan})).bind(result).execute(&mut *tx).await?;
            adopted.push(task);
        }
        plan["mode"] = json!("applied");
        plan["adopted_count"] = json!(adopted.len());
        plan["adopted_task_ids"] = json!(adopted);
        plan["python_owned_remaining"] = json!(
            sqlx::query_scalar::<_, i64>("SELECT count(*) FROM tasks WHERE runtime_owner='python'")
                .fetch_one(&mut *tx)
                .await?
        );
    }
    tx.commit().await?;
    Ok(plan)
}

async fn verify_asset(
    tx: &mut Transaction<'_, Postgres>,
    store: &AssetRegistry,
    cfg: &Config,
    task: Uuid,
    kind: &str,
    required: bool,
) -> AppResult<Option<Value>> {
    let row=sqlx::query("SELECT id,storage_key,sha256,size_bytes FROM assets WHERE task_id=$1 AND kind::text=$2 ORDER BY created_at DESC,id DESC LIMIT 1")
        .bind(task).bind(kind).fetch_optional(&mut **tx).await?;
    let Some(row) = row else {
        if required {
            return Err(AppError::Conflict(format!(
                "historical {kind} asset missing"
            )));
        }
        return Ok(None);
    };
    let id: Uuid = row.try_get("id")?;
    let key: String = row.try_get("storage_key")?;
    let path = store.resolve_existing(&key).await?;
    let info = tokio::fs::metadata(&path).await?;
    if !info.is_file() || info.len() == 0 {
        return Err(AppError::Conflict(format!(
            "historical {kind} file missing or empty"
        )));
    }
    let (sha, size) = store.sha256(&key).await?;
    if row
        .try_get::<Option<String>, _>("sha256")?
        .as_ref()
        .is_some_and(|v| !v.is_empty() && !v.eq_ignore_ascii_case(&sha))
        || row
            .try_get::<Option<i64>, _>("size_bytes")?
            .is_some_and(|v| v != size)
    {
        return Err(AppError::Conflict(format!(
            "historical {kind} checksum/size mismatch"
        )));
    }
    if kind.starts_with("video_") {
        tokio::time::timeout(
            std::time::Duration::from_secs(30),
            crate::media::ffmpeg::probe_video(&cfg.ffprobe_binary, &path),
        )
        .await
        .map_err(|_| AppError::Unavailable("historical video probe timed out".into()))??;
    } else if kind == "subtitle_srt" {
        if size > 32 * 1024 * 1024 {
            return Err(AppError::Conflict("historical SRT too large".into()));
        }
        let text = tokio::fs::read_to_string(&path).await?;
        if !text.contains(" --> ") {
            return Err(AppError::Conflict("historical SRT has no time cues".into()));
        }
    }
    // Preserve the original asset ID and key. No copy, rename or registration
    // into native GC ownership; historical files are not silently reclaimed.
    Ok(Some(
        json!({"asset_id":id,"kind":kind,"storage_key":key,"sha256":sha,"size_bytes":size}),
    ))
}
pub async fn adopt(
    db: &Database,
    cfg: &Config,
    task: Uuid,
    quiesced: bool,
    resume: bool,
    stopped_runs: Vec<Uuid>,
) -> AppResult<Value> {
    if !crate::outbox_dispatcher::native_only() || !quiesced {
        return Err(AppError::Conflict("ownership adoption requires native-only mode and stopped producers/consumers confirmation".into()));
    }
    let mut tx = db.pool.begin().await?;
    // Offline cutover; lock every original business ledger used by the
    // decision, so old workers cannot create an unobserved side effect.
    sqlx::query(ADOPTION_LOCK).execute(&mut *tx).await?;
    let r=sqlx::query("SELECT runtime_owner,status::text AS status,source_type::text AS source_type,source_license::text AS license,source_url FROM tasks WHERE id=$1 FOR UPDATE")
        .bind(task).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::NotFound("historical task not found".into()))?;
    let owner: String = r.try_get("runtime_owner")?;
    let key = format!("task-ownership-cutover:{task}");
    if owner == "rust" {
        let receipt:Option<Value>=sqlx::query_scalar("SELECT result_json FROM operation_inbox WHERE operation_key=$1 AND runtime_engine=$2 AND status='completed'")
            .bind(&key).bind(ENGINE).fetch_optional(&mut *tx).await?.flatten();
        return receipt.ok_or_else(|| {
            AppError::Conflict("task already Rust-owned outside historical adoption".into())
        });
    }
    if owner != "python" {
        return Err(AppError::Conflict("unknown task owner".into()));
    }
    // Explicitly identified externally stopped Hatchet runs may be retired,
    // but all other original operations must still be settled. Do not fake
    // success or delete external run IDs.
    let active:Vec<Uuid>=sqlx::query_scalar(&format!("SELECT id FROM pipeline_runs WHERE task_id=$1 AND state NOT IN {TERMINAL} AND engine='hatchet' ORDER BY id"))
        .bind(task).fetch_all(&mut *tx).await?;
    let mut confirmed = stopped_runs;
    confirmed.sort();
    confirmed.dedup();
    if !confirmed.is_empty() {
        if confirmed != active {
            return Err(AppError::Conflict(
                "confirmed stopped run IDs must exactly match active historical Hatchet runs"
                    .into(),
            ));
        }
        sqlx::query("UPDATE pipeline_runs SET state='superseded',finished_at=COALESCE(finished_at,now()),updated_at=now() WHERE id=ANY($1) AND task_id=$2 AND engine='hatchet'")
            .bind(&confirmed).bind(task).execute(&mut *tx).await?;
    }
    let reasons = blockers(&mut tx, task).await?;
    if !reasons.is_empty() {
        return Err(AppError::Conflict(format!(
            "historical task requires reconciliation: {}",
            reasons.join(", ")
        )));
    }
    let status: String = r.try_get("status")?;
    let source: String = r.try_get("source_type")?;
    let history: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1)")
            .bind(task)
            .fetch_one(&mut *tx)
            .await?;
    let stage = if resume {
        if history || r.try_get::<String, _>("license")? == "unknown" {
            return Err(AppError::Conflict(
                "publication history or unknown source license prevents automatic recovery".into(),
            ));
        }
        Some(stage_for(&status, &source).ok_or_else(|| {
            AppError::Conflict(
                "task state requires manual stage selection/review; no automatic resume".into(),
            )
        })?)
    } else {
        None
    };
    let pending_download:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key=$1 AND runtime_engine='python' AND status='pending')")
        .bind(format!("youtube-download:{task}")).fetch_one(&mut *tx).await?;
    if pending_download && stage != Some(Stage::Download) {
        return Err(AppError::Conflict(
            "untouched pending download must be explicitly resumed, not silently retired".into(),
        ));
    }
    let store = AssetRegistry::new(db.clone(), cfg.storage_root.clone());
    let mut media = vec![];
    if let Some(stage) = stage {
        // Existing valid raw video wins over a stale created/ingested status.
        // A historical SRT or final already present must be validated because
        // native workers may reuse them. Never choose a different older file.
        let raw = verify_asset(
            &mut tx,
            &store,
            cfg,
            task,
            "video_raw",
            stage != Stage::Download,
        )
        .await?;
        if stage == Stage::Download && raw.is_some() {
            return Err(AppError::Conflict(
                "raw asset already exists; reconcile historical download status before resume"
                    .into(),
            ));
        }
        if stage == Stage::Download {
            let completed:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key=$1 AND status='completed')")
                .bind(format!("youtube-download:{task}")).fetch_one(&mut *tx).await?;
            if completed {
                return Err(AppError::Conflict(
                    "historical completed download has no verified raw asset".into(),
                ));
            }

            let url: Option<String> = r.try_get("source_url")?;
            if url.as_deref().is_none_or(|s| s.trim().is_empty()) {
                return Err(AppError::Conflict("YouTube source missing".into()));
            }
        }
        if let Some(raw) = raw {
            media.push(raw);
        }
        for kind in ["subtitle_srt", "subtitle_ass", "video_final"] {
            if let Some(asset) = verify_asset(
                &mut tx,
                &store,
                cfg,
                task,
                kind,
                kind == "subtitle_srt" && stage == Stage::Render,
            )
            .await?
            {
                if kind == "video_final" || stage == Stage::Download {
                    return Err(AppError::Conflict("historical media conflicts with recorded checkpoint; review before resuming".into()));
                }
                media.push(asset);
            }
        }
    }
    let retired=sqlx::query("UPDATE operation_inbox SET status='canceled',completed_at=now(),updated_at=now(),last_error='retired before execution by Rust ownership cutover' WHERE runtime_engine='python' AND operation_key=$1 AND request_json->>'task_id'=$2 AND status='pending' AND attempt_count=0 AND lease_owner IS NULL AND lease_until IS NULL AND result_json IS NULL")
        .bind(format!("youtube-download:{task}")).bind(task.to_string()).execute(&mut *tx).await?.rows_affected();
    sqlx::query("UPDATE tasks SET runtime_owner='rust',runtime_origin=COALESCE(runtime_origin,'legacy_adopted'),updated_at=now() WHERE id=$1 AND runtime_owner='python'")
        .bind(task).execute(&mut *tx).await?;
    let flow = if let Some(stage) = stage {
        Some(crate::scheduler::Scheduler::adopt_resume(&mut tx, task, stage).await?)
    } else {
        None
    };
    let result = json!({"task_id":task,"previous_owner":"python","previous_status":status,"runtime_owner":"rust","workflow_id":flow,"resumed_stage":stage.map(|s|s.as_str()),"verified_assets":media,"retired_unattempted_downloads":retired,"retired_hatchet_runs":confirmed,"automatic_publish":false});
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,task_id,request_json,result_json,completed_at) VALUES($1,$2,$3,'completed',$4,$5,$6,now())")
        .bind(Uuid::new_v4()).bind(&key).bind(ENGINE).bind(task).bind(json!({"resume":resume})).bind(&result).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(result)
}
