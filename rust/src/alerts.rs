//! Shared operational alert feed using the original Python alert_events table.
//! The scanner creates alerts only for Rust-owned stage failures.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::get,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
struct ListQuery {
    status: Option<String>,
    limit: Option<i64>,
    task_id: Option<Uuid>,
}
fn check_status(value: &str) -> AppResult<()> {
    if !matches!(
        value,
        "all" | "active" | "open" | "acknowledged" | "resolved"
    ) {
        return Err(AppError::Invalid("invalid alert status".into()));
    }
    Ok(())
}
fn safe_limit(value: i64) -> AppResult<i64> {
    if !(1..=1000).contains(&value) {
        return Err(AppError::Invalid("alert limit must be 1..1000".into()));
    }
    Ok(value)
}
const PROJECTION: &str =
    "id, fingerprint, source, severity, status, title, message, details_json AS details, occurrence_count,
           first_seen_at, last_seen_at, acknowledged_at, resolved_at";

fn row_json(row: &sqlx::postgres::PgRow) -> AppResult<Value> {
    Ok(json!({
        "id": row.try_get::<Uuid,_>("id")?,
        "fingerprint":row.try_get::<String,_>("fingerprint")?,
        "source":row.try_get::<String,_>("source")?,
        "severity":row.try_get::<String,_>("severity")?,
        "status":row.try_get::<String,_>("status")?,
        "title":row.try_get::<String,_>("title")?,
        "message":row.try_get::<String,_>("message")?,
        "details":row.try_get::<Value,_>("details")?,
        "occurrence_count":row.try_get::<i32,_>("occurrence_count")?,
        "first_seen_at":row.try_get::<chrono::DateTime<chrono::Utc>,_>("first_seen_at")?,
        "last_seen_at":row.try_get::<chrono::DateTime<chrono::Utc>,_>("last_seen_at")?,
        "acknowledged_at":row.try_get::<Option<chrono::DateTime<chrono::Utc>>,_>("acknowledged_at")?,
        "resolved_at":row.try_get::<Option<chrono::DateTime<chrono::Utc>>,_>("resolved_at")?,
    }))
}
async fn list(
    State(state): State<AppState>,
    Query(query): Query<ListQuery>,
) -> AppResult<Json<Vec<Value>>> {
    let status = query.status.as_deref().unwrap_or("all");
    check_status(status)?;
    let limit = safe_limit(query.limit.unwrap_or(200))?;
    let statement = format!(
        "SELECT {PROJECTION} FROM alert_events
         WHERE ($1='all' OR ($1='active' AND status IN ('open','acknowledged')) OR status=$1)
         AND ($2::uuid IS NULL OR details_json->>'task_id'=$2::text)
         ORDER BY last_seen_at DESC,id LIMIT $3"
    );
    let rows = sqlx::query(&statement)
        .bind(status)
        .bind(query.task_id)
        .bind(limit)
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(
        rows.iter().map(row_json).collect::<AppResult<Vec<_>>>()?,
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Report {
    fingerprint: String,
    source: String,
    #[serde(default = "warning")]
    severity: String,
    title: String,
    #[serde(default)]
    message: String,
    #[serde(default = "empty")]
    details: Value,
    #[serde(default)]
    resolved: bool,
}
fn warning() -> String {
    "warning".into()
}
fn empty() -> Value {
    json!({})
}
fn validate(payload: &Report) -> AppResult<()> {
    if !(1..=255).contains(&payload.fingerprint.chars().count())
        || !(1..=64).contains(&payload.source.chars().count())
        || !(1..=255).contains(&payload.title.chars().count())
        || payload.message.len() > 16000
        || !matches!(payload.severity.as_str(), "info" | "warning" | "critical")
        || !payload.details.is_object()
        || serde_json::to_vec(&payload.details).map_or(true, |v| v.len() > 64 * 1024)
    {
        return Err(AppError::Invalid("invalid alert report payload".into()));
    }
    Ok(())
}
async fn report(
    State(state): State<AppState>,
    Json(payload): Json<Report>,
) -> AppResult<Json<Value>> {
    validate(&payload)?;
    let statement = if payload.resolved {
        format!(
            "UPDATE alert_events SET status='resolved',resolved_at=now(),last_seen_at=now(),
              details_json=$2,updated_at=now() WHERE fingerprint=$1
             RETURNING {PROJECTION}"
        )
    } else {
        format!(
            "INSERT INTO alert_events(id,fingerprint,source,severity,title,message,details_json)
             VALUES($7,$1,$2,$3,$4,$5,$6)
             ON CONFLICT(fingerprint) DO UPDATE SET
               source=EXCLUDED.source,severity=EXCLUDED.severity,title=EXCLUDED.title,
               message=EXCLUDED.message,details_json=EXCLUDED.details_json,
               occurrence_count=alert_events.occurrence_count+1,last_seen_at=now(),
               status=CASE WHEN alert_events.status='resolved' THEN 'open' ELSE alert_events.status END,
               resolved_at=CASE WHEN alert_events.status='resolved' THEN NULL ELSE alert_events.resolved_at END,
               acknowledged_at=CASE WHEN alert_events.status='resolved' THEN NULL ELSE alert_events.acknowledged_at END,
               updated_at=now()
             RETURNING {PROJECTION}"
        )
    };
    let row = if payload.resolved {
        sqlx::query(&statement)
            .bind(&payload.fingerprint)
            .bind(&payload.details)
            .fetch_optional(&state.db.pool)
            .await?
    } else {
        sqlx::query(&statement)
            .bind(&payload.fingerprint)
            .bind(&payload.source)
            .bind(&payload.severity)
            .bind(&payload.title)
            .bind(&payload.message)
            .bind(&payload.details)
            .bind(Uuid::new_v4())
            .fetch_optional(&state.db.pool)
            .await?
    };
    Ok(Json(match row {
        Some(row) => row_json(&row)?,
        None => Value::Null,
    }))
}
async fn transition(state: &AppState, id: Uuid, status: &'static str) -> AppResult<Json<Value>> {
    let statement = format!(
        "UPDATE alert_events SET status=$2,
           acknowledged_at=CASE WHEN $2='acknowledged' THEN now() ELSE acknowledged_at END,
           resolved_at=CASE WHEN $2='resolved' THEN now() ELSE resolved_at END,
           updated_at=now()
         WHERE id=$1 RETURNING {PROJECTION}"
    );
    let row = sqlx::query(&statement)
        .bind(id)
        .bind(status)
        .fetch_optional(&state.db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("alert not found".into()))?;
    Ok(Json(row_json(&row)?))
}
async fn acknowledge(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    transition(&state, id, "acknowledged").await
}
async fn resolve(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    transition(&state, id, "resolved").await
}

#[derive(Serialize)]
struct ScanResponse {
    scanned_at: chrono::DateTime<chrono::Utc>,
    active: i64,
    opened_or_updated: usize,
    resolved: u64,
    alerts: Vec<Value>,
}
async fn scan(State(state): State<AppState>) -> AppResult<Json<ScanResponse>> {
    let mut tx = state.db.pool.begin().await?;
    // Alert scanner is operator-triggered, not a 60-second full-table scan.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('vr2.alert.scan',0))")
        .execute(&mut *tx)
        .await?;
    let failures = sqlx::query(
        "SELECT j.id,j.task_id,j.stage,j.state,j.last_error
         FROM vr2_jobs j
         JOIN vr2_workflows w ON w.id=j.workflow_id
         JOIN tasks m ON m.id=j.task_id AND m.runtime_owner='rust'
         WHERE j.state IN ('failed','unknown')
           AND w.generation=(SELECT MAX(x.generation) FROM vr2_workflows x WHERE x.task_id=j.task_id)
         ORDER BY j.updated_at DESC LIMIT 1000",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut alerts = Vec::new();
    for failed in failures {
        let id: Uuid = failed.get("id");
        let task: Uuid = failed.get("task_id");
        let stage: String = failed.get("stage");
        let status: String = failed.get("state");
        let last_error: Option<String> = failed.get("last_error");
        let fingerprint = format!("vr2.job:{id}");
        let severity = if status == "unknown" {
            "critical"
        } else {
            "warning"
        };
        let title = format!("V2 {} job {}", stage, status);
        let row = sqlx::query(&format!(
            "INSERT INTO alert_events(id,fingerprint,source,severity,title,message,details_json)
             VALUES($1,$2,'v2_job',$3,$4,$5,$6)
             ON CONFLICT(fingerprint) DO UPDATE SET
             severity=EXCLUDED.severity,title=EXCLUDED.title,message=EXCLUDED.message,
             details_json=EXCLUDED.details_json,last_seen_at=now(),updated_at=now()
             RETURNING {PROJECTION}"
        ))
        .bind(Uuid::new_v4())
        .bind(&fingerprint)
        .bind(severity)
        .bind(title)
        .bind(last_error.unwrap_or_default())
        .bind(json!({"task_id":task,"job_id":id,"stage":stage,"state":status}))
        .fetch_one(&mut *tx)
        .await?;
        alerts.push(row_json(&row)?);
    }
    // Resolve ONLY alerts generated by this V2 scanner and no longer active.
    let resolved = sqlx::query(
        "UPDATE alert_events a SET status='resolved',resolved_at=now(),updated_at=now()
         WHERE a.source='v2_job' AND a.status!='resolved'
           AND NOT EXISTS(
             SELECT 1 FROM vr2_jobs j
             JOIN vr2_workflows w ON w.id=j.workflow_id
             JOIN tasks m ON m.id=j.task_id AND m.runtime_owner='rust'
             WHERE a.fingerprint=('vr2.job:'||j.id::text)
               AND j.state IN ('failed','unknown')
               AND w.generation=(SELECT MAX(x.generation) FROM vr2_workflows x WHERE x.task_id=j.task_id)
           )",
    )
    .execute(&mut *tx)
    .await?
    .rows_affected();
    tx.commit().await?;
    Ok(Json(ScanResponse {
        scanned_at: chrono::Utc::now(),
        active: alerts.len() as i64,
        opened_or_updated: alerts.len(),
        resolved,
        alerts,
    }))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/operations/alerts", get(list))
        .route("/operations/alerts/report", axum::routing::post(report))
        .route("/operations/alerts/scan", axum::routing::post(scan))
        .route(
            "/operations/alerts/{id}/ack",
            axum::routing::post(acknowledge),
        )
        .route(
            "/operations/alerts/{id}/resolve",
            axum::routing::post(resolve),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn alert_query_and_validation_bounds() {
        assert!(check_status("active").is_ok());
        assert!(check_status("random").is_err());
        assert!(safe_limit(1000).is_ok());
        assert!(safe_limit(1001).is_err());
        let bad = Report {
            fingerprint: "test".into(),
            source: "tests".into(),
            severity: "emergency".into(),
            title: "fake".into(),
            message: String::new(),
            details: json!({}),
            resolved: false,
        };
        assert!(validate(&bad).is_err());
    }
}
