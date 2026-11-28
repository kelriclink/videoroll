//! Original Python publish_lifecycle batch evaluation using original tables.
//! This is deliberately an independent module: a Bilibili success must never
//! make an unfinished Douyin/Kuaishou/Xiaohongshu target look published.
use crate::{AppError, AppResult};
use chrono::{DateTime, Duration, Utc};
use serde_json::Value;
#[cfg(test)]
use serde_json::json;
use sqlx::{Postgres, Row, Transaction};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

const MAX_TARGETS: usize = 64;
fn target_key(target: &Value) -> AppResult<String> {
    let platform = target.get("platform").and_then(Value::as_str).unwrap_or("");
    if !matches!(platform, "bilibili" | "douyin" | "xiaohongshu" | "kuaishou") {
        return Err(AppError::Conflict(
            "invalid original publish target platform".into(),
        ));
    }
    let account = target
        .get("account_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("default");
    let expected = format!("{platform}:{account}");
    if !target
        .get("key")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
        && target.get("key").and_then(Value::as_str) != Some(expected.as_str())
    {
        return Err(AppError::Conflict(
            "publish target key does not match its platform/account".into(),
        ));
    }
    Ok(expected)
}
pub(crate) fn keys(value: &Value) -> AppResult<Vec<String>> {
    let items = value.as_array().ok_or_else(|| {
        AppError::Conflict("publish batch expected_targets must be a list".into())
    })?;
    if items.is_empty() || items.len() > MAX_TARGETS {
        return Err(AppError::Conflict(
            "publish batch target count outside 1..64".into(),
        ));
    }
    let mut seen = HashSet::new();
    let mut output = Vec::with_capacity(items.len());
    for item in items {
        let key = target_key(item)?;
        if !seen.insert(key.clone()) {
            return Err(AppError::Conflict(
                "duplicate publish target in batch".into(),
            ));
        }
        output.push(key);
    }
    Ok(output)
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum State {
    Active,
    Succeeded,
    PartialFailed,
    Failed,
}
impl State {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Succeeded => "succeeded",
            Self::PartialFailed => "partial_failed",
            Self::Failed => "failed",
        }
    }
}
fn evaluate(targets: &[String], latest: &HashMap<String, String>, outcomes: &Value) -> State {
    let mut success = false;
    let mut failure = false;
    let mut pending = false;
    for key in targets {
        let state = latest
            .get(key)
            .map(String::as_str)
            .or_else(|| {
                outcomes
                    .get(key)
                    .and_then(|v| v.get("state"))
                    .and_then(Value::as_str)
            })
            .unwrap_or("");
        match state {
            "submitted" | "published" => success = true,
            "failed" | "unknown" => failure = true,
            _ => pending = true,
        }
    }
    if !pending && !failure {
        State::Succeeded
    } else if pending {
        State::Active
    } else if success {
        State::PartialFailed
    } else {
        State::Failed
    }
}
fn task_state(states: &[State]) -> (String, Option<&'static str>, Option<&'static str>) {
    // Exact `evaluate_task_publish_batches` ordering from Python.
    if states.contains(&State::Active) {
        return ("publishing".into(), None, None);
    }
    if !states.is_empty() && states.iter().all(|v| *v == State::Succeeded) {
        return ("published".into(), None, None);
    }
    if states
        .iter()
        .any(|v| matches!(v, State::Succeeded | State::PartialFailed))
    {
        return (
            "publishing".into(),
            Some("PUBLISH_PARTIAL_FAILURE"),
            Some("one or more publish targets failed; retry only failed targets"),
        );
    }
    (
        "failed".into(),
        Some("PUBLISH_FAILED"),
        Some("all publish targets failed"),
    )
}
/// Only call after the original `publish_jobs` row has been updated within the
/// same transaction and the parent Task row is already locked. Serializes
/// with Python Task -> Batch reconciliation without a second business table.
pub(crate) async fn reconcile(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    batch: Uuid,
) -> AppResult<()> {
    reconcile_impl(tx, task, batch, true).await
}
pub(crate) async fn reconcile_native_sau(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    batch: Uuid,
) -> AppResult<()> {
    let native_events: bool = sqlx::query_scalar("SELECT COALESCE(request_json->>'runtime' IN ('rust-mixed-v1','rust-auto-bilibili-v1'),false) FROM publish_batches WHERE id=$1 AND task_id=$2")
        .bind(batch).bind(task).fetch_one(&mut **tx).await?;
    reconcile_impl(tx, task, batch, native_events).await
}
async fn reconcile_impl(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    batch: Uuid,
    native_events: bool,
) -> AppResult<()> {
    let row = sqlx::query(
        "SELECT state,expected_targets,outcomes_json,finished_at,cleanup_enqueued_at,request_json
        FROM publish_batches WHERE id=$1 AND task_id=$2 FOR UPDATE",
    )
    .bind(batch)
    .bind(task)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or_else(|| {
        AppError::Conflict("original publish batch missing during reconciliation".into())
    })?;
    let expected: Value = row.try_get("expected_targets")?;
    let targets = keys(&expected)?;
    let outcomes: Value = row.try_get("outcomes_json")?;
    let rows = sqlx::query(
        "SELECT platform::text AS platform,account_id,
        CASE WHEN state::text='submitted' AND response_json->>'runtime'='rust'
              AND response_json->>'receipt_verified'='false'
              AND NULLIF(external_id,'') IS NULL AND NULLIF(external_url,'') IS NULL
             THEN 'unknown' ELSE state::text END AS state
        FROM publish_jobs WHERE batch_id=$1 ORDER BY updated_at DESC,created_at DESC,id DESC",
    )
    .bind(batch)
    .fetch_all(&mut **tx)
    .await?;
    let mut latest = HashMap::new();
    for record in rows {
        let platform: String = record.try_get("platform")?;
        let account: Option<Uuid> = record.try_get("account_id")?;
        let key = format!(
            "{platform}:{}",
            account.map(|v| v.to_string()).unwrap_or("default".into())
        );
        let status: String = record.try_get("state")?;
        latest.entry(key).or_insert(status);
    }
    let target = evaluate(&targets, &latest, &outcomes);
    let prior: String = row.try_get("state")?;
    sqlx::query(
        "UPDATE publish_batches SET state=$2,
        finished_at=CASE WHEN $2='succeeded' AND state<>'succeeded' THEN now() WHEN $3 THEN COALESCE(finished_at,now()) ELSE NULL END,
        updated_at=now() WHERE id=$1",
    )
    .bind(batch)
    .bind(target.name())
    .bind(target != State::Active)
    .execute(&mut **tx)
    .await?;
    if native_events
        && target != State::Active
        && (prior != target.name()
            || row
                .try_get::<Option<DateTime<Utc>>, _>("finished_at")?
                .is_none())
    {
        crate::publish_batch_events::enqueue(tx, task, batch, target.name(), None).await?;
    }
    // Python's currentness is per `(platform, account_id)`; do not let an
    // older completed batch override newer attempts on another channel.
    let currents = current_batches(tx, task).await?;
    if !currents.iter().any(|(id, _, _, _)| *id == batch) {
        return Ok(());
    }
    let aggregate = currents
        .iter()
        .map(|(_, state, _, _)| *state)
        .collect::<Vec<_>>();
    let (task_status, code, message) = task_state(&aggregate);
    sqlx::query(
        "UPDATE tasks SET status=$2::task_status,error_code=$3,error_message=$4,
        updated_at=now() WHERE id=$1 AND status <> 'canceled'::task_status",
    )
    .bind(task)
    .bind(task_status)
    .bind(code)
    .bind(message)
    .execute(&mut **tx)
    .await?;
    if native_events
        && aggregate.iter().all(|s| *s == State::Succeeded)
        && currents
            .iter()
            .any(|(id, _, _, enqueued)| *id == batch && !enqueued)
    {
        let retention = currents
            .iter()
            .filter_map(|(_, _, finished, _)| *finished)
            .max()
            .unwrap_or_else(Utc::now)
            + Duration::hours(24);
        crate::publish_batch_events::enqueue(tx, task, batch, target.name(), Some(retention))
            .await?;
    }
    let request: Value = row.try_get("request_json")?;
    settle_auto_parent(tx,task,batch,&request,target).await
}

/// Settle the exact parent from the target aggregate. The normal receipt
/// path and the terminal-batch repair share this transition.
pub(crate) async fn settle_auto_parent(
    tx:&mut Transaction<'_,Postgres>,task:Uuid,batch:Uuid,request:&Value,target:State,
)->AppResult<()> {
    if let Some(parent) = request.get("auto_parent").filter(|v| !v.is_null()) {
        let parse = |key: &str| {
            parent[key]
                .as_str()
                .and_then(|v| Uuid::parse_str(v).ok())
                .ok_or_else(|| AppError::Conflict("invalid automatic batch parent identity".into()))
        };
        let flow = parse("workflow_id")?;
        let job = parse("job_id")?;
        let generation = parent["generation"]
            .as_i64()
            .and_then(|v| i32::try_from(v).ok())
            .ok_or_else(|| AppError::Conflict("invalid automatic batch generation".into()))?;
        let ticket = format!("native-batch:{batch}");
        let parent_state:Option<String>=sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 AND task_id=$2 AND generation=$3 FOR UPDATE").bind(flow).bind(task).bind(generation).fetch_optional(&mut **tx).await?;
        let job_state:Option<String>=sqlx::query_scalar("SELECT state FROM vr2_jobs WHERE id=$1 AND workflow_id=$2 AND task_id=$3 AND generation=$4 AND stage='publish' AND external_ticket=$5 FOR UPDATE").bind(job).bind(flow).bind(task).bind(generation).bind(ticket).fetch_optional(&mut **tx).await?;
        if target != State::Active
            && matches!(parent_state.as_deref(), Some("running" | "unknown"))
            && matches!(job_state.as_deref(), Some("awaiting" | "unknown"))
        {
            let success = target == State::Succeeded;
            let next = if success { "succeeded" } else { "unknown" };
            if job_state.as_deref() != Some(next) {
                let result = serde_json::json!({"batch_id":batch,"confirmed":success,"batch_state":target.name()});
                let error = if success {
                    None
                } else {
                    Some("native batch contains failed or unverified target receipts")
                };
                sqlx::query("UPDATE vr2_jobs SET state=$2,result=$3,last_error=$4,finished_at=now(),updated_at=now() WHERE id=$1").bind(job).bind(next).bind(&result).bind(error).execute(&mut **tx).await?;
                sqlx::query(
                    "UPDATE vr2_workflows SET state=$2,last_error=$3,updated_at=now() WHERE id=$1",
                )
                .bind(flow)
                .bind(next)
                .bind(error)
                .execute(&mut **tx)
                .await?;
                crate::scheduler::Scheduler::event(
                    tx,
                    flow,
                    Some(job),
                    if success {
                        "workflow.succeeded"
                    } else {
                        "job.unknown"
                    },
                    result,
                )
                .await?;
            }
        }
    }
    Ok(())
}
pub(crate) async fn current_batches(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
) -> AppResult<Vec<(Uuid, State, Option<DateTime<Utc>>, bool)>> {
    let batches = sqlx::query(
        "SELECT id,state,expected_targets,finished_at,cleanup_enqueued_at
        FROM publish_batches WHERE task_id=$1 ORDER BY created_at DESC,id DESC",
    )
    .bind(task)
    .fetch_all(&mut **tx)
    .await?;
    let mut seen = HashSet::new();
    let mut currents = Vec::<(Uuid, State, Option<DateTime<Utc>>, bool)>::new();
    for item in batches {
        let expected: Value = item.try_get("expected_targets")?;
        // V1 contains historical marker batches with no targets; Python's
        // current_batches_for_task ignores those instead of raising an error.
        if expected.as_array().is_some_and(|items| items.is_empty()) {
            continue;
        }
        let listed = keys(&expected)?;
        let mut is_current = false;
        for key in &listed {
            if seen.insert(key.clone()) {
                is_current = true;
            }
        }
        if is_current {
            let status: String = item.try_get("state")?;
            let state = match status.as_str() {
                "succeeded" => State::Succeeded,
                "partial_failed" => State::PartialFailed,
                "failed" => State::Failed,
                _ => State::Active,
            };
            currents.push((
                item.try_get("id")?,
                state,
                item.try_get("finished_at")?,
                item.try_get::<Option<DateTime<Utc>>, _>("cleanup_enqueued_at")?
                    .is_some(),
            ));
        }
    }
    Ok(currents)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_batch_evaluation() {
        let targets: Vec<String> = vec!["bilibili:default".into(), "douyin:default".into()];
        let mut statuses = HashMap::new();
        statuses.insert(targets[0].clone(), "published".into());
        assert_eq!(evaluate(&targets, &statuses, &json!({})), State::Active);
        statuses.insert(targets[1].clone(), "unknown".into());
        assert_eq!(
            evaluate(&targets, &statuses, &json!({})),
            State::PartialFailed
        );
        statuses.insert(targets[1].clone(), "submitted".into());
        assert_eq!(evaluate(&targets, &statuses, &json!({})), State::Succeeded);
        statuses.insert(targets[0].clone(), "failed".into());
        assert_eq!(
            evaluate(&targets, &statuses, &json!({})),
            State::PartialFailed
        );
        statuses.insert(targets[1].clone(), "failed".into());
        assert_eq!(evaluate(&targets, &statuses, &json!({})), State::Failed);
        assert_eq!(
            task_state(&[State::Active, State::Succeeded]).0,
            "publishing"
        );
        assert_eq!(
            task_state(&[State::Succeeded, State::Succeeded]).0,
            "published"
        );
        assert_eq!(
            task_state(&[State::PartialFailed, State::Succeeded]).1,
            Some("PUBLISH_PARTIAL_FAILURE")
        );
        assert_eq!(task_state(&[State::Failed]).1, Some("PUBLISH_FAILED"));
    }
    #[test]
    fn target_list_validates_unique_account_key() {
        assert_eq!(
            keys(&json!([{"platform":"bilibili"},{"platform":"douyin","account_id":null}]))
                .unwrap()
                .len(),
            2
        );
        assert!(
            keys(
                &json!([{"platform":"bilibili"},{"platform":"bilibili","key":"bilibili:default"}])
            )
            .is_err()
        );
        assert!(keys(&json!([{"platform":"bilibili","key":"douyin:default"}])).is_err());
    }
}
