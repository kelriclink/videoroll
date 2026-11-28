//! Original V1 selective retranslation, scheduled as a durable native Subtitle
//! stage. Only Rust-produced, aligned source/translated snapshots can be used.
//! Never rewrite the source snapshots or silently run full ASR again.
use crate::{
    AppError, AppResult,
    api::AppState,
    domain::Stage,
    legacy_contracts::read_task,
    storage::AssetRegistry,
    subtitles::{SubtitleCue, validate},
};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::post,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use tokio::fs;
use uuid::Uuid;

const MAX_SNAPSHOT: usize = 8 * 1024 * 1024;
const MAX_CUES: usize = 5000;
const MAX_RETRANSLATE: usize = 500;
#[derive(Debug, Deserialize)]
struct Request {
    indices: Vec<i64>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct Plan {
    pub(crate) indices: Vec<usize>, // 1-based indices as in Python
    pub(crate) base_segments_key: String,
    pub(crate) base_sha256: String,
    pub(crate) source_job_id: Uuid,
}
#[derive(Debug)]
pub(crate) struct Snapshot {
    pub(crate) source: Vec<SubtitleCue>,
    pub(crate) translated: Vec<SubtitleCue>,
    pub(crate) metadata: Value,
}
fn bad(s: &str) -> AppError {
    AppError::Invalid(s.into())
}
fn selected(raw: &[i64], total: usize) -> AppResult<Vec<usize>> {
    if raw.is_empty() || raw.len() > MAX_CUES {
        return Err(bad("indices must contain 1..5000 entries"));
    }
    let mut indices = raw
        .iter()
        .copied()
        .filter(|i| *i > 0)
        .map(|i| i as usize)
        .collect::<Vec<_>>();
    indices.sort_unstable();
    indices.dedup();
    if indices.is_empty() || indices.len() > MAX_RETRANSLATE {
        return Err(bad(
            "at least one and at most 500 selected subtitle indices required",
        ));
    }
    if indices.last().is_some_and(|i| *i > total) {
        return Err(bad("selected subtitle index exceeds aligned snapshot"));
    }
    Ok(indices)
}
fn parse_snapshot(bytes: &[u8]) -> AppResult<Snapshot> {
    if bytes.len() > MAX_SNAPSHOT {
        return Err(bad("subtitle snapshot exceeds 8 MiB"));
    }
    let metadata: Value =
        serde_json::from_slice(bytes).map_err(|_| bad("subtitle snapshot has invalid JSON"))?;
    if metadata.get("translation").and_then(Value::as_bool) != Some(true) {
        return Err(AppError::Conflict(
            "full translated subtitle snapshot required".into(),
        ));
    }
    let source: Vec<SubtitleCue> =
        serde_json::from_value(metadata.get("source").cloned().unwrap_or(Value::Null))
            .map_err(|_| bad("subtitle source snapshot missing or invalid"))?;
    let translated: Vec<SubtitleCue> =
        serde_json::from_value(metadata.get("translated").cloned().unwrap_or(Value::Null))
            .map_err(|_| bad("subtitle aligned translations missing or invalid"))?;
    if source.is_empty() || source.len() > MAX_CUES || source.len() != translated.len() {
        return Err(AppError::Conflict(
            "subtitle source and translated segments are not aligned".into(),
        ));
    }
    validate(&source)?;
    validate(&translated)?;
    for (a, b) in source.iter().zip(&translated) {
        if a.start_ms != b.start_ms
            || a.end_ms != b.end_ms
            || a.text.trim().is_empty()
            || b.text.trim().is_empty()
        {
            return Err(AppError::Conflict(
                "subtitle timing or content no longer aligns".into(),
            ));
        }
    }
    Ok(Snapshot {
        source,
        translated,
        metadata,
    })
}
pub(crate) fn source_job(task: Uuid, key: &str) -> AppResult<Uuid> {
    let parts = key.split('/').collect::<Vec<_>>();
    if parts.len() != 5
        || parts[0] != "v2"
        || parts[1] != "sub"
        || parts[2] != task.to_string()
        || parts[4] != "segments.json"
    {
        return Err(bad(
            "base snapshot must be a task-owned native Rust segments.json",
        ));
    }
    Uuid::parse_str(parts[3]).map_err(|_| bad("base snapshot job id invalid"))
}
pub(crate) async fn load_snapshot(
    registry: &AssetRegistry,
    task: Uuid,
    key: &str,
) -> AppResult<(Snapshot, String)> {
    source_job(task, key)?;
    let file = registry.resolve_existing(key).await?;
    let size = fs::metadata(&file).await?.len();
    if size == 0 || size > MAX_SNAPSHOT as u64 {
        return Err(bad("subtitle snapshot empty or too large"));
    }
    let raw = fs::read(&file).await?;
    if raw.len() > MAX_SNAPSHOT {
        return Err(bad("subtitle snapshot exceeded read limit"));
    }
    let digest = hex::encode(Sha256::digest(&raw));
    Ok((parse_snapshot(&raw)?, digest))
}
pub(crate) fn merge(
    snapshot: &Snapshot,
    indices: &[usize],
    replacements: &[String],
) -> AppResult<Vec<SubtitleCue>> {
    if indices.len() != replacements.len() || indices.is_empty() {
        return Err(bad("replacement count mismatch"));
    }
    let mut edited = snapshot.translated.clone();
    for (&i, translation) in indices.iter().zip(replacements) {
        if i == 0
            || i > edited.len()
            || translation.trim().is_empty()
            || translation.chars().count() > 6000
        {
            return Err(bad("invalid replacement translation"));
        }
        edited[i - 1].text = translation.trim().into();
    }
    validate(&edited)?;
    Ok(edited)
}
async fn request(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Json(request): Json<Request>,
) -> AppResult<Json<Value>> {
    schedule(&state, task_id, &request.indices, None)
        .await
        .map(Json)
}
pub(crate) async fn schedule(
    state: &AppState,
    task_id: Uuid,
    indices: &[i64],
    pinned: Option<&str>,
) -> AppResult<Value> {
    if let Some(key) = pinned {
        source_job(task_id, key)?;
    }
    let task = read_task(&state.db, task_id).await?;
    if matches!(
        task.status.as_str(),
        "CANCELED" | "PUBLISHED" | "PUBLISHING"
    ) {
        return Err(AppError::Conflict(
            "stopped or published task cannot retranslate".into(),
        ));
    }
    let original_busy:bool=sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM subtitle_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))")
        .bind(task_id).fetch_one(&state.db.pool).await?;
    if original_busy {
        return Err(AppError::Conflict(
            "original subtitle job still owns the task".into(),
        ));
    }
    // Only select snapshots from confirmed successful subtitle stages. A
    // just-written snapshot from the current in-flight retranslation must not
    // replace its pinned base when a client clicks twice.
    let row = sqlx::query(
        "SELECT a.storage_key,j.id AS source_job_id,w.configuration
         FROM assets a JOIN vr2_jobs j ON j.task_id=a.task_id
           AND a.storage_key=('v2/sub/'||a.task_id::text||'/'||j.id::text||'/segments.json')
         JOIN vr2_workflows w ON w.id=j.workflow_id
         WHERE a.task_id=$1 AND a.kind='segments_json'::asset_kind
           AND j.stage='subtitle' AND j.state='succeeded' AND ($2::text IS NULL OR a.storage_key=$2)
         ORDER BY a.created_at DESC,a.id DESC LIMIT 1",
    )
    .bind(task_id)
    .bind(pinned)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| {
        AppError::Conflict(
            "no confirmed native aligned translation snapshot; run full translation first".into(),
        )
    })?;
    let key: String = row.try_get("storage_key")?;
    let source_job_id: Uuid = row.try_get("source_job_id")?;
    if source_job(task_id, &key)? != source_job_id {
        return Err(AppError::Conflict(
            "snapshot job does not match stored source job".into(),
        ));
    }
    let (snapshot, digest) = load_snapshot(&state.storage, task_id, &key).await?;
    let indices = selected(indices, snapshot.source.len())?;
    let existing_srt = key.replace("/segments.json", "/subtitle.srt");
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM assets
        WHERE task_id=$1 AND kind='subtitle_srt'::asset_kind AND storage_key=$2)",
    )
    .bind(task_id)
    .bind(&existing_srt)
    .fetch_one(&state.db.pool)
    .await?;
    if !exists
        || fs::metadata(state.storage.resolve_existing(&existing_srt).await?)
            .await
            .is_err()
    {
        return Err(AppError::Conflict(
            "aligned subtitle snapshot is missing its SRT asset".into(),
        ));
    }
    let source_config: Value = row.try_get("configuration")?;
    let global = crate::settings::effective(&state.db, "subtitle.auto_profile").await?;
    let subtitle = source_config
        .get("subtitle")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let provider = subtitle
        .get("translate_provider")
        .and_then(Value::as_str)
        .or_else(|| global.get("translate_provider").and_then(Value::as_str))
        .unwrap_or("openai")
        .to_owned();
    if !matches!(provider.as_str(), "openai" | "index_translate") {
        return Err(AppError::Conflict(
            "selective retranslation requires a real OpenAI or Index translation provider".into(),
        ));
    }
    let mut subtitle = subtitle;
    if !subtitle.is_object() {
        return Err(bad("previous subtitle config invalid"));
    }
    subtitle["translate_provider"] = json!(provider);
    subtitle["translate_enabled"] = json!(true);
    // Resolve provider before accepting a durable job: no fake queued model.
    if provider == "openai" {
        let provider_id = subtitle
            .get("translate_model_provider_id")
            .and_then(Value::as_str)
            .or_else(|| {
                global
                    .get("translate_model_provider_id")
                    .and_then(Value::as_str)
            })
            .unwrap_or("legacy");
        let model_id = subtitle
            .get("translate_model_id")
            .and_then(Value::as_str)
            .or_else(|| global.get("translate_model_id").and_then(Value::as_str))
            .unwrap_or("");
        crate::providers::resolve(&state.db, &state.config, provider_id, model_id).await?;
    } else {
        crate::media::index_translate::url()?;
    }
    let plan = Plan {
        indices,
        base_segments_key: key,
        base_sha256: digest,
        source_job_id,
    };
    let flow = state
        .scheduler
        .restart_at(
            task_id,
            Stage::Subtitle,
            json!({
                "selective_retranslate":plan,"subtitle":subtitle,"standalone_subtitle_runtime_profile":false,
                "auto_publish":false,"auto_publish_platforms":[]
            }),
        )
        .await?;
    let row = sqlx::query(
        "SELECT id,state FROM vr2_jobs WHERE workflow_id=$1 AND stage='subtitle'
        ORDER BY created_at DESC LIMIT 1",
    )
    .bind(flow.id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::Unavailable("selective subtitle stage not queued".into()))?;
    let status: String = row.try_get("state")?;
    let status = match status.as_str() {
        "pending" | "awaiting" => "queued",
        "running" => "running",
        "succeeded" => "succeeded",
        other => {
            return Err(AppError::Conflict(format!(
                "subtitle stage state {other} not ready"
            )));
        }
    };
    Ok(json!({"job_id":row.try_get::<Uuid,_>("id")?,"status":status}))
}
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/tasks/{task_id}/actions/subtitle_retranslate",
        post(request),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selected_indices_match_python_and_are_bounded() {
        assert_eq!(selected(&[3, 2, 3, -1, 0, 1], 4).unwrap(), [1, 2, 3]);
        assert!(selected(&[0, -1], 4).is_err());
        assert!(selected(&[2, 5], 4).is_err());
        assert!(selected(&vec![1; 5001], 3).is_err());
    }
    #[test]
    fn merge_preserves_all_unselected_cues_and_timestamps() {
        let src = vec![
            SubtitleCue {
                start_ms: 0,
                end_ms: 1000,
                text: "one".into(),
                secondary_text: None,
                confidence: None,
            },
            SubtitleCue {
                start_ms: 1000,
                end_ms: 2000,
                text: "two".into(),
                secondary_text: None,
                confidence: None,
            },
            SubtitleCue {
                start_ms: 2000,
                end_ms: 3000,
                text: "three".into(),
                secondary_text: None,
                confidence: None,
            },
        ];
        let out = src
            .iter()
            .map(|v| SubtitleCue {
                text: format!("old {}", v.text),
                secondary_text: Some(v.text.clone()),
                ..v.clone()
            })
            .collect::<Vec<_>>();
        let snapshot = Snapshot {
            source: src.clone(),
            translated: out.clone(),
            metadata: json!({}),
        };
        let actual = merge(&snapshot, &[2], &["new two".into()]).unwrap();
        assert_eq!(actual[0], out[0]);
        assert_eq!(actual[2], out[2]);
        assert_eq!(actual[1].text, "new two");
        assert_eq!(actual[1].secondary_text.as_deref(), Some("two"));
        assert_eq!(actual[1].start_ms, 1000);
    }
    #[test]
    fn rejects_misaligned_or_untranslated_snapshots() {
        let base = json!({"translation":true,"source":[{"start_ms":0,"end_ms":1000,"text":"a"}],
            "translated":[{"start_ms":1,"end_ms":1000,"text":"b"}]});
        assert!(parse_snapshot(&serde_json::to_vec(&base).unwrap()).is_err());
        let mut missing = base.clone();
        missing["translation"] = json!(false);
        assert!(parse_snapshot(&serde_json::to_vec(&missing).unwrap()).is_err());
        assert!(source_job(Uuid::nil(), "v2/sub/other/job/segments.json").is_err());
    }
}
