//! Restore Python's read-time standalone published-task compensation.
//! Only proven, quiescent Rust-owned tasks qualify; never replay publishing.
use crate::{AppResult, Database};
use sqlx::Row;
use uuid::Uuid;

/// Called only by the original task list/detail routes, not by internal reads.
/// Lock candidate tasks in stable order, then fence direct receipt/ledger writers.
/// Contention is harmless: return the current stored state, try on a later read.
pub(crate) async fn repair(db:&Database, ids:&[Uuid])->AppResult<Vec<Uuid>> {
    if ids.is_empty(){return Ok(Vec::new())}
    let mut tx=db.pool.begin().await?;
    let candidates:Vec<Uuid>=sqlx::query_scalar("SELECT t.id FROM tasks t
        WHERE t.id=ANY($1) AND t.runtime_owner='rust'
          AND t.status::text NOT IN ('published','canceled') AND t.stopped_status IS NULL
          AND t.active_publish_batch_id IS NULL
          AND to_jsonb(t)->>'lock_owner' IS NULL AND to_jsonb(t)->>'lock_until' IS NULL
          AND EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=t.id AND j.batch_id IS NULL AND j.state::text='published')
          AND NOT EXISTS(SELECT 1 FROM publish_batches b WHERE b.task_id=t.id)
        ORDER BY t.id FOR UPDATE OF t SKIP LOCKED")
        .bind(ids).fetch_all(&mut *tx).await?;
    if candidates.is_empty(){return Ok(Vec::new())}
    // These are the original physical tables (the vr2 views project these).
    // NOWAIT avoids blocking the UI or deadlocking a legacy writer.
    if let Err(e)=sqlx::query("LOCK TABLE pipeline_runs,operation_inbox,subtitle_jobs,render_jobs,render_executions,dubbing_jobs,publish_jobs,publish_batches,outbox_events IN SHARE ROW EXCLUSIVE MODE NOWAIT").execute(&mut *tx).await {
        if e.as_database_error().and_then(|e|e.code()).as_deref()==Some("55P03"){return Ok(Vec::new())}
        return Err(e.into())
    }
    let mut changed=Vec::new();
    for task in candidates {
        let blockers=crate::task_ownership::blockers(&mut tx,task).await?;
        if blockers.iter().any(|b| b!="task_publishing"){continue}
        // All surviving independent attempts must agree on their confirmed
        // outcome. Even old failed or ambiguous attempts require review:
        // do not infer which target is current without a batch snapshot.
        let row=sqlx::query("SELECT count(*) AS count,
            count(*) FILTER (WHERE batch_id IS NULL AND state::text='published'
              AND lease_owner IS NULL AND lease_until IS NULL AND NOT upload_active
              AND COALESCE(response_json->>'receipt_verified','true')<>'false'
              AND COALESCE(response_json->>'confirmed','true')<>'false'
              AND CASE WHEN platform::text='bilibili'
                    THEN COALESCE(NULLIF(trim(bvid),''),NULLIF(trim(aid),''),NULLIF(trim(external_id),'')) IS NOT NULL
                    ELSE COALESCE(NULLIF(trim(external_id),''),NULLIF(trim(external_url),'')) IS NOT NULL END) AS confirmed,
            max(COALESCE(finished_at,updated_at)) AS receipt_at
            FROM publish_jobs WHERE task_id=$1")
            .bind(task).fetch_one(&mut *tx).await?;
        let count:i64=row.try_get("count")?;
        if count==0 || row.try_get::<i64,_>("confirmed")?!=count{continue}
        let receipt_at:Option<chrono::DateTime<chrono::Utc>>=row.try_get("receipt_at")?;
        let busy:bool=sqlx::query_scalar("SELECT
          EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1)
          OR EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND created_at>$2)
          OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1)
          OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE task_id=$1)
          OR EXISTS(SELECT 1 FROM render_executions e LEFT JOIN render_jobs j ON e.render_job_id=j.id
              WHERE (e.task_id=$1 OR j.task_id=$1) AND (to_jsonb(e)->>'lease_owner' IS NOT NULL OR e.lease_until IS NOT NULL))
          OR EXISTS(SELECT 1 FROM operation_inbox i WHERE
              (i.task_id=$1 OR i.request_json->>'task_id'=$1::text OR i.operation_key='youtube-download:'||$1::text
                OR EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1 AND
                   (i.operation_key=j.operation_key OR i.operation_key='publish-job:'||j.id::text)))
              AND (i.status NOT IN ('completed','succeeded','failed','canceled') OR i.lease_owner IS NOT NULL OR i.lease_until IS NOT NULL OR i.lease_token IS NOT NULL))
          OR EXISTS(SELECT 1 FROM outbox_events e LEFT JOIN operation_inbox i ON i.operation_key=e.operation_key
              WHERE (e.aggregate_type='task' AND e.aggregate_id=$1::text
                 OR EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1 AND
                       (e.operation_key=j.operation_key OR e.operation_key='publish-job:'||j.id::text
                        OR (e.aggregate_type='publish_job' AND e.aggregate_id=j.id::text))))
              AND (e.lease_owner IS NOT NULL OR e.lease_until IS NOT NULL OR e.status NOT IN ('dispatched','native_completed')
                 OR i.status IS DISTINCT FROM 'completed'))")
            .bind(task).bind(receipt_at).fetch_one(&mut *tx).await?;
        if busy{continue}
        let n=sqlx::query("UPDATE tasks SET status='published',error_code=NULL,error_message=NULL,updated_at=now()
              WHERE id=$1 AND runtime_owner='rust' AND status::text NOT IN ('published','canceled')
                AND stopped_status IS NULL AND active_publish_batch_id IS NULL")
            .bind(task).execute(&mut *tx).await?.rows_affected();
        if n==1{changed.push(task)}
    }
    tx.commit().await?;
    Ok(changed)
}
