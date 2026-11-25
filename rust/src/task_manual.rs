//! Stage-specific V1 TaskDetail actions implemented on durable V2 workflows.
//! No V1 Python forwarding, detached task, or "accepted" response without a
//! real queued vr2_job. Unsupported legacy options fail explicitly.
use crate::{
    AppError, AppResult,
    api::AppState,
    domain::{Stage, WorkflowInfo},
    legacy_contracts::read_task,
};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::post,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
pub(crate) struct SubtitleAction {
    #[serde(default)]
    resume: bool,
    #[serde(default)]
    burn_in: Option<bool>,
    #[serde(default)]
    soft_sub: Option<bool>,
    #[serde(default)]
    ass_style: Option<String>,
    #[serde(default)]
    video_codec: Option<String>,
    #[serde(default)]
    use_intel_gpu: Option<bool>,
    #[serde(default)]
    video_crf: Option<u8>,
    #[serde(default)]
    video_preset: Option<String>,
    #[serde(default)]
    asr_engine: Option<String>,
    #[serde(default)]
    asr_language: Option<String>,
    #[serde(default)]
    asr_model: Option<String>,
    #[serde(default)]
    translate_enabled: Option<bool>,
    #[serde(default)]
    translate_provider: Option<String>,
    #[serde(default)]
    translate_model_provider_id: Option<String>,
    #[serde(default)]
    translate_model_id: Option<String>,
    #[serde(default)]
    target_lang: Option<String>,
    #[serde(default)]
    translate_style: Option<String>,
    #[serde(default)]
    bilingual: Option<bool>,
    #[serde(default)]
    formats: Option<Vec<String>>,
    #[serde(default)]
    prefer_youtube_subtitles: Option<bool>,
    #[serde(default)]
    youtube_subtitle_mode: Option<String>,
    #[serde(default)]
    translate_batch_size: Option<i32>,
    #[serde(default)]
    translate_enable_summary: Option<bool>,
    #[serde(default)]
    translate_glossary: Option<Value>,
    #[serde(default)]
    auto_publish: Option<bool>,
    #[serde(default)]
    publish_payload: Option<Value>,
}
pub(crate) fn subtitle_config(request: SubtitleAction) -> AppResult<Value> {
    if let Some(g) = request.translate_glossary.as_ref() {
        crate::media::translation_quality::validate_glossary(g)?;
    }
    if request
        .translate_batch_size
        .is_some_and(|n| n < 1 || n > 50)
    {
        return Err(AppError::Invalid(
            "translation batch size must be 1..50".into(),
        ));
    }
    // Local inference may be explicitly selected. Other engine-specific
    // selectors stay rejected until their request semantics are migrated.
    if request.asr_engine.as_deref().is_some_and(|s| {
        !matches!(
            s,
            "auto" | "openvino" | "faster-whisper" | "cloudflare-workers-ai"
        )
    }) || (request.asr_model.is_some()
        && !matches!(
            request.asr_engine.as_deref(),
            Some("openvino" | "faster-whisper" | "cloudflare-workers-ai")
        ))
        || request.auto_publish == Some(true)
        || request.publish_payload.is_some()
    {
        return Err(AppError::Invalid(
            "selected legacy subtitle source, engine, summary or automatic publish option has no native V2 equivalent".into()
        ));
    }
    if request
        .formats
        .as_ref()
        .is_some_and(|f| f.iter().any(|v| !matches!(v.as_str(), "srt" | "ass")))
    {
        return Err(AppError::Invalid(
            "only srt and ass subtitle formats supported by V2".into(),
        ));
    }
    let mut subtitle = json!({});
    if request.prefer_youtube_subtitles.is_some() || request.youtube_subtitle_mode.is_some() {
        let mode = crate::media::youtube_subtitles::mode(
            request.youtube_subtitle_mode.as_deref(),
            request.prefer_youtube_subtitles,
        )?;
        subtitle["prefer_youtube_subtitles"] = json!(mode != "off");
        subtitle["youtube_subtitle_mode"] = json!(mode);
    }

    let mut render = json!({});
    macro_rules! put {
        ($target:ident,$name:literal,$val:expr) => {
            if let Some(v) = $val {
                $target[$name] = json!(v);
            }
        };
    }
    put!(subtitle, "asr_engine", request.asr_engine);
    put!(
        subtitle,
        "translate_batch_size",
        request.translate_batch_size
    );
    put!(
        subtitle,
        "translate_enable_summary",
        request.translate_enable_summary
    );
    put!(subtitle, "translate_glossary", request.translate_glossary);
    put!(subtitle, "asr_model", request.asr_model);
    put!(subtitle, "asr_language", request.asr_language);
    put!(subtitle, "translate_enabled", request.translate_enabled);
    put!(subtitle, "translate_provider", request.translate_provider);
    put!(
        subtitle,
        "translate_model_provider_id",
        request.translate_model_provider_id
    );
    put!(subtitle, "translate_model_id", request.translate_model_id);
    put!(subtitle, "target_lang", request.target_lang);
    put!(subtitle, "translate_style", request.translate_style);
    put!(subtitle, "bilingual", request.bilingual);
    put!(render, "burn_in", request.burn_in);
    put!(render, "soft_sub", request.soft_sub);
    put!(render, "ass_style", request.ass_style);
    put!(render, "video_codec", request.video_codec);
    put!(render, "use_intel_gpu", request.use_intel_gpu);
    put!(render, "video_crf", request.video_crf);
    put!(render, "video_preset", request.video_preset);
    if let Some(codec) = render["video_codec"].as_str()
        && !matches!(codec, "h264" | "av1")
    {
        return Err(AppError::Invalid(
            "V2 supports only h264 or av1 render codec".into(),
        ));
    }
    // A native subtitle retry uses the persisted ASR checkpoint. The resumed
    // generation is independent of the prior lease and never replays publish.
    let mut result = json!({"subtitle":subtitle,"render":render,"auto_publish":false,
        "auto_publish_platforms":[]});
    if request.resume {
        result["subtitle"]["resume"] = json!(true);
    }
    Ok(result)
}
#[derive(Debug, Serialize)]
struct ManualJobResponse {
    job_id: Uuid,
    status: String,
}
async fn response(
    state: &AppState,
    flow: WorkflowInfo,
    stage: Stage,
) -> AppResult<Json<ManualJobResponse>> {
    let job = sqlx::query(
        "SELECT id,state FROM vr2_jobs WHERE workflow_id=$1 AND stage=$2
         ORDER BY created_at DESC LIMIT 1",
    )
    .bind(flow.id)
    .bind(stage.as_str())
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::Unavailable("new V2 workflow has no stage job".into()))?;
    Ok(Json(ManualJobResponse {
        job_id: job.try_get("id")?,
        status: job.try_get("state")?,
    }))
}
async fn manual_subtitle(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(req): Json<SubtitleAction>,
) -> AppResult<Json<ManualJobResponse>> {
    read_task(&state.db, id).await?;
    let overrides = subtitle_config(req)?;
    let flow = state
        .scheduler
        .restart_at(id, Stage::Subtitle, overrides)
        .await?;
    response(&state, flow, Stage::Subtitle).await
}
async fn resume_subtitle(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<ManualJobResponse>> {
    read_task(&state.db, id).await?;
    let raw = crate::media::ffmpeg::latest_asset_key(&state.db, id, "video_raw").await?;
    let mut changes = json!({"auto_publish":false,"auto_publish_platforms":[]});
    if let Some(raw) = raw {
        if let Some(plan) =
            crate::subtitle_resume_v1::prepare(&state.db, &state.storage, id, &raw, None, None)
                .await?
        {
            changes["subtitle_resume_plan"] = json!(plan);
            changes["auto_dub_p2"] = json!(false);
            changes["standalone_subtitle_runtime_profile"] = json!(false);
        }
    }

    let flow = state
        .scheduler
        .restart_at(id, Stage::Subtitle, changes)
        .await?;
    response(&state, flow, Stage::Subtitle).await
}
#[derive(Debug, Deserialize, Serialize)]
struct PublishPayload {
    #[serde(default = "default_platform")]
    platform: String,
    #[serde(default)]
    meta: Option<Value>,
    account_id: Option<String>,
    video_key: Option<String>,
    video_parts: Option<Vec<Value>>,
    cover_key: Option<String>,
    typeid_mode: Option<String>,
    #[serde(default)]
    platform_options: Value,
    #[serde(default)]
    skip_review: bool,
    #[serde(default)]
    force_retry: bool,
}
fn default_platform() -> String {
    "bilibili".into()
}
async fn manual_publish(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(payload): Json<PublishPayload>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, id).await?;
    if payload.platform != "bilibili" {
        return crate::social_publish::manual(
            &state,
            id,
            serde_json::to_value(payload)
                .map_err(|_| AppError::Invalid("invalid social publish request".into()))?,
        )
        .await
        .map(Json);
    }
    if payload.skip_review || payload.force_retry {
        return Err(AppError::Invalid(
            "review bypass and forced Bilibili retry are not supported; reconcile first".into(),
        ));
    }
    // Original platform_options are scoped by platform. Only the partition
    // selection override is supported by the native Bilibili publisher.
    let options = match &payload.platform_options {
        Value::Null => None,
        Value::Object(map) if map.is_empty() => None,
        Value::Object(map) if map.len() == 1 && map.contains_key("bilibili") => {
            let opts = map["bilibili"].as_object().ok_or_else(|| {
                AppError::Invalid("Bilibili platform_options must be an object".into())
            })?;
            if opts.keys().any(|k| k != "typeid_mode") {
                return Err(AppError::Invalid(
                    "unsupported Bilibili platform option".into(),
                ));
            }
            opts.get("typeid_mode")
                .map(|v| {
                    v.as_str()
                        .ok_or_else(|| AppError::Invalid("typeid_mode must be a string".into()))
                })
                .transpose()?
        }
        _ => {
            return Err(AppError::Invalid(
                "invalid Bilibili platform_options".into(),
            ));
        }
    };
    let typeid_mode = options.or(payload.typeid_mode.as_deref()).unwrap_or("meta");

    if !crate::publish_platforms::enabled(&state.db, "bilibili").await? {
        return Err(AppError::Conflict("Bilibili platform is disabled".into()));
    }
    if !state.config.native_bilibili_allowed {
        return Err(AppError::Unavailable(
            "manual native Bilibili publishing is not enabled on this isolated V2 host".into(),
        ));
    }
    let saved = crate::publish_meta::saved(&state.db, id).await?;
    let meta = if let Some(provided) = payload.meta {
        let provided = crate::publish_meta::normalized(provided)?;
        if let Some(ref existing) = saved
            && existing != &provided
        {
            return Err(AppError::Conflict(
                "metadata differs from the saved revision; save the draft first".into(),
            ));
        }
        Some(provided)
    } else {
        None
    };
    let resolved = saved
        .or(meta)
        .ok_or_else(|| AppError::Invalid("save Bilibili metadata before publishing".into()))?;
    let video_key = if let Some(key) = payload.video_key.as_deref() {
        key.to_owned()
    } else {
        sqlx::query_scalar::<_, String>("SELECT storage_key FROM assets WHERE task_id=$1 AND kind='video_final'::asset_kind ORDER BY created_at DESC,id DESC LIMIT 1")
            .bind(id).fetch_optional(&state.db.pool).await?
            .ok_or_else(|| AppError::Invalid("no final video asset found; render first".into()))?
    };
    let cover = payload
        .cover_key
        .as_ref()
        .map(|key| json!({"type":"storage","key":key}));
    let request = json!({"task_id":id,"account_id":payload.account_id,"video":{"type":"storage","key":video_key},
        "video_parts":payload.video_parts.unwrap_or_default(),"cover":cover,"meta":resolved,"typeid_mode":typeid_mode});
    let mut result = crate::bilibili_publish_v1::enqueue_task_action(&state, request).await?;
    result["platform"] = json!("bilibili");
    Ok(Json(result))
}
/// Resume the V2-owned auto pipeline at the earliest remaining reversible
/// stage. Reuse active stage jobs; never queue a second download or rerun an
/// archive that might already have been submitted.
async fn auto_subtitle_handoff(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let task = read_task(&state.db, id).await?;
    if task.status == "PUBLISHED" {
        return Ok(Json(
            json!({"status":"complete","job_id":null,"job_kind":null,
            "detail":"task already published"}),
        ));
    }
    if task.status == "CANCELED" {
        return Err(AppError::Conflict(
            "task is stopped; resume it before continuing".into(),
        ));
    }
    let has_final: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets
         WHERE task_id=$1 AND kind='video_final'::asset_kind AND storage_key LIKE 'v2/%')",
    )
    .bind(id)
    .fetch_one(&state.db.pool)
    .await?;
    if has_final {
        return Ok(Json(
            json!({"status":"complete","job_id":null,"job_kind":null,
            "detail":"final video already exists"}),
        ));
    }
    let active = sqlx::query(
        "SELECT j.id,j.stage,j.state FROM vr2_jobs j
         JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE j.task_id=$1 AND j.stage IN ('subtitle','render')
           AND j.state IN ('pending','running','awaiting')
           AND w.state='running'
         ORDER BY w.generation DESC,j.created_at DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    if let Some(row) = active {
        let stage: String = row.try_get("stage")?;
        let job_state: String = row.try_get("state")?;
        return Ok(Json(json!({
            "status":if job_state=="pending"{"queued"}else{"running"},
            "job_id":row.try_get::<Uuid,_>("id")?,
            "job_kind":stage,"detail":"reused active V2 stage job"
        })));
    }
    let has_srt: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets
         WHERE task_id=$1 AND kind='subtitle_srt'::asset_kind AND storage_key LIKE 'v2/%')",
    )
    .bind(id)
    .fetch_one(&state.db.pool)
    .await?;
    let stage = if has_srt {
        Stage::Render
    } else {
        Stage::Subtitle
    };
    let flow = state.scheduler.restart_at(id, stage, json!({})).await?;
    let result = response(&state, flow, stage).await?.0;
    Ok(Json(json!({
        "status":if result.status=="pending"{"queued"}else{result.status.as_str()},
        "job_id":result.job_id,"job_kind":stage.as_str(),
        "detail":"native V2 stage handoff scheduled"
    })))
}

pub(crate) fn auto_platform_selection(
    profile: &Value,
    overrides: &Value,
) -> AppResult<(bool, Vec<String>)> {
    let enabled = overrides
        .get("auto_publish")
        .and_then(Value::as_bool)
        .or_else(|| profile.get("auto_publish").and_then(Value::as_bool))
        .unwrap_or(true);
    let raw = overrides
        .get("auto_publish_platforms")
        .or_else(|| profile.get("auto_publish_platforms"))
        .cloned()
        .unwrap_or_else(|| json!([]));
    let items = raw
        .as_array()
        .ok_or_else(|| AppError::Invalid("auto_publish_platforms must be an array".into()))?;
    if items.len() > 4 {
        return Err(AppError::Invalid(
            "too many auto publish platform targets".into(),
        ));
    }
    let mut names = Vec::new();
    for item in items {
        let name = item
            .as_str()
            .ok_or_else(|| AppError::Invalid("invalid auto publish platform".into()))?;
        if !matches!(name, "bilibili" | "douyin" | "xiaohongshu" | "kuaishou") {
            return Err(AppError::Invalid("unknown auto publish platform".into()));
        }
        if !names.iter().any(|existing| existing == name) {
            names.push(name.to_owned());
        }
    }
    Ok((enabled, names))
}

/// Schedule the original automatic publish action. Native batch mode handles
/// social targets; without it, fail closed rather than dropping platforms.
async fn auto_publish(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Query(query): Query<AutoPublishQuery>,
) -> AppResult<Json<Value>> {
    let task = read_task(&state.db, id).await?;
    if task.status == "PUBLISHED" {
        return Ok(Json(
            json!({"status":"complete","detail":"task already published"}),
        ));
    }
    if matches!(task.status.as_str(), "CANCELED" | "PUBLISHING") {
        return Err(AppError::Conflict(
            "task already stopped or publishing; no automatic replay".into(),
        ));
    }
    let profile = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
    let latest_configuration: Option<Value> = sqlx::query_scalar(
        "SELECT configuration FROM vr2_workflows WHERE task_id=$1 ORDER BY generation DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    let (enabled, platforms) =
        auto_platform_selection(&profile, &latest_configuration.unwrap_or_else(|| json!({})))?;
    if !enabled {
        return Ok(Json(
            json!({"status":"skipped","detail":"automatic publishing disabled"}),
        ));
    }
    if platforms.is_empty() {
        return Ok(Json(
            json!({"status":"skipped","detail":"no automatic publishing platforms selected"}),
        ));
    }
    if (platforms.len() != 1 || platforms[0] != "bilibili")
        && !crate::social_publish::mixed_enabled()
    {
        return Err(AppError::Conflict(
            "automatic publish cannot silently drop unfinished Douyin/Xiaohongshu/Kuaishou targets"
                .into(),
        ));
    }
    if platforms.iter().any(|p| p == "bilibili")
        && (!state.config.native_bilibili_allowed
            || !crate::publish_platforms::enabled(&state.db, "bilibili").await?)
    {
        return Err(AppError::Conflict(
            "native Bilibili publishing disabled".into(),
        ));
    }
    if let Some(dubbing_job_id) = query.dubbing_job_id {
        if !platforms.iter().any(|p| p == "bilibili") {
            return Err(AppError::Invalid("P2 requires a Bilibili target".into()));
        }
        let matching: bool = sqlx::query_scalar(
            "SELECT EXISTS(
                SELECT 1 FROM vr2_dubbing_runs r
                JOIN vr2_jobs j ON j.id=r.job_id
                JOIN assets a ON a.task_id=r.task_id
                    AND a.storage_key=r.output_video_key
                    AND a.kind='video_dubbed'::asset_kind
                WHERE r.job_id=$1 AND r.task_id=$2
                  AND r.model_status='succeeded'
                  AND j.state='succeeded'
                  AND r.output_video_key=(
                    SELECT a2.storage_key FROM assets a2 WHERE a2.task_id=$2
                    AND a2.kind='video_dubbed'::asset_kind
                    ORDER BY a2.created_at DESC LIMIT 1)
            )",
        )
        .bind(dubbing_job_id)
        .bind(id)
        .fetch_one(&state.db.pool)
        .await?;
        if !matching {
            return Err(AppError::Conflict(
                "automatic P2 requires the selected successful V2 YouDub job and latest dubbed video".into(),
            ));
        }
    }
    let flow = state
        .scheduler
        .restart_at(
            id,
            Stage::Publish,
            json!({"auto_publish":true,"auto_publish_platforms":platforms,"auto_publish_dubbing_job_id":query.dubbing_job_id}),
        )
        .await?;
    let job = response(&state, flow, Stage::Publish).await?.0;
    Ok(Json(json!({"status":job.status,"job_id":job.job_id,
        "detail":"automatic native publish stage scheduled"})))
}
#[derive(Debug, Deserialize)]
struct AutoPublishQuery {
    dubbing_job_id: Option<Uuid>,
}
#[derive(Debug, Deserialize)]
struct BulkQuery {
    window_hours: Option<i64>,
    limit: Option<i64>,
}
async fn resume_recent(
    State(state): State<AppState>,
    Query(query): Query<BulkQuery>,
) -> AppResult<Json<Value>> {
    let hours = query.window_hours.unwrap_or(24);
    let limit = query.limit.unwrap_or(200);
    if !(1..=168).contains(&hours) || !(1..=500).contains(&limit) {
        return Err(AppError::Invalid(
            "window_hours must be 1..168 and limit 1..500".into(),
        ));
    }
    let rows = sqlx::query(
        "SELECT t.id,j.stage,j.state FROM tasks t
         JOIN tasks m ON m.id=t.id AND m.runtime_owner='rust'
         JOIN LATERAL (
           SELECT stage,state FROM vr2_jobs
           WHERE task_id=t.id
           ORDER BY created_at DESC,id DESC LIMIT 1
         ) j ON true
         WHERE t.status='failed'::task_status
           AND t.updated_at>=now()-($1 * interval '1 hour')
         ORDER BY t.updated_at DESC LIMIT $2",
    )
    .bind(hours)
    .bind(limit)
    .fetch_all(&state.db.pool)
    .await?;
    let mut results = Vec::new();
    let mut resumed = 0;
    let mut skipped = 0;
    let mut failed = 0;
    for row in &rows {
        let id: Uuid = row.try_get("id")?;
        let stage_name: String = row.try_get("stage")?;
        let state_name: String = row.try_get("state")?;
        let stage = Stage::from_name(&stage_name);
        let answer = if state_name != "failed"
            || stage.is_none_or(|s| s == Stage::Publish || s == Stage::Cleanup)
        {
            skipped += 1;
            json!({"task_id":id,"job_id":null,"status":"skipped",
                "detail":"only failed, reversible V2 stages can be resumed"})
        } else {
            let stage = stage.expect("verified above");
            match state.scheduler.restart_at(id, stage, json!({})).await {
                Ok(flow) => {
                    resumed += 1;
                    let job: Option<Uuid> = sqlx::query_scalar(
                        "SELECT id FROM vr2_jobs WHERE workflow_id=$1 AND stage=$2",
                    )
                    .bind(flow.id)
                    .bind(stage.as_str())
                    .fetch_optional(&state.db.pool)
                    .await?;
                    json!({"task_id":id,"job_id":job,"status":"resumed","detail":stage.as_str()})
                }
                Err(err) => {
                    failed += 1;
                    tracing::warn!(task_id=%id,error=%err,"V2 bulk resume refused");
                    json!({"task_id":id,"job_id":null,"status":"failed",
                        "detail":"safety or asset precondition rejected restart"})
                }
            }
        };
        results.push(answer);
    }
    Ok(Json(
        json!({"window_hours":hours,"matched_count":rows.len(),
        "resumed_count":resumed,"skipped_count":skipped,
        "failed_count":failed,"results":results}),
    ))
}
// The current Python service has already retired sentence rewriting, preview
// and the old CosyVoice paths. Only native YouDub VoxCPM2 is supported here.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DubbingPayload {
    subtitle_job_id: Uuid,
    subtitle_video_key: String,
    operation_key: String,
    #[serde(default = "default_youdub")]
    model_path: String,
    #[serde(default = "default_voice")]
    voice: String,
    #[serde(default)]
    preview_only: bool,
}
fn default_youdub() -> String {
    "youdub:voxcpm2".into()
}
fn default_voice() -> String {
    "reference".into()
}
async fn manual_dubbing(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(request): Json<DubbingPayload>,
) -> AppResult<Json<ManualJobResponse>> {
    read_task(&state.db, id).await?;
    if request.model_path != "youdub:voxcpm2"
        || request.voice != "reference"
        || request.preview_only
    {
        return Err(AppError::Invalid(
            "only native YouDub VoxCPM2 reference voice without legacy previews is supported"
                .into(),
        ));
    }
    if request.operation_key.trim().is_empty() || request.operation_key.len() > 255 {
        return Err(AppError::Invalid(
            "operation_key must be 1..255 bytes".into(),
        ));
    }
    if state.config.youdub_bridge.is_none() || state.config.youdub_python.is_none() {
        return Err(AppError::Unavailable(
            "V2 isolated YouDub bridge is not configured".into(),
        ));
    }
    let subtitle_belongs: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE j.id=$1 AND j.task_id=$2 AND j.stage='subtitle'
           AND j.state='succeeded' AND w.task_id=$2)",
    )
    .bind(request.subtitle_job_id)
    .bind(id)
    .fetch_one(&state.db.pool)
    .await?;
    if !subtitle_belongs {
        return Err(AppError::Conflict(
            "completed V2 subtitle job does not belong to this task".into(),
        ));
    }
    let video_owned: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1
          AND kind='video_raw'::asset_kind AND storage_key=$2 AND storage_key LIKE 'v2/%')",
    )
    .bind(id)
    .bind(&request.subtitle_video_key)
    .fetch_one(&state.db.pool)
    .await?;
    if !video_owned {
        return Err(AppError::Conflict(
            "YouDub source key must reference the task's V2 raw video".into(),
        ));
    }
    let latest_raw = crate::media::ffmpeg::latest_asset_key(&state.db, id, "video_raw").await?;
    if latest_raw.as_deref() != Some(request.subtitle_video_key.as_str()) {
        return Err(AppError::Conflict(
            "native YouDub requires the latest original video; source changed".into(),
        ));
    }
    let latest_subtitle: Option<Uuid> = sqlx::query_scalar(
        "SELECT id FROM vr2_jobs WHERE task_id=$1 AND stage='subtitle'
           AND state='succeeded' ORDER BY generation DESC,created_at DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    if latest_subtitle != Some(request.subtitle_job_id) {
        return Err(AppError::Conflict(
            "native YouDub requires the latest completed subtitle job".into(),
        ));
    }
    // The V2 YouDub bridge currently supports one deterministic completed
    // native model run, not legacy preview/speech editing controls.
    let flow = state
        .scheduler
        .restart_at(
            id,
            Stage::Dubbing,
            json!({
                "manual_dub_operation_key":request.operation_key,
                "manual_dub_request":{
                    "subtitle_job_id":request.subtitle_job_id,
                    "source_video_key":request.subtitle_video_key
                },
                "auto_dub_p2":true,
                "dubbing":{"enabled":true},
                "auto_publish":false,"auto_publish_platforms":[]
            }),
        )
        .await?;
    response(&state, flow, Stage::Dubbing).await
}
async fn retry_dubbing(
    State(state): State<AppState>,
    Path((id, job_id)): Path<(Uuid, Uuid)>,
) -> AppResult<Json<ManualJobResponse>> {
    read_task(&state.db, id).await?;
    let prior = sqlx::query(
        "SELECT j.state,w.id AS workflow_id FROM vr2_jobs j
         JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE j.id=$1 AND j.task_id=$2 AND j.stage='dubbing'",
    )
    .bind(job_id)
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("V2 YouDub stage job not found".into()))?;
    let state_name: String = prior.try_get("state")?;
    if state_name != "failed" {
        return Err(AppError::Conflict(
            "only a failed native YouDub stage is retryable".into(),
        ));
    }
    let flow = state
        .scheduler
        .restart_at(
            id,
            Stage::Dubbing,
            json!({
                "manual_dub_operation_key":null,
                "auto_dub_p2":true,"dubbing":{"enabled":true},
                "auto_publish":false,"auto_publish_platforms":[]
            }),
        )
        .await?;
    response(&state, flow, Stage::Dubbing).await
}
#[derive(Debug, Deserialize)]
struct ControlQuery {
    action: String,
}
async fn control_dubbing(
    State(state): State<AppState>,
    Path((id, job_id)): Path<(Uuid, Uuid)>,
    Query(query): Query<ControlQuery>,
) -> AppResult<Json<Value>> {
    read_task(&state.db, id).await?;
    if !matches!(query.action.as_str(), "pause" | "cancel") {
        return Err(AppError::Invalid(
            "YouDub control action must be pause or cancel".into(),
        ));
    }
    let row = sqlx::query(
        "SELECT j.state,w.id AS workflow_id,w.current_stage,w.state AS workflow_state
         FROM vr2_jobs j JOIN vr2_workflows w ON j.workflow_id=w.id
         WHERE j.id=$1 AND j.task_id=$2 AND j.stage='dubbing'",
    )
    .bind(job_id)
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("V2 dubbing job missing".into()))?;
    let state_name: String = row.try_get("state")?;
    let stage: String = row.try_get("current_stage")?;
    let state_flow: String = row.try_get("workflow_state")?;
    if stage != "dubbing"
        || state_flow != "running"
        || !matches!(state_name.as_str(), "pending" | "running" | "awaiting")
    {
        return Err(AppError::Conflict(
            "YouDub stage is not currently controllable".into(),
        ));
    }
    let workflow_id: Uuid = row.try_get("workflow_id")?;
    if query.action == "pause" {
        state.scheduler.pause(workflow_id).await?;
    } else {
        state.scheduler.cancel(workflow_id).await?;
    }
    Ok(Json(json!({"job_id":job_id,
        "status":if query.action=="pause"{"paused"}else{"canceled"}})))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{id}/actions/subtitle", post(manual_subtitle))
        .route("/tasks/{id}/actions/dubbing", post(manual_dubbing))
        .route(
            "/tasks/{id}/dubbing_jobs/{job_id}/retry",
            post(retry_dubbing),
        )
        .route(
            "/tasks/{id}/dubbing_jobs/{job_id}/control",
            post(control_dubbing),
        )
        .route("/tasks/{id}/actions/subtitle_resume", post(resume_subtitle))
        .route(
            "/tasks/{id}/actions/auto_subtitle_handoff",
            post(auto_subtitle_handoff),
        )
        .route("/tasks/{id}/actions/auto_publish", post(auto_publish))
        .route("/tasks/{id}/actions/publish", post(manual_publish))
        .route("/tasks/actions/resume_failed_recent", post(resume_recent))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn auto_publish_selection_requires_known_targets_and_respects_overrides() {
        let (enabled, platforms) = auto_platform_selection(
            &json!({"auto_publish":true,"auto_publish_platforms":["bilibili"]}),
            &json!({"auto_publish":false,"auto_publish_platforms":[]}),
        )
        .unwrap();
        assert!(!enabled && platforms.is_empty());
        assert!(
            auto_platform_selection(&json!({"auto_publish_platforms":["TikTok"]}), &json!({}))
                .is_err()
        );
        assert!(
            auto_platform_selection(&json!({"auto_publish_platforms":"bilibili"}), &json!({}))
                .is_err()
        );
        let (_, platforms) = auto_platform_selection(
            &json!({"auto_publish_platforms":["bilibili","bilibili"]}),
            &json!({}),
        )
        .unwrap();
        assert_eq!(platforms, ["bilibili"]);
    }
    #[test]
    fn unsupported_subtitle_options_never_get_silently_ignored() {
        assert!(
            subtitle_config(
                serde_json::from_value(json!({
            "asr_engine":"groq-whisper","burn_in":true}))
                .unwrap()
            )
            .is_err()
        );
        assert!(
            subtitle_config(
                serde_json::from_value(json!({
            "youtube_subtitle_mode":"invalid"}))
                .unwrap()
            )
            .is_err()
        );
        assert!(
            subtitle_config(
                serde_json::from_value(json!({
                    "translate_enabled":true,"burn_in":true,"video_codec":"h264",
                    "prefer_youtube_subtitles":false,"youtube_subtitle_mode":"off"
                }))
                .unwrap()
            )
            .is_ok()
        );
    }
}
