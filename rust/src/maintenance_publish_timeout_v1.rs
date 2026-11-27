//! Original task-level publishing timeout, without replaying an irreversible
//! external action. Explicitly native, quiescent batches use the normal
//! per-target reconciler; ambiguous historical ownership is never inferred.
use crate::{AppResult, Database};
use chrono::{DateTime, Duration, Utc};
use serde_json::Value;
use sqlx::{Postgres, Row, Transaction};
use std::collections::HashSet;
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

/// A waiting automatic parent is not an executing publisher. Verify the
/// complete immutable admission identity before exempting that exact pair
/// from the usual ownership blockers. No other lease or waiter is exempted.
async fn verified_waiter(tx:&mut Transaction<'_,Postgres>,task:Uuid,batch:Uuid,parent:&Value,cutoff:DateTime<Utc>)->AppResult<Option<(Uuid,Uuid)>> {
    let parse=|key:&str|parent[key].as_str().and_then(|s|Uuid::parse_str(s).ok());
    let (Some(flow),Some(job),Some(generation))=(parse("workflow_id"),parse("job_id"),parent["generation"].as_i64().and_then(|v|i32::try_from(v).ok()).filter(|v|*v>0)) else {return Ok(None)};
    // Existing parent admission takes the Task lock. NOWAIT additionally
    // excludes direct legacy workflow writers while the snapshot is checked.
    sqlx::query("LOCK TABLE pipeline_runs IN SHARE ROW EXCLUSIVE MODE NOWAIT").execute(&mut **tx).await?;
    let valid:bool=sqlx::query_scalar("SELECT EXISTS(
        SELECT 1 FROM pipeline_runs w JOIN operation_inbox i ON i.workflow_id=w.id
        WHERE w.id=$1 AND w.task_id=$3 AND w.generation=$4 AND w.engine='rust-v2'
          AND w.state='running' AND w.current_stage='publish' AND w.updated_at<$6
          AND i.id=$2 AND i.task_id=$3 AND i.generation=$4 AND i.runtime_engine='rust-v2'
          AND i.stage='publish' AND i.status='awaiting' AND i.external_ticket=$5
          AND i.operation_key=$7 AND i.updated_at<$6 AND i.lease_owner IS NULL
          AND i.lease_token IS NULL AND i.lease_until IS NULL AND i.finished_at IS NULL
          AND i.completed_at IS NULL AND i.result_json IS NULL
          AND NOT EXISTS(SELECT 1 FROM publish_jobs p WHERE p.operation_key=i.operation_key)
          AND NOT EXISTS(SELECT 1 FROM outbox_events e WHERE e.operation_key=i.operation_key)
          AND NOT EXISTS(SELECT 1 FROM operation_inbox other WHERE other.workflow_id=w.id
            AND other.id<>i.id AND (other.status NOT IN ('completed','succeeded','failed','canceled')
              OR other.lease_owner IS NOT NULL OR other.lease_until IS NOT NULL OR other.lease_token IS NOT NULL))
    )")
        .bind(flow).bind(job).bind(task).bind(generation).bind(format!("native-batch:{batch}"))
        .bind(cutoff).bind(format!("vr2:{flow}:publish:{generation}"))
        .fetch_one(&mut **tx).await?;
    Ok(valid.then_some((flow,job)))
}

/// Repair only a single explicit native batch with its entire target snapshot
/// and no live or ambiguous delivery ledger. Multiple attempts or missing
/// targets remain with their respective reconciliation paths.
async fn recover_batch(tx:&mut Transaction<'_,Postgres>,task:Uuid,cutoff:DateTime<Utc>)->AppResult<bool> {
    let batches=sqlx::query("SELECT id,state,expected_targets,outcomes_json,request_json,updated_at,
        finished_at,cleanup_enqueued_at FROM publish_batches WHERE task_id=$1 ORDER BY id")
        .bind(task).fetch_all(&mut **tx).await?;
    if batches.len()!=1{return Ok(false)}
    let row=&batches[0];
    let batch:Uuid=row.try_get("id")?;
    let request:Value=row.try_get("request_json")?;
    if row.try_get::<String,_>("state")?!="active"
        || row.try_get::<DateTime<Utc>,_>("updated_at")?>=cutoff
        || row.try_get::<Option<DateTime<Utc>>,_>("finished_at")?.is_some()
        || row.try_get::<Option<DateTime<Utc>>,_>("cleanup_enqueued_at")?.is_some()
        || request["runtime"]!="rust-mixed-v1" {
        return Ok(false)
    }
    let (parent_flow,parent_job)=if request["auto_parent"].is_null() {
        (None,None)
    } else {
        let Some((flow,job))=verified_waiter(tx,task,batch,&request["auto_parent"],cutoff).await? else {return Ok(false)};
        (Some(flow),Some(job))
    };
    let blockers=crate::task_ownership::blockers_except_batch_waiter(tx,task,parent_flow,parent_job).await?;
    if blockers.iter().any(|s| !matches!(s.as_str(),"task_publishing"|"publication_unsettled"|"publish_batch_unsettled")) {
        return Ok(false)
    }
    let expected:Value=row.try_get("expected_targets")?;
    let outcomes:Value=row.try_get("outcomes_json")?;
    if !outcomes.as_object().is_some_and(|v|v.is_empty()){return Ok(false)}
    let keys=crate::bilibili_batch_reconcile_v1::keys(&expected)?;
    // Guard against new workflow or legacy writers. All admission paths
    // take the task lock; table locks also fence legacy direct receipt writes.
    let busy:bool=sqlx::query_scalar("SELECT
      EXISTS(SELECT 1 FROM tasks WHERE id=$1 AND active_publish_batch_id IS DISTINCT FROM $2)
      OR EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND id IS DISTINCT FROM $3::uuid AND state IN ('running','paused','unknown'))
      OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND id IS DISTINCT FROM $4::uuid AND state IN ('pending','running','awaiting','unknown'))
      OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1)
      OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE task_id=$1)
      OR EXISTS(SELECT 1 FROM operation_inbox i WHERE
          (i.task_id=$1 OR i.request_json->>'task_id'=$1::text OR
           EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1 AND
             (i.operation_key=j.operation_key OR i.operation_key='publish-job:'||j.id::text)))
          AND i.id IS DISTINCT FROM $4::uuid
          AND (i.status NOT IN ('completed','succeeded','failed','canceled') OR
               i.lease_owner IS NOT NULL OR i.lease_until IS NOT NULL OR i.lease_token IS NOT NULL))
      OR EXISTS(SELECT 1 FROM outbox_events WHERE aggregate_type='publish_batch' AND aggregate_id=$2::text)")
        .bind(task).bind(batch).bind(parent_flow).bind(parent_job).fetch_one(&mut **tx).await?;
    if busy{return Ok(false)}
    let jobs=sqlx::query("SELECT j.id,j.task_id,j.batch_id,j.platform::text AS platform,j.account_id,j.state::text AS state,
      j.operation_key,j.response_json,
      (j.updated_at >= $2 OR j.heartbeat_at >= $2 OR j.lease_owner IS NOT NULL OR j.lease_until IS NOT NULL OR j.upload_active
        OR COALESCE(j.response_json->'auto_dub_p2'->>'state','') NOT IN ('','published','failed')) IS TRUE AS busy,
      (NULLIF(j.external_id,'') IS NOT NULL OR NULLIF(j.external_url,'') IS NOT NULL
        OR NULLIF(j.bvid,'') IS NOT NULL OR NULLIF(j.aid,'') IS NOT NULL) AS any_receipt,
      CASE WHEN j.platform::text='bilibili' THEN COALESCE(NULLIF(j.bvid,''),NULLIF(j.aid,''),NULLIF(j.external_id,'')) IS NOT NULL
        ELSE COALESCE(NULLIF(j.external_id,''),NULLIF(j.external_url,'')) IS NOT NULL END AS valid_receipt,
      (EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key=j.operation_key OR i.operation_key='publish-job:'||j.id::text)
        OR EXISTS(SELECT 1 FROM outbox_events e WHERE e.operation_key=j.operation_key OR
            e.operation_key='publish-job:'||j.id::text OR (e.aggregate_type='publish_job' AND e.aggregate_id=j.id::text))
        OR EXISTS(SELECT 1 FROM operation_inbox i WHERE
            (i.task_id=$1 OR i.request_json->>'task_id'=$1::text)
            AND i.id IS DISTINCT FROM $4::uuid
            AND (i.operation_key LIKE 'publish%' OR i.operation_key LIKE '%:publish:%')
            AND NOT EXISTS(SELECT 1 FROM publish_jobs p WHERE p.task_id=$1 AND
                (p.operation_key=i.operation_key OR 'publish-job:'||p.id::text=i.operation_key))
            )
        OR EXISTS(SELECT 1 FROM outbox_events e WHERE e.aggregate_type='task' AND e.aggregate_id=$1::text
            AND (e.event_type LIKE 'publish%' OR e.task_name LIKE '%publish%'))) AS has_delivery
      FROM publish_jobs j WHERE j.task_id=$1 OR j.batch_id=$3 ORDER BY j.id")
        .bind(task).bind(cutoff).bind(batch).bind(parent_job).fetch_all(&mut **tx).await?;
    if jobs.len()!=keys.len(){return Ok(false)}
    let mut seen=HashSet::new();
    let mut orphans=Vec::new();
    for job in jobs {
        let id:Uuid=job.try_get("id")?;
        let platform:String=job.try_get("platform")?;
        let account:Option<Uuid>=job.try_get("account_id")?;
        let key=format!("{platform}:{}",account.map(|v|v.to_string()).unwrap_or_else(||"default".into()));
        if job.try_get::<Uuid,_>("task_id")?!=task
            || job.try_get::<Option<Uuid>,_>("batch_id")?!=Some(batch)
            || !keys.contains(&key) || !seen.insert(key)
            || job.try_get::<bool,_>("busy")? {
            return Ok(false)
        }
        let state:String=job.try_get("state")?;
        let response:Option<Value>=job.try_get("response_json")?;
        let any_receipt=job.try_get::<bool,_>("any_receipt")?;
        match state.as_str() {
            "published"|"submitted" if job.try_get::<bool,_>("valid_receipt")? => {},
            // The existing reconciler treats this exact native unverified
            // SAU receipt as unknown, while preserving the original row.
            "submitted" if !any_receipt && response.as_ref().is_some_and(|v|v["runtime"]=="rust" && v["receipt_verified"]==false)=>{},
            "failed" if !any_receipt=>{},
            "unknown"=>{},
            "draft"|"submitting" if !any_receipt
                && !job.try_get::<bool,_>("has_delivery")?
                && job.try_get::<Option<String>,_>("operation_key")?.as_deref()==Some(format!("publish-job:{id}").as_str())
                && response.as_ref().is_none_or(Value::is_object)=>{orphans.push(id);},
            _=>return Ok(false)
        }
    }
    for id in orphans {
        sqlx::query("UPDATE publish_jobs SET state='unknown',upload_active=false,
            finished_at=COALESCE(finished_at,now()),updated_at=now(),
            response_json=COALESCE(response_json,'{}'::jsonb)||jsonb_build_object(
                'timeout_recovery','unknown','verify_platform_before_retry',true)
            WHERE id=$1 AND task_id=$2 AND batch_id=$3 AND state::text IN ('draft','submitting')")
            .bind(id).bind(task).bind(batch).execute(&mut **tx).await?;
    }
    crate::bilibili_batch_reconcile_v1::reconcile(tx,task,batch).await?;
    Ok(true)
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
    let has_batch:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1)")
        .bind(task).fetch_one(&mut *tx).await?;
    if has_batch {
        if !recover_batch(&mut tx,task,cutoff).await? {return Ok(false)}
        tx.commit().await?;
        return Ok(true)
    }
    let blockers=crate::task_ownership::blockers(&mut tx,task).await?;
    if blockers.iter().any(|s| !matches!(s.as_str(),"task_publishing"|"publication_unsettled")){
        return Ok(false);
    }
    let busy:bool=sqlx::query_scalar("SELECT
      EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown'))
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

