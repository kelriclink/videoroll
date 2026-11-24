//! Native SAU submission intake. The external browser execution remains the
//! dedicated SAU worker, consuming the original publish.dispatch outbox.
use crate::{AppError, AppResult, sau_account_native_v1::SauState, storage::AssetRegistry};
use axum::{
    Json,
    extract::{Path, State},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use std::{collections::HashSet, sync::Arc};
use uuid::Uuid;

#[derive(Deserialize, Serialize)]
struct InputRef {
    #[serde(rename = "type", default = "storage")]
    kind: String,
    key: String,
}
fn storage() -> String {
    "storage".into()
}
fn empty_object() -> Value {
    json!({})
}

#[derive(Deserialize)]
pub(crate) struct PublishRequest {
    platform: String,
    task_id: Uuid,
    batch_id: Option<Uuid>,
    account_id: Uuid,
    video: InputRef,
    cover: Option<InputRef>,
    meta: Value,
    #[serde(default = "empty_object")]
    platform_options: Value,
    #[serde(default)]
    force_retry: bool,
}

fn platform(value: &str) -> AppResult<&'static str> {
    match value.trim().to_ascii_lowercase().as_str() {
        "douyin" => Ok("douyin"),
        "xiaohongshu" => Ok("xiaohongshu"),
        "kuaishou" => Ok("kuaishou"),
        _ => Err(AppError::Invalid("publish platform mismatch".into())),
    }
}
fn validate_ref(value: &InputRef) -> AppResult<()> {
    if !matches!(value.kind.as_str(), "storage" | "s3") {
        return Err(AppError::Invalid("unsupported media reference type".into()));
    }
    AssetRegistry::checked_key(&value.key)?;
    Ok(())
}
fn text(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}
fn normalize_meta(meta: &Value, platform: &str) -> AppResult<Value> {
    if !meta.is_object() || serde_json::to_vec(meta).map_or(true, |v| v.len() > 65536) {
        return Err(AppError::Invalid(
            "meta must be a JSON object <=64 KiB".into(),
        ));
    }
    let mut title = text(meta.get("title")).trim().to_owned();
    if title.is_empty() {
        return Err(AppError::Invalid("meta.title is required".into()));
    }
    let mut desc = text(meta.get("desc"));
    if desc.is_empty() {
        desc = text(meta.get("description"));
    }
    let mut seen = HashSet::new();
    let raw = match meta.get("tags") {
        Some(Value::Array(a)) => a.iter().map(|v| text(Some(v))).collect::<Vec<_>>(),
        v => text(v)
            .replace('，', ",")
            .split(',')
            .map(str::to_owned)
            .collect(),
    };
    let mut tags: Vec<String> = raw
        .into_iter()
        .filter_map(|s| {
            let t = s.trim().trim_start_matches('#').to_owned();
            if t.is_empty() || !seen.insert(t.to_lowercase()) {
                None
            } else {
                Some(t)
            }
        })
        .collect();
    if platform == "douyin" {
        let author = desc
            .lines()
            .find_map(|line| {
                let line = line.trim();
                [
                    "原作者：",
                    "原作者:",
                    "博主：",
                    "博主:",
                    "作者：",
                    "作者:",
                    "UP：",
                    "UP:",
                ]
                .iter()
                .find_map(|prefix| {
                    line.strip_prefix(prefix)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                })
            })
            .unwrap_or("");
        while let Some(rest) = title.strip_prefix("【熟肉】") {
            title = rest.trim_start().to_owned();
        }
        if title.is_empty() {
            title = "未命名".into();
        }
        if !author.is_empty() {
            for separator in [" - ", " — ", " – ", "-", "—", "–"] {
                let suffix = format!("{separator}{author}");
                // Compare on a UTF-8 boundary, not the byte length of folded text.
                let cut = title.len().checked_sub(suffix.len());
                if let Some((head, tail)) = cut.and_then(|n| title.split_at_checked(n)) {
                    if tail.to_lowercase() == suffix.to_lowercase() && !head.trim().is_empty() {
                        title = head.trim().to_owned();
                        break;
                    }
                }
            }
        }
        desc = format!(
            "原作者：{}",
            if author.is_empty() {
                "未提供"
            } else {
                author
            }
        )
        .chars()
        .take(1000)
        .collect();
        tags.retain(|t| !t.eq_ignore_ascii_case("videoroll"));
        tags.truncate(4);
    } else if platform == "xiaohongshu" && tags.len() > 10 {
        return Err(AppError::Invalid(
            "xiaohongshu accepts at most 10 tags".into(),
        ));
    }
    Ok(json!({"title":title,"desc":desc.trim(),"tags":tags}))
}
fn receipt(row: &PgRow) -> AppResult<Value> {
    Ok(json!({
        "job_id":row.try_get::<Uuid,_>("id")?,
        "platform":row.try_get::<String,_>("platform")?,
        "state":row.try_get::<String,_>("state")?,
        "external_id":row.try_get::<Option<String>,_>("external_id")?,
        "external_url":row.try_get::<Option<String>,_>("external_url")?,
        "response":row.try_get::<Option<Value>,_>("response_json")?,
    }))
}
async fn read_receipt(tx: &mut Transaction<'_, Postgres>, id: Uuid) -> AppResult<Value> {
    let row=sqlx::query("SELECT id,platform::text AS platform,state::text AS state,external_id,external_url,response_json FROM publish_jobs WHERE id=$1")
        .bind(id).fetch_one(&mut **tx).await?;
    receipt(&row)
}
async fn enqueue(tx: &mut Transaction<'_, Postgres>, job: Uuid) -> AppResult<()> {
    let key = format!("publish-job:{job}");
    // Same task/account row locks as original Python, so replay creates no
    // duplicate outbox event. Never reset an existing delivered event.
    let actual: String=sqlx::query_scalar("UPDATE publish_jobs SET operation_key=COALESCE(NULLIF(operation_key,''),$2) WHERE id=$1 RETURNING operation_key")
        .bind(job).bind(&key).fetch_one(&mut **tx).await?;
    sqlx::query("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key)
        SELECT $1,'publish.dispatch','publish_job',$2,'social_publisher.process_job',$3,$4
        WHERE NOT EXISTS(SELECT 1 FROM outbox_events WHERE operation_key=$4)")
        .bind(Uuid::new_v4()).bind(job.to_string()).bind(json!({"args":[job.to_string()],"queue":"social_publish"})).bind(actual)
        .execute(&mut **tx).await?;
    Ok(())
}

pub(crate) async fn publish(
    State(state): State<Arc<SauState>>,
    Path(raw): Path<String>,
    Json(body): Json<PublishRequest>,
) -> AppResult<Json<Value>> {
    let p = platform(&raw)?;
    if p != body.platform {
        return Err(AppError::Invalid("publish platform mismatch".into()));
    }
    if body.force_retry {
        return Err(AppError::Conflict(
            "unknown receipt requires independent reconciliation; force retry disabled".into(),
        ));
    }
    if !state.publish_outbox_enabled {
        return Err(AppError::Conflict(
            "SAU browser worker/outbox dispatch not explicitly enabled".into(),
        ));
    }
    validate_ref(&body.video)?;
    if let Some(cover) = &body.cover {
        validate_ref(cover)?;
    }
    let meta = normalize_meta(&body.meta, p)?;
    if !body.platform_options.is_object()
        || serde_json::to_vec(&body.platform_options).map_or(true, |v| v.len() > 16384)
    {
        return Err(AppError::Invalid(
            "platform_options must be a JSON object <=16 KiB".into(),
        ));
    }
    let mut tx = state.pool.begin().await?;
    let task_status: String =
        sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(body.task_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::NotFound("task not found".into()))?;
    if let Some(batch) = body.batch_id {
        let expected: Option<Value> = sqlx::query_scalar(
            "SELECT expected_targets FROM publish_batches WHERE id=$1 AND task_id=$2 FOR UPDATE",
        )
        .bind(batch)
        .bind(body.task_id)
        .fetch_optional(&mut *tx)
        .await?;
        let expected = expected.ok_or_else(|| {
            AppError::Invalid("publish batch does not belong to this task".into())
        })?;
        if !crate::bilibili_batch_reconcile_v1::keys(&expected)?
            .contains(&format!("{p}:{}", body.account_id))
        {
            return Err(AppError::Invalid(
                "publish target is not part of this batch".into(),
            ));
        }
    }
    let account=sqlx::query("SELECT platform::text AS platform,name,is_active,check_state FROM accounts WHERE id=$1 FOR UPDATE")
        .bind(body.account_id).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::Invalid("active account for platform not found".into()))?;
    if account.try_get::<String, _>("platform")? != p || !account.try_get::<bool, _>("is_active")? {
        return Err(AppError::Invalid(
            "active account for platform not found".into(),
        ));
    }
    if account.try_get::<String, _>("check_state")? != "valid" {
        return Err(AppError::Invalid(
            "social account must be validated before publishing".into(),
        ));
    }
    let name: String = account.try_get("name")?;
    crate::sau_login_native_v1::ensure_not_logging_in(&mut tx, p, &name).await?;
    // First find an exact batch receipt (or latest active receipt for the
    // original non-batch request), then examine all other batches for safety.
    let prior = sqlx::query(
        "SELECT id,batch_id,state::text AS state,started_at,lease_until
      FROM publish_jobs WHERE task_id=$1 AND platform::text=$2 AND account_id=$3
      AND state::text IN ('submitting','submitted','unknown','published')
      ORDER BY CASE WHEN $4::uuid IS NULL OR batch_id IS NOT DISTINCT FROM $4 THEN 0 ELSE 1 END,created_at DESC,id DESC
      LIMIT 1 FOR UPDATE",
    )
    .bind(body.task_id)
    .bind(p)
    .bind(body.account_id)
    .bind(body.batch_id)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some(prior) = prior {
        let id: Uuid = prior.try_get("id")?;
        let existing_batch: Option<Uuid> = prior.try_get("batch_id")?;
        let status: String = prior.try_get("state")?;
        let mut result_id = id;
        if let Some(batch) = body.batch_id.filter(|_| body.batch_id != existing_batch) {
            if matches!(status.as_str(), "submitted" | "published") {
                let copy = Uuid::new_v4();
                sqlx::query("INSERT INTO publish_jobs(id,task_id,batch_id,platform,account_id,bili_account_id,
                    cover_key,meta_json,state,external_id,external_url,bvid,aid,response_json,started_at,finished_at)
                    SELECT $1,task_id,$2,platform,account_id,bili_account_id,cover_key,
                    meta_json || jsonb_build_object('reused_from_job_id',id::text),state,external_id,external_url,bvid,aid,response_json,started_at,finished_at
                    FROM publish_jobs WHERE id=$3")
                    .bind(copy).bind(batch).bind(id).execute(&mut *tx).await?;
                result_id = copy;
            } else if existing_batch.is_none() {
                sqlx::query("UPDATE publish_jobs SET batch_id=$2,updated_at=now() WHERE id=$1")
                    .bind(id)
                    .bind(batch)
                    .execute(&mut *tx)
                    .await?;
            } else {
                return Err(AppError::Conflict(
                    "prior batch has unresolved submission; reconcile before publishing".into(),
                ));
            }
        }
        // Only a never-started submission with no lease can be missing a
        // historical dispatch record. Never recreate a possibly started job.
        if status == "submitting"
            && prior
                .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("started_at")?
                .is_none()
            && prior
                .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("lease_until")?
                .is_none()
        {
            enqueue(&mut tx, id).await?;
        }
        let response = read_receipt(&mut tx, result_id).await?;
        tx.commit().await?;
        return Ok(Json(response));
    }
    if task_status == "canceled" {
        return Err(AppError::Conflict(
            "canceled task cannot be submitted".into(),
        ));
    }
    // Never accept arbitrary media from a different task. Both video and
    // optional cover must be registered original task assets.
    let owns_video: bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND storage_key=$2 AND kind::text IN ('video_raw','video_final','video_dubbed'))")
        .bind(body.task_id).bind(&body.video.key).fetch_one(&mut *tx).await?;
    if !owns_video {
        return Err(AppError::Invalid(
            "video asset does not belong to task".into(),
        ));
    }
    if let Some(cover) = &body.cover {
        let owns: bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND storage_key=$2 AND kind::text='cover_image')")
            .bind(body.task_id).bind(&cover.key).fetch_one(&mut *tx).await?;
        if !owns {
            return Err(AppError::Invalid(
                "cover asset does not belong to task".into(),
            ));
        }
    }
    let id = Uuid::new_v4();
    let saved = json!({"platform":p,"account_id":body.account_id,"account_name":name,"video":body.video,
        "cover":body.cover,"meta":meta,"platform_options":body.platform_options});
    sqlx::query("INSERT INTO publish_jobs(id,task_id,batch_id,platform,account_id,cover_key,meta_json,state)
      SELECT $1,$2,$3,platform,id,$4,$5,'submitting' FROM accounts WHERE id=$6")
        .bind(id).bind(body.task_id).bind(body.batch_id).bind(body.cover.as_ref().map(|v| &v.key))
        .bind(saved).bind(body.account_id).execute(&mut *tx).await?;
    enqueue(&mut tx, id).await?;
    if body.batch_id.is_none() && task_status != "published" {
        sqlx::query("UPDATE tasks SET status='publishing',error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1")
            .bind(body.task_id).execute(&mut *tx).await?;
    }
    let response = read_receipt(&mut tx, id).await?;
    tx.commit().await?;
    Ok(Json(response))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_douyin_metadata_and_safety() {
        assert_eq!(normalize_meta(&json!({"title":"【熟肉】Test - Author","desc":"原作者：Author","tags":["#one","ONE","videoroll","two","three","four","five"]}),"douyin").unwrap(),
            json!({"title":"Test","desc":"原作者：Author","tags":["one","two","three","four"]}));
        assert!(normalize_meta(&json!({"title":""}), "douyin").is_err());
        assert!(
            normalize_meta(
                &json!({"title":"a","tags":(0..11).map(|i|i.to_string()).collect::<Vec<_>>()}),
                "xiaohongshu"
            )
            .is_err()
        );
        assert!(
            validate_ref(&InputRef {
                kind: "storage".into(),
                key: "../secret".into()
            })
            .is_err()
        );
    }
}
