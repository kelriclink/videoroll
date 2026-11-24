//! Broker-free native dispatch and explicit historical cutover. The
//! browser executor owns long-running SAU work; the control plane owns
//! short transactional notifications. Never replay a broker attempt.
use crate::{AppError, AppResult, Config, Database};
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

pub const LEGACY_HANDLERS: &[&str] = &[
    "bilibili_publisher.process_job",
    "bilibili_publisher.auto_dub_p2",
    "social_publisher.process_job",
    "subtitle_service.push_publish_workflow_event",
    "subtitle_service.cleanup_task",
    "subtitle_service.push_render_workflow_event",
    "subtitle_service.push_dubbing_workflow_event",
    "subtitle_service.after_render_publish",
    "subtitle_service.cancel_workflow_run",
];

pub fn native_only() -> bool {
    std::env::var("VIDEOROLL_V2_NATIVE_OUTBOX_ONLY")
        .ok()
        .as_deref()
        == Some("true")
}

pub fn validate_config(cfg: &Config) -> AppResult<()> {
    if native_only() && (cfg.legacy_publish_allowed || cfg.compat_writes_allowed) {
        return Err(AppError::Conflict(
            "native-only outbox excludes legacy publication and compatibility writes".into(),
        ));
    }
    Ok(())
}

/// No external calls. Counts are delivery state, not business success.
/// All unrecognized or unresolved legacy rows remain visible blockers.
pub async fn audit(db: &Database) -> AppResult<Value> {
    let rows=sqlx::query(r#"SELECT e.task_name,e.status,
        CASE
          WHEN e.status='native_completed' AND e.task_name IN
            ('rust.publish.batch_finished','rust.publish.cleanup','rust.render.dubbing_failed','social_publisher.process_job')
            AND i.status='completed' AND i.runtime_engine=CASE
              WHEN e.task_name='social_publisher.process_job' THEN 'rust-sau-publish'
              WHEN e.task_name='rust.render.dubbing_failed' THEN 'rust-render-events'
              ELSE 'rust-publish-batch' END THEN 'native_acknowledged'
          WHEN e.status='native_pending' AND e.task_name IN ('rust.publish.batch_finished','rust.publish.cleanup')
            AND EXISTS(SELECT 1 FROM publish_batches b JOIN tasks t ON t.id=b.task_id WHERE b.id::text=e.aggregate_id AND t.runtime_owner='rust')
            THEN 'native_batch_queue'
          WHEN e.status IN ('native_pending','native_running') AND e.task_name='social_publisher.process_job'
            AND i.runtime_engine='rust-sau-publish'
            AND ((e.status='native_pending' AND i.status='pending') OR
                 (e.status='native_running' AND i.status='processing' AND e.lease_owner IS NOT NULL AND e.lease_owner=i.lease_owner))
            AND EXISTS(SELECT 1 FROM publish_jobs j JOIN tasks t ON t.id=j.task_id WHERE j.id::text=e.aggregate_id
              AND j.operation_key=e.operation_key AND t.runtime_owner='rust') THEN 'native_sau_queue'
          WHEN e.status='dispatched' AND e.task_name=ANY($1) AND i.status='completed' AND i.runtime_engine='python'
            THEN 'legacy_acknowledged'
          WHEN e.status='pending' AND e.attempt_count=0 AND e.lease_owner IS NULL AND e.lease_until IS NULL
            AND e.delivered_at IS NULL AND i.id IS NULL AND e.task_name=ANY($1)
            THEN 'legacy_unattempted'
          ELSE 'requires_reconciliation'
        END AS disposition,count(*)::bigint AS count
      FROM outbox_events e LEFT JOIN operation_inbox i ON i.operation_key=e.operation_key
      GROUP BY e.task_name,e.status,disposition ORDER BY e.task_name,e.status,disposition"#)
      .bind(LEGACY_HANDLERS).fetch_all(&db.pool).await?;
    let mut blockers = 0_i64;
    let mut groups = vec![];
    for r in rows {
        let handler: String = r.try_get("task_name")?;
        let status: String = r.try_get("status")?;
        let disposition: String = r.try_get("disposition")?;
        let count: i64 = r.try_get("count")?;
        if matches!(
            disposition.as_str(),
            "legacy_unattempted" | "requires_reconciliation"
        ) {
            blockers += count;
        }
        groups.push(
            json!({"handler":handler,"status":status,"disposition":disposition,"count":count}),
        );
    }
    Ok(
        json!({"legacy_handler_catalog":LEGACY_HANDLERS,"groups":groups,
      "unresolved_delivery_rows":blockers,"delivery_ledger_ready":blockers==0,
      "note":"delivery ledger only; does not certify broker shutdown, historical workflow migration, SAU availability, or platform receipts"}),
    )
}

/// Explicit offline transition, not invoked automatically during coexistence.
/// The caller must stop all old and new producers/consumers first. NOWAIT
/// table locks and transaction rollback make partial adoption impossible.
pub async fn adopt(db: &Database, quiesced: bool, sau_ready: bool) -> AppResult<Value> {
    if !native_only() || !quiesced {
        return Err(AppError::Conflict("adoption requires native-only mode and explicit stopped-producers/consumers confirmation".into()));
    }
    let mut tx = db.pool.begin().await?;
    sqlx::query("LOCK TABLE tasks,pipeline_runs,publish_batches,publish_jobs,operation_inbox,outbox_events IN SHARE ROW EXCLUSIVE MODE NOWAIT").execute(&mut *tx).await?;
    let batches=sqlx::query(r#"UPDATE outbox_events e SET status='native_pending',
        task_name=CASE WHEN e.task_name='subtitle_service.cleanup_task' THEN 'rust.publish.cleanup' ELSE 'rust.publish.batch_finished' END,
        updated_at=now()
      FROM publish_batches b JOIN tasks t ON t.id=b.task_id
      WHERE e.aggregate_type='publish_batch' AND e.aggregate_id=b.id::text AND t.runtime_owner='rust'
        AND NOT EXISTS(SELECT 1 FROM pipeline_runs w WHERE w.task_id=t.id AND w.engine<>'rust-v2'
          AND w.state NOT IN ('succeeded','failed','canceled','completed','superseded'))
        AND e.status='pending' AND e.attempt_count=0 AND e.lease_owner IS NULL AND e.lease_until IS NULL AND e.delivered_at IS NULL
        AND NOT EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key=e.operation_key)
        AND NOT EXISTS(SELECT 1 FROM outbox_events o WHERE o.operation_key=e.operation_key AND o.id<>e.id)
        AND COALESCE(e.args_json->'kwargs','{}'::jsonb)='{}'::jsonb
        AND ((e.task_name='subtitle_service.cleanup_task' AND e.event_type='publish.cleanup'
            AND e.operation_key='publish-cleanup:'||b.id::text AND b.state='succeeded'
            AND e.args_json->'args'=jsonb_build_array(t.id::text,b.id::text))
          OR (e.task_name='subtitle_service.push_publish_workflow_event' AND e.event_type='workflow.publish.finished'
            AND b.state IN ('succeeded','failed','partial_failed')
            AND e.operation_key='workflow-publish-finished:'||b.id::text||':'||b.state
            AND e.args_json->'args'=jsonb_build_array(b.id::text,b.state,t.id::text)))"#)
        .execute(&mut *tx).await?.rows_affected();
    let mut sau = 0;
    if sau_ready {
        // Only exact never-started jobs, no old dispatch/receipt evidence.
        // Native SAU independently validates media and account at execution.
        let rows=sqlx::query(r#"SELECT e.id,e.operation_key,j.id AS job FROM outbox_events e
          JOIN publish_jobs j ON j.id::text=e.aggregate_id AND j.operation_key=e.operation_key
          JOIN tasks t ON t.id=j.task_id
          WHERE e.task_name='social_publisher.process_job' AND e.event_type='publish.dispatch' AND e.aggregate_type='publish_job'
            AND t.runtime_owner='rust' AND t.source_license::text<>'unknown' AND t.status::text<>'canceled'
            AND NOT EXISTS(SELECT 1 FROM pipeline_runs w WHERE w.task_id=t.id AND w.engine<>'rust-v2'
              AND w.state NOT IN ('succeeded','failed','canceled','completed','superseded'))
            AND e.status='pending' AND e.attempt_count=0 AND e.lease_owner IS NULL AND e.lease_until IS NULL AND e.delivered_at IS NULL
            AND j.platform::text IN ('douyin','kuaishou','xiaohongshu') AND j.state::text='submitting'
            AND j.started_at IS NULL AND j.finished_at IS NULL AND j.lease_owner IS NULL AND j.lease_until IS NULL
            AND NOT j.upload_active AND j.upload_progress=0 AND j.external_id IS NULL AND j.external_url IS NULL
            AND j.response_json IS NULL
            AND NOT (j.meta_json ? 'retry_of_job_id')
            AND NOT EXISTS(SELECT 1 FROM publish_jobs prior WHERE prior.id<>j.id AND prior.task_id=j.task_id
                AND prior.platform=j.platform AND prior.account_id IS NOT DISTINCT FROM j.account_id
                AND prior.state::text IN ('submitting','submitted','published','unknown'))
            AND j.account_id IS NOT NULL AND j.operation_key='publish-job:'||j.id::text
            AND e.args_json->'args'=jsonb_build_array(j.id::text)
            AND COALESCE(e.args_json->'kwargs','{}'::jsonb)='{}'::jsonb
            AND NOT EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key=e.operation_key)
            AND NOT EXISTS(SELECT 1 FROM outbox_events o WHERE o.operation_key=e.operation_key AND o.id<>e.id)"#)
            .fetch_all(&mut *tx).await?;
        for row in rows {
            let event: Uuid = row.try_get("id")?;
            let job: Uuid = row.try_get("job")?;
            let key: String = row.try_get("operation_key")?;
            sqlx::query(
                "UPDATE outbox_events SET status='native_pending',updated_at=now() WHERE id=$1",
            )
            .bind(event)
            .execute(&mut *tx)
            .await?;
            crate::sau_worker_native_v1::enqueue(&mut tx, job, &key).await?;
            sau += 1;
        }
    }
    tx.commit().await?;
    Ok(json!({"adopted_batch_events":batches,"adopted_sau_events":sau,"audit":audit(db).await?}))
}

/// Unified control-plane dispatcher; no broker, no HTTP publisher calls.
/// Handler failures remain independent so one queue cannot starve the other.
pub async fn tick(db: &Database, cfg: &Config, limit: i64) -> AppResult<Value> {
    let batch = crate::publish_batch_events::tick(db, limit).await;
    let render = crate::render_events::tick(db, cfg, limit).await;
    match (batch, render) {
        (Ok(b), Ok(r)) => Ok(json!({"batch_events":b,"render_receipts":r})),
        (b, r) => Err(AppError::Unavailable(format!(
            "outbox tick incomplete: batch_ok={},render_ok={}",
            b.is_ok(),
            r.is_ok()
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn handler_catalog_is_unique() {
        assert_eq!(LEGACY_HANDLERS.len(), 9);
        assert_eq!(
            LEGACY_HANDLERS
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len(),
            9
        );
    }
}
