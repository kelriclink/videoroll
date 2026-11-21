//! PostgreSQL-backed queue telemetry. Stage counters are derived from actual
//! lease/job rows, never approximated from user-facing Task.status values.
use crate::{AppResult, Database, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use serde::Serialize;
use sqlx::{FromRow, Row};
use uuid::Uuid;

#[derive(Debug, Serialize)]
pub struct QueueSnapshot {
    pub pending: i64,
    pub runnable: i64,
    pub active: i64,
    pub expired_leases: i64,
    pub awaiting_external: i64,
    pub unknown_outcome: i64,
    pub failed: i64,
    pub succeeded: i64,
    pub oldest_pending_seconds: Option<f64>,
    pub oldest_active_seconds: Option<f64>,
    pub per_stage: Vec<StageCounts>,
}
#[derive(Debug, Serialize, FromRow)]
pub struct StageCounts {
    pub stage: String,
    pub pending: i64,
    pub running: i64,
    pub awaiting: i64,
    pub succeeded: i64,
    pub failed: i64,
    pub unknown: i64,
    pub oldest_pending_seconds: Option<f64>,
    pub longest_running_seconds: Option<f64>,
}
impl QueueSnapshot {
    pub fn potentially_stuck(&self) -> bool {
        self.expired_leases > 0
            || self.unknown_outcome > 0
            || self.oldest_pending_seconds.is_some_and(|age| age > 3600.0)
    }
}
pub async fn snapshot(db: &Database) -> AppResult<QueueSnapshot> {
    let row = sqlx::query(
        r#"SELECT
          COUNT(*) FILTER(WHERE state='pending')::bigint AS pending,
          COUNT(*) FILTER(WHERE state='pending' AND available_at<=now())::bigint AS runnable,
          COUNT(*) FILTER(WHERE state='running' AND lease_until>now())::bigint AS active,
          COUNT(*) FILTER(WHERE state='running' AND lease_until<=now())::bigint AS expired_leases,
          COUNT(*) FILTER(WHERE state='awaiting')::bigint AS awaiting_external,
          COUNT(*) FILTER(WHERE state='unknown')::bigint AS unknown_outcome,
          COUNT(*) FILTER(WHERE state='failed')::bigint AS failed,
          COUNT(*) FILTER(WHERE state='succeeded')::bigint AS succeeded,
          MAX(EXTRACT(EPOCH FROM (now()-created_at)))
             FILTER(WHERE state='pending')::float8 AS oldest_pending_seconds,
          MAX(EXTRACT(EPOCH FROM (now()-started_at)))
             FILTER(WHERE state='running')::float8 AS oldest_active_seconds
          FROM vr2_jobs"#,
    )
    .fetch_one(&db.pool)
    .await?;
    let stages = sqlx::query_as::<_, StageCounts>(
        r#"SELECT stage,
        COUNT(*) FILTER(WHERE state='pending')::bigint AS pending,
        COUNT(*) FILTER(WHERE state='running')::bigint AS running,
        COUNT(*) FILTER(WHERE state='awaiting')::bigint AS awaiting,
        COUNT(*) FILTER(WHERE state='succeeded')::bigint AS succeeded,
        COUNT(*) FILTER(WHERE state='failed')::bigint AS failed,
        COUNT(*) FILTER(WHERE state='unknown')::bigint AS unknown,
        MAX(EXTRACT(EPOCH FROM (now()-created_at)))
            FILTER(WHERE state='pending')::float8 AS oldest_pending_seconds,
        MAX(EXTRACT(EPOCH FROM (now()-started_at)))
            FILTER(WHERE state='running')::float8 AS longest_running_seconds
        FROM vr2_jobs GROUP BY stage ORDER BY stage"#,
    )
    .fetch_all(&db.pool)
    .await?;
    Ok(QueueSnapshot {
        pending: row.try_get("pending")?,
        runnable: row.try_get("runnable")?,
        active: row.try_get("active")?,
        expired_leases: row.try_get("expired_leases")?,
        awaiting_external: row.try_get("awaiting_external")?,
        unknown_outcome: row.try_get("unknown_outcome")?,
        failed: row.try_get("failed")?,
        succeeded: row.try_get("succeeded")?,
        oldest_pending_seconds: row.try_get("oldest_pending_seconds")?,
        oldest_active_seconds: row.try_get("oldest_active_seconds")?,
        per_stage: stages,
    })
}
async fn queue(State(s): State<AppState>) -> AppResult<Json<QueueSnapshot>> {
    Ok(Json(snapshot(&s.db).await?))
}
async fn task_history(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<serde_json::Value>> {
    s.db.task(id).await?;
    let runs = s.db.workflows(id).await?;
    let mut data = Vec::new();
    for flow in runs {
        let jobs = s.scheduler.jobs(flow.id).await?;
        let events = s.db.job_events(flow.id).await?;
        data.push(serde_json::json!({"workflow":flow,"jobs":jobs,"events":events}));
    }
    Ok(Json(serde_json::json!({"task_id":id,"runs":data})))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/queue", get(queue))
        .route("/tasks/{id}/timeline", get(task_history))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn empty_queue_does_not_report_a_stall() {
        let result = QueueSnapshot {
            pending: 0,
            runnable: 0,
            active: 0,
            expired_leases: 0,
            awaiting_external: 0,
            unknown_outcome: 0,
            failed: 0,
            succeeded: 0,
            oldest_pending_seconds: None,
            oldest_active_seconds: None,
            per_stage: vec![],
        };
        assert!(!result.potentially_stuck());
    }
}
