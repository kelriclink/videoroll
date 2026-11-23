//! V1-shaped subtitle task queue, backed by *real* persistent PostgreSQL
//! execution leases. Combines Python's legacy queue candidates and native V2
//! jobs; never derives a running task merely from Task.status.
//!
//! Python source: subtitle_service/main.py _download_queue_settings,
//! _read_task_queue and youtube_ingest/download_dispatch.py.
//! In V2, broker observability is PostgreSQL, not Celery/Redis Kombu.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Query, State},
    routing::get,
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

const DOWNLOAD_KEY: &str = "workflow.youtube_download_concurrency";
fn bad(s: &str) -> AppError {
    AppError::Invalid(s.into())
}
fn write_enabled() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    )
}
pub(crate) fn read_concurrency(data: &Value) -> i64 {
    // Python's read_download_concurrency defaults to 2 when setting is absent,
    // malformed, or non-numeric; values are clamped between 1 and 16.
    let requested = data.get("max_concurrent");
    let requested = match requested {
        Some(Value::String(s)) => s.parse::<i64>().ok(),
        Some(Value::Number(n)) => n.as_i64(),
        _ => None,
    };
    requested.unwrap_or(2).clamp(1, 16)
}
fn worker_slots() -> i64 {
    std::env::var("HATCHET_SUBTITLE_WORKER_SLOTS")
        .ok()
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(1)
        .clamp(1, 256)
}
pub(crate) async fn settings_snapshot(state: &AppState) -> AppResult<Value> {
    let saved: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(DOWNLOAD_KEY)
            .fetch_optional(&state.db.pool)
            .await?;
    let concurrency = read_concurrency(&saved.unwrap_or(Value::Null));

    // Do not confuse a pending download inbox record with a live lease.
    let activity=sqlx::query(r#"
      SELECT
        COUNT(*) FILTER (WHERE status='processing' AND lease_until>now())::BIGINT AS active,
        COUNT(*) FILTER (WHERE status='pending'
                 OR (status='processing' AND (lease_until IS NULL OR lease_until<=now())))::BIGINT AS pending
      FROM operation_inbox WHERE operation_key LIKE 'youtube-download:%'
    "#).fetch_one(&state.db.pool).await?;
    let legacy_active: i64 = activity.try_get("active")?;
    let legacy_pending: i64 = activity.try_get("pending")?;

    // Native V2 download jobs are never represented as Python OperationInbox
    // rows. Count them separately, so both generations appear.
    let own=sqlx::query(r#"
      SELECT COUNT(*) FILTER (WHERE j.state='running' AND j.lease_until>now())::BIGINT AS active,
        COUNT(*) FILTER (WHERE j.state='pending' OR
          (j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=now())))::BIGINT AS pending
      FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
      JOIN tasks t ON t.id=j.task_id
      WHERE j.stage='download' AND w.state='running'
        AND t.status::text NOT IN ('published','canceled')
        AND j.state IN ('pending','running')
    "#).fetch_one(&state.db.pool).await?;
    let v2_active: i64 = own.try_get("active")?;
    let v2_pending: i64 = own.try_get("pending")?;
    // The field is a compatibility discriminator; do not present Hatchet as
    // the V2 scheduler. When no native jobs are present preserve Python shape.
    let native: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE state IN ('pending','running','awaiting'))",
    )
    .fetch_one(&state.db.pool)
    .await?;
    Ok(json!({
        "scheduler": if native {"rust-v2"}else{"hatchet"},
        "subtitle_worker_slots":worker_slots(),
        "download_concurrency":concurrency,
        "active_downloads":legacy_active+v2_active,
        "pending_downloads":legacy_pending+v2_pending
    }))
}
#[derive(Deserialize)]
struct DownloadUpdate {
    max_concurrent: i64,
}
async fn queue_settings_put(
    State(state): State<AppState>,
    Json(payload): Json<DownloadUpdate>,
) -> AppResult<Json<Value>> {
    if !write_enabled() {
        return Err(AppError::Conflict(
            "V1 download queue settings writes disabled".into(),
        ));
    }
    if !(1..=16).contains(&payload.max_concurrent) {
        return Err(bad("simultaneous downloads must be 1..16"));
    }
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,
         updated_at=now()",
    )
    .bind(DOWNLOAD_KEY)
    .bind(json!({"max_concurrent":payload.max_concurrent}))
    .execute(&state.db.pool)
    .await?;
    Ok(Json(settings_snapshot(&state).await?))
}
#[derive(Debug, Deserialize)]
struct QueueQuery {
    limit: Option<i64>,
}
fn task_limit(v: Option<i64>) -> i64 {
    // Python: int(v or 200), clamp 1..2000
    match v {
        None | Some(0) => 200,
        Some(n) => n.clamp(1, 2000),
    }
}
fn secs_since(since: DateTime<Utc>, now: DateTime<Utc>) -> i64 {
    (now - since).num_seconds().max(0)
}
async fn queue_get(
    State(state): State<AppState>,
    Query(q): Query<QueueQuery>,
) -> AppResult<Json<Value>> {
    let limit = task_limit(q.limit);
    let now = Utc::now();
    // Restrict the JOINs to actual queue candidates, not the entire Tasks
    // table, and only expose active V2 workflows. Legacy queued subtitle rows
    // without an active workflow are NOT considered executable work.
    let rows = sqlx::query(
        r#"
      WITH candidates AS (
        SELECT j.task_id FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
          WHERE w.state='running' AND j.state IN ('pending','running','awaiting')
        UNION
        SELECT task_id FROM render_jobs WHERE status::text IN('queued','running')
        UNION
        SELECT task_id FROM subtitle_jobs WHERE status::text IN('queued','running')
        UNION
        SELECT task_id FROM pipeline_runs
          WHERE workflow_name='VideoPipelineV1'
          AND state IN('submitting','submitted','running','cancel_requested','launch_failed')
        UNION
        SELECT (substring(operation_key from 18))::uuid AS task_id FROM operation_inbox
          WHERE operation_key ~ '^youtube-download:[0-9a-fA-F-]{36}$'
          AND status IN ('pending','processing')
      )
      SELECT t.id AS task_id,t.priority,t.created_at AS task_created,t.updated_at AS task_updated,
             t.error_message AS task_error,
             v.id AS v_id,v.stage AS v_stage,v.state AS v_state,
             v.lease_until AS v_lease,v.last_error AS v_error,
             v.created_at AS v_created,v.updated_at AS v_updated,
             sv.id AS subtitle_id,sv.status::text AS subtitle_state,
             sv.progress AS subtitle_progress,sv.error_message AS subtitle_error,
             sv.created_at AS subtitle_created,sv.updated_at AS subtitle_updated,
             rj.id AS render_id,rj.subtitle_job_id AS render_subtitle_id,
             rj.status::text AS render_state,rj.progress AS render_progress,
             rj.error_message AS render_error,rj.created_at AS render_created,
             rj.updated_at AS render_updated,
             p.state AS pipeline_state,p.error_message AS pipeline_error,
             p.created_at AS pipeline_created,p.updated_at AS pipeline_updated,
             d.status AS download_state,d.lease_until AS download_lease,
             d.last_error AS download_error,d.created_at AS download_created,
             d.updated_at AS download_updated,
             prog.percent AS download_progress
      FROM tasks t JOIN candidates c ON c.task_id=t.id
      LEFT JOIN LATERAL (
         SELECT j.id,j.stage,j.state,j.lease_until,j.last_error,j.created_at,j.updated_at
         FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE j.task_id=t.id AND w.state='running'
             AND j.state IN ('pending','running','awaiting')
         ORDER BY w.generation DESC,j.created_at DESC LIMIT 1
      ) v ON TRUE
      LEFT JOIN LATERAL (
         SELECT sj.id,sj.status,sj.progress,sj.error_message,sj.created_at,sj.updated_at
         FROM subtitle_jobs sj
         WHERE sj.task_id=t.id AND sj.status::text IN ('queued','running')
           AND (
             (sj.status::text='running' AND sj.lease_owner IS NOT NULL AND
                sj.lease_owner<>'' AND sj.lease_until>now())
             OR EXISTS (
                SELECT 1 FROM pipeline_runs q
                WHERE q.task_id=t.id AND q.workflow_name='SubtitleJobV1'
                  AND q.state IN ('submitting','submitted','running','cancel_requested')
                  AND q.request_json->>'job_id'=sj.id::text)
           )
         ORDER BY sj.updated_at DESC,sj.created_at DESC LIMIT 1
      ) sv ON TRUE
      LEFT JOIN LATERAL (
         SELECT r.id,r.subtitle_job_id,r.status,r.progress,r.error_message,
           r.created_at,r.updated_at
         FROM render_jobs r
         WHERE r.task_id=t.id AND
           (r.status::text='queued' OR
             (r.status::text='running' AND r.lease_owner IS NOT NULL AND
              r.lease_owner<>'' AND r.lease_until>now()))
         ORDER BY r.updated_at DESC,r.created_at DESC LIMIT 1
      ) rj ON TRUE
      LEFT JOIN LATERAL (
         SELECT state,error_message,created_at,updated_at FROM pipeline_runs p
         WHERE p.task_id=t.id AND p.workflow_name='VideoPipelineV1'
         AND p.state IN ('submitting','submitted','running','cancel_requested','launch_failed')
         ORDER BY p.created_at DESC LIMIT 1
      ) p ON TRUE
      LEFT JOIN LATERAL (
         SELECT status,lease_until,last_error,created_at,updated_at FROM operation_inbox op
         WHERE op.operation_key='youtube-download:'||t.id::text
           AND op.status IN ('pending','processing')
         ORDER BY op.created_at ASC LIMIT 1
      ) d ON TRUE
      LEFT JOIN LATERAL (
         SELECT percent FROM vr2_download_progress dp
         WHERE dp.task_id=t.id AND dp.job_id=v.id LIMIT 1
      ) prog ON TRUE
      WHERE t.status::text NOT IN ('published','canceled')
        AND (v.id IS NOT NULL OR sv.id IS NOT NULL OR
             rj.id IS NOT NULL OR p.state IS NOT NULL OR d.status IS NOT NULL)
      ORDER BY t.priority DESC,t.created_at ASC
      LIMIT $1
    "#,
    )
    .bind(limit)
    .fetch_all(&state.db.pool)
    .await?;
    let mut items = Vec::<Value>::new();
    for row in rows {
        let task_id: Uuid = row.try_get("task_id")?;
        let priority: i32 = row.try_get("priority")?;
        let task_created: DateTime<Utc> = row.try_get("task_created")?;
        let task_updated: DateTime<Utc> = row.try_get("task_updated")?;
        let task_error: Option<String> = row.try_get("task_error")?;
        let mut state = "queued".to_string();
        let mut stage = "workflow".to_string();
        let mut progress = 0i32;
        let mut error = task_error;
        let mut created = task_created;
        let mut updated = task_updated;
        let mut since = task_created;
        let mut subtitle_id: Option<Uuid> = row.try_get("subtitle_id")?;
        let mut render_id: Option<Uuid> = row.try_get("render_id")?;
        let v_id: Option<Uuid> = row.try_get("v_id")?;
        if v_id.is_some() {
            let v_stage: String = row.try_get("v_stage")?;
            let v_state: String = row.try_get("v_state")?;
            let v_lease: Option<DateTime<Utc>> = row.try_get("v_lease")?;
            let live =
                v_state == "awaiting" || (v_state == "running" && v_lease.is_some_and(|v| v > now));
            state = if live { "running" } else { "queued" }.into();
            stage = match (v_stage.as_str(), live) {
                ("download", false) => "waiting_download".into(),
                ("render", false) => "waiting_render".into(),
                ("publish", false) => "waiting_publish".into(),
                ("dubbing", false) => "waiting_dubbing".into(),
                _ => v_stage,
            };
            let when: DateTime<Utc> = row.try_get("v_created")?;
            created = when;
            since = when;
            updated = row.try_get("v_updated")?;
            error = row.try_get("v_error")?;
            if stage == "download" || stage == "waiting_download" {
                progress = row
                    .try_get::<Option<i16>, _>("download_progress")?
                    .unwrap_or(0) as i32;
            }
            if stage == "subtitle" {
                subtitle_id = v_id;
            }
            if stage == "render" || stage == "waiting_render" {
                render_id = v_id;
            }
        } else if render_id.is_some() {
            let status: String = row.try_get("render_state")?;
            let running = status == "running";
            state = if running { "running" } else { "queued" }.into();
            stage = if running { "render" } else { "waiting_render" }.into();
            progress = row.try_get::<i32, _>("render_progress")?;
            error = row.try_get("render_error")?;
            let when: DateTime<Utc> = row.try_get("render_created")?;
            created = when;
            since = when;
            updated = row.try_get("render_updated")?;
            subtitle_id = row
                .try_get::<Option<Uuid>, _>("render_subtitle_id")?
                .or(subtitle_id);
        } else if subtitle_id.is_some() {
            let status: String = row.try_get("subtitle_state")?;
            state = if status == "running" {
                "running"
            } else {
                "queued"
            }
            .into();
            stage = "subtitle".into();
            progress = row.try_get::<i32, _>("subtitle_progress")?;
            error = row.try_get("subtitle_error")?;
            let when: DateTime<Utc> = row.try_get("subtitle_created")?;
            created = when;
            since = when;
            updated = row.try_get("subtitle_updated")?;
        } else {
            let pipeline: Option<String> = row.try_get("pipeline_state")?;
            if let Some(p) = pipeline {
                let when: DateTime<Utc> = row.try_get("pipeline_created")?;
                created = when;
                since = when;
                updated = row.try_get("pipeline_updated")?;
                state = if p == "running" { "running" } else { "queued" }.into();
                if p == "launch_failed" {
                    stage = "workflow_retry".into();
                    let message: Option<String> = row.try_get("pipeline_error")?;
                    error = Some(format!(
                        "Waiting to retry Hatchet launch: {}",
                        message
                            .filter(|s| !s.is_empty())
                            .unwrap_or_else(|| "workflow service unavailable".into())
                    ));
                }
            }
            let ds: Option<String> = row.try_get("download_state")?;
            if let Some(ds) = ds {
                let lease: Option<DateTime<Utc>> = row.try_get("download_lease")?;
                state = if ds == "processing" && lease.is_some_and(|d| d > now) {
                    "running"
                } else {
                    "queued"
                }
                .into();
                stage = "waiting_download".into();
                let when: DateTime<Utc> = row.try_get("download_created")?;
                created = when;
                since = when;
                updated = row.try_get("download_updated")?;
                error = row.try_get("download_error")?;
            }
        }
        items.push(json!({
           "task_id":task_id,"state":state,"stage":stage,
           "priority":priority,"subtitle_job_id":subtitle_id,
           "render_job_id":render_id,"progress":progress.clamp(0,100),
           "stage_elapsed_seconds":secs_since(since,now),
           "error_message":error,"created_at":created,"updated_at":updated
        }));
    }
    // Python chooses candidates by Task.priority then sorts selected rows for
    // display with live running work first.
    items.sort_by(|a, b| {
        let key = |v: &Value| {
            (
                if v["state"] == "running" { 0 } else { 1 },
                -v["priority"].as_i64().unwrap_or(0),
            )
        };
        key(a)
            .cmp(&key(b))
            .then_with(|| a["created_at"].as_str().cmp(&b["created_at"].as_str()))
    });
    let running = items.iter().filter(|v| v["state"] == "running").count();
    let queued = items.len() - running;
    Ok(Json(json!({
        "settings":settings_snapshot(&state).await?,
        "running_count":running,"queued_count":queued,"tasks":items
    })))
}
async fn queue_broker(State(state): State<AppState>) -> AppResult<Json<Value>> {
    // This deployment has no Celery broker and no Kombu ACK state. Return
    // the same DTO structure with named V2 stages and actual PostgreSQL
    // lease/ready counts; do not invent Redis Unacked or worker heartbeat.
    let now = Utc::now();
    let rows=sqlx::query(r#"
      SELECT j.stage AS name,
       COUNT(*) FILTER (WHERE j.state='pending' AND j.available_at<=now()
          OR j.state='running' AND j.lease_until<=now() AND j.stage<>'publish')::BIGINT AS ready,
       COUNT(*) FILTER (WHERE j.state='running' AND j.lease_until>now())::BIGINT AS running,
       COUNT(*) FILTER (WHERE j.state='awaiting')::BIGINT AS awaiting,
       COALESCE(SUM(j.attempts) FILTER (WHERE j.state='pending'
                       OR (j.state='running' AND j.lease_until<=now())),0)::BIGINT AS attempts,
       MIN(j.created_at) FILTER (WHERE j.state='pending' AND j.available_at<=now()
                       OR (j.state='running' AND j.lease_until<=now() AND j.stage<>'publish')) AS oldest
      FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
      JOIN tasks t ON t.id=j.task_id
      WHERE w.state='running' AND t.status::text NOT IN('published','canceled')
        AND j.state IN ('pending','running','awaiting')
      GROUP BY j.stage ORDER BY j.stage
    "#).fetch_all(&state.db.pool).await?;
    let mut queues = Vec::<Value>::new();
    for r in rows {
        let name: String = r.try_get("name")?;
        let ready: i64 = r.try_get("ready")?;
        let active: i64 = r.try_get("running")?;
        let awaiting: i64 = r.try_get("awaiting")?;
        let attempts: i64 = r.try_get("attempts")?;
        let oldest: Option<DateTime<Utc>> = r.try_get("oldest")?;
        queues.push(json!({
            "name":format!("v2:{name}"),
            "ready":ready,"reserved":Value::Null,"active":active,
            "unacked":0,
            "oldest_wait_seconds":if ready==0{Some(0)}else{oldest.map(|x|secs_since(x,now))},
            "ready_retry_attempts":attempts.max(0),
            "sampled_ready":ready,"scan_truncated":false,
            "awaiting_external":awaiting
        }));
    }
    Ok(Json(json!({
        "observed_at":now,"worker_observed":false,
        "queues":queues,
        "warnings":[
           "当前为 Rust PostgreSQL 持久队列；不使用 Celery/Redis Kombu，Unacked 不适用",
           "Active 依据未到期的租约统计，尚未独立核实 Worker 心跳",
           "Awaiting 外部执行计数在队列条目的 awaiting_external 扩展字段中"
        ]
    })))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/task_queue", get(queue_get))
        .route(
            "/subtitle/task_queue/settings",
            axum::routing::put(queue_settings_put),
        )
        .route("/subtitle/task_queue/broker", get(queue_broker))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_queue_clamp_defaults() {
        assert_eq!(task_limit(None), 200);
        assert_eq!(task_limit(Some(0)), 200);
        assert_eq!(task_limit(Some(-1)), 1);
        assert_eq!(task_limit(Some(3000)), 2000);
        assert_eq!(read_concurrency(&json!({})), 2);
        assert_eq!(read_concurrency(&json!({"max_concurrent":0})), 1);
        assert_eq!(read_concurrency(&json!({"max_concurrent":99})), 16);
        assert_eq!(read_concurrency(&json!({"max_concurrent":"oops"})), 2);
    }
}
