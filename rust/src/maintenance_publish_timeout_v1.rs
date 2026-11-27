//! Original task-level publishing timeout, without replaying an irreversible
//! external action. Batch-owned publication remains with its own reconciler.
use crate::{AppResult, Database};
use chrono::{DateTime, Duration, Utc};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

pub type Cursor = Option<(DateTime<Utc>, Uuid)>;

pub fn timeout_hours() -> i64 {
    std::env::var("PUBLISHING_TASK_TIMEOUT_HOURS").ok()
        .and_then(|s| s.parse::<i64>().ok()).unwrap_or(48).clamp(1, 3650 * 24)
}

/// Ordinary workflow restart is not a publication receipt reconciliation.
/// Use original rows as a fence even when there is no native workflow.
pub(crate) async fn publication_fence(tx:&mut Transaction<'_,Postgres>, task:Uuid)->AppResult<bool> {
    Ok(sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND
      (state::text IN ('draft','submitting','submitted','published','unknown')
       OR NULLIF(external_id,'') IS NOT NULL OR NULLIF(external_url,'') IS NOT NULL
       OR NULLIF(bvid,'') IS NOT NULL OR NULLIF(aid,'') IS NOT NULL))")
       .bind(task).fetch_one(&mut **tx).await?)
}

async fn recover_one(db:&Database, task:Uuid, cutoff:DateTime<Utc>, hours:i64)->AppResult<bool> {
    let mut tx=db.pool.begin().await?;
    let parent:Option<Uuid>=sqlx::query_scalar("SELECT id FROM tasks t
       WHERE id=$1 AND runtime_owner='rust' AND status::text='publishing' AND updated_at<$2
       AND stopped_status IS NULL AND to_jsonb(t)->>'lock_owner' IS NULL
       AND to_jsonb(t)->>'lock_until' IS NULL
       FOR UPDATE SKIP LOCKED")
       .bind(task).bind(cutoff).fetch_optional(&mut *tx).await?;
    if parent.is_none(){return Ok(false)}
    // Legacy writers may update a receipt without taking the task lock.
    // Never wait for a competing write while holding the parent lock.
    sqlx::query("LOCK TABLE publish_jobs,publish_batches,operation_inbox,outbox_events IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        .execute(&mut *tx).await?;
    let blockers=crate::task_ownership::blockers(&mut tx,task).await?;
    if blockers.iter().any(|s| !matches!(s.as_str(),"task_publishing"|"publication_unsettled")){
        return Ok(false);
    }
    let busy:bool=sqlx::query_scalar("SELECT
      EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1)
      OR EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown'))
      OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND
          (state IN ('pending','running','awaiting','unknown') OR
           (stage='publish' AND state='succeeded' AND COALESCE(result->>'skipped','')='')))
      OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1)
      OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE task_id=$1)
      OR EXISTS(SELECT 1 FROM operation_inbox i WHERE
           (i.task_id=$1 OR i.request_json->>'task_id'=$1::text OR
            EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1 AND
              (i.operation_key=j.operation_key OR i.operation_key='publish-job:'||j.id::text)))
           AND (i.status NOT IN ('completed','succeeded','failed','canceled')
             OR i.lease_owner IS NOT NULL OR i.lease_until IS NOT NULL))
      OR EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND
          (updated_at >= $2 OR heartbeat_at >= $2 OR lease_until>now()
           OR state::text NOT IN ('draft','submitting','unknown','failed')
           OR NULLIF(external_id,'') IS NOT NULL OR NULLIF(external_url,'') IS NOT NULL
           OR NULLIF(bvid,'') IS NOT NULL OR NULLIF(aid,'') IS NOT NULL
           OR COALESCE(response_json->'auto_dub_p2'->>'state','') NOT IN ('','published','failed')))
      OR EXISTS(SELECT 1 FROM operation_inbox i WHERE
          (i.task_id=$1 OR i.request_json->>'task_id'=$1::text)
          AND (i.operation_key LIKE 'publish%' OR i.operation_key LIKE '%:publish:%')
          AND NOT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1))
      OR EXISTS(SELECT 1 FROM outbox_events e WHERE e.aggregate_type='task' AND e.aggregate_id=$1::text
          AND (e.event_type LIKE 'publish%' OR e.task_name LIKE '%publish%')
          AND NOT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1))")
        .bind(task).bind(cutoff).fetch_one(&mut *tx).await?;
    if busy{return Ok(false)}
    // An old draft/submitting receipt is NOT proof that the request failed
    // before delivery. Preserve evidence and quarantine; do not reset it.
    let uncertain:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1
         AND state::text IN ('draft','submitting','unknown'))")
         .bind(task).fetch_one(&mut *tx).await?;
    if uncertain {
        sqlx::query("UPDATE publish_jobs SET state='unknown',upload_active=false,
            lease_owner=NULL,lease_until=NULL,finished_at=COALESCE(finished_at,now()),updated_at=now(),
            response_json=COALESCE(response_json,'{}'::jsonb)||jsonb_build_object(
                'timeout_recovery','unknown','verify_platform_before_retry',true)
            WHERE task_id=$1 AND state::text IN ('draft','submitting')")
            .bind(task).execute(&mut *tx).await?;
    }
    let code=if uncertain {"PUBLISH_OUTCOME_UNKNOWN"}else{"PUBLISHING_TIMEOUT"};
    let message=if uncertain {
        "Publishing timed out; external outcome unconfirmed. Verify the platform before retry.".to_string()
    } else {format!("publishing status exceeded {hours} hours")};
    sqlx::query("UPDATE tasks SET status='failed',error_code=$2,error_message=$3,updated_at=now() WHERE id=$1")
        .bind(task).bind(code).bind(message).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(true)
}

/// Bounded keyset scan, rotating even if oldest rows are blocked. The cursor
/// is local to one maintenance loop; no production schema change is needed.
pub async fn tick(db:&Database, hours:i64, cursor:&mut Cursor)->AppResult<u64> {
    let hours=hours.clamp(1,3650*24);
    let cutoff=Utc::now()-Duration::hours(hours);
    let rows:Vec<(DateTime<Utc>,Uuid)>=sqlx::query_as(
        "SELECT updated_at,id FROM tasks WHERE runtime_owner='rust' AND status::text='publishing'
         AND updated_at<$1 AND ($2::timestamptz IS NULL OR (updated_at,id)>($2,$3::uuid))
         ORDER BY updated_at,id LIMIT 100")
        .bind(cutoff).bind(cursor.map(|c|c.0)).bind(cursor.map(|c|c.1))
        .fetch_all(&db.pool).await?;
    let mut changed=0;
    for &(updated,task) in &rows {
        match recover_one(db,task,cutoff,hours).await {
            Ok(true)=>changed+=1,
            Ok(false)=>{},
            Err(error)=>tracing::warn!(%task,%error,"publishing timeout deferred"),
        }
        *cursor=Some((updated,task));
    }
    if rows.len()<100 {*cursor=None;}
    Ok(changed)
}

