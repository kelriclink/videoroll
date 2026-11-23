//! Remaining original subtitle-service read/diagnostic contracts.
//!
//! These handlers use the original PostgreSQL/configuration facts directly.
//! They do not create a second subtitle/RAG state store.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{Row, postgres::PgRow};
use std::{collections::HashMap, path::PathBuf};
use uuid::Uuid;

#[derive(Debug, Deserialize)]
struct AgentQuery {
    status: Option<String>,
    limit: Option<i64>,
    include_descendants: Option<bool>,
}

fn agent_row(row: &PgRow) -> AppResult<Value> {
    let steps: Value = row.try_get("steps")?;
    let result: Value = row.try_get("result")?;
    Ok(json!({
        "id": row.try_get::<Uuid,_>("id")?,
        "agent_type": row.try_get::<String,_>("agent_type")?,
        "status": row.try_get::<String,_>("status")?,
        "term": row.try_get::<String,_>("term")?,
        "domain": row.try_get::<String,_>("domain")?,
        "target_lang": row.try_get::<String,_>("target_lang")?,
        "task_id": row.try_get::<Option<Uuid>,_>("task_id")?,
        "subtitle_job_id": row.try_get::<Option<Uuid>,_>("subtitle_job_id")?,
        "subtitle_job_status": row.try_get::<Option<String>,_>("subtitle_job_status")?,
        "query": row.try_get::<String,_>("query")?,
        "steps": if steps.is_array(){steps}else{json!([])},
        "result": if result.is_object(){result}else{json!({})},
        "error": row.try_get::<String,_>("error")?,
        "knowledge_item_id": row.try_get::<Option<Uuid>,_>("knowledge_item_id")?,
        "parent_agent_run_id": row.try_get::<Option<Uuid>,_>("parent_agent_run_id")?,
        "started_at": row.try_get::<DateTime<Utc>,_>("started_at")?,
        "finished_at": row.try_get::<Option<DateTime<Utc>>,_>("finished_at")?,
        "created_at": row.try_get::<DateTime<Utc>,_>("created_at")?,
        "updated_at": row.try_get::<DateTime<Utc>,_>("updated_at")?,
    }))
}

async fn hydrate_empty_steps(state: &AppState, values: &mut [Value]) -> AppResult<()> {
    let ids = values
        .iter()
        .filter(|v| {
            v.get("steps")
                .and_then(Value::as_array)
                .is_none_or(|x| x.is_empty())
        })
        .filter_map(|v| {
            v.get("id")
                .and_then(Value::as_str)
                .and_then(|s| Uuid::parse_str(s).ok())
        })
        .collect::<Vec<_>>();
    if ids.is_empty() {
        return Ok(());
    }
    let rows = sqlx::query(
        "SELECT run_id,event FROM translation_agent_events
         WHERE run_id=ANY($1) ORDER BY created_at ASC,id ASC",
    )
    .bind(&ids)
    .fetch_all(&state.db.pool)
    .await?;
    let mut events = HashMap::<Uuid, Vec<Value>>::new();
    for row in rows {
        let id: Uuid = row.try_get("run_id")?;
        let event: Value = row.try_get("event")?;
        if event.is_object() {
            events.entry(id).or_default().push(event);
        }
    }
    for value in values {
        if value
            .get("steps")
            .and_then(Value::as_array)
            .is_some_and(|x| !x.is_empty())
        {
            continue;
        }
        let id = value
            .get("id")
            .and_then(Value::as_str)
            .and_then(|s| Uuid::parse_str(s).ok());
        if let Some(rows) = id.and_then(|id| events.remove(&id)) {
            value["steps"] = json!(rows);
        }
    }
    Ok(())
}

async fn agent_runs(
    State(state): State<AppState>,
    Query(query): Query<AgentQuery>,
) -> AppResult<Json<Vec<Value>>> {
    let status = query
        .status
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty());
    if status.is_some_and(|v| v.len() > 64 || v.chars().any(char::is_control)) {
        return Err(AppError::Invalid("invalid agent status filter".into()));
    }
    let limit = query.limit.unwrap_or(30).clamp(1, 100);
    let rows = if query.include_descendants.unwrap_or(false) {
        sqlx::query(
            r#"WITH RECURSIVE roots AS (
                 SELECT id FROM translation_agent_runs
                 WHERE parent_agent_run_id IS NULL
                   AND ($1::text IS NULL OR status=$1)
                 ORDER BY updated_at DESC,started_at DESC LIMIT $2
               ), agent_tree AS (
                 SELECT r.* FROM translation_agent_runs r JOIN roots ON roots.id=r.id
                 UNION ALL
                 SELECT child.* FROM translation_agent_runs child
                 JOIN agent_tree parent ON child.parent_agent_run_id=parent.id
               )
               SELECT r.id,r.agent_type,r.status,r.term,r.domain,r.target_lang,
                 r.task_id,r.subtitle_job_id,
                 (SELECT j.status::text FROM subtitle_jobs j
                   WHERE j.id=r.subtitle_job_id) AS subtitle_job_status,
                 r.query,r.steps,r.result,r.error,r.knowledge_item_id,
                 r.parent_agent_run_id,r.started_at,r.finished_at,r.created_at,r.updated_at
               FROM agent_tree r ORDER BY r.updated_at DESC,r.started_at DESC"#,
        )
        .bind(status)
        .bind(limit)
        .fetch_all(&state.db.pool)
        .await?
    } else {
        sqlx::query(
            r#"SELECT r.id,r.agent_type,r.status,r.term,r.domain,r.target_lang,
                 r.task_id,r.subtitle_job_id,
                 (SELECT j.status::text FROM subtitle_jobs j
                   WHERE j.id=r.subtitle_job_id) AS subtitle_job_status,
                 r.query,r.steps,r.result,r.error,r.knowledge_item_id,
                 r.parent_agent_run_id,r.started_at,r.finished_at,r.created_at,r.updated_at
               FROM translation_agent_runs r
               WHERE ($1::text IS NULL OR r.status=$1)
               ORDER BY r.updated_at DESC,r.started_at DESC LIMIT $2"#,
        )
        .bind(status)
        .bind(limit)
        .fetch_all(&state.db.pool)
        .await?
    };
    let mut values = rows.iter().map(agent_row).collect::<AppResult<Vec<_>>>()?;
    hydrate_empty_steps(&state, &mut values).await?;
    Ok(Json(values))
}

async fn agent_run(
    State(state): State<AppState>,
    Path(run_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let row = sqlx::query(
        r#"SELECT r.id,r.agent_type,r.status,r.term,r.domain,r.target_lang,
             r.task_id,r.subtitle_job_id,
             (SELECT j.status::text FROM subtitle_jobs j
               WHERE j.id=r.subtitle_job_id) AS subtitle_job_status,
             r.query,r.steps,r.result,r.error,r.knowledge_item_id,
             r.parent_agent_run_id,r.started_at,r.finished_at,r.created_at,r.updated_at
           FROM translation_agent_runs r WHERE r.id=$1"#,
    )
    .bind(run_id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("agent run not found".into()))?;
    let mut values = vec![agent_row(&row)?];
    hydrate_empty_steps(&state, &mut values).await?;
    Ok(Json(values.remove(0)))
}

#[derive(Debug, Deserialize)]
struct EmbeddingTest {
    #[serde(default = "embedding_default_text")]
    text: String,
    provider: Option<String>,
    model: Option<String>,
    api_key: Option<String>,
    base_url: Option<String>,
    timeout_seconds: Option<f64>,
    #[allow(dead_code)]
    model_dir: Option<String>,
    dimensions: Option<usize>,
    #[allow(dead_code)]
    device: Option<String>,
}
fn embedding_default_text() -> String {
    "hello world".into()
}
async fn embedding_test(
    State(state): State<AppState>,
    Json(body): Json<EmbeddingTest>,
) -> AppResult<Json<Value>> {
    let text = body.text.trim();
    if text.is_empty() {
        return Err(AppError::Invalid("text is required".into()));
    }
    if text.chars().count() > 2000 {
        return Err(AppError::Invalid("text too long (max 2000 chars)".into()));
    }
    let mut settings = crate::subtitle_embedding_infer_v1::load(&state).await?;
    if let Some(provider) = body.provider {
        settings.provider = provider.trim().to_ascii_lowercase();
    }
    if let Some(model) = body.model {
        let model = model.trim();
        if model.is_empty() || model.chars().count() > 512 {
            return Err(AppError::Invalid("embedding model is invalid".into()));
        }
        settings.model = model.into();
    }
    if let Some(key) = body.api_key {
        settings.api_key = Some(key);
    }
    if let Some(base) = body.base_url {
        settings.endpoint = crate::subtitle_embedding_infer_v1::endpoint(&base)?;
    }
    if let Some(timeout) = body.timeout_seconds {
        if !timeout.is_finite() || !(1.0..=600.0).contains(&timeout) {
            return Err(AppError::Invalid("embedding timeout is invalid".into()));
        }
        settings.timeout_seconds = timeout;
    }
    if let Some(dimensions) = body.dimensions {
        if !(1..=4096).contains(&dimensions) {
            return Err(AppError::Invalid(
                "embedding dimensions must be 1..4096".into(),
            ));
        }
        settings.dimensions = dimensions;
    }
    let expected = settings.dimensions;
    let provider = settings.provider.clone();
    let model = settings.model.clone();
    let vector = crate::subtitle_embedding_infer_v1::embed(&settings, text).await?;
    Ok(Json(json!({
        "provider":provider,
        "model":model,
        "dimensions":vector.len(),
        "expected_dimensions":expected,
        "ok":vector.len()==expected
    })))
}

fn env(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.into())
}
fn env_i64(key: &str, default: i64) -> i64 {
    env(key, &default.to_string()).parse().unwrap_or(default)
}
async fn subtitle_settings(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let raw = crate::settings::effective(&state.db, "subtitle.asr").await?;
    let runtime = crate::subtitle_asr_settings_v1::view(&raw);
    let cpu_threads = env_i64("SUBTITLE_WHISPER_CPU_THREADS", 0).max(0);
    let workers = env_i64("SUBTITLE_WHISPER_NUM_WORKERS", 1).max(1);
    let effective_threads = if cpu_threads > 0 {
        cpu_threads
    } else {
        std::thread::available_parallelism()
            .map(|v| v.get() as i64)
            .unwrap_or(4)
    };
    Ok(Json(json!({
        "asr_engine":env("SUBTITLE_ASR_ENGINE","faster-whisper"),
        "whisper_model":env("SUBTITLE_WHISPER_MODEL","tiny"),
        "whisper_model_dir":env("SUBTITLE_WHISPER_MODEL_DIR","/models/whisper"),
        "whisper_device":env("SUBTITLE_WHISPER_DEVICE","cpu"),
        "whisper_compute_type":env("SUBTITLE_WHISPER_COMPUTE_TYPE","int8"),
        "openvino_model":env("SUBTITLE_OPENVINO_MODEL",""),
        "openvino_device":runtime["openvino_device"],
        "openvino_num_beams":runtime["openvino_num_beams"],
        "openvino_max_new_tokens":runtime["openvino_max_new_tokens"],
        "openvino_vad_enabled":runtime["openvino_vad_enabled"],
        "openvino_vad_threshold":runtime["openvino_vad_threshold"],
        "whisper_cpu_threads":cpu_threads,
        "whisper_num_workers":workers,
        "whisper_cpu_threads_effective":effective_threads,
        "whisper_num_workers_effective":workers,
        // Python package-presence flags do not apply to the native replacement.
        "faster_whisper_installed":false,
        "openvino_installed":false,
        "external_whisper_base_url":runtime["external_whisper_base_url"],
        "external_whisper_model":runtime["external_whisper_model"],
        "external_whisper_api_key_set":runtime["external_whisper_api_key_set"],
        "external_whisper_batch_size":runtime["external_whisper_batch_size"],
        "external_whisper_vad_enabled":runtime["external_whisper_vad_enabled"],
        "external_whisper_vad_threshold":runtime["external_whisper_vad_threshold"],
        "external_whisper_min_silence_ms":runtime["external_whisper_min_silence_ms"],
        "external_whisper_speech_pad_ms":runtime["external_whisper_speech_pad_ms"],
        "external_whisper_condition_on_previous_text":runtime["external_whisper_condition_on_previous_text"],
        "external_whisper_max_segment_seconds":runtime["external_whisper_max_segment_seconds"],
        "external_whisper_max_segment_chars":runtime["external_whisper_max_segment_chars"],
        "groq_whisper_model":runtime["groq_whisper_model"],
        "groq_whisper_api_key_set":runtime["groq_whisper_api_key_set"],
        "cloudflare_workers_ai_account_id":runtime["cloudflare_workers_ai_account_id"],
        "cloudflare_workers_ai_model":runtime["cloudflare_workers_ai_model"],
        "cloudflare_workers_ai_api_key_set":runtime["cloudflare_workers_ai_api_key_set"]
    })))
}

async fn intel_hardware() -> AppResult<Json<Value>> {
    let render_device = env("SUBTITLE_INTEL_GPU_RENDER_DEVICE", "/dev/dri/renderD128");
    let node = PathBuf::from(&render_device);
    let name = node
        .file_name()
        .and_then(|v| v.to_str())
        .unwrap_or("renderD128");
    let sys = PathBuf::from("/sys/class/drm").join(name).join("device");
    if !node.exists() || !sys.exists() {
        return Ok(Json(json!({
            "checked":true,"available":false,"render_device":render_device,
            "model_name":null,"driver":null,"pci_slot":null,"pci_id":null,
            "detail":format!("未找到渲染设备：{render_device}"),
            "openvino_devices":[],"openvino_gpu_available":false,
            "openvino_error":"native Rust does not load the Python OpenVINO runtime"
        })));
    }
    let vendor = tokio::fs::read_to_string(sys.join("vendor"))
        .await
        .unwrap_or_default();
    let pci_id = tokio::fs::read_to_string(sys.join("device"))
        .await
        .ok()
        .map(|v| v.trim().to_string());
    let canonical = tokio::fs::canonicalize(&sys).await.ok();
    let pci_slot = canonical
        .as_ref()
        .and_then(|v| v.file_name())
        .and_then(|v| v.to_str())
        .map(str::to_owned);
    let driver = tokio::fs::canonicalize(sys.join("driver"))
        .await
        .ok()
        .and_then(|v| v.file_name().map(|x| x.to_string_lossy().to_string()));
    let intel = vendor.trim().eq_ignore_ascii_case("0x8086");
    Ok(Json(json!({
        "checked":true,"available":intel,"render_device":render_device,
        "model_name":null,"driver":driver,"pci_slot":pci_slot,"pci_id":pci_id,
        "detail":if intel{"检测到 Intel DRM 渲染设备"}else{"渲染设备不是 Intel PCI 设备"},
        "openvino_devices":[],"openvino_gpu_available":false,
        "openvino_error":"native Rust does not load the Python OpenVINO runtime"
    })))
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/agents/runs", get(agent_runs))
        .route("/subtitle/agents/runs/{run_id}", get(agent_run))
        .route("/subtitle/embedding/test", post(embedding_test))
        .route("/subtitle/hardware/intel", get(intel_hardware))
        .route("/subtitle/settings", get(subtitle_settings))
}
