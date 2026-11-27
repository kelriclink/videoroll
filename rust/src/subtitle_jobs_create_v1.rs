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
pub(crate) fn config(request: &Value) -> AppResult<(Uuid, String, Value)> {
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
        .is_some_and(|v| !v.is_null() && !v.is_boolean())
    {
        return Err(invalid("runtime_profile must be boolean or null"));
    }
    let selective = optional_object(map, "selective_retranslate")?.filter(|v| !v.is_empty());
    let artifacts = optional_object(map, "artifacts")?.filter(|v| !v.is_empty());
    if let Some(plan) = selective {
        if map.get("runtime_profile").and_then(Value::as_bool) == Some(true) {
            return Err(invalid(
                "native selective retranslation freezes the prior source configuration; runtime_profile=true is not supported",
            ));
        }

        allowed(
            plan,
            &[
                "indices",
                "base_translated_segments_key",
                "translation_context_key",
            ],
            "selective_retranslate",
        )?;
        if !plan.get("indices").is_some_and(|v| v.is_array()) {
            return Err(invalid("selective indices required"));
        }
        if plan
            .get("translation_context_key")
            .is_some_and(|v| !v.is_null() && v.as_str().is_none_or(|s| !s.is_empty()))
        {
            return Err(invalid(
                "legacy separate translation context is not supported; use native aligned snapshot",
            ));
        }
        if let Some(a) = artifacts {
            allowed(
                a,
                &["source_segments_key", "final_subtitle_segments_key"],
                "artifacts",
            )?;
            for key in ["source_segments_key", "final_subtitle_segments_key"] {
                if let Some(v) = a.get(key).filter(|v| !v.is_null()) {
                    if v.as_str().is_none() || Some(v) != plan.get("base_translated_segments_key") {
                        return Err(invalid(
                            "selective artifacts must match the single native aligned snapshot",
                        ));
                    }
                }
            }
        }
    } else if let Some(a) = artifacts {
        if map.get("resume").and_then(Value::as_bool) != Some(true) {
            return Err(invalid("legacy artifacts require resume=true"));
        }
        allowed(
            a,
            &["source_segments_key", "final_subtitle_segments_key"],
            "artifacts",
        )?;
        for value in a.values() {
            if !value.is_null() && value.as_str().is_none_or(|s| s.is_empty()) {
                return Err(invalid("legacy artifact key must be a nonempty string"));
            }
        }
    }
    if let Some(after) = optional_object(map, "after_render")? {
        allowed(
            after,
            &["publish", "publish_payload", "payload", "runtime_profile"],
            "after_render",
        )?;
        for k in ["publish", "runtime_profile"] {
            if after.get(k).is_some_and(|v| !v.is_boolean()) {
                return Err(invalid("after_render flags must be boolean"));
            }
        }
        let a = after.get("publish_payload").filter(|v| !v.is_null());
        let b = after.get("payload").filter(|v| !v.is_null());
        if a.is_some() && b.is_some() && a != b {
            return Err(invalid("conflicting after_render payload aliases"));
        }
        let payload = a
            .or(b)
            .filter(|v| v.as_object().is_none_or(|o| !o.is_empty()));
        if after.get("publish").and_then(Value::as_bool) == Some(true) {
            if selective.is_some() {
                return Err(invalid(
                    "selective retranslation cannot automatically publish",
                ));
            }
            if let Some(payload) = payload {
                if after.get("runtime_profile").and_then(Value::as_bool) == Some(true) {
                    return Err(invalid(
                        "explicit after_render payload and runtime profile are mutually exclusive",
                    ));
                }
                crate::social_publish::validate_after_render(payload.clone())?;
            } else if after.get("runtime_profile").and_then(Value::as_bool) != Some(true) {
                return Err(invalid(
                    "after_render publish requires explicit payload or runtime_profile=true",
                ));
            }
        } else if payload.is_some() {
            return Err(invalid(
                "disabled after_render cannot carry a publication payload",
            ));
        }
    }
    if let Some(prefix) = map.get("output_prefix") {
        let prefix = prefix
            .as_str()
            .ok_or_else(|| invalid("output_prefix must be a string"))?;
        // Original worker uses content-addressed task directories, not this
        // request hint. Accept only the original canonical task prefix.
        if !prefix.is_empty() && prefix != format!("sub/{id}/") {
            return Err(invalid(
                "output_prefix must be empty or the original task-owned sub/<task_id>/ prefix",
            ));
        }
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
    if (!key.starts_with("v2/") && map.get("resume").and_then(Value::as_bool) != Some(true))
        || key.len() > 512
        || crate::storage::AssetRegistry::checked_key(key).is_err()
    {
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
    let mode = crate::media::youtube_subtitles::mode(
        map.get("youtube_subtitle_mode").and_then(Value::as_str),
        map.get("prefer_youtube_subtitles").and_then(Value::as_bool),
    )?;
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
    flat.insert("prefer_youtube_subtitles".into(), json!(mode != "off"));
    flat.insert("youtube_subtitle_mode".into(), json!(mode));
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
    let (task_id, key, mut config) = config(&request)?;
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
    if let Some(plan) = request
        .get("selective_retranslate")
        .and_then(Value::as_object)
        .filter(|v| !v.is_empty())
    {
        let indices: Vec<i64> = serde_json::from_value(plan["indices"].clone())
            .map_err(|_| invalid("selective indices must be integers"))?;
        let pinned = plan
            .get("base_translated_segments_key")
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_str()
                    .ok_or_else(|| invalid("base snapshot key must be string"))
            })
            .transpose()?;
        // Native selective retranslation must inherit its proven source
        // configuration; separate Python-only artifacts are rejected above.
        return crate::subtitle_retranslate_v1::schedule(&state, task_id, &indices, pinned)
            .await
            .map(Json);
    }
    let automatic=match request.get("runtime_profile").and_then(Value::as_bool){
        Some(value)=>value,
        None=>sqlx::query_scalar::<_,bool>("SELECT COALESCE(lower(trim(origin_kind)) IN ('auto_youtube','youtube_home_scan','youtube_task_restart'),false) OR COALESCE(lower(trim(split_part(created_by,';',1))) IN ('auto_youtube','youtube_home_scan','youtube_task_restart'),false) FROM tasks WHERE id=$1").bind(task_id).fetch_one(&state.db.pool).await?,
    };
    config["standalone_subtitle_runtime_profile"] = json!(automatic);
    if automatic {
        config["subtitle"] = json!({});
        config["render"] = json!({});
        if request["resume"].as_bool() == Some(true) {
            config["subtitle"]["resume"] = json!(true);
        }
        config.as_object_mut().unwrap().remove("auto_publish");
        config
            .as_object_mut()
            .unwrap()
            .remove("auto_publish_platforms");
    } else {
        config["auto_dub_p2"] = json!(false);
    }
    if request["after_render"]["publish"].as_bool() == Some(true) {
        if let Some(payload) = request["after_render"]
            .get("publish_payload")
            .filter(|v| !v.is_null())
            .or_else(|| {
                request["after_render"]
                    .get("payload")
                    .filter(|v| !v.is_null())
            })
            .filter(|v| v.as_object().is_some_and(|o| !o.is_empty()))
        {
            if !crate::social_publish::mixed_enabled() {
                return Err(AppError::Conflict(
                    "explicit after_render requires native-only publication batch mode".into(),
                ));
            }
            if automatic {
                return Err(invalid(
                    "explicit after_render payload requires runtime_profile=false",
                ));
            }
            config["after_render_input"] = json!(
                crate::subtitle_resume_v1::asset_digest(
                    &state.db,
                    &state.storage,
                    task_id,
                    "video_raw",
                    &key
                )
                .await?
            );
            config["after_render_publish"] =
                crate::social_publish::validate_after_render(payload.clone())?;
            config["auto_publish"] = json!(true);
            config["auto_publish_platforms"] = config["after_render_publish"]["platforms"].clone();
            config["auto_dub_p2"] = json!(false);
        } else if !automatic {
            return Err(invalid(
                "after_render runtime profile requires automatic subtitle execution",
            ));
        }
    }
    let resume = request["resume"].as_bool() == Some(true);
    if resume {
        let plan = crate::subtitle_resume_v1::prepare(
            &state.db,
            &state.storage,
            task_id,
            &key,
            request["artifacts"]["source_segments_key"].as_str(),
            request["artifacts"]["final_subtitle_segments_key"].as_str(),
        )
        .await?;
        if let Some(plan) = plan {
            config["subtitle_resume_plan"] = json!(plan);
        }
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
    if latest_srt && !active_native && config.get("subtitle_resume_plan").is_none() {
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
            ("youtube_subtitle_mode", json!("invalid")),
            ("selective_retranslate", json!({"indices":"invalid"})),
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
