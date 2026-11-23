//! Python-shaped manual subtitle translation context mutation.
//! Updates the same SubtitleJob.request_json.artifacts pointer and writes
//! the same sub/<task-id>/translation_context/manual-<uuid>.json payload.
//! All source-index effects are derived from the latest actual segments asset.
use crate::{AppError, AppResult, api::AppState, subtitle_management_v1 as parent};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::put,
};
use serde_json::{Value, json};
use sqlx::Row;
use std::collections::BTreeMap;
use uuid::Uuid;

fn invalid(s: &str) -> AppError {
    AppError::Invalid(s.into())
}

fn term_map(memory: &Value) -> BTreeMap<String, Vec<String>> {
    let mut map = BTreeMap::new();
    if let Some(items) = memory.get("characters").and_then(Value::as_array) {
        for item in items {
            let clean = |k: &str| {
                item.get(k)
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_owned()
            };
            let name = clean("name");
            if !name.is_empty() {
                map.insert(
                    format!("character:{}", name.to_lowercase()),
                    vec![name, clean("target_name"), clean("role"), clean("notes")],
                );
            }
            if let Some(aliases) = item.get("aliases").and_then(Value::as_array) {
                for alias in aliases {
                    let a = alias.as_str().unwrap_or("").trim();
                    if !a.is_empty() {
                        map.insert(
                            format!("character-alias:{}", a.to_lowercase()),
                            vec![a.to_string(), clean("target_name")],
                        );
                    }
                }
            }
        }
    }
    for (field, prefix, names) in [
        ("terminology", "term", &["source", "target", "meaning"][..]),
        ("ambiguities", "ambiguity", &["term", "resolution"][..]),
    ] {
        if let Some(items) = memory.get(field).and_then(Value::as_array) {
            for item in items {
                let record: Vec<String> = names
                    .iter()
                    .map(|name| {
                        item.get(*name)
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .trim()
                            .to_string()
                    })
                    .collect();
                if !record[0].is_empty() {
                    map.insert(format!("{prefix}:{}", record[0].to_lowercase()), record);
                }
            }
        }
    }
    map
}
fn changed_source_terms(before: &Value, after: &Value) -> Vec<String> {
    let a = term_map(before);
    let b = term_map(after);
    let mut keys: Vec<&String> = a.keys().chain(b.keys()).collect();
    keys.sort();
    keys.dedup();
    let mut changed = Vec::<String>::new();
    for key in keys {
        if a.get(key) == b.get(key) {
            continue;
        }
        if let Some(pair) = b.get(key).or_else(|| a.get(key)) {
            let name = &pair[0];
            if !name.is_empty() && !changed.contains(name) {
                changed.push(name.clone());
            }
        }
    }
    changed
}
fn contains_term(text: &str, term: &str) -> bool {
    let text = text.to_lowercase();
    let term = term.trim().to_lowercase();
    if term.is_empty() || text.is_empty() {
        return false;
    }
    // Python uses bounded word matching only for ASCII Latin/digit terms.
    let latin = term
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_alphanumeric)
        && term
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'+' | b'-' | b' '));
    if !latin {
        return text.contains(&term);
    }
    text.match_indices(&term).any(|(i, match_text)| {
        let before = i
            .checked_sub(1)
            .and_then(|pos| text.as_bytes().get(pos))
            .copied();
        let after = text.as_bytes().get(i + match_text.len()).copied();
        !before.is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            && !after.is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    })
}
async fn affected_indices(state: &AppState, task: Uuid, terms: &[String]) -> AppResult<Vec<usize>> {
    if terms.is_empty() {
        return Ok(vec![]);
    }
    let key: Option<String> = sqlx::query_scalar(
        "SELECT storage_key FROM assets WHERE task_id=$1
         AND kind::text='segments_json'
         ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(&state.db.pool)
    .await?;
    let Some(key) = key else {
        return Ok(vec![]);
    };
    let rows = match parent::read_job_report(state, &key).await {
        Ok(Value::Array(rows)) => rows,
        _ => return Ok(vec![]),
    };
    Ok(rows
        .iter()
        .filter_map(|v| {
            if !v.is_object() {
                return None;
            }
            let text = v.get("text").and_then(Value::as_str).unwrap_or("").trim();
            let secondary = v
                .get("secondary_text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if text.is_empty() && secondary.is_empty() {
                None
            } else {
                Some(text)
            }
        })
        .enumerate()
        .filter_map(|(index, text)| {
            terms
                .iter()
                .any(|term| contains_term(text, term))
                .then_some(index + 1)
        })
        .collect())
}
pub(crate) async fn save_context(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    Json(payload): Json<Value>,
) -> AppResult<Json<Value>> {
    let body = payload
        .as_object()
        .ok_or_else(|| invalid("translation context requires object"))?;
    let mut tx = state.db.pool.begin().await?;
    let job = sqlx::query(
        "SELECT id,request_json FROM subtitle_jobs WHERE task_id=$1
         ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE",
    )
    .bind(task_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(job) = job else {
        let exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1)")
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
        return Err(if exists {
            AppError::NotFound("subtitle job not found".into())
        } else {
            AppError::NotFound("task not found".into())
        });
    };
    let job_id: Uuid = job.try_get("id")?;
    let mut request: Value = job.try_get("request_json")?;
    let previous = if let Some(key) = parent::job_artifact_key(&request, "translation_context_key")
    {
        match parent::read_job_report(&state, &key).await {
            Ok(v) => v,
            Err(AppError::NotFound(_)) => json!({}),
            Err(error) => return Err(error),
        }
    } else {
        json!({})
    };
    let before = parent::context_memory(previous.get("memory").unwrap_or(&Value::Null));
    let memory = parent::context_memory(body.get("memory").unwrap_or(&Value::Null));
    if ["topic", "style_notes"].iter().all(|name| {
        memory
            .get(*name)
            .and_then(Value::as_str)
            .unwrap_or("")
            .is_empty()
    }) && ["characters", "terminology", "ambiguities"]
        .iter()
        .all(|name| {
            memory
                .get(*name)
                .and_then(Value::as_array)
                .is_none_or(Vec::is_empty)
        })
        && memory.get("recent_scene").is_none()
    {
        return Err(invalid("translation context memory cannot be empty"));
    }
    let summary = body
        .get("summary")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| previous.get("summary").and_then(Value::as_str))
        .unwrap_or("")
        .trim()
        .chars()
        .take(500)
        .collect::<String>();
    let changed = changed_source_terms(&before, &memory);
    let affected = affected_indices(&state, task_id, &changed).await?;
    let key = format!(
        "sub/{task_id}/translation_context/manual-{}.json",
        Uuid::new_v4().simple()
    );
    let path = state.storage.resolve_existing(&key).await?;
    let parent = path
        .parent()
        .ok_or_else(|| invalid("missing context output directory"))?;
    tokio::fs::create_dir_all(parent).await?;
    let payload = json!({"version":1,"summary":summary,"memory":memory});
    let raw = serde_json::to_vec_pretty(&payload)
        .map_err(|_| invalid("failed to serialize translation context"))?;
    let temp = path.with_extension(format!("{}.partial", Uuid::new_v4()));
    tokio::fs::write(&temp, &raw).await?;
    if let Err(e) = tokio::fs::rename(&temp, &path).await {
        let _ = tokio::fs::remove_file(&temp).await;
        return Err(e.into());
    }
    if !request.is_object() {
        request = json!({});
    }
    if !request["artifacts"].is_object() {
        request["artifacts"] = json!({});
    }
    request["artifacts"]["translation_context_key"] = json!(key);
    if let Err(error) =
        sqlx::query("UPDATE subtitle_jobs SET request_json=$1,updated_at=now() WHERE id=$2")
            .bind(&request)
            .bind(job_id)
            .execute(&mut *tx)
            .await
    {
        let _ = tokio::fs::remove_file(&path).await;
        return Err(error.into());
    }
    if let Err(error) = tx.commit().await {
        let _ = tokio::fs::remove_file(&path).await;
        return Err(error.into());
    }
    Ok(Json(json!({
       "available":true,"job_id":job_id,"key":key,"summary":summary,
       "memory":memory,"changed_terms":changed,"affected_indices":affected
    })))
}
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/subtitle/tasks/{task_id}/translation-context",
        put(save_context),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn context_delta_and_word_boundaries() {
        let old = json!({"characters":[{"name":"Alice","target_name":"爱丽丝"}]});
        let new = json!({"characters":[{"name":"Alice","target_name":"艾丽斯"}],
       "terminology":[{"source":"PCB","target":"印刷电路板"}]});
        assert_eq!(changed_source_terms(&old, &new), vec!["Alice", "PCB"]);
        assert!(contains_term("A PCB is present", "pcb"));
        assert!(!contains_term("PCBA", "pcb"));
        assert!(contains_term("电路板需要检修", "电路板"));
    }
}
