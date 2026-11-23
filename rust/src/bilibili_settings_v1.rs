//! Original Bilibili default metadata settings, stored in the original
//! app_settings('bilibili.publish'). This is a draft template: empty tags are
//! allowed here, but NEVER allowed by an actual archive submission.
use crate::{AppError, AppResult, api::AppState, settings};
use axum::{Json, Router, extract::State, routing::get};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::Row;

const KEY: &str = "bilibili.publish";
fn baseline() -> Value {
    json!({"title":"示例标题","desc":"示例简介（请包含来源/授权说明）", "tags":[],
     "typeid":17,"copyright":1,"source":"","dtime":null,"dynamic":"",
     "desc_format_id":9999,"desc_v2":null,"recreate":-1,"interactive":0,
     "act_reserve_create":0,"no_reprint":1,"no_disturbance":0,
     "subtitle":{"open":0,"lan":""},"dolby":0,"lossless_music":0,
     "up_selection_reply":false,"up_close_reply":false,"up_close_danmu":false,
     "web_os":3,"is_only_self":null,"topic_id":null,"mission_id":null,
     "is_360":null,"neutral_mark":null,"human_type2":null})
}
fn normalize(input: &Value) -> AppResult<Value> {
    let data = input
        .as_object()
        .ok_or_else(|| AppError::Invalid("default_meta must be an object".into()))?;
    let mut out = baseline().as_object().cloned().unwrap();
    for (key, value) in data {
        out.insert(key.clone(), value.clone());
    }
    if !data.contains_key("typeid") {
        if let Some(tid) = data.get("tid") {
            out.insert("typeid".into(), tid.clone());
        }
    }
    if !data.contains_key("tags") {
        if let Some(tags) = data.get("tag").or_else(|| data.get("keywords")) {
            out.insert("tags".into(), tags.clone());
        }
    }
    let title = out
        .get("title")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Invalid("meta.title is required".into()))?
        .trim();
    if title.is_empty() || title.chars().count() > 80 {
        return Err(AppError::Invalid(
            "meta.title must contain 1–80 characters".into(),
        ));
    }
    out.insert("title".into(), json!(title));
    let typeid = out
        .get("typeid")
        .and_then(Value::as_i64)
        .filter(|x| *x > 0)
        .ok_or_else(|| AppError::Invalid("meta.typeid must be positive".into()))?;
    out.insert("typeid".into(), json!(typeid));
    let tags = match out.get("tags") {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|v| v.as_str().map(str::trim))
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect::<Vec<_>>(),
        Some(Value::String(text)) => text
            .replace('，', ",")
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect(),
        Some(Value::Null) => vec![],
        _ => {
            return Err(AppError::Invalid(
                "meta.tags must be an array or comma-separated text".into(),
            ));
        }
    };
    let mut unique = Vec::new();
    for tag in tags {
        if !unique.contains(&tag) {
            unique.push(tag);
        }
    }
    if unique.len() > 10 {
        return Err(AppError::Invalid("meta.tags limited to 10".into()));
    }
    out.insert("tags".into(), json!(unique));
    for field in ["desc", "source", "dynamic"] {
        let text = out
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Invalid(format!("meta.{field} must be text")))?
            .trim()
            .to_string();
        out.insert(field.into(), json!(text));
    }
    let copyright = out.get("copyright").and_then(Value::as_i64).unwrap_or(0);
    if copyright != 1 && copyright != 2 {
        return Err(AppError::Invalid("meta.copyright must be 1 or 2".into()));
    }
    if copyright == 2 && out.get("source").and_then(Value::as_str) == Some("") {
        return Err(AppError::Invalid("meta.source required for reposts".into()));
    }
    if !matches!(out.get("recreate").and_then(Value::as_i64), Some(-1 | 1))
        || !matches!(out.get("no_reprint").and_then(Value::as_i64), Some(0 | 1))
        || !matches!(
            out.get("no_disturbance").and_then(Value::as_i64),
            Some(0 | 1)
        )
    {
        return Err(AppError::Invalid("invalid Bilibili metadata flags".into()));
    }
    // Python Pydantic serializes these aliases under their canonical names.
    for alias in ["tid", "tag", "keywords", "up_close_danmaku"] {
        out.remove(alias);
    }
    Ok(Value::Object(out))
}
fn view(stored: &Value) -> Value {
    let raw = stored
        .get("default_meta")
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let mut merged = baseline().as_object().cloned().unwrap();
    for (k, v) in raw.as_object().unwrap() {
        merged.insert(k.clone(), v.clone());
    }
    if let Some(v) = merged.get("tags")
        && (v == &json!(["videoroll"]) || v == &json!(["视频分享"]))
    {
        merged.insert("tags".into(), json!([]));
    }
    let meta = normalize(&Value::Object(merged)).unwrap_or_else(|_| baseline());
    let workers = stored
        .get("upload_workers")
        .and_then(Value::as_i64)
        .unwrap_or(4)
        .clamp(1, 8);
    json!({"default_meta":meta,"upload_workers":workers,
           "ai_activity_enabled":stored.get("ai_activity_enabled").and_then(Value::as_bool)==Some(true)})
}
async fn read(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(view(&settings::effective(&state.db, KEY).await?)))
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Update {
    default_meta: Option<Value>,
    upload_workers: Option<i64>,
    ai_activity_enabled: Option<bool>,
}
async fn write(
    State(state): State<AppState>,
    Json(update): Json<Update>,
) -> AppResult<Json<Value>> {
    if update.upload_workers.is_some_and(|v| !(1..=8).contains(&v)) {
        return Err(AppError::Invalid("upload_workers must be 1..8".into()));
    }
    let normalized = update.default_meta.as_ref().map(normalize).transpose()?;
    let mut tx = state.db.pool.begin().await?;
    sqlx::query(
        "SELECT pg_advisory_xact_lock(hashtextextended('app.settings:bilibili.publish',0))",
    )
    .execute(&mut *tx)
    .await?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES($1,'{}'::jsonb,1) ON CONFLICT(key) DO NOTHING")
       .bind(KEY).execute(&mut *tx).await?;
    let row = sqlx::query("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
        .bind(KEY)
        .fetch_one(&mut *tx)
        .await?;
    let mut stored: Value = row.try_get("value_json")?;
    if !stored.is_object() {
        return Err(AppError::Conflict(
            "Bilibili settings are not an object".into(),
        ));
    }
    let changed = normalized.is_some()
        || update.upload_workers.is_some()
        || update.ai_activity_enabled.is_some();
    if let Some(meta) = normalized {
        stored["default_meta"] = meta;
    }
    if let Some(workers) = update.upload_workers {
        stored["upload_workers"] = json!(workers);
    }
    if let Some(enabled) = update.ai_activity_enabled {
        stored["ai_activity_enabled"] = json!(enabled);
    }
    if changed {
        sqlx::query(
            "UPDATE app_settings SET value_json=$2,version=version+1,updated_at=now() WHERE key=$1",
        )
        .bind(KEY)
        .bind(&stored)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(Json(view(&stored)))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/bilibili/publish/settings", get(read).put(write))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn templates_support_empty_tags_without_weakening_archive_validation() {
        assert_eq!(view(&json!({}))["default_meta"]["tags"], json!([]));
        assert_eq!(
            normalize(&json!({"tags":"abc,abc,def"})).unwrap()["tags"],
            json!(["abc", "def"])
        );
        assert!(normalize(&json!({"copyright":2,"source":""})).is_err());
    }
}
