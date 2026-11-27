//! Durable Rust render-control notifications and recovery. The original
//! render execution row is the source of truth, not a broker message.
use crate::{AppError, AppResult, Config, Database, scheduler::Scheduler, storage::AssetRegistry};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

pub(crate) async fn record_p2_failure(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    dub: Uuid,
    render: Uuid,
    execution: Uuid,
    message: &str,
) -> AppResult<()> {
    let key = format!("workflow-dubbing-finished:{dub}:failed");
    // Never race a broker delivery with a native acknowledgment. Untouched
    // pending rows can be settled in this same transaction; an attempted
    // legacy delivery needs explicit reconciliation.
    let old=sqlx::query("SELECT id,status,attempt_count,lease_owner,lease_until,delivered_at,task_name FROM outbox_events WHERE operation_key=$1 FOR UPDATE")
        .bind(&key).fetch_all(&mut **tx).await?;
    for row in &old {
        if row.try_get::<String, _>("status")? == "native_completed"
            && row.try_get::<String, _>("task_name")? == "rust.render.dubbing_failed"
        {
            continue;
        }
        if row.try_get::<String, _>("status")? != "pending"
            || row.try_get::<i32, _>("attempt_count")? != 0
            || row.try_get::<Option<String>, _>("lease_owner")?.is_some()
            || row
                .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("lease_until")?
                .is_some()
            || row
                .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("delivered_at")?
                .is_some()
            || row.try_get::<String, _>("task_name")?
                != "subtitle_service.push_dubbing_workflow_event"
        {
            return Err(AppError::Conflict(
                "legacy dubbing event may already have been delivered".into(),
            ));
        }
    }
    // Historical dubbing jobs are NOT interchangeable with native stage IDs.
    // Preserve the original business record and an exact durable notification;
    // never settle an unrelated Rust workflow by task ID alone.
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,task_id,request_json,result_json,completed_at)
       VALUES($1,$2,'rust-render-events','completed',$3,$4,$5,now())
       ON CONFLICT(operation_key) DO NOTHING")
        .bind(Uuid::new_v4()).bind(&key).bind(task)
        .bind(json!({"dubbing_job_id":dub,"render_job_id":render,"execution_id":execution}))
        .bind(json!({"status":"failed","error":message})).execute(&mut **tx).await?;
    let owned:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key=$1 AND runtime_engine='rust-render-events' AND status='completed'
        AND task_id=$2 AND request_json->>'execution_id'=$3)")
        .bind(&key).bind(task).bind(execution.to_string()).fetch_one(&mut **tx).await?;
    if !owned {
        return Err(AppError::Conflict(
            "historical dubbing notification belongs to another runtime".into(),
        ));
    }
    for row in old {
        sqlx::query("UPDATE outbox_events SET status='native_completed',task_name='rust.render.dubbing_failed',delivered_at=now(),updated_at=now() WHERE id=$1 AND status='pending'")
            .bind(row.try_get::<Uuid,_>("id")?).execute(&mut **tx).await?;
    }
    Ok(())
}

/// Restore the exact completed receipt after a crash between the durable
/// execution update and scheduler settlement. For pre-upgrade rows only
/// derive the receipt from an explicitly linked uploaded asset or pinned
/// noop input, never from the newest arbitrary task asset.
pub(crate) async fn completed_receipt(
    db: &Database,
    store: &AssetRegistry,
    row: &sqlx::postgres::PgRow,
) -> AppResult<Value> {
    let id: Uuid = row.try_get("id")?;
    let task: Uuid = row.try_get("task_id")?;
    let mode: String = row.try_get("mode")?;
    let saved: Value = row.try_get("output_json")?;
    let asset: Option<Uuid> = row.try_get("uploaded_asset_id")?;
    let (asset, key) = if mode == "noop" {
        let spec: Value = row.try_get("render_spec")?;
        let key = spec["request"]["input_key"]
            .as_str()
            .filter(|k| !k.is_empty())
            .ok_or_else(|| AppError::Conflict("completed noop has no pinned input".into()))?;
        let owned:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND storage_key=$2 AND kind::text='video_final')")
            .bind(task).bind(key).fetch_one(&db.pool).await?;
        if !owned {
            return Err(AppError::Conflict(
                "completed noop final asset missing".into(),
            ));
        }
        (None, key.to_owned())
    } else {
        let asset = asset
            .ok_or_else(|| AppError::Conflict("completed render has no uploaded asset".into()))?;
        let record = db.asset(asset).await?;
        if record.task_id != task
            || record.kind != crate::remote_render::output_kind(&mode)
            || !crate::remote_render::matches_output_key(task,id,&mode,&record.storage_key)
        {
            return Err(AppError::Conflict(
                "completed render asset identity mismatch".into(),
            ));
        }
        if mode == crate::media::dubbing_handoff::MODE {
            let (sha, size) = store.sha256(&record.storage_key).await?;
            if record.sha256.as_deref() != Some(&sha) || record.size_bytes != Some(size) {
                return Err(AppError::Conflict("completed P2 checksum mismatch".into()));
            }
        }
        (Some(asset), record.storage_key)
    };
    let path = store.resolve_existing(&key).await?;
    let info = tokio::fs::metadata(path).await?;
    if !info.is_file() || info.len() == 0 {
        return Err(AppError::Conflict("completed render output missing".into()));
    }
    let expected = crate::remote_render::receipt(id, asset, Some(&key), &mode);
    if saved.get("remote_completed").is_some() && saved != expected {
        return Err(AppError::Conflict(
            "stored render receipt does not match asset".into(),
        ));
    }
    Ok(expected)
}
/// Called only by the physical GC transaction after checking current TTL,
/// ownership, shared references, and task activity. The caller holds the task
/// and render_executions table fence and rolls back on any failed unlink.
/// Never retire a failed/canceled upload whose outcome was not settled.
pub(crate) async fn retire_asset_refs(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    key: &str,
    full_retention: bool,
) -> AppResult<bool> {
    let rows=sqlx::query("SELECT e.*,a.task_id AS asset_task,a.kind::text AS asset_kind
        FROM render_executions e JOIN assets a ON a.id=e.uploaded_asset_id
        WHERE a.storage_key=$1 ORDER BY e.id FOR UPDATE OF e")
        .bind(key).fetch_all(&mut **tx).await?;
    if rows.is_empty(){return Ok(true)}
    if !full_retention{return Ok(false)}
    for row in rows {
        let id:Uuid=row.try_get("id")?;
        let mode:String=row.try_get("mode")?;
        let asset:Uuid=row.try_get("uploaded_asset_id")?;
        let owner:Option<Uuid>=row.try_get("task_id")?;
        let job:Option<Uuid>=row.try_get("job_id")?;
        let flow:Option<Uuid>=row.try_get("workflow_id")?;
        let saved:Value=row.try_get("output_json")?;
        let expected=crate::remote_render::receipt(id,Some(asset),Some(key),&mode);
        if row.try_get::<String,_>("runtime_engine")?!="rust-v2"
            || row.try_get::<String,_>("state")?!="completed"
            || row.try_get::<Option<chrono::DateTime<chrono::Utc>>,_>("lease_until")?.is_some()
            || row.try_get::<Option<chrono::DateTime<chrono::Utc>>,_>("finished_at")?.is_none()
            || owner!=Some(task) || row.try_get::<Uuid,_>("asset_task")?!=task
            || mode=="noop" || row.try_get::<String,_>("asset_kind")?!=crate::remote_render::output_kind(&mode)
            || !crate::remote_render::matches_output_key(task,id,&mode,key)
            || job.is_none() || flow.is_none()
            || saved!=expected {return Ok(false)}
        let settled:bool=sqlx::query_scalar("SELECT EXISTS(
            SELECT 1 FROM operation_inbox j JOIN pipeline_runs w ON w.id=j.workflow_id
            WHERE j.id=$1 AND j.task_id=$2 AND j.workflow_id=$3 AND j.runtime_engine='rust-v2'
            AND j.stage=$4 AND j.external_ticket=$5 AND j.status='succeeded'
            AND j.result_json=$6 AND j.lease_until IS NULL AND j.lease_owner IS NULL
            AND w.engine='rust-v2' AND w.task_id=$2 AND w.state IN ('succeeded','failed','canceled'))")
            .bind(job).bind(task).bind(flow).bind(crate::remote_render::output_stage(&mode))
            .bind(id.to_string()).bind(&expected).fetch_one(&mut **tx).await?;
        if !settled {return Ok(false)}
        let changed=sqlx::query("UPDATE render_executions SET uploaded_asset_id=NULL,
            metrics=COALESCE(metrics,'{}'::jsonb)||jsonb_build_object('resource_retired',true),
            updated_at=now() WHERE id=$1 AND uploaded_asset_id=$2 AND state='completed'
            AND jsonb_typeof(metrics)='object'")
            .bind(id).bind(asset).execute(&mut **tx).await?.rows_affected();
        if changed!=1{return Ok(false)}
    }
    Ok(true)
}

/// A late callback after successful TTL retirement is an acknowledgement
/// of historical completion, not proof that bytes still exist. The settled
/// job and immutable execution receipt must agree exactly.
pub(crate) async fn retired_receipt(db:&Database,id:Uuid,task:Uuid,job:Uuid,flow:Uuid,mode:&str)->AppResult<Option<Value>> {
    let saved:Option<Value>=sqlx::query_scalar("SELECT e.output_json FROM render_executions e
        JOIN operation_inbox j ON j.id=e.job_id
        WHERE e.id=$1 AND e.task_id=$2 AND e.job_id=$3 AND e.workflow_id=$4
        AND e.mode=$5 AND e.runtime_engine='rust-v2' AND e.state='completed'
        AND e.uploaded_asset_id IS NULL AND e.finished_at IS NOT NULL
        AND e.metrics->'resource_retired'='true'::jsonb
        AND j.task_id=$2 AND j.workflow_id=$4 AND j.runtime_engine='rust-v2'
        AND j.status='succeeded' AND j.stage=$6 AND j.external_ticket=$7
        AND j.result_json=e.output_json")
        .bind(id).bind(task).bind(job).bind(flow).bind(mode)
        .bind(crate::remote_render::output_stage(mode)).bind(id.to_string())
        .fetch_optional(&db.pool).await?;
    let Some(saved)=saved else{return Ok(None)};
    let asset=saved["asset_id"].as_str().and_then(|v|Uuid::parse_str(v).ok());
    let key=saved["storage_key"].as_str().unwrap_or("");
    if asset.is_none() || !crate::remote_render::matches_output_key(task,id,mode,key) || saved!=crate::remote_render::receipt(id,asset,Some(key),mode) {
        return Err(AppError::Conflict("retired render receipt identity mismatch".into()))
    }
    Ok(Some(saved))
}

// An upload is acknowledged only after ffprobe, fsync, rename, asset insert
// and storage reference commit. Recover its lost completion callback without
// claiming a second worker. Require both the original asset and object ledger
// to match the exact bytes, not just an arbitrary final asset of this task.
async fn recover_uploaded(
    db: &Database,
    store: &AssetRegistry,
    row: &sqlx::postgres::PgRow,
) -> AppResult<()> {
    let id: Uuid = row.try_get("id")?;
    let task: Uuid = row.try_get("task_id")?;
    let flow: Uuid = row.try_get("workflow_id")?;
    let job: Uuid = row.try_get("job_id")?;
    let asset: Uuid = row.try_get("uploaded_asset_id")?;
    let mode: String = row.try_get("mode")?;
    let record = db.asset(asset).await?;
    let key=record.storage_key.clone();
    if mode == "noop"
        || record.task_id != task
        || record.kind != crate::remote_render::output_kind(&mode)
        || !crate::remote_render::matches_output_key(task,id,&mode,&key)
    {
        return Err(AppError::Conflict(
            "uploaded render asset identity mismatch".into(),
        ));
    }
    let sha = record
        .sha256
        .filter(|v| v.len() == 64)
        .ok_or_else(|| AppError::Conflict("uploaded render checksum absent".into()))?;
    let size = record
        .size_bytes
        .filter(|v| *v > 0)
        .ok_or_else(|| AppError::Conflict("uploaded render size absent".into()))?;
    let path = store.resolve_existing(&key).await?;
    let metadata = tokio::fs::metadata(path).await?;
    if !metadata.is_file() || metadata.len() != size as u64 {
        return Err(AppError::Conflict("uploaded render size mismatch".into()));
    }
    // Hash outside the row locks. A still-active worker cannot be recovered;
    // the final transaction rechecks the lease and every durable identity.
    let (actual_sha, actual_size) = store.sha256(&key).await?;
    if actual_sha != sha || actual_size != size {
        return Err(AppError::Conflict(
            "uploaded render checksum mismatch".into(),
        ));
    }
    let mut tx = db.pool.begin().await?;
    let task_ok:Option<bool>=sqlx::query_scalar("SELECT runtime_owner='rust' AND status::text NOT IN ('canceled','failed','publishing','published') FROM tasks WHERE id=$1 FOR UPDATE")
        .bind(task).fetch_optional(&mut *tx).await?;
    let flow_ok:Option<bool>=sqlx::query_scalar("SELECT engine='rust-v2' AND task_id=$2 AND state='running' AND current_stage=$3 FROM pipeline_runs WHERE id=$1 FOR UPDATE")
        .bind(flow).bind(task).bind(crate::remote_render::output_stage(&mode)).fetch_optional(&mut *tx).await?;
    let job_ok:Option<bool>=sqlx::query_scalar("SELECT runtime_engine='rust-v2' AND stage=$5 AND task_id=$2 AND workflow_id=$3 AND status='awaiting' AND external_ticket=$4 FROM operation_inbox WHERE id=$1 FOR UPDATE")
        .bind(job).bind(task).bind(flow).bind(id.to_string()).bind(crate::remote_render::output_stage(&mode)).fetch_optional(&mut *tx).await?;
    if task_ok != Some(true) || flow_ok != Some(true) || job_ok != Some(true) {
        return Ok(());
    }
    let result = crate::remote_render::receipt(id, Some(asset), Some(&key), &mode);
    // No output can be reassigned or overwritten by this path. Recheck the
    // registered upload evidence at the same commit as the durable receipt.
    let updated=sqlx::query("UPDATE render_executions e SET state='completed',progress=100,output_json=$5,lease_until=NULL,error_message=NULL,finished_at=now(),updated_at=now()
      WHERE e.id=$1 AND e.task_id=$2 AND e.workflow_id=$3 AND e.job_id=$4 AND e.runtime_engine='rust-v2'
        AND e.mode=$6 AND e.uploaded_asset_id=$7 AND (e.state='queued' OR (e.state IN ('running','uploading') AND e.lease_until<=now()))
        AND EXISTS(SELECT 1 FROM assets a JOIN storage_objects o ON o.storage_key=a.storage_key
          JOIN storage_object_refs r ON r.storage_key=a.storage_key
          WHERE a.id=$7 AND a.task_id=$2 AND a.kind::text=$11 AND a.storage_key=$8 AND a.sha256=$9 AND a.size_bytes=$10
            AND o.state='live' AND o.checksum_sha256=$9 AND o.size_bytes=$10 AND r.owner_type='workflow' AND r.owner_id=$3 AND r.role=$11)")
      .bind(id).bind(task).bind(flow).bind(job).bind(result).bind(&mode).bind(asset).bind(key).bind(sha).bind(size).bind(crate::remote_render::output_kind(&mode)).execute(&mut *tx).await?;
    if updated.rows_affected() == 0 {
        // A concurrent tick/callback may already have changed the state.
        let current: String = sqlx::query_scalar("SELECT state FROM render_executions WHERE id=$1")
            .bind(id)
            .fetch_one(&mut *tx)
            .await?;
        if matches!(current.as_str(), "queued" | "running" | "uploading") {
            return Err(AppError::Conflict(
                "uploaded render evidence or lease changed".into(),
            ));
        }
    }
    tx.commit().await?;
    Ok(())
}
pub async fn tick(db: &Database, config: &Config, limit: i64) -> AppResult<u64> {
    let store = AssetRegistry::new(db.clone(), config.storage_root.clone());
    let scheduler = Scheduler::new(db.clone(), config.job_lease);
    // Canceled executions retain their former slot until stop acknowledgment
    // or lease expiry; never extend a canceled lease or requeue that execution.
    sqlx::query("WITH expired AS (SELECT id FROM render_executions WHERE runtime_engine='rust-v2' AND state IN ('canceled','paused') AND lease_until<=now() ORDER BY lease_until,id LIMIT $1 FOR UPDATE SKIP LOCKED) UPDATE render_executions e SET lease_until=NULL,updated_at=now() FROM expired WHERE e.id=expired.id")
        .bind(limit.clamp(1,100)).execute(&db.pool).await?;
    // Resume only after the former lease is drained. Keep uploaded asset
    // evidence and original fence for completed-receipt replay; the claim
    // path excludes uploaded assets. Do not reset the attempt budget.
    sqlx::query("WITH ready AS (SELECT e.id FROM render_executions e JOIN operation_inbox j ON j.id=e.job_id JOIN pipeline_runs w ON w.id=e.workflow_id JOIN tasks t ON t.id=e.task_id
      WHERE e.runtime_engine='rust-v2' AND e.state='paused' AND (e.lease_until IS NULL OR e.lease_until<=now())
        AND j.runtime_engine='rust-v2' AND ((j.stage='render' AND e.mode<>'dub_burn_in') OR (j.stage='dubbing' AND e.mode='dub_burn_in')) AND j.status='awaiting' AND j.task_id=e.task_id AND j.workflow_id=e.workflow_id AND j.external_ticket=e.id::text
        AND w.engine='rust-v2' AND w.task_id=e.task_id AND w.state='running' AND w.current_stage=j.stage
        AND t.runtime_owner='rust' AND t.status::text NOT IN ('canceled','failed','publishing','published')
      ORDER BY e.updated_at,e.id LIMIT $1 FOR UPDATE OF e SKIP LOCKED)
      UPDATE render_executions e SET state='queued',lease_until=NULL,error_message=NULL,updated_at=now(),progress=CASE WHEN e.uploaded_asset_id IS NULL THEN 0 ELSE e.progress END,
        worker_id=CASE WHEN e.uploaded_asset_id IS NULL THEN NULL ELSE e.worker_id END,fence_token=CASE WHEN e.uploaded_asset_id IS NULL THEN NULL ELSE e.fence_token END FROM ready WHERE e.id=ready.id")
       .bind(limit.clamp(1,100)).execute(&db.pool).await?;
    // Limit expensive full-file verification per tick. Preserve bad evidence
    // for manual reconciliation rather than silently re-rendering or posting.
    let uploads=sqlx::query("SELECT e.* FROM render_executions e JOIN operation_inbox j ON j.id=e.job_id
      JOIN pipeline_runs w ON w.id=e.workflow_id JOIN tasks t ON t.id=e.task_id
      WHERE e.runtime_engine='rust-v2' AND j.runtime_engine='rust-v2' AND ((j.stage='render' AND e.mode<>'dub_burn_in') OR (j.stage='dubbing' AND e.mode='dub_burn_in'))
        AND j.task_id=e.task_id AND j.workflow_id=e.workflow_id AND j.status='awaiting'
        AND j.external_ticket=e.id::text AND w.engine='rust-v2' AND w.task_id=e.task_id AND w.state='running' AND w.current_stage=j.stage
        AND t.runtime_owner='rust' AND t.status::text NOT IN ('canceled','failed','publishing','published')
        AND e.uploaded_asset_id IS NOT NULL AND (e.state='queued' OR (e.state IN ('running','uploading') AND e.lease_until<=now()))
      ORDER BY e.updated_at,e.id LIMIT $1").bind(limit.clamp(1,4)).fetch_all(&db.pool).await?;
    for row in uploads {
        if let Err(error) = recover_uploaded(db, &store, &row).await {
            let id: Uuid = row.try_get("id")?;
            tracing::warn!(execution_id=%id,%error,"uploaded render recovery deferred");
            sqlx::query("UPDATE render_executions SET updated_at=now() WHERE id=$1 AND runtime_engine='rust-v2' AND uploaded_asset_id IS NOT NULL AND (state='queued' OR (state IN ('running','uploading') AND lease_until<=now()))")
                .bind(id).execute(&db.pool).await?;
        }
    }
    // A dead worker cannot send an explicit failure callback. Reclaim attempts
    // must obey the same budget as callback-driven retries. Persist terminal
    // evidence first so an interrupted tick is recovered by the receipt scan.
    // Do not touch an active lease, paused flow, unrelated ticket or legacy row.
    sqlx::query("WITH exhausted AS (
      SELECT e.id FROM render_executions e JOIN operation_inbox j ON j.id=e.job_id
      JOIN pipeline_runs w ON w.id=e.workflow_id JOIN tasks t ON t.id=e.task_id
      WHERE e.runtime_engine='rust-v2' AND j.runtime_engine='rust-v2' AND ((j.stage='render' AND e.mode<>'dub_burn_in') OR (j.stage='dubbing' AND e.mode='dub_burn_in'))
        AND j.task_id=e.task_id AND j.workflow_id=e.workflow_id AND j.status='awaiting'
        AND j.external_ticket=e.id::text AND w.engine='rust-v2' AND w.task_id=e.task_id AND w.state='running'
        AND t.runtime_owner='rust' AND t.status::text NOT IN ('canceled','failed')
        AND e.uploaded_asset_id IS NULL AND e.attempt >= $1 AND (e.state='queued' OR
          (e.state IN ('running','uploading') AND e.lease_until<=now()))
      ORDER BY e.updated_at,e.id LIMIT $2 FOR UPDATE OF e SKIP LOCKED
    ) UPDATE render_executions e SET state='failed',lease_until=NULL,
      error_message='remote render attempt budget exhausted after worker lease expiry',
      finished_at=now(),updated_at=now() FROM exhausted WHERE e.id=exhausted.id")
        .bind(crate::remote_render::MAX_RENDER_ATTEMPTS)
        .bind(limit.clamp(1,100)).execute(&db.pool).await?;
    let rows=sqlx::query("SELECT e.* FROM render_executions e JOIN operation_inbox j ON j.id=e.job_id
      JOIN pipeline_runs w ON w.id=e.workflow_id JOIN tasks t ON t.id=e.task_id
      WHERE e.runtime_engine='rust-v2' AND j.runtime_engine='rust-v2' AND ((j.stage='render' AND e.mode<>'dub_burn_in') OR (j.stage='dubbing' AND e.mode='dub_burn_in'))
        AND j.task_id=e.task_id AND j.workflow_id=e.workflow_id AND j.status='awaiting'
        AND j.external_ticket=e.id::text AND w.engine='rust-v2' AND w.task_id=e.task_id AND w.state='running'
        AND t.runtime_owner='rust' AND t.status::text<>'canceled'
        AND e.state IN ('completed','failed')
      ORDER BY e.updated_at,e.id LIMIT $1").bind(limit.clamp(1,100)).fetch_all(&db.pool).await?;
    let mut n = 0;
    for row in rows {
        let id: Uuid = row.try_get("id")?;
        let job: Uuid = row.try_get("job_id")?;
        let state: String = row.try_get("state")?;
        let result = if state == "completed" {
            match completed_receipt(db, &store, &row).await {
                Ok(receipt) => {
                    scheduler
                        .complete_external(job, &id.to_string(), receipt)
                        .await
                }
                Err(e) => Err(e),
            }
        } else {
            let error: Option<String> = row.try_get("error_message")?;
            scheduler
                .fail_external(
                    job,
                    &id.to_string(),
                    error.as_deref().unwrap_or("remote render failed"),
                )
                .await
        };
        match result {
            Ok(()) => n += 1,
            Err(e) => {
                tracing::warn!(execution_id=%id,error=%e,"render receipt recovery deferred");
                // Move the failed candidate behind other receipts. Do not
                // modify its immutable output or claim success.
                sqlx::query("UPDATE render_executions SET updated_at=now() WHERE id=$1 AND runtime_engine='rust-v2' AND state IN ('completed','failed')")
                    .bind(id).execute(&db.pool).await?;
            }
        }
    }
    Ok(n)
}
