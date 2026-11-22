//! The original PublishMeta UI contract, stored transactionally for V2 tasks.
//! Drafts are deterministic; this module does NOT claim to run the V1 AI
//! recommendation, topic enrollment or external publishing review services.
//! An operator-saved metadata snapshot is used by native Bilibili publication.
use crate::{AppError, AppResult, Database, api::AppState, legacy_contracts::read_task};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use uuid::Uuid;

const MAX_META_BYTES: usize = 64 * 1024;
const MAX_DESC_UNITS: usize = 1_800;

fn desc_units(s: &str) -> usize {
    s.chars().map(|c| if c.is_ascii() { 1 } else { 2 }).sum()
}
fn clamped_desc(s: &str) -> String {
    let trimmed = s.trim();
    if desc_units(trimmed) <= MAX_DESC_UNITS {
        return trimmed.to_string();
    }
    let mut out = String::new();
    let mut used = 0usize;
    for c in trimmed.chars() {
        let units = if c.is_ascii() { 1 } else { 2 };
        if used + units + 2 > MAX_DESC_UNITS {
            break;
        }
        out.push(c);
        used += units;
    }
    format!("{}…", out.trim_end())
}
fn tags_from_input(value: &Value) -> AppResult<Vec<String>> {
    let tags: Vec<String> = if let Some(raw) = value.as_str() {
        raw.replace('，', ",")
            .split(',')
            .map(str::trim)
            .filter(|x| !x.is_empty())
            .map(str::to_owned)
            .collect()
    } else if let Some(items) = value.as_array() {
        let mut tags = Vec::new();
        for tag in items {
            let s = tag
                .as_str()
                .ok_or_else(|| AppError::Invalid("Bilibili tag must be a string".into()))?;
            if !s.trim().is_empty() {
                tags.push(s.trim().into());
            }
        }
        tags
    } else {
        return Err(AppError::Invalid(
            "Bilibili tags must be array or comma-separated string".into(),
        ));
    };
    let mut result = Vec::new();
    for tag in tags {
        if tag.chars().count() > 30 {
            return Err(AppError::Invalid(
                "Bilibili tag longer than 30 characters".into(),
            ));
        }
        if !result.contains(&tag) {
            result.push(tag);
        }
    }
    if result.is_empty() || result.len() > 10 {
        return Err(AppError::Invalid("Bilibili requires 1-10 tags".into()));
    }
    Ok(result)
}
pub(crate) fn normalized(mut meta: Value) -> AppResult<Value> {
    let source = meta
        .as_object_mut()
        .ok_or_else(|| AppError::Invalid("publish_meta must be a JSON object".into()))?;
    if serde_json::to_vec(source)
        .map(|v| v.len())
        .unwrap_or(usize::MAX)
        > MAX_META_BYTES
    {
        return Err(AppError::Invalid("publish_meta exceeds 64KiB".into()));
    }
    if source.keys().any(|key| {
        let name = key.to_ascii_lowercase();
        key.len() > 128
            || key.chars().any(char::is_control)
            || [
                "password",
                "cookie",
                "api_key",
                "authorization",
                "secret",
                "credential",
            ]
            .iter()
            .any(|unsafe_key| name.contains(unsafe_key))
    }) {
        return Err(AppError::Invalid("invalid publish metadata field".into()));
    }
    let title = source
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if title.is_empty() || title.chars().count() > 80 {
        return Err(AppError::Invalid(
            "Bilibili title must contain 1-80 characters".into(),
        ));
    }
    let title = title.to_owned();
    let typeid = source
        .get("typeid")
        .or_else(|| source.get("tid"))
        .and_then(Value::as_u64)
        .ok_or_else(|| AppError::Invalid("Bilibili typeid is required".into()))?;
    if typeid == 0 || typeid > u32::MAX as u64 {
        return Err(AppError::Invalid("Bilibili typeid must be positive".into()));
    }
    let tags = tags_from_input(
        source
            .get("tags")
            .or_else(|| source.get("tag"))
            .ok_or_else(|| AppError::Invalid("Bilibili tags missing".into()))?,
    )?;
    let copyright = source.get("copyright").and_then(Value::as_u64).unwrap_or(1);
    if copyright != 1 && copyright != 2 {
        return Err(AppError::Invalid(
            "Bilibili copyright must be 1 or 2".into(),
        ));
    }
    let original = source
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if copyright == 2 && original.is_empty() {
        return Err(AppError::Invalid(
            "reposted video requires source attribution".into(),
        ));
    }
    for name in ["topic_id", "mission_id"] {
        if let Some(value) = source.get(name).filter(|v| !v.is_null())
            && value.as_i64().is_none_or(|id| id <= 0)
        {
            return Err(AppError::Invalid(format!(
                "{name} must be a positive ID (platform eligibility not yet verified)"
            )));
        }
    }
    let desc = source
        .get("desc")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    source.insert("title".into(), json!(title));
    source.insert("typeid".into(), json!(typeid));
    source.insert("tags".into(), json!(tags));
    source.insert("copyright".into(), json!(copyright));
    source.insert("source".into(), json!(original));
    source.insert("desc".into(), json!(clamped_desc(&desc)));
    source.remove("tid");
    source.remove("tag");
    // Match Bilibili's original field defaults without requiring AI.
    source.entry("dynamic").or_insert(json!(""));
    source.entry("desc_format_id").or_insert(json!(9999));
    source.entry("no_reprint").or_insert(json!(1));
    source.entry("web_os").or_insert(json!(3));
    source
        .entry("subtitle")
        .or_insert(json!({"open":0,"lan":""}));
    // The same typed schema and whitelist are used by the irreversible
    // Bilibili publisher. Never claim a setting is saved if upload ignores it.
    let parsed: crate::bilibili::ArchiveMeta = serde_json::from_value(meta.clone())
        .map_err(|e| AppError::Invalid(format!("native Bilibili metadata schema invalid: {e}")))?;
    parsed.validate()?;
    Ok(meta)
}
fn overlay(base: &mut Map<String, Value>, value: &Value) -> AppResult<()> {
    let obj = value
        .as_object()
        .ok_or_else(|| AppError::Invalid("publish metadata must be an object".into()))?;
    for (k, v) in obj {
        base.insert(k.to_string(), v.clone());
    }
    Ok(())
}
pub async fn saved(db: &Database, id: Uuid) -> AppResult<Option<Value>> {
    sqlx::query_scalar::<_, Value>("SELECT value_json FROM vr2_publish_meta WHERE task_id=$1")
        .bind(id)
        .fetch_optional(&db.pool)
        .await
        .map_err(Into::into)
}
async fn read_saved(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    read_task(&state.db, id).await?;
    let meta = saved(&state.db, id)
        .await?
        .ok_or_else(|| AppError::NotFound("publish_meta not saved".into()))?;
    Ok(Json(meta))
}
#[derive(Deserialize)]
struct DraftRequest {
    #[serde(default = "default_mode")]
    mode: String,
    meta: Option<Value>,
}
fn default_mode() -> String {
    "auto".into()
}
async fn title_for(db: &Database, id: Uuid) -> AppResult<Option<String>> {
    let value: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(format!("task.title.{id}"))
            .fetch_optional(&db.pool)
            .await?;
    let v1 = value.and_then(|v| {
        v.get("translated_title")
            .or_else(|| v.get("source_title"))
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
            .map(str::to_owned)
    });
    if v1.is_some() {
        return Ok(v1);
    }
    let v2: Option<String> = sqlx::query_scalar(
        "SELECT COALESCE(NULLIF(trim(translated_title),''),NULLIF(trim(source_title),''))
         FROM vr2_task_titles WHERE task_id=$1",
    )
    .bind(id)
    .fetch_optional(&db.pool)
    .await?;
    Ok(v2)
}
async fn source_metadata(state: &AppState, id: Uuid) -> AppResult<Option<Value>> {
    let key: Option<String> = sqlx::query_scalar(
        "SELECT storage_key FROM assets WHERE task_id=$1 AND kind='metadata_json'::asset_kind
         AND storage_key LIKE 'v2/%' ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    let Some(key) = key else { return Ok(None) };
    let path = state.storage.resolve_existing(&key).await?;
    let bytes = tokio::fs::read(path).await?;
    if bytes.len() > 2_000_000 {
        return Err(AppError::Invalid(
            "YouTube metadata exceeds read budget".into(),
        ));
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Invalid("invalid cached YouTube metadata".into()))?;
    Ok(if value.is_object() { Some(value) } else { None })
}
async fn build_draft(state: &AppState, id: Uuid, req: DraftRequest) -> AppResult<Value> {
    if !matches!(req.mode.as_str(), "auto" | "default" | "source") {
        return Err(AppError::Invalid(
            "draft mode must be auto,default,source".into(),
        ));
    }
    let task = read_task(&state.db, id).await?;
    let configured = crate::settings::effective(&state.db, "publish.settings").await?;
    let mut meta = configured
        .get("default_meta")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let stored = saved(&state.db, id).await?;
    if req.mode == "auto"
        && let Some(value) = stored.as_ref()
    {
        overlay(&mut meta, value)?;
    }
    if let Some(value) = req.meta.as_ref() {
        overlay(&mut meta, value)?;
    }
    let use_source =
        req.mode == "source" || (req.mode == "auto" && stored.is_none() && req.meta.is_none());
    if use_source {
        let yt = source_metadata(state, id).await?;
        let source_title = title_for(&state.db, id)
            .await?
            .filter(|v| !v.trim().is_empty())
            .or_else(|| {
                yt.as_ref()
                    .and_then(|v| v.get("title").and_then(Value::as_str))
                    .map(str::to_owned)
            });
        if let Some(title) = source_title.filter(|v| !v.trim().is_empty()) {
            let profile = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
            let prefix = profile
                .get("publish_title_prefix")
                .and_then(Value::as_str)
                .unwrap_or("");
            let combined = if !prefix.is_empty() && !title.starts_with(prefix) {
                format!("{prefix}{title}")
            } else {
                title
            };
            meta.insert(
                "title".into(),
                json!(combined.chars().take(80).collect::<String>()),
            );
        }
        if let Some(source) = task.source_url.as_deref() {
            // Source attribution is required for reposted content.
            meta.insert("copyright".into(), json!(2));
            meta.insert("source".into(), json!(source));
            let desc = yt
                .as_ref()
                .and_then(|v| v.get("description").and_then(Value::as_str))
                .unwrap_or("");
            if !desc.is_empty()
                && meta
                    .get("desc")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .is_empty()
            {
                meta.insert(
                    "desc".into(),
                    json!(clamped_desc(&format!("{desc}\n\n原视频：{source}"))),
                );
            }
        }
    }
    // Never invent a partition or a mission ID. A missing partition prevents
    // save/publish until the operator selects one.
    if !meta.contains_key("typeid")
        && let Some(tid) = meta.get("tid").cloned()
    {
        meta.insert("typeid".into(), tid);
    }
    if meta
        .get("tags")
        .and_then(Value::as_array)
        .is_some_and(Vec::is_empty)
        || !meta.contains_key("tags")
    {
        meta.insert("tags".into(), json!(["视频分享"]));
    }
    Ok(Value::Object(meta))
}
async fn draft_get(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    Ok(Json(json!({"meta":build_draft(&state,id,DraftRequest{
        mode:"auto".into(),meta:None
    }).await?})))
}
async fn draft_post(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(req): Json<DraftRequest>,
) -> AppResult<Json<Value>> {
    Ok(Json(json!({"meta":build_draft(&state,id,req).await?})))
}
async fn save_meta(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(meta): Json<Value>,
) -> AppResult<Json<Value>> {
    let normalized = normalized(meta)?;
    let bytes = serde_json::to_vec(&normalized)
        .map_err(|_| AppError::Invalid("cannot serialize metadata".into()))?;
    let digest = hex::encode(Sha256::digest(&bytes));
    let mut tx = state.db.pool.begin().await?;
    // Task-first lock order matches the scheduler and publish lifecycle.
    let state_row = sqlx::query(
        "SELECT status::text AS status FROM tasks t WHERE t.id=$1
         AND EXISTS(SELECT 1 FROM vr2_managed_tasks WHERE task_id=$1) FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("V2 task not found".into()))?;
    let status: String = state_row.try_get("status")?;
    if matches!(status.as_str(), "PUBLISHED" | "PUBLISHING" | "CANCELED") {
        return Err(AppError::Conflict(
            "published, publishing or stopped task metadata is immutable".into(),
        ));
    }
    let risky: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND stage='publish'
         AND state IN ('pending','running','awaiting','unknown','succeeded'))",
    )
    .bind(id)
    .fetch_one(&mut *tx)
    .await?;
    if risky {
        return Err(AppError::Conflict(
            "publish job already queued, submitted or unresolved; metadata frozen".into(),
        ));
    }
    sqlx::query("DELETE FROM vr2_publish_reviews WHERE task_id=$1")
        .bind(id)
        .execute(&mut *tx)
        .await?;
    sqlx::query(
        "INSERT INTO vr2_publish_meta(task_id,value_json,sha256) VALUES($1,$2,$3)
         ON CONFLICT(task_id) DO UPDATE SET value_json=EXCLUDED.value_json,
         sha256=EXCLUDED.sha256,revision=vr2_publish_meta.revision+1,updated_at=now()",
    )
    .bind(id)
    .bind(&normalized)
    .bind(&digest)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"stored":true,"key":format!("db:vr2_publish_meta:{id}"),"meta":normalized}),
    ))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{id}/publish_meta", get(read_saved).put(save_meta))
        .route(
            "/tasks/{id}/publish_meta/draft",
            get(draft_get).post(draft_post),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn metadata_validation_prevents_unreviewed_activity_and_bad_tags() {
        let valid = json!({"title":"测试","typeid":21,"tags":["视频分享"],"copyright":2,
            "source":"https://youtu.be/0123456789A","topic_id":null});
        assert!(normalized(valid).is_ok());
        assert!(
            normalized(json!({"title":"bad","typeid":21,"tags":["foo"],"copyright":2})).is_err()
        );
        assert!(
            normalized(json!({"title":"bad","typeid":21,"tags":["foo"],"topic_id":0})).is_err()
        );
        assert!(normalized(json!({"title":"bad","typeid":0,"tags":["foo"]})).is_err());
        assert!(normalized(json!({"title":"bad","typeid":21,"tags":[]})).is_err());
        assert!(
            normalized(json!({"title":"bad","typeid":21,"tags":["one"],"api_key":"unsafe"}))
                .is_err()
        );
    }
    #[test]
    fn metadata_tags_and_description_match_legacy_rules() {
        let result = normalized(json!({"title":" 测试 ","tid":17,"tags":"猫，视频,猫",
            "desc":"中".repeat(1_000)}))
        .unwrap();
        assert_eq!(result["title"], "测试");
        assert_eq!(result["typeid"], 17);
        assert_eq!(result["tags"], json!(["猫", "视频"]));
        assert!(desc_units(result["desc"].as_str().unwrap()) <= 1_800);
    }
}
