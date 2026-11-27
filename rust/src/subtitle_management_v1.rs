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
pub(crate) fn normalized_profile(raw: &Value) -> Value {
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
    // Older profiles may only contain the compatibility boolean.
    let mode = raw["youtube_subtitle_mode"].as_str()
        .unwrap_or(if raw["prefer_youtube_subtitles"].as_bool()==Some(false) {"off"} else {"target"});
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
    let codec=out["video_codec"].as_str().unwrap_or("av1");
    let preset=normalized_preset(codec,&out["video_preset"]);
    out.insert("video_preset".into(),preset);
    // Match the original profile view: stable ordering, distinct known platforms.
    let mut platforms=Vec::new();
    if let Some(items)=out["auto_publish_platforms"].as_array(){
        for item in items{
            if let Some(name)=item.as_str(){
                let name=name.trim().to_ascii_lowercase();
                if matches!(name.as_str(),"bilibili"|"douyin"|"xiaohongshu"|"kuaishou") && !platforms.contains(&name){
                    platforms.push(name);
                }
            }
        }
    }
    out.insert("auto_publish_platforms".into(),json!(platforms));
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
fn normalized_preset(codec:&str,value:&Value)->Value{
    let Some(preset)=value.as_str().map(str::trim).filter(|s|!s.is_empty())else{return Value::Null};
    if codec=="h264"{
        let name=preset.to_ascii_lowercase();
        if matches!(name.as_str(),"ultrafast"|"superfast"|"veryfast"|"faster"|"fast"|"medium"|"slow"|"slower"|"veryslow"|"placebo"){
            return json!(name)
        }
    }else if let Ok(n)=preset.parse::<i32>(){
        return json!(n.clamp(0,13).to_string())
    }
    Value::Null
}
fn supported_update(key: &str) -> bool {
    matches!(
        key,
        "translate_enabled"
            | "target_lang"
            | "bilingual"
            | "asr_language"
            | "asr_engine"
            | "asr_model"
            | "prefer_youtube_subtitles"
            | "youtube_subtitle_mode"
            | "translate_enable_summary"
            | "translate_style"
            | "translate_provider"
            | "translate_model_provider_id"
            | "translate_model_id"
            | "publish_translate_title"
            | "publish_enable_reprint"
            | "publish_title_prefix"
            | "auto_dub_p2"
            | "auto_dub_p2_prefix"
            | "auto_publish"
            | "auto_publish_platforms"
            | "burn_in"
            | "video_codec"
            | "use_intel_gpu"
            | "video_preset"
            | "video_crf"
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
            "translate_enabled"
            | "bilingual"
            | "publish_translate_title"
            | "publish_enable_reprint"
            | "auto_dub_p2"
            | "prefer_youtube_subtitles"
            | "translate_enable_summary"
            | "auto_publish"
            | "burn_in"
            | "use_intel_gpu"
                if !value.is_boolean() =>
            {
                return Err(AppError::Invalid(format!("{key} requires bool")));
            }
            "publish_title_prefix" | "auto_dub_p2_prefix"
                if value
                    .as_str()
                    .is_none_or(|s| s.chars().count() > 40 || s.chars().any(char::is_control)) =>
            {
                return Err(AppError::Invalid("invalid publish title prefix".into()));
            }
            "auto_publish_platforms"=>{
                crate::task_manual::auto_platform_selection(&json!({}),&json!({"auto_publish_platforms":value}))?;
            }
            "video_codec" if !value.as_str().is_some_and(|s|matches!(s,"h264"|"av1"))=>{
                return Err(AppError::Invalid("unsupported automatic render codec".into()));
            }
            "video_crf" if value.as_i64().is_none_or(|n|!(0..=63).contains(&n))=>{
                return Err(AppError::Invalid("video_crf must be an integer between 0 and 63".into()));
            }
            "video_preset" if value.as_str().is_none_or(|s|s.len()>32 || s.chars().any(char::is_control))=>{
                return Err(AppError::Invalid("invalid video preset".into()));
            }
            "asr_engine" if !value.as_str().is_some_and(|s|matches!(s,
                "auto"|"mock"|"faster-whisper"|"openvino"|"external-whisper"|"groq-whisper"|"cloudflare-workers-ai"))=>{
                return Err(AppError::Invalid("unsupported ASR engine".into()));
            }
            "asr_model" if value.as_str().is_none_or(|s|s.len()>1024 || s.chars().any(char::is_control))=>{
                return Err(AppError::Invalid("invalid ASR model".into()));
            }
            "youtube_subtitle_mode" if !value.as_str().is_some_and(|s|matches!(s,"off"|"target"|"auto_source"))=>{
                return Err(AppError::Invalid("invalid YouTube subtitle mode".into()));
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
    // Serialize the first write too; a missing row cannot be locked by
    // SELECT FOR UPDATE. Share the original setting with other API writers.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('app.settings:subtitle.auto_profile',0))")
        .execute(&mut *tx).await?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES('subtitle.auto_profile','{}'::jsonb,0) ON CONFLICT(key) DO NOTHING")
        .execute(&mut *tx).await?;
    let raw: Value = sqlx::query_scalar(
        "SELECT value_json FROM app_settings WHERE key='subtitle.auto_profile' FOR UPDATE",
    )
    .fetch_optional(&mut *tx)
    .await?
    .unwrap_or(json!({}));
    let mut merged = raw.as_object().cloned().unwrap_or_default();
    for (key, val) in update {
        // The original nullable preset/quality fields explicitly support
        // clearing an override. Other nulls retain the saved value.
        if !val.is_null() || matches!(key.as_str(),"video_preset"|"video_crf") {
            merged.insert(key.clone(), val.clone());
        }
    }
    if update.get("auto_publish_platforms").is_some_and(|v|!v.is_null()){
        let (_,platforms)=crate::task_manual::auto_platform_selection(&json!({}),&Value::Object(merged.clone()))?;
        merged.insert("auto_publish_platforms".into(),json!(platforms));
    }
    if update.get("video_preset").is_some() || update.get("video_codec").is_some_and(|v|!v.is_null()){
        let codec=merged.get("video_codec").and_then(Value::as_str).unwrap_or("av1");
        let raw=merged.get("video_preset").cloned().unwrap_or(Value::Null);
        let preset=normalized_preset(codec,&raw);
        if let Some(input)=update.get("video_preset").and_then(Value::as_str).filter(|s|!s.trim().is_empty()){
            let valid=if codec=="h264"{!preset.is_null()}else{input.trim().parse::<i32>().is_ok_and(|n|(0..=13).contains(&n))};
            if !valid{return Err(AppError::Invalid("preset is incompatible with the selected codec (H.264 names or AV1 0..13)".into()))}
        }
        // Changing codecs without a new compatible preset clears the old
        // codec-specific override, rather than pretending it still applies.
        merged.insert("video_preset".into(),preset);
    }
    if let Some(model)=update.get("asr_model").and_then(Value::as_str){
        let model=model.trim();
        merged.insert("asr_model".into(),if model.is_empty(){Value::Null}else{json!(model)});
    }
    // One authoritative mode. A legacy boolean-only update must not be
    // masked by a previously saved mode; explicit mode wins if both arrive.
    if let Some(mode)=update.get("youtube_subtitle_mode").and_then(Value::as_str){
        merged.insert("prefer_youtube_subtitles".into(),json!(mode!="off"));
    }else if let Some(prefer)=update.get("prefer_youtube_subtitles").and_then(Value::as_bool){
        let mode=if !prefer{"off"}else if merged.get("youtube_subtitle_mode").and_then(Value::as_str)==Some("auto_source"){"auto_source"}else{"target"};
        merged.insert("youtube_subtitle_mode".into(),json!(mode));
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
    // Compare the latest original/native subtitle jobs by creation time.
    // Native reports live in the existing operation receipt, not a new table.
    let native=sqlx::query("SELECT j.id,j.state,j.result FROM vr2_jobs j JOIN tasks t ON t.id=j.task_id WHERE j.task_id=$1 AND j.stage='subtitle' AND t.runtime_owner='rust' AND NOT EXISTS(SELECT 1 FROM subtitle_jobs s WHERE s.task_id=j.task_id AND s.created_at>j.created_at) ORDER BY j.created_at DESC,j.id DESC LIMIT 1").bind(task_id).fetch_optional(&state.db.pool).await?;
    if let Some(row) = native {
        let id: Uuid = row.try_get("id")?;
        let status: String = row.try_get("state")?;
        let result: Option<Value> = row.try_get("result")?;
        let report = result
            .as_ref()
            .and_then(|v| v.get("subtitle_quality"))
            .filter(|v| status == "succeeded" && v.is_object());
        return Ok(Json(
            json!({"available":report.is_some(),"job_id":id,"report":report}),
        ));
    }
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
    #[tokio::test]
    async fn legacy_auto_runtime_profile_real_postgres(){
        use crate::{Database,Config,domain::{Stage,WorkItem},media::{asr,dubbing,ffmpeg}};
        let Ok(url)=std::env::var("VIDEOROLL_TEST_AUTO_RUNTIME_PROFILE_DB_URL")else{return};
        let parsed=reqwest::Url::parse(&url).unwrap();
        assert_eq!(parsed.host_str(),Some("127.0.0.1"));
        assert_eq!(parsed.path(),"/videoroll_test");
        let db=Database::connect(&url).await.unwrap();
        let (task,flow,id):(Uuid,Uuid,Uuid)=sqlx::query_as("SELECT t.id,w.id,j.id FROM tasks t JOIN vr2_workflows w ON w.task_id=t.id JOIN vr2_jobs j ON j.workflow_id=w.id WHERE t.source_url='https://www.youtube.com/watch?v=AutoCfg0001' AND j.stage='download'")
            .fetch_one(&db.pool).await.unwrap();
        let item=WorkItem{id,workflow_id:flow,task_id:task,stage:Stage::Publish,generation:1,
            operation_key:format!("fixture:{id}"),lease_token:Uuid::new_v4(),attempts:1,max_attempts:3,payload:json!({})};
        let mut config=Config::from_env().unwrap();
        config.asr_endpoint=Some("http://127.0.0.1:18089/mock-asr".into());
        sqlx::query("DELETE FROM app_settings WHERE key='subtitle.asr'").execute(&db.pool).await.unwrap();
        async fn saved(db:&Database,p:Value){
            sqlx::query("INSERT INTO app_settings(key,value_json) VALUES('subtitle.auto_profile',$1) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json")
                .bind(p).execute(&db.pool).await.unwrap();
        }
        let original:Value=sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1").bind(flow).fetch_one(&db.pool).await.unwrap();
        // The Python HTTP fixture saved these fields via PUT, after the
        // workflow had already been created. Verify the actual consumers.
        let choice=crate::publish_legacy::read_selection(&db,&item).await.unwrap();
        assert!(choice.enabled && choice.platforms==["bilibili","kuaishou"]);
        assert!(!dubbing::auto_enabled(&db,&item).await.unwrap());
        let p=ffmpeg::profile(&db,&item).await.unwrap();
        assert!(p.burn_in && p.use_intel_gpu && p.video_codec=="av1");
        assert_eq!(p.video_preset.as_deref(),Some("5"));
        assert_eq!(p.video_crf,Some(58));
        saved(&db,json!({"auto_publish":false,"auto_publish_platforms":[],"auto_dub_p2":true,"asr_engine":"auto","asr_model":"fixture-one","video_codec":"h264","burn_in":false})).await;
        let choice=crate::publish_legacy::read_selection(&db,&item).await.unwrap();
        assert!(!choice.enabled && choice.platforms.is_empty());
        assert!(dubbing::auto_enabled(&db,&item).await.unwrap());
        assert_eq!(asr::runtime_asr_config(&config,&db,&item).await.unwrap().unwrap().asr_model,"fixture-one");
        let p=ffmpeg::profile(&db,&item).await.unwrap();assert_eq!(p.video_codec,"h264");assert!(!p.burn_in);
        saved(&db,json!({"auto_publish":true,"auto_publish_platforms":["douyin","kuaishou"],"auto_dub_p2":true,"asr_engine":"auto","asr_model":"fixture-two","video_codec":"av1","burn_in":true})).await;
        let choice=crate::publish_legacy::read_selection(&db,&item).await.unwrap();
        assert!(choice.enabled && choice.platforms==["douyin","kuaishou"]);
        assert!(dubbing::auto_enabled(&db,&item).await.unwrap());
        assert_eq!(asr::runtime_asr_config(&config,&db,&item).await.unwrap().unwrap().asr_model,"fixture-two");
        assert_eq!(ffmpeg::profile(&db,&item).await.unwrap().video_codec,"av1");
        saved(&db,json!({"auto_publish":true,"auto_publish_platforms":["bilibili"],"auto_dub_p2":false})).await;
        assert!(!dubbing::auto_enabled(&db,&item).await.unwrap());
        let current:Value=sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1").bind(flow).fetch_one(&db.pool).await.unwrap();
        assert_eq!(current,original);
        saved(&db,json!({"auto_publish":true,"auto_publish_platforms":["bilibili"],"auto_dub_p2":true})).await;
        sqlx::query("UPDATE vr2_workflows SET configuration=$2 WHERE id=$1").bind(flow).bind(json!({"legacy_auto_runtime_profile":true,
            "auto_publish":false,"auto_publish_platforms":[],"auto_dub_p2":false,
            "subtitle":{"asr_engine":"auto","asr_model":"explicit-model"},"render":{"video_codec":"h264"}})).execute(&db.pool).await.unwrap();
        assert!(!crate::publish_legacy::read_selection(&db,&item).await.unwrap().enabled);
        assert!(!dubbing::auto_enabled(&db,&item).await.unwrap());
        assert_eq!(asr::runtime_asr_config(&config,&db,&item).await.unwrap().unwrap().asr_model,"explicit-model");
        assert_eq!(ffmpeg::profile(&db,&item).await.unwrap().video_codec,"h264");
        sqlx::query("UPDATE vr2_workflows SET configuration=$2 WHERE id=$1").bind(flow).bind(original).execute(&db.pool).await.unwrap();
    }
    #[test]
    fn defaults_and_native_scope() {
        let view = normalized_profile(&json!({}));
        assert_eq!(view["translate_provider"], "openai");
        assert_eq!(view["auto_publish_platforms"], json!([]));
        assert!(validate_update(json!({"auto_publish":true,"auto_publish_platforms":["bilibili","douyin"],"burn_in":false,"video_codec":"av1","video_preset":"4","video_crf":60,"use_intel_gpu":true}).as_object().unwrap()).is_ok());
        assert!(validate_update(json!({"auto_publish_platforms":["unknown"]}).as_object().unwrap()).is_err());
        assert!(validate_update(json!({"video_crf":64}).as_object().unwrap()).is_err());
        assert!(validate_update(json!({"video_codec":"rawvideo"}).as_object().unwrap()).is_err());
        assert_eq!(normalized_preset("h264",&json!(" FAST ")),json!("fast"));
        assert_eq!(normalized_preset("av1",&json!("4")),json!("4"));
        assert!(validate_update(json!({"translate_enabled":false}).as_object().unwrap()).is_ok());
        assert!(validate_update(json!({"auto_dub_p2":true,"auto_dub_p2_prefix":"【中配】"}).as_object().unwrap()).is_ok());
        assert!(validate_update(json!({"auto_dub_p2":"true"}).as_object().unwrap()).is_err());
        assert!(validate_update(json!({"asr_engine":"external-whisper","asr_model":"whisper-1","translate_enable_summary":false,"youtube_subtitle_mode":"auto_source"}).as_object().unwrap()).is_ok());
        assert!(validate_update(json!({"asr_engine":"not-an-engine"}).as_object().unwrap()).is_err());
        assert!(validate_update(json!({"youtube_subtitle_mode":"invalid"}).as_object().unwrap()).is_err());
        assert_eq!(normalized_profile(&json!({"prefer_youtube_subtitles":false}))["youtube_subtitle_mode"],"off");
    }
}
