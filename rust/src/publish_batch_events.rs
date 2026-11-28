//! Native batch notifications and delayed intermediate media cleanup.
//! No broker or external workflow event is required. All delivery effects
//! are database-only and committed with their original outbox/inbox receipts.
use crate::{AppError, AppResult, Database};
use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

const ENGINE: &str = "rust-publish-batch";
const FINISHED: &str = "rust.publish.batch_finished";
const CLEANUP: &str = "rust.publish.cleanup";

pub(crate) async fn enqueue(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    batch: Uuid,
    status: &str,
    due: Option<DateTime<Utc>>,
) -> AppResult<()> {
    let (kind, handler, key, args) = if due.is_some() {
        (
            "publish.cleanup",
            CLEANUP,
            format!("publish-cleanup:{batch}"),
            json!({"args":[task,batch]}),
        )
    } else {
        (
            "workflow.publish.finished",
            FINISHED,
            format!("workflow-publish-finished:{batch}:{status}"),
            json!({"args":[batch,status,task]}),
        )
    };
    // Existing legacy operation keys are not silently appropriated. A safe
    // never-delivered pending event can be adopted by the migration instead.
    sqlx::query("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,available_at,status)
      SELECT $1,$2,'publish_batch',$3,$4,$5,$6,COALESCE($7,now()),'native_pending'
      WHERE NOT EXISTS(SELECT 1 FROM outbox_events WHERE operation_key=$6)")
        .bind(Uuid::new_v4()).bind(kind).bind(batch.to_string()).bind(handler).bind(args).bind(key).bind(due)
        .execute(&mut **tx).await?;
    Ok(())
}
async fn finish(
    tx: &mut Transaction<'_, Postgres>,
    event: Uuid,
    key: &str,
    result: Value,
) -> AppResult<()> {
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,result_json,request_json,completed_at)
      VALUES($1,$2,$3,'completed',$4,jsonb_build_object('outbox_event_id',$5::text),now()) ON CONFLICT(operation_key) DO NOTHING")
        .bind(Uuid::new_v4()).bind(key).bind(ENGINE).bind(&result).bind(event.to_string()).execute(&mut **tx).await?;
    let owned:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key=$1 AND runtime_engine=$2 AND status='completed')")
        .bind(key).bind(ENGINE).fetch_one(&mut **tx).await?;
    if !owned {
        return Err(AppError::Conflict(
            "legacy operation owns batch event".into(),
        ));
    }
    sqlx::query("UPDATE outbox_events SET status='native_completed',attempt_count=attempt_count+1,delivered_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE id=$1 AND status='native_pending'")
        .bind(event).execute(&mut **tx).await?;
    Ok(())
}
async fn defer(
    tx: &mut Transaction<'_, Postgres>,
    id: Uuid,
    due: DateTime<Utc>,
    why: &str,
) -> AppResult<()> {
    sqlx::query("UPDATE outbox_events SET available_at=$2,last_error=$3,updated_at=now() WHERE id=$1 AND status='native_pending'")
        .bind(id).bind(due).bind(why).execute(&mut **tx).await?;
    Ok(())
}
async fn consume(db: &Database, id: Uuid, batch: Uuid, task: Uuid) -> AppResult<bool> {
    let mut tx = db.pool.begin().await?;
    // Match the existing task -> batch -> event lock order. No external
    // side effects occur in this transaction, so rollback is safe to retry.
    let owner = sqlx::query(
        "SELECT runtime_owner,status::text AS status FROM tasks WHERE id=$1 FOR UPDATE",
    )
    .bind(task)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(owner) = owner else { return Ok(false) };
    let row=sqlx::query("SELECT state,finished_at,cleanup_enqueued_at FROM publish_batches WHERE id=$1 AND task_id=$2 FOR UPDATE")
        .bind(batch).bind(task).fetch_optional(&mut *tx).await?;
    let Some(row) = row else { return Ok(false) };
    let event=sqlx::query("SELECT operation_key,task_name,args_json FROM outbox_events WHERE id=$1 AND status='native_pending' AND available_at<=now() FOR UPDATE")
        .bind(id).fetch_optional(&mut *tx).await?;
    let Some(event) = event else { return Ok(false) };
    let handler: String = event.try_get("task_name")?;
    let key: String = event.try_get("operation_key")?;
    let status: String = row.try_get("state")?;
    let old_flow:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND engine<>'rust-v2' AND state NOT IN ('succeeded','failed','canceled','completed','superseded'))")
        .bind(task).fetch_one(&mut *tx).await?;
    if old_flow {
        defer(
            &mut tx,
            id,
            Utc::now() + Duration::minutes(30),
            "historical workflow still active",
        )
        .await?;
        tx.commit().await?;
        return Ok(false);
    }
    let args: Value = event.try_get("args_json")?;
    let expected = if handler == CLEANUP {
        key == format!("publish-cleanup:{batch}") && args["args"] == json!([task, batch])
    } else if handler == FINISHED {
        args["args"].as_array().is_some_and(|a| {
            a.len() == 3
                && a[0] == json!(batch)
                && a[2] == json!(task)
                && a[1].as_str().is_some_and(|s| {
                    matches!(s, "succeeded" | "failed" | "partial_failed")
                        && key == format!("workflow-publish-finished:{batch}:{s}")
                })
        })
    } else {
        false
    };
    if !expected || args.get("kwargs").is_some_and(|v| v != &json!({})) {
        defer(
            &mut tx,
            id,
            Utc::now() + Duration::minutes(30),
            "batch event envelope does not match original record",
        )
        .await?;
        tx.commit().await?;
        return Ok(false);
    }

    // Historical outbox keys were not unique. A duplicate native delivery
    // must acknowledge the existing receipt without repeating effects.
    let prior = sqlx::query(
        "SELECT runtime_engine,status FROM operation_inbox WHERE operation_key=$1 FOR UPDATE",
    )
    .bind(&key)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(prior) = prior {
        if prior.try_get::<String, _>("runtime_engine")? == ENGINE
            && prior.try_get::<String, _>("status")? == "completed"
        {
            sqlx::query("UPDATE outbox_events SET status='native_completed',delivered_at=COALESCE(delivered_at,now()),updated_at=now(),lease_owner=NULL,lease_until=NULL WHERE id=$1")
                .bind(id).execute(&mut *tx).await?;
        } else {
            defer(
                &mut tx,
                id,
                Utc::now() + Duration::minutes(30),
                "existing operation requires reconciliation",
            )
            .await?;
        }
        tx.commit().await?;
        return Ok(true);
    }

    if owner.try_get::<String, _>("runtime_owner")? != "rust" {
        defer(
            &mut tx,
            id,
            Utc::now() + Duration::minutes(30),
            "task is not Rust-owned",
        )
        .await?;
    } else if handler == FINISHED {
        let args: Value = event.try_get("args_json")?;
        let expected = args["args"][1].as_str().unwrap_or("");
        // A late event is only a notification, never authority to settle a
        // stage, overwrite a newer batch or resurrect a canceled workflow.
        if expected != status || status == "active" {
            finish(
                &mut tx,
                id,
                &key,
                json!({"status":"superseded","publish_batch_id":batch}),
            )
            .await?;
        } else {
            let flow:Option<Uuid>=sqlx::query_scalar("SELECT w.id FROM vr2_workflows w JOIN publish_jobs j
              ON j.id::text=COALESCE(w.configuration->'bilibili_direct_v1'->>'publish_job_id',w.configuration->>'bilibili_auto_job_id')
              WHERE w.task_id=$1 AND j.task_id=$1 AND j.batch_id=$2 ORDER BY w.generation DESC LIMIT 1")
                .bind(task).bind(batch).fetch_optional(&mut *tx).await?;
            if let Some(flow) = flow {
                sqlx::query("INSERT INTO workflow_events(id,workflow_id,kind,details) VALUES($1,$2,'publish.batch.finished',$3)")
                    .bind(Uuid::new_v4()).bind(flow).bind(json!({"task_id":task,"publish_batch_id":batch,"status":status,"operation_key":key})).execute(&mut *tx).await?;
            }
            finish(&mut tx,id,&key,json!({"status":"ok","task_id":task,"publish_batch_id":batch,"publish_status":status,"workflow_id":flow})).await?;
        }
    } else if handler == CLEANUP {
        if row
            .try_get::<Option<DateTime<Utc>>, _>("cleanup_enqueued_at")?
            .is_some()
        {
            finish(&mut tx, id, &key, json!({"status":"already_scheduled"})).await?;
            tx.commit().await?;
            return Ok(true);
        }
        let currents = crate::bilibili_batch_reconcile_v1::current_batches(&mut tx, task).await?;
        if !currents.iter().any(|(b, _, _, _)| *b == batch) {
            finish(&mut tx, id, &key, json!({"status":"superseded"})).await?;
        } else if owner.try_get::<String, _>("status")? == "canceled" {
            finish(&mut tx, id, &key, json!({"status":"canceled"})).await?;
        } else if owner.try_get::<String, _>("status")? != "published"
            || status != "succeeded"
            || currents
                .iter()
                .any(|(_, s, _, _)| *s != crate::bilibili_batch_reconcile_v1::State::Succeeded)
        {
            defer(
                &mut tx,
                id,
                Utc::now() + Duration::minutes(10),
                "current publication batches not all successful",
            )
            .await?;
        } else {
            let due = currents
                .iter()
                .map(|(_, _, t, _)| *t)
                .collect::<Option<Vec<_>>>()
                .and_then(|v| v.into_iter().max())
                .map(|t| t + Duration::hours(24));
            if due.is_none_or(|t| t > Utc::now()) {
                defer(
                    &mut tx,
                    id,
                    due.unwrap_or_else(|| Utc::now() + Duration::minutes(10)),
                    "waiting for 24h after latest target",
                )
                .await?;
            } else if crate::storage::publication_cleanup_blocked(&mut tx, task).await? {
                defer(
                    &mut tx,
                    id,
                    Utc::now() + Duration::minutes(10),
                    "media still held by active or unresolved work",
                )
                .await?;
            } else {
                // Only native registered task-owned intermediates. Do not
                // directly unlink files, scan legacy paths or shorten holds.
                let candidates=sqlx::query_scalar::<_,String>(r#"SELECT DISTINCT r.storage_key FROM storage_object_refs r
                  JOIN storage_objects o ON r.storage_key=o.storage_key AND o.state='live'
                  WHERE true
                    AND ((r.owner_type='task' AND r.owner_id=$1) OR
                         (r.owner_type='workflow' AND EXISTS(SELECT 1 FROM vr2_workflows w WHERE w.id=r.owner_id AND w.task_id=$1 AND w.state IN ('succeeded','failed','canceled'))))
                    AND EXISTS(SELECT 1 FROM assets a WHERE a.task_id=$1 AND a.storage_key=r.storage_key)
                    AND NOT EXISTS(SELECT 1 FROM assets a WHERE a.task_id=$1 AND a.storage_key=r.storage_key
                      AND a.kind::text IN ('video_final','video_dubbed','dubbing_audio','publish_result'))
                    AND NOT EXISTS(SELECT 1 FROM assets a WHERE a.task_id<>$1 AND a.storage_key=r.storage_key)
                    AND NOT EXISTS(SELECT 1 FROM subtitles s WHERE s.task_id<>$1 AND s.storage_key=r.storage_key)
                    AND (EXISTS(SELECT 1 FROM assets a WHERE a.task_id=$1 AND a.kind::text='video_final')
                         OR r.storage_key IS DISTINCT FROM (SELECT a.storage_key FROM assets a WHERE a.task_id=$1 AND a.kind::text='video_raw' ORDER BY a.created_at DESC,a.id DESC LIMIT 1))
                  "#).bind(task).fetch_all(&mut *tx).await?;
                let mut released=Vec::new();
                for key in candidates {
                    if !crate::storage::release_key_owned(&mut tx,task,&key).await? {continue;}
                    sqlx::query("DELETE FROM storage_object_refs r WHERE r.storage_key=$2 AND ((r.owner_type='task' AND r.owner_id=$1) OR (r.owner_type='workflow' AND EXISTS(SELECT 1 FROM vr2_workflows w WHERE w.id=r.owner_id AND w.task_id=$1 AND w.state IN ('succeeded','failed','canceled'))))")
                        .bind(task).bind(&key).execute(&mut *tx).await?;
                    released.push(key);
                }
                for key in &released {
                    sqlx::query("UPDATE storage_objects SET retain_until=GREATEST(retain_until,manual_hold_until,now()+interval '1 hour'),updated_at=now() WHERE storage_key=$1 AND state='live'")
                        .bind(key).execute(&mut *tx).await?;
                }
                // Explicit Rust workdirs only; no legacy WORK_DIR scan.
                // The existing collector still checks object and asset rows.
                let flow:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_workflows WHERE task_id=$1 AND state='succeeded' ORDER BY generation DESC LIMIT 1")
                    .bind(task).fetch_optional(&mut *tx).await?;
                if let Some(flow) = flow {
                    for category in ["raw", "sub", "render", "dubbing"] {
                        let path = format!("v2/{category}/{task}");
                        sqlx::query("INSERT INTO storage_workdirs(path_key,task_id,workflow_id,due_at) VALUES($1,$2,$3,now()+interval '1 hour')
                           ON CONFLICT(path_key) DO UPDATE SET due_at=GREATEST(storage_workdirs.due_at,EXCLUDED.due_at),updated_at=now()
                           WHERE storage_workdirs.state='retained' AND storage_workdirs.task_id=EXCLUDED.task_id")
                            .bind(path).bind(task).bind(flow).execute(&mut *tx).await?;
                    }
                }
                // Mark scheduling complete, not physical deletion. GC has
                // its own durable object/workdir state and retry mechanism.
                sqlx::query("UPDATE publish_batches SET cleanup_enqueued_at=now(),cleanup_delivery_version=2,updated_at=now() WHERE id=$1")
                    .bind(batch).execute(&mut *tx).await?;
                finish(&mut tx,id,&key,json!({"status":"scheduled","task_id":task,"publish_batch_id":batch,"released_refs":released.len()})).await?;
            }
        }
    } else {
        return Err(AppError::Invalid("unknown native batch event".into()));
    }
    tx.commit().await?;
    Ok(true)
}
pub async fn tick(db: &Database, limit: i64) -> AppResult<u64> {
    let rows=sqlx::query("SELECT e.id,b.id AS batch,b.task_id FROM outbox_events e JOIN publish_batches b ON b.id::text=e.aggregate_id
        WHERE e.task_name IN ('rust.publish.batch_finished','rust.publish.cleanup') AND e.status='native_pending'
        AND e.available_at<=now() ORDER BY e.available_at,e.created_at,e.id LIMIT $1")
        .bind(limit.clamp(1,100)).fetch_all(&db.pool).await?;
    let mut n = 0;
    for row in rows {
        let id: Uuid = row.try_get("id")?;
        match consume(db, id, row.try_get("batch")?, row.try_get("task_id")?).await {
            Ok(true) => n += 1,
            Ok(false) => {}
            Err(e) => tracing::warn!(event_id=%id,error=%e,"native publication event deferred"),
        }
    }
    Ok(n)
}
