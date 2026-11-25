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
            || record.kind != "video_final"
            || record.storage_key != super::remote_render::output_key(task, id)
        {
            return Err(AppError::Conflict(
                "completed render asset identity mismatch".into(),
            ));
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
pub async fn tick(db: &Database, config: &Config, limit: i64) -> AppResult<u64> {
    let store = AssetRegistry::new(db.clone(), config.storage_root.clone());
    let scheduler = Scheduler::new(db.clone(), config.job_lease);
    // A dead worker cannot send an explicit failure callback. Reclaim attempts
    // must obey the same budget as callback-driven retries. Persist terminal
    // evidence first so an interrupted tick is recovered by the receipt scan.
    // Do not touch an active lease, paused flow, unrelated ticket or legacy row.
    sqlx::query("WITH exhausted AS (
      SELECT e.id FROM render_executions e JOIN operation_inbox j ON j.id=e.job_id
      JOIN pipeline_runs w ON w.id=e.workflow_id JOIN tasks t ON t.id=e.task_id
      WHERE e.runtime_engine='rust-v2' AND j.runtime_engine='rust-v2' AND j.stage='render'
        AND j.task_id=e.task_id AND j.workflow_id=e.workflow_id AND j.status='awaiting'
        AND j.external_ticket=e.id::text AND w.engine='rust-v2' AND w.task_id=e.task_id AND w.state='running'
        AND t.runtime_owner='rust' AND t.status::text NOT IN ('canceled','failed')
        AND e.attempt >= $1 AND (e.state='queued' OR
          (e.state IN ('running','uploading') AND e.lease_until<=now()))
      ORDER BY e.updated_at,e.id LIMIT $2 FOR UPDATE OF e SKIP LOCKED
    ) UPDATE render_executions e SET state='failed',lease_until=NULL,
      error_message='remote render attempt budget exhausted after worker lease expiry',
      finished_at=now(),updated_at=now() FROM exhausted WHERE e.id=exhausted.id")
        .bind(crate::remote_render::MAX_RENDER_ATTEMPTS)
        .bind(limit.clamp(1,100)).execute(&db.pool).await?;
    let rows=sqlx::query("SELECT e.* FROM render_executions e JOIN operation_inbox j ON j.id=e.job_id
      JOIN pipeline_runs w ON w.id=e.workflow_id JOIN tasks t ON t.id=e.task_id
      WHERE e.runtime_engine='rust-v2' AND j.runtime_engine='rust-v2' AND j.stage='render'
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
