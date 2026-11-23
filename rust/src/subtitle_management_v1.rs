//! Source-backed legacy subtitle management: original Python-shaped profile
//! and subtitle job detail. Unsupported automatic modes fail closed rather
//! than persisting options the native Rust worker would silently ignore.
use crate::{AppError, AppResult, api::AppState, settings};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use chrono::{DateTime, Utc};
use serde_json::{Map, Value, json};
use sqlx::Row;
use uuid::Uuid;

fn default_profile() -> Value {
    json!({
        "formats":["srt","ass"],"burn_in":true,"soft_sub":false,
        "ass_style":"clean_white","video_codec":"av1",
        "use_intel_gpu":false,"video_preset":null,"video_crf":null,
        "primary_font_scale_percent":100,
        "secondary_font_scale_percent":100,
        "asr_engine":"auto","asr_language":"auto","asr_model":null,
        "prefer_youtube_subtitles":true,"youtube_subtitle_mode":"target",
        "translate_enabled":true,"translate_provider":"openai",
        "translate_model_provider_id":"legacy","translate_model_id":"",
        "target_lang":"zh","translate_style":"口语自然",
        "translate_enable_summary":true,"bilingual":false,
        "auto_publish":true,"auto_publish_platforms":[],
        "publish_typeid_mode":"ai_summary","publish_title_prefix":"【熟肉】",
        "auto_dub_p2":false,"auto_dub_p2_prefix":"【中配】",
        "publish_translate_title":true,"publish_use_youtube_cover":true,
        "publish_enable_reprint":true
    })
}
fn normalized_profile(raw: &Value) -> Value {
    let mut baseline = default_profile();
    let Some(out) = baseline.as_object_mut() else {
        return baseline;
    };
    if let Some(data) = raw.as_object() {
        for (key, val) in data {
            if out.contains_key(key) {
                out.insert(key.clone(), val.clone());
            }
        }
    }
    // Keep legacy Python's view normalization for common stored values.
    let valid_formats = out
        .get("formats")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|s| matches!(*s, "srt" | "ass"))
        .map(str::to_string)
        .collect::<Vec<_>>();
    out.insert(
        "formats".into(),
        json!(if valid_formats.is_empty() {
            vec!["srt".to_string(), "ass".to_string()]
        } else {
            valid_formats
        }),
    );
    if out
        .get("video_codec")
        .and_then(Value::as_str)
        .is_none_or(|s| !matches!(s, "av1" | "h264"))
    {
        out.insert("video_codec".into(), json!("av1"));
    }
    if out
        .get("asr_engine")
        .and_then(Value::as_str)
        .is_none_or(|s| {
            !matches!(
                s,
                "auto"
                    | "mock"
                    | "faster-whisper"
                    | "openvino"
                    | "external-whisper"
                    | "groq-whisper"
                    | "cloudflare-workers-ai"
            )
        })
    {
        out.insert("asr_engine".into(), json!("auto"));
    }
    if out
        .get("translate_provider")
        .and_then(Value::as_str)
        .is_none_or(|s| !matches!(s, "mock" | "noop" | "openai" | "index_translate"))
    {
        out.insert("translate_provider".into(), json!("openai"));
    }
    let mode = out
        .get("youtube_subtitle_mode")
        .and_then(Value::as_str)
        .unwrap_or("target");
    let mode = if matches!(mode, "off" | "target" | "auto_source") {
        mode.to_owned()
    } else {
        "target".to_owned()
    };
    out.insert("youtube_subtitle_mode".into(), json!(mode));
    out.insert("prefer_youtube_subtitles".into(), json!(mode != "off"));
    for name in ["primary_font_scale_percent", "secondary_font_scale_percent"] {
        let v = out
            .get(name)
            .and_then(Value::as_i64)
            .unwrap_or(100)
            .clamp(25, 300);
        out.insert(name.into(), json!(v));
    }
    if let Some(v) = out.get("video_crf").and_then(Value::as_i64) {
        out.insert("video_crf".into(), json!(v.clamp(0, 63)));
    }
    for name in [
        "burn_in",
        "soft_sub",
        "use_intel_gpu",
        "translate_enabled",
        "translate_enable_summary",
        "bilingual",
        "auto_publish",
        "auto_dub_p2",
        "publish_translate_title",
        "publish_use_youtube_cover",
        "publish_enable_reprint",
    ] {
        let value = out.get(name).and_then(Value::as_bool).unwrap_or(false);
        out.insert(name.into(), json!(value));
    }
    baseline
}
async fn profile_get(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let stored = settings::effective(&state.db, "subtitle.auto_profile").await?;
    Ok(Json(normalized_profile(&stored)))
}
fn supported_update(key: &str) -> bool {
    matches!(
        key,
        "translate_enabled"
            | "target_lang"
            | "bilingual"
            | "asr_language"
            | "translate_style"
            | "translate_provider"
            | "translate_model_provider_id"
            | "translate_model_id"
    )
}
fn validate_update(update: &Map<String, Value>) -> AppResult<()> {
    for (key, value) in update {
        if !supported_update(key) {
            return Err(AppError::Conflict(format!(
                "subtitle profile field {key} requires an unported native workflow; not persisted"
            )));
        }
        if value.is_null() {
            continue;
        }
        match key.as_str() {
            "translate_enabled" | "bilingual" if !value.is_boolean() => {
                return Err(AppError::Invalid(format!("{key} requires bool")));
            }
            "translate_provider"
                if !value.as_str().is_some_and(|s| {
                    matches!(s, "openai" | "index_translate" | "mock" | "noop")
                }) =>
            {
                return Err(AppError::Invalid("unsupported translation provider".into()));
            }
            "translate_model_provider_id" if value.as_str().is_none_or(|s| s.len() > 100) => {
                return Err(AppError::Invalid("invalid provider id".into()));
            }
            "translate_model_id" if value.as_str().is_none_or(|s| s.len() > 256) => {
                return Err(AppError::Invalid("invalid model id".into()));
            }
            "target_lang" | "asr_language" | "translate_style"
                if value
                    .as_str()
                    .is_none_or(|s| s.trim().is_empty() || s.len() > 128) =>
            {
                return Err(AppError::Invalid(format!(
                    "{key} cannot be empty/oversized"
                )));
            }
            _ => {}
        }
    }
    Ok(())
}
async fn profile_put(
    State(state): State<AppState>,
    Json(payload): Json<Value>,
) -> AppResult<Json<Value>> {
    let update = payload
        .as_object()
        .ok_or_else(|| AppError::Invalid("subtitle profile must be a JSON object".into()))?;
    validate_update(update)?;
    let mut tx = state.db.pool.begin().await?;
    let raw: Value = sqlx::query_scalar(
        "SELECT value_json FROM app_settings WHERE key='subtitle.auto_profile' FOR UPDATE",
    )
    .fetch_optional(&mut *tx)
    .await?
    .unwrap_or(json!({}));
    let mut merged = raw.as_object().cloned().unwrap_or_default();
    for (key, val) in update {
        if !val.is_null() {
            merged.insert(key.clone(), val.clone());
        }
    }
    let normalized = normalized_profile(&Value::Object(merged.clone()));
    if normalized.get("translate_enabled").and_then(Value::as_bool) == Some(true)
        && normalized.get("translate_provider").and_then(Value::as_str) == Some("openai")
        && update.keys().any(|key| {
            matches!(
                key.as_str(),
                "translate_enabled"
                    | "translate_provider"
                    | "translate_model_provider_id"
                    | "translate_model_id"
            )
        })
    {
        let provider = normalized["translate_model_provider_id"]
            .as_str()
            .unwrap_or("legacy");
        let model = normalized["translate_model_id"].as_str().unwrap_or("");
        // Fail instead of silently switching to a hidden or unresolved model.
        crate::providers::resolve(&state.db, &state.config, provider, model).await?;
    }
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES('subtitle.auto_profile',$1,1)
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,
           version=app_settings.version+1,updated_at=now()",
    )
    .bind(json!(merged))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(normalized))
}
async fn job_get(
    State(state): State<AppState>,
    Path(job_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    // Read original Python job records; also accept stage ID from native V2.
    let row = sqlx::query(
        "SELECT id,task_id,status::text AS status,progress,logs_key,error_message,
            created_at,updated_at FROM subtitle_jobs WHERE id=$1",
    )
    .bind(job_id)
    .fetch_optional(&state.db.pool)
    .await?;
    let (id, task, status, progress, logs, error, created, updated) = if let Some(row) = row {
        (
            row.try_get::<Uuid, _>("id")?,
            row.try_get::<Uuid, _>("task_id")?,
            row.try_get::<String, _>("status")?,
            row.try_get::<i32, _>("progress")?,
            row.try_get::<Option<String>, _>("logs_key")?,
            row.try_get::<Option<String>, _>("error_message")?,
            row.try_get::<DateTime<Utc>, _>("created_at")?,
            row.try_get::<DateTime<Utc>, _>("updated_at")?,
        )
    } else {
        let row = sqlx::query(
            "SELECT id,task_id,state,created_at,updated_at,last_error FROM vr2_jobs
              WHERE id=$1 AND stage='subtitle'",
        )
        .bind(job_id)
        .fetch_optional(&state.db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("subtitle job not found".into()))?;
        let state: String = row.try_get("state")?;
        let progress = if state == "succeeded" { 100 } else { 0 };
        (
            row.try_get::<Uuid, _>("id")?,
            row.try_get::<Uuid, _>("task_id")?,
            match state.as_str() {
                "pending" | "awaiting" => "queued",
                "succeeded" => "succeeded",
                "running" => "running",
                "failed" => "failed",
                "canceled" => "canceled",
                _ => "paused",
            }
            .into(),
            progress,
            None,
            row.try_get::<Option<String>, _>("last_error")?,
            row.try_get::<DateTime<Utc>, _>("created_at")?,
            row.try_get::<DateTime<Utc>, _>("updated_at")?,
        )
    };
    let assets = sqlx::query(
        "SELECT kind::text AS kind,storage_key FROM assets WHERE task_id=$1
         AND kind::text IN ('audio_wav','segments_json','subtitle_srt',
                            'subtitle_ass','video_final','log')
         ORDER BY created_at ASC",
    )
    .bind(task)
    .fetch_all(&state.db.pool)
    .await?;
    let artifacts = assets
        .into_iter()
        .map(|row| -> AppResult<Value> {
            Ok(json!({"kind":row.try_get::<String,_>("kind")?,
                  "key":row.try_get::<String,_>("storage_key")?}))
        })
        .collect::<AppResult<Vec<_>>>()?;
    Ok(Json(
        json!({"job_id":id,"task_id":task,"status":status,"progress":progress,
      "artifacts":artifacts,"logs_key":logs,"error_message":error,
      "created_at":created,"updated_at":updated}),
    ))
}

async fn latest_task_subtitle_job(state: &AppState, task_id: Uuid) -> AppResult<(Uuid, Value)> {
    let exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1)")
        .bind(task_id)
        .fetch_one(&state.db.pool)
        .await?;
    if !exists {
        return Err(AppError::NotFound("task not found".into()));
    }
    let row = sqlx::query(
        "SELECT id,request_json FROM subtitle_jobs WHERE task_id=$1
         ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("subtitle job not found".into()))?;
    Ok((row.try_get("id")?, row.try_get("request_json")?))
}
pub(crate) fn job_artifact_key(request: &Value, field: &str) -> Option<String> {
    request
        .get("artifacts")
        .and_then(|v| v.get(field))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}
pub(crate) async fn read_job_report(state: &AppState, key: &str) -> AppResult<Value> {
    // Never trust a stored Python filename as a path; use the existing
    // symlink-aware V2 storage resolver even for read-only compatibility.
    let path = state.storage.resolve_existing(key).await?;
    let metadata = tokio::fs::metadata(&path).await.map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            AppError::NotFound("artifact not found".into())
        } else {
            AppError::Io(e)
        }
    })?;
    if metadata.len() > 2 * 1024 * 1024 {
        return Err(AppError::Invalid(
            "subtitle JSON artifact exceeds 2 MiB".into(),
        ));
    }
    let bytes = tokio::fs::read(path).await.map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            AppError::NotFound("artifact not found".into())
        } else {
            AppError::Io(e)
        }
    })?;
    if bytes.len() > 2 * 1024 * 1024 {
        return Err(AppError::Invalid(
            "subtitle JSON artifact exceeds 2 MiB".into(),
        ));
    }
    serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Invalid("subtitle artifact JSON invalid".into()))
}
fn collapsed(value: Option<&Value>, max: usize) -> String {
    let text = value.and_then(Value::as_str).unwrap_or("");
    let collapsed = text.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed.chars().take(max).collect()
}
pub(crate) fn context_memory(value: &Value) -> Value {
    let obj = value.as_object();
    let field = |name: &str, max| collapsed(obj.and_then(|o| o.get(name)), max);
    let mut characters = Vec::<Value>::new();
    if let Some(arr) = value.get("characters").and_then(Value::as_array) {
        for item in arr {
            if !item.is_object() {
                continue;
            }
            let name = collapsed(item.get("name"), 120);
            let target = collapsed(item.get("target_name"), 120);
            if name.is_empty() && target.is_empty() {
                continue;
            }
            let mut character = json!({
                "name":if name.is_empty(){target.clone()}else{name},
                "target_name":target,
                "role":collapsed(item.get("role"),240),
                "notes":collapsed(item.get("notes"),500)
            });
            let mut aliases = Vec::<String>::new();
            if let Some(list) = item.get("aliases").and_then(Value::as_array) {
                for entry in list {
                    let alias = collapsed(Some(entry), 120);
                    if !alias.is_empty() && !aliases.contains(&alias) {
                        aliases.push(alias);
                    }
                    if aliases.len() >= 8 {
                        break;
                    }
                }
            }
            if !aliases.is_empty() {
                character["aliases"] = json!(aliases);
            }
            characters.push(character);
            if characters.len() >= 32 {
                break;
            }
        }
    }
    let mut terms = Vec::<Value>::new();
    if let Some(list) = value.get("terminology").and_then(Value::as_array) {
        for item in list {
            if !item.is_object() {
                continue;
            }
            let source = collapsed(item.get("source"), 160);
            let target = collapsed(item.get("target"), 160);
            if source.is_empty() || target.is_empty() {
                continue;
            }
            terms.push(json!({"source":source,"target":target,
                              "meaning":collapsed(item.get("meaning"),500)}));
            if terms.len() >= 64 {
                break;
            }
        }
    }
    let mut ambiguities = Vec::<Value>::new();
    if let Some(list) = value.get("ambiguities").and_then(Value::as_array) {
        for item in list {
            if !item.is_object() {
                continue;
            }
            let term = collapsed(item.get("term"), 160);
            let resolution = collapsed(item.get("resolution"), 500);
            if term.is_empty() || resolution.is_empty() {
                continue;
            }
            ambiguities.push(json!({"term":term,"resolution":resolution}));
            if ambiguities.len() >= 32 {
                break;
            }
        }
    }
    let mut result = json!({
        "version":1,"topic":field("topic",500),
        "style_notes":field("style_notes",500),
        "characters":characters,"terminology":terms,"ambiguities":ambiguities
    });
    if let Some(scene) = value.get("recent_scene").filter(|v| v.is_object()) {
        let summary = collapsed(scene.get("summary"), 800);
        if !summary.is_empty() {
            let id = scene
                .get("scene_id")
                .and_then(Value::as_i64)
                .unwrap_or(1)
                .max(1);
            result["recent_scene"] = json!({"scene_id":id,"summary":summary});
        }
    }
    result
}
async fn task_quality(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let (job_id, request) = latest_task_subtitle_job(&state, task_id).await?;
    let Some(key) = job_artifact_key(&request, "subtitle_quality_key") else {
        return Ok(Json(
            json!({"available":false,"job_id":job_id,"report":null}),
        ));
    };
    let report = read_job_report(&state, &key).await?;
    Ok(Json(
        json!({"available":true,"job_id":job_id,"key":key,"report":report}),
    ))
}
async fn task_translation_context(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let (job_id, request) = latest_task_subtitle_job(&state, task_id).await?;
    let Some(key) = job_artifact_key(&request, "translation_context_key") else {
        return Ok(Json(json!({
            "available":false,"job_id":job_id,"summary":"",
            "memory":context_memory(&json!({})),"affected_indices":[]
        })));
    };
    let report = read_job_report(&state, &key).await?;
    let summary = report.get("summary").and_then(Value::as_str).unwrap_or("");
    Ok(Json(json!({
        "available":true,"job_id":job_id,"key":key,
        "summary":summary.chars().take(500).collect::<String>(),
        "memory":context_memory(report.get("memory").unwrap_or(&Value::Null)),
        "affected_indices":[]
    })))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/auto/profile", get(profile_get).put(profile_put))
        .route("/subtitle/jobs/{job_id}", get(job_get))
        .route("/subtitle/tasks/{task_id}/quality", get(task_quality))
        .route(
            "/subtitle/tasks/{task_id}/translation-context",
            get(task_translation_context).put(crate::subtitle_context_write_v1::save_context),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_and_native_scope() {
        let view = normalized_profile(&json!({}));
        assert_eq!(view["translate_provider"], "openai");
        assert_eq!(view["auto_publish_platforms"], json!([]));
        assert!(validate_update(json!({"auto_publish":true}).as_object().unwrap()).is_err());
        assert!(validate_update(json!({"translate_enabled":false}).as_object().unwrap()).is_ok());
    }
}
