//! Native V1 `POST /subtitle/jobs` adapter.
//!
//! Python queued a `subtitle_jobs` row for Hatchet. A Rust-native request must
//! instead enqueue the real durable Rust subtitle stage; inserting an orphan
//! legacy queued row would lie about work being performed. GET /subtitle/jobs/id
//! already resolves both legacy rows and Rust stage IDs.
use crate::{AppError, AppResult, api::AppState, domain::Stage, legacy_contracts::read_task};
use axum::{Json, Router, extract::State, routing::post};
use serde_json::{Map, Value, json};
use sqlx::Row;
use uuid::Uuid;

fn invalid(reason: &str) -> AppError {
    AppError::Invalid(reason.into())
}
fn object<'a>(value: &'a Value, field: &str) -> AppResult<&'a Map<String, Value>> {
    value
        .as_object()
        .ok_or_else(|| invalid(&format!("{field} must be an object")))
}
fn allowed(map: &Map<String, Value>, keys: &[&str], field: &str) -> AppResult<()> {
    if let Some(key) = map.keys().find(|k| !keys.contains(&k.as_str())) {
        return Err(invalid(&format!(
            "{field}.{key} is unsupported by native Rust subtitle jobs"
        )));
    }
    Ok(())
}
fn optional_object<'a>(
    input: &'a Map<String, Value>,
    key: &str,
) -> AppResult<Option<&'a Map<String, Value>>> {
    input
        .get(key)
        .filter(|v| !v.is_null())
        .map(|v| object(v, key))
        .transpose()
}
fn config(request: &Value) -> AppResult<(Uuid, String, Value)> {
    let map = object(request, "subtitle job")?;
    allowed(
        map,
        &[
            "task_id",
            "runtime_profile",
            "resume",
            "prefer_youtube_subtitles",
            "youtube_subtitle_mode",
            "input",
            "asr",
            "translate",
            "output",
            "artifacts",
            "selective_retranslate",
            "output_prefix",
            "after_render",
        ],
        "subtitle job",
    )?;
    let id = map
        .get("task_id")
        .and_then(Value::as_str)
        .and_then(|s| Uuid::parse_str(s).ok())
        .ok_or_else(|| invalid("task_id must be UUID"))?;
    if map
        .get("runtime_profile")
        .is_some_and(|v| v == &Value::Bool(true))
    {
        return Err(invalid(
            "runtime_profile=true requires an automatic workflow already owned by Rust",
        ));
    }
    if map
        .get("runtime_profile")
        .is_some_and(|v| !v.is_null() && !v.is_boolean())
    {
        return Err(invalid("runtime_profile must be boolean or null"));
    }
    for key in ["artifacts", "selective_retranslate", "after_render"] {
        if map
            .get(key)
            .is_some_and(|v| !v.is_null() && v.as_object().is_none_or(|o| !o.is_empty()))
        {
            return Err(invalid(&format!(
                "{key} requires the unported Python subtitle worker"
            )));
        }
    }
    if map
        .get("output_prefix")
        .and_then(Value::as_str)
        .is_some_and(|v| !v.is_empty())
    {
        return Err(invalid(
            "output_prefix requires the unported Python subtitle worker",
        ));
    }
    let input = object(
        map.get("input")
            .ok_or_else(|| invalid("input is required"))?,
        "input",
    )?;
    allowed(input, &["type", "key"], "input")?;
    if input.get("type").is_some_and(|v| !v.is_string()) {
        return Err(invalid("input.type must be a string"));
    }
    let input_type = input
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("storage");
    if input_type != "storage" {
        return Err(invalid("only V2-owned storage input is supported"));
    }
    let key = input.get("key").and_then(Value::as_str).unwrap_or("");
    if !key.starts_with("v2/") || key.len() > 512 || key.chars().any(char::is_control) {
        return Err(invalid(
            "input.key must reference a V2-owned storage object",
        ));
    }
    if map
        .get("prefer_youtube_subtitles")
        .is_some_and(|v| !v.is_boolean())
    {
        return Err(invalid("prefer_youtube_subtitles must be boolean"));
    }
    if map
        .get("youtube_subtitle_mode")
        .is_some_and(|v| !v.is_string())
    {
        return Err(invalid("youtube_subtitle_mode must be a string"));
    }
    let prefer = map
        .get("prefer_youtube_subtitles")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let mode = map
        .get("youtube_subtitle_mode")
        .and_then(Value::as_str)
        .unwrap_or(if prefer { "target" } else { "off" });
    if prefer || mode != "off" {
        return Err(invalid(
            "YouTube subtitle source selection is not supported by the native ASR stage",
        ));
    }
    let asr = optional_object(map, "asr")?;
    let translation = optional_object(map, "translate")?;
    let output = optional_object(map, "output")?;
    if let Some(v) = asr {
        allowed(v, &["engine", "language", "model"], "asr")?;
    }
    if let Some(v) = translation {
        allowed(
            v,
            &[
                "enabled",
                "target_lang",
                "provider",
                "style",
                "batch_size",
                "enable_summary",
                "glossary_id",
                "bilingual",
            ],
            "translate",
        )?;
    }
    if let Some(v) = output {
        allowed(v, &["formats", "render"], "output")?;
    }
    let render = output
        .and_then(|v| v.get("render"))
        .filter(|v| !v.is_null())
        .map(|v| object(v, "output.render"))
        .transpose()?;
    if let Some(v) = render {
        allowed(
            v,
            &[
                "burn_in",
                "soft_sub",
                "ass_style",
                "video_codec",
                "use_intel_gpu",
                "video_preset",
                "video_crf",
                "primary_font_scale_percent",
                "secondary_font_scale_percent",
            ],
            "output.render",
        )?;
    }
    if render.is_some_and(|r| {
        ["primary_font_scale_percent", "secondary_font_scale_percent"]
            .iter()
            .any(|field| {
                r.get(*field)
                    .is_some_and(|value| value.as_i64() != Some(100))
            })
    }) {
        return Err(invalid(
            "custom or invalid subtitle font scaling requires the unported render adapter",
        ));
    }
    if translation.is_some_and(|r| {
        r.get("glossary_id")
            .is_some_and(|v| !v.is_null() && v.as_str().is_none_or(|s| !s.is_empty()))
    }) {
        return Err(invalid(
            "explicit glossary_id requires the unported translation adapter",
        ));
    }
    // Python's SubtitleJobCreate default options are NOT the same as the
    // saved automatic Rust profile. Freeze the original manual defaults here,
    // otherwise a minimal request can unexpectedly translate or burn in media.
    let mut flat = Map::new();
    for (field, value) in [
        ("asr_engine", json!("auto")),
        ("asr_language", json!("auto")),
        ("translate_enabled", json!(false)),
        ("translate_provider", json!("mock")),
        ("target_lang", json!("zh")),
        ("translate_style", json!("口语自然")),
        ("bilingual", json!(false)),
        ("formats", json!(["srt"])),
        ("burn_in", json!(false)),
        ("soft_sub", json!(false)),
        ("ass_style", json!("clean_white")),
        ("video_codec", json!("av1")),
        ("use_intel_gpu", json!(false)),
    ] {
        flat.insert(field.into(), value);
    }
    if let Some(v) = map.get("resume") {
        flat.insert("resume".into(), v.clone());
    }
    flat.insert("prefer_youtube_subtitles".into(), json!(false));
    flat.insert("youtube_subtitle_mode".into(), json!("off"));
    if let Some(v) = asr {
        for (from, to) in [
            ("engine", "asr_engine"),
            ("language", "asr_language"),
            ("model", "asr_model"),
        ] {
            if let Some(value) = v.get(from) {
                flat.insert(to.into(), value.clone());
            }
        }
    }
    if let Some(v) = translation {
        for (from, to) in [
            ("enabled", "translate_enabled"),
            ("target_lang", "target_lang"),
            ("provider", "translate_provider"),
            ("style", "translate_style"),
            ("batch_size", "translate_batch_size"),
            ("enable_summary", "translate_enable_summary"),
            ("bilingual", "bilingual"),
        ] {
            if let Some(value) = v.get(from) {
                flat.insert(to.into(), value.clone());
            }
        }
    }
    if let Some(v) = output {
        if let Some(value) = v.get("formats") {
            flat.insert("formats".into(), value.clone());
        }
    }
    if let Some(v) = render {
        for field in [
            "burn_in",
            "soft_sub",
            "ass_style",
            "video_codec",
            "use_intel_gpu",
            "video_preset",
            "video_crf",
        ] {
            if let Some(value) = v.get(field) {
                flat.insert(field.into(), value.clone());
            }
        }
    }
    let action = serde_json::from_value::<crate::task_manual::SubtitleAction>(Value::Object(flat))
        .map_err(|_| invalid("invalid subtitle job field types or limits"))?;
    let config = crate::task_manual::subtitle_config(action)?;
    Ok((id, key.into(), config))
}

async fn create(
    State(state): State<AppState>,
    Json(request): Json<Value>,
) -> AppResult<Json<Value>> {
    let (task_id, key, config) = config(&request)?;
    let task = read_task(&state.db, task_id).await?;
    if matches!(
        task.status.as_str(),
        "PUBLISHED" | "CANCELED" | "PUBLISHING"
    ) {
        return Err(AppError::Conflict(
            "task is stopped or already publishing/published".into(),
        ));
    }
    // Match Python's original active-job idempotency: do not schedule a Rust
    // generation while a still-live Python/Hatchet job owns the task.
    let existing = sqlx::query(
        "SELECT id,status::text AS status FROM subtitle_jobs
        WHERE task_id=$1 AND status::text IN ('queued','running')
        ORDER BY created_at,id LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?;
    if let Some(row) = existing {
        return Ok(Json(json!({"job_id":row.try_get::<Uuid,_>("id")?,
            "status":row.try_get::<String,_>("status")?})));
    }
    let actual: Option<String> = sqlx::query_scalar(
        "SELECT storage_key FROM assets
        WHERE task_id=$1 AND kind='video_raw'::asset_kind
        ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_optional(&state.db.pool)
    .await?;
    if actual.as_deref() != Some(key.as_str()) {
        return Err(AppError::Conflict(
            "requested input.key is not the task's current V2 raw video asset".into(),
        ));
    }
    // An active native subtitle stage may produce an SRT before its lease is
    // committed. A repeated request must reuse that exact in-flight stage,
    // not be rejected simply because the output artifact appeared meanwhile.
    // restart_at still rejects different effective settings under task lock.
    let active_native: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE j.task_id=$1 AND j.stage='subtitle'
           AND j.state IN ('pending','running','awaiting')
           AND w.state='running' AND w.current_stage='subtitle')",
    )
    .bind(task_id)
    .fetch_one(&state.db.pool)
    .await?;
    let latest_srt: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND kind='subtitle_srt'::asset_kind)",
    )
    .bind(task_id)
    .fetch_one(&state.db.pool)
    .await?;
    if latest_srt && !active_native {
        return Err(AppError::Conflict(
            "existing subtitles cannot be silently reused as a new V1 subtitle job".into(),
        ));
    }
    let flow = state
        .scheduler
        .restart_at(task_id, Stage::Subtitle, config)
        .await?;
    let row = sqlx::query(
        "SELECT id,state FROM vr2_jobs WHERE workflow_id=$1 AND stage='subtitle'
        ORDER BY created_at DESC LIMIT 1",
    )
    .bind(flow.id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::Unavailable("native subtitle stage not durably queued".into()))?;
    let status: String = row.try_get("state")?;
    let status = match status.as_str() {
        "pending" | "awaiting" => "queued",
        "running" => "running",
        "succeeded" => "succeeded",
        "failed" => "failed",
        "unknown" => {
            return Err(AppError::Conflict(
                "subtitle result is uncertain; do not reschedule".into(),
            ));
        }
        _ => {
            return Err(AppError::Conflict(
                "native subtitle stage is not schedulable".into(),
            ));
        }
    };
    Ok(Json(
        json!({"job_id":row.try_get::<Uuid,_>("id")?,"status":status}),
    ))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/jobs", post(create))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_subtitle_requests_validate_options_before_scheduling() {
        let id = Uuid::new_v4();
        let request = json!({"task_id":id,"input":{"type":"storage","key":"v2/video/raw.mp4"},
            "prefer_youtube_subtitles":false,"youtube_subtitle_mode":"off",
            "asr":{"engine":"auto","language":"en"},
            "translate":{"enabled":true,"provider":"mock","target_lang":"zh"},
            "output":{"formats":["srt","ass"],"render":{"burn_in":false}}});
        let (result, key, normalized) = config(&request).unwrap();
        assert_eq!(result, id);
        assert_eq!(key, "v2/video/raw.mp4");
        assert_eq!(normalized["subtitle"]["target_lang"], "zh");
        let minimal = json!({"task_id":id,"input":{"key":"v2/video/raw.mp4"},
            "prefer_youtube_subtitles":false,"youtube_subtitle_mode":"off"});
        let (_, _, defaults) = config(&minimal).unwrap();
        assert_eq!(defaults["subtitle"]["translate_enabled"], false);
        assert_eq!(defaults["subtitle"]["translate_provider"], "mock");
        assert_eq!(defaults["render"]["burn_in"], false);
        assert_eq!(defaults["render"]["video_codec"], "av1");
        for (field, value) in [
            ("prefer_youtube_subtitles", json!(true)),
            ("youtube_subtitle_mode", json!("target")),
            ("selective_retranslate", json!({"indices":[1]})),
            ("output_prefix", json!("ignored")),
        ] {
            let mut bad = request.clone();
            bad[field] = value;
            assert!(config(&bad).is_err(), "{field} not rejected");
        }
        let mut wrong = request.clone();
        wrong["input"]["key"] = json!("other-task.mp4");
        assert!(config(&wrong).is_err());
    }
}
