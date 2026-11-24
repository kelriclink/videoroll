//! Original FastAPI POST /settings/youtube/home_scan/run, now Rust-owned.
//! Cross-generation settings/lease and ingest tables are the original schema.
use crate::{
    AppError, AppResult, api::AppState, youtube_home_feed_v1 as feed, youtube_settings_v1,
};
use axum::{Json, extract::State};
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde_json::{Map, Value, json};
use std::time::Duration;
use uuid::Uuid;

const SETTINGS: &str = "youtube.settings";
struct AbortOnDrop(tokio::task::AbortHandle);
impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}
fn ttl() -> i64 {
    std::env::var("YOUTUBE_HOME_SCAN_LOCK_TTL_SECONDS")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(900)
        .clamp(30, 3600)
}
fn object(value: Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}
fn parse_until(value: &Value) -> Option<DateTime<Utc>> {
    value
        .as_str()
        .and_then(|v| DateTime::parse_from_rfc3339(v).ok())
        .map(|v| v.with_timezone(&Utc))
}
fn modified_value(row: &Value) -> Map<String, Value> {
    row.as_object().cloned().unwrap_or_default()
}
async fn acquire(state: &AppState, owner: &str) -> AppResult<Value> {
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES($1,'{}'::jsonb,1) ON CONFLICT(key) DO NOTHING")
        .bind(SETTINGS).execute(&mut *tx).await?;
    let old: Value =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(SETTINGS)
            .fetch_one(&mut *tx)
            .await?;
    let other = old
        .get("home_scan_lock_owner")
        .and_then(Value::as_str)
        .unwrap_or("");
    let current = old.get("home_scan_lock_until").and_then(parse_until);
    if !other.is_empty() && current.is_some_and(|until| until > Utc::now()) {
        return Err(AppError::Conflict(
            "YOUTUBE_HOME_SCAN_ALREADY_RUNNING: another home scan is active".into(),
        ));
    }
    let mut config = modified_value(&old);
    let now = Utc::now();
    config.insert("home_scan_lock_owner".into(), json!(owner));
    config.insert(
        "home_scan_lock_until".into(),
        json!((now + ChronoDuration::seconds(ttl())).to_rfc3339()),
    );
    config.insert("home_scan_last_started_at".into(), json!(now.to_rfc3339()));
    sqlx::query(
        "UPDATE app_settings SET value_json=$2,version=version+1,updated_at=now() WHERE key=$1",
    )
    .bind(SETTINGS)
    .bind(Value::Object(config))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(old)
}
async fn heartbeat(state: &AppState, owner: &str) -> AppResult<bool> {
    let mut tx = state.db.pool.begin().await?;
    let row: Value =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(SETTINGS)
            .fetch_one(&mut *tx)
            .await?;
    if row.get("home_scan_lock_owner").and_then(Value::as_str) != Some(owner) {
        return Ok(false);
    }
    let mut next = object(row);
    next.insert(
        "home_scan_lock_until".into(),
        json!((Utc::now() + ChronoDuration::seconds(ttl())).to_rfc3339()),
    );
    sqlx::query(
        "UPDATE app_settings SET value_json=$2,version=version+1,updated_at=now() WHERE key=$1",
    )
    .bind(SETTINGS)
    .bind(Value::Object(next))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(true)
}
async fn own_lease(state: &AppState, owner: &str) -> AppResult<()> {
    let value: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(SETTINGS)
            .fetch_optional(&state.db.pool)
            .await?;
    let value = value.unwrap_or(Value::Null);
    let matched = value.get("home_scan_lock_owner").and_then(Value::as_str) == Some(owner);
    let active = value
        .get("home_scan_lock_until")
        .and_then(parse_until)
        .is_some_and(|until| until > Utc::now());
    if !matched || !active {
        return Err(AppError::Conflict(
            "YouTube home scan lease lost; refusing new workflows".into(),
        ));
    }
    Ok(())
}
async fn finish(
    state: &AppState,
    owner: &str,
    response: Option<&Value>,
    failure: Option<&str>,
) -> AppResult<()> {
    let mut tx = state.db.pool.begin().await?;
    let old: Value =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(SETTINGS)
            .fetch_one(&mut *tx)
            .await?;
    if old.get("home_scan_lock_owner").and_then(Value::as_str) != Some(owner) {
        return Err(AppError::Conflict(
            "YouTube home scan lease lost before finish".into(),
        ));
    }
    let mut next = object(old);
    next.remove("home_scan_lock_owner");
    next.remove("home_scan_lock_until");
    next.insert(
        "home_scan_last_finished_at".into(),
        json!(Utc::now().to_rfc3339()),
    );
    let map = [
        ("discovered_count", "home_scan_last_discovered_count"),
        ("started_pipeline_job_ids", "home_scan_last_started_count"),
        ("skipped_duplicates", "home_scan_last_skipped_duplicates"),
        ("failed_count", "home_scan_last_failed_count"),
        ("candidate_count", "home_scan_last_candidate_count"),
        (
            "explicit_shorts_count",
            "home_scan_last_explicit_shorts_count",
        ),
        (
            "known_duration_count",
            "home_scan_last_known_duration_count",
        ),
        (
            "unknown_duration_count",
            "home_scan_last_unknown_duration_count",
        ),
        (
            "below_min_duration_count",
            "home_scan_last_below_min_duration_count",
        ),
        (
            "kept_unknown_duration_count",
            "home_scan_last_kept_unknown_duration_count",
        ),
        ("eligible_count", "home_scan_last_eligible_count"),
    ];
    for (source, dest) in map {
        let n = if source == "started_pipeline_job_ids" {
            response
                .and_then(|v| v.get(source))
                .and_then(Value::as_array)
                .map(|v| v.len() as u64)
                .unwrap_or(0)
        } else {
            response
                .and_then(|v| v.get(source))
                .and_then(Value::as_u64)
                .unwrap_or(0)
        };
        next.insert(dest.into(), json!(n));
    }
    let lines = response
        .and_then(|v| v.get("log_lines"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .take(40)
        .map(|s| s.chars().take(240).collect::<String>())
        .collect::<Vec<_>>();
    let urls = response
        .and_then(|v| v.get("sample_urls"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .take(10)
        .map(str::to_string)
        .collect::<Vec<_>>();
    next.insert("home_scan_last_log_lines".into(), json!(lines));
    next.insert("home_scan_last_sample_urls".into(), json!(urls));
    let partial = if failure.is_none()
        && response
            .and_then(|v| v.get("failed_count"))
            .and_then(Value::as_u64)
            .unwrap_or(0)
            > 0
    {
        Some("one or more YouTube Home candidates could not be enqueued")
    } else {
        None
    };
    if let Some(failure) = failure.or(partial) {
        next.insert(
            "home_scan_last_error".into(),
            json!(failure.chars().take(1000).collect::<String>()),
        );
    } else {
        next.remove("home_scan_last_error");
    }
    sqlx::query(
        "UPDATE app_settings SET value_json=$2,version=version+1,updated_at=now() WHERE key=$1",
    )
    .bind(SETTINGS)
    .bind(Value::Object(next))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}
async fn execute(state: &AppState, owner: &str, settings: &Value) -> AppResult<Value> {
    let cookies = youtube_settings_v1::read_cookie(&state.config)
        .await?
        .ok_or_else(|| {
            AppError::Invalid(
                "youtube cookies are not configured; save cookies.txt before running Home scan"
                    .into(),
            )
        })?;
    let limit = settings
        .get("home_scan_limit")
        .and_then(Value::as_u64)
        .unwrap_or(10)
        .clamp(1, 100) as usize;
    let long_only = settings
        .get("home_scan_long_videos_only")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let duration = settings
        .get("home_scan_min_duration_seconds")
        .and_then(Value::as_u64)
        .unwrap_or(180)
        .min(86400);
    let proxy = youtube_settings_v1::validate_proxy(
        settings.get("proxy").and_then(Value::as_str).unwrap_or(""),
    )?;
    let feed = feed::fetch(&cookies, proxy.as_deref(), limit, long_only, duration).await?;
    own_lease(state, owner).await?;
    let mut ids = Vec::<Uuid>::new();
    let mut started = Vec::<String>::new();
    let mut skipped = 0usize;
    let mut failed = 0usize;
    let mut errors = Vec::<String>::new();
    let sample_urls = feed
        .videos
        .iter()
        .take(5)
        .map(feed::Video::url)
        .collect::<Vec<_>>();
    for item in &feed.videos {
        own_lease(state, owner).await?;
        let url = item.url();
        let candidate = state
            .scheduler
            .create_youtube_source_task(&url, "authorized", 0, json!({}), true)
            .await;
        match candidate {
            Ok((task, flow, true)) => {
                let _ = task;
                let _ = flow;
                skipped += 1;
            }
            Ok((task, flow, false)) => {
                // Use the original created_by syntax as well as V2's structured
                // origin fields; old front-end and billing reports read both.
                let run = Uuid::new_v4().simple().to_string();
                let metadata = format!("youtube_home_scan;auto_publish=;run_id={run}");
                let updated=sqlx::query(
                    "UPDATE tasks SET created_by=$2,origin_kind='youtube_home_scan',origin_run_id=$3
                     WHERE id=$1 AND runtime_owner='rust'")
                    .bind(task.id).bind(metadata).bind(run).execute(&state.db.pool).await?;
                if updated.rows_affected() != 1 {
                    // We cannot erase an already admitted durable workflow.
                    // Count and report failure without pretending no side effect.
                    failed += 1;
                    errors.push("task origin metadata not saved".into());
                }
                ids.push(task.id);
                if let Some(flow) = flow {
                    started.push(flow.id.to_string());
                }
            }
            Err(e) => {
                failed += 1;
                // Never include cookies, URLs with secrets or provider responses.
                tracing::warn!(video_id=%item.id,"YouTube Home task ingestion failed");
                errors.push(format!(
                    "{}: {}",
                    item.id,
                    match e {
                        AppError::Conflict(_) => "conflict",
                        _ => "ingest failed",
                    }
                ));
            }
        }
    }
    let logs = feed
        .stats
        .log_lines
        .into_iter()
        .take(40)
        .collect::<Vec<_>>();
    let response = json!({
        "discovered_count":feed.videos.len(),
        "created_task_ids":ids,
        "skipped_duplicates":skipped,
        "failed_count":failed,
        "candidate_count":feed.stats.candidate_count,
        "explicit_shorts_count":feed.stats.explicit_shorts_count,
        "known_duration_count":feed.stats.known_duration_count,
        "unknown_duration_count":feed.stats.unknown_duration_count,
        "below_min_duration_count":feed.stats.below_min_duration_count,
        "kept_unknown_duration_count":feed.stats.kept_unknown_duration_count,
        "eligible_count":feed.stats.eligible_count,
        "min_duration_seconds":feed.stats.min_duration_seconds,
        "log_lines":logs,
        "started_pipeline_job_ids":started,
        "sample_urls":sample_urls,
    });
    if !errors.is_empty() {
        tracing::warn!(failed, "YouTube Home scan had item intake failures");
    }
    Ok(response)
}
pub(crate) async fn run(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let owner = format!(
        "youtube-router:rust:{}:{}",
        std::process::id(),
        Uuid::new_v4().simple()
    );
    let old = acquire(&state, &owner).await?;
    let period = Duration::from_secs((ttl() / 3).clamp(5, 60) as u64);
    let heartbeat_state = state.clone();
    let heartbeat_owner = owner.clone();
    let heartbeat = tokio::spawn(async move {
        let mut interval = tokio::time::interval(period);
        interval.tick().await;
        loop {
            interval.tick().await;
            match heartbeat(&heartbeat_state, &heartbeat_owner).await {
                Ok(true) => (),
                Ok(false) | Err(_) => break,
            }
        }
    });
    let _heartbeat_guard = AbortOnDrop(heartbeat.abort_handle());
    let result = execute(&state, &owner, &old).await;
    heartbeat.abort();
    let release = finish(
        &state,
        &owner,
        result.as_ref().ok(),
        result.as_ref().err().map(|err| match err {
            AppError::Invalid(_) => "YouTube Home scan input or Cookie configuration rejected",
            AppError::Conflict(_) => "YouTube Home scan lost exclusive lease",
            _ => "YouTube Home scan failed",
        }),
    )
    .await;
    release?;
    result.map(Json)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn settings_lock_timestamps_are_offset_aware() {
        let v = json!("2026-11-24T12:30:00+08:00");
        assert_eq!(parse_until(&v).unwrap().timestamp(), 1795494600);
        assert!(parse_until(&json!("invalid")).is_none());
    }
}
