//! Original V1 terminal task resource cleanup, with additional fail-closed
//! protection for active native/legacy operations and shared objects.
//! The business tables remain tasks/assets/subtitles/app_settings.
use crate::{AppError, AppResult, api::AppState, storage::AssetRegistry};
use axum::{Json, Router, extract::State, routing::post};
use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{Postgres, Row, Transaction};
use std::{collections::HashSet, fs, io, path::Path};
use uuid::Uuid;

const LOCK: &str = "orchestrator.storage_resource_cleanup";
const PREFIXES: [&str; 7] = ["raw", "final", "sub", "work", "log", "meta", "dub"];
const MAX_TASKS: i64 = 32;
const MAX_FILES: usize = 1000;

fn enabled() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP")
            .ok()
            .as_deref(),
        Some("true" | "1")
    )
}
async fn acquire(state: &AppState) -> AppResult<String> {
    let owner = format!("rust:{}:{}", std::process::id(), Uuid::new_v4());
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("INSERT INTO app_settings(key,value_json) VALUES($1,'{}'::jsonb) ON CONFLICT(key) DO NOTHING")
        .bind(LOCK).execute(&mut *tx).await?;
    let current: Value =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(LOCK)
            .fetch_one(&mut *tx)
            .await?;
    let someone = current
        .get("lock_owner")
        .and_then(Value::as_str)
        .unwrap_or("");
    let until = current
        .get("lock_until")
        .and_then(Value::as_str)
        .and_then(|text| DateTime::parse_from_rfc3339(text).ok());
    if !someone.is_empty() && until.is_some_and(|t| t > Utc::now()) {
        return Err(AppError::Conflict(
            "storage resource cleanup already running".into(),
        ));
    }
    let ttl = std::env::var("STORAGE_RESOURCE_CLEANUP_LOCK_TTL_SECONDS")
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(3600)
        .clamp(60, 7200);
    sqlx::query("UPDATE app_settings SET value_json=jsonb_set(jsonb_set(value_json,'{lock_owner}',to_jsonb($2::text)),'{lock_until}',to_jsonb($3::text)),updated_at=now() WHERE key=$1")
        .bind(LOCK).bind(&owner).bind((Utc::now()+Duration::seconds(ttl)).to_rfc3339())
        .execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(owner)
}
async fn release(state: &AppState, owner: &str) -> AppResult<()> {
    sqlx::query("UPDATE app_settings SET value_json=value_json-'lock_owner'-'lock_until',updated_at=now() WHERE key=$1 AND value_json->>'lock_owner'=$2")
        .bind(LOCK).bind(owner).execute(&state.db.pool).await?;
    Ok(())
}
fn resource_key(key: &str, task: Uuid) -> bool {
    let mut parts = key.split('/');
    let (Some(kind), Some(id), Some(_)) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    PREFIXES.contains(&kind) && Uuid::parse_str(id).ok() == Some(task)
}
fn walk(root: &Path, current: &Path, keys: &mut HashSet<String>) -> io::Result<()> {
    let metadata = fs::symlink_metadata(current)?;
    if metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "storage symlink in terminal resources",
        ));
    }
    if metadata.is_file() {
        let relative = current
            .strip_prefix(root)
            .map_err(|_| io::Error::other("storage path outside root"))?;
        let key = relative
            .to_str()
            .ok_or_else(|| io::Error::other("invalid unicode storage key"))?;
        AssetRegistry::checked_key(key).map_err(|_| io::Error::other("invalid storage key"))?;
        keys.insert(key.to_owned());
        if keys.len() > MAX_FILES {
            return Err(io::Error::other("too many files in terminal cleanup batch"));
        }
    } else if metadata.is_dir() {
        for entry in fs::read_dir(current)? {
            walk(root, &entry?.path(), keys)?;
        }
    } else {
        return Err(io::Error::other(
            "unsupported object type in storage cleanup",
        ));
    }
    Ok(())
}
fn scan_orphans(root: &Path, task: Uuid) -> io::Result<HashSet<String>> {
    let mut keys = HashSet::new();
    // Reject a replaced or symlinked storage root. Only task-specific original
    // prefixes are walked; never traverse or remove unrelated objects.
    let metadata = fs::symlink_metadata(root)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(io::Error::other("unsafe storage root"));
    }
    for prefix in PREFIXES {
        let parent = root.join(prefix);
        match fs::symlink_metadata(&parent) {
            Err(err) if err.kind() == io::ErrorKind::NotFound => continue,
            Err(err) => return Err(err),
            Ok(meta) if !meta.is_dir() || meta.file_type().is_symlink() => {
                return Err(io::Error::other("unsafe original storage namespace"));
            }
            _ => (),
        }
        let task_dir = parent.join(task.to_string());
        match fs::symlink_metadata(&task_dir) {
            Err(err) if err.kind() == io::ErrorKind::NotFound => continue,
            Err(err) => return Err(err),
            Ok(_) => walk(root, &task_dir, &mut keys)?,
        }
    }
    if !keys.iter().all(|key| resource_key(key, task)) {
        return Err(io::Error::other("resource namespace does not match task"));
    }
    Ok(keys)
}
async fn unsafe_to_delete(tx: &mut Transaction<'_, Postgres>, task: Uuid) -> AppResult<bool> {
    // A canceled task with stopped_status is resumable, not terminal.
    // Check all original and native in-flight work before removing any media.
    let busy:bool=sqlx::query_scalar(r#"
      SELECT EXISTS(SELECT 1 FROM subtitle_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))
       OR EXISTS(SELECT 1 FROM render_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))
       OR EXISTS(SELECT 1 FROM dubbing_jobs WHERE task_id=$1 AND
         (status IN ('queued','preparing','synthesizing','aligning','mixing','rendering',
                     'pause_requested','cancel_requested','paused','preview_ready','needs_review')))
       OR EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND state IN ('submitted','submitting','running','cancel_requested'))
       OR EXISTS(SELECT 1 FROM operation_inbox WHERE task_id=$1 AND status IN ('pending','awaiting','running','processing','unknown'))
       OR EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND
          (state::text IN ('draft','submitting','unknown') OR
           (state::text='submitted' AND response_json->>'runtime'='rust'
            AND response_json->>'receipt_verified'='false'
            AND NULLIF(external_id,'') IS NULL AND NULLIF(external_url,'') IS NULL)))
       OR EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown'))
       OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND state IN ('pending','running','awaiting','unknown'))
       OR EXISTS(SELECT 1 FROM storage_object_refs WHERE owner_type='task' AND owner_id=$1)
       OR EXISTS(SELECT 1 FROM playout_asset_links p JOIN assets a ON a.id=p.asset_id WHERE a.task_id=$1)
       OR EXISTS(SELECT 1 FROM render_executions e JOIN assets a ON e.uploaded_asset_id=a.id WHERE a.task_id=$1)
     "#).bind(task).fetch_one(&mut **tx).await?;
    Ok(busy)
}
async fn externally_referenced(
    tx: &mut Transaction<'_, Postgres>,
    key: &str,
    task: Uuid,
) -> AppResult<bool> {
    let shared: bool = sqlx::query_scalar(
        r#"
        SELECT EXISTS(SELECT 1 FROM assets WHERE storage_key=$1 AND task_id<>$2)
           OR EXISTS(SELECT 1 FROM subtitles WHERE storage_key=$1 AND task_id<>$2)
           OR EXISTS(SELECT 1 FROM storage_object_refs WHERE storage_key=$1)
    "#,
    )
    .bind(key)
    .bind(task)
    .fetch_one(&mut **tx)
    .await?;
    Ok(shared)
}
async fn pending(tx: &mut Transaction<'_, Postgres>, key: &str) -> AppResult<()> {
    let digest = Sha256::digest(format!("filesystem\0{key}").as_bytes());
    let setting = format!("storage.pending_delete.{}", hex::encode(digest));
    sqlx::query("INSERT INTO app_settings(key,value_json) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=now()")
        .bind(setting).bind(json!({"storage_key":key,"bucket":"filesystem",
          "reason":"terminal_task_resource_cleanup","requested_at":Utc::now().to_rfc3339()}))
        .execute(&mut **tx).await?;
    Ok(())
}
#[derive(Default)]
struct Summary {
    matched_tasks: i64,
    matched_assets: i64,
    matched_subtitles: i64,
    deleted_assets: i64,
    deleted_subtitles: i64,
    deleted_objects: i64,
    pending_objects: i64,
}
impl Summary {
    fn into_json(self) -> Value {
        json!({"matched_tasks":self.matched_tasks,"matched_assets":self.matched_assets,
            "matched_subtitles":self.matched_subtitles,"deleted_assets":self.deleted_assets,
            "deleted_subtitles":self.deleted_subtitles,"deleted_objects":self.deleted_objects,
            "pending_objects":self.pending_objects})
    }
}
async fn cleanup(state: &AppState) -> AppResult<Value> {
    let root = state.config.storage_root.clone();
    // Never create the storage root as a side effect of a cleanup request.
    let root = fs::canonicalize(&root)?;
    let registry = AssetRegistry::new(state.db.clone(), root.clone());
    let mut summary = Summary::default();
    // Keyset pages avoid starvation: repeatedly reprocessing the first 32
    // already-cleaned terminal tasks must not hide a later orphan-heavy task.
    let mut cursor: Option<(DateTime<Utc>, Uuid)> = None;
    let mut scanned = 0usize;
    'scan_pages: loop {
        if scanned >= 20_000 || summary.matched_tasks >= MAX_TASKS {
            break;
        }
        let after_time = cursor.map(|(time, _)| time);
        let after_id = cursor.map(|(_, id)| id);
        let rows=sqlx::query(
        "SELECT t.id,t.updated_at FROM tasks t
         WHERE t.status::text IN ('published','failed','canceled')
         AND (t.status::text<>'canceled' OR t.stopped_status IS NULL)
         AND ($1::timestamptz IS NULL OR (t.updated_at,t.id)>($1::timestamptz,$2::uuid))
         AND NOT EXISTS(SELECT 1 FROM dubbing_jobs d WHERE d.task_id=t.id
             AND d.status IN ('queued','preparing','synthesizing','aligning','mixing','rendering',
                 'pause_requested','cancel_requested','paused','preview_ready','needs_review'))
         AND NOT EXISTS(SELECT 1 FROM subtitle_jobs s WHERE s.task_id=t.id AND s.status::text IN('queued','running'))
         AND NOT EXISTS(SELECT 1 FROM render_jobs r WHERE r.task_id=t.id AND r.status::text IN('queued','running'))
         AND NOT EXISTS(SELECT 1 FROM operation_inbox o WHERE o.task_id=t.id AND o.status IN('pending','awaiting','running','processing','unknown'))
         AND NOT EXISTS(SELECT 1 FROM vr2_workflows w WHERE w.task_id=t.id AND w.state IN('running','paused','unknown'))
         ORDER BY t.updated_at,t.id LIMIT 128"
        ).bind(after_time).bind(after_id).fetch_all(&state.db.pool).await?;
        if rows.is_empty() {
            break;
        }
        for candidate in rows {
            let task: Uuid = candidate.try_get("id")?;
            let updated: DateTime<Utc> = candidate.try_get("updated_at")?;
            cursor = Some((updated, task));
            scanned += 1;
            if scanned > 20_000 || summary.matched_tasks >= MAX_TASKS {
                break 'scan_pages;
            }
            let mut tx = state.db.pool.begin().await?;
            let row = sqlx::query(
                "SELECT status::text AS status,stopped_status::text AS stopped
           FROM tasks WHERE id=$1 FOR UPDATE SKIP LOCKED",
            )
            .bind(task)
            .fetch_optional(&mut *tx)
            .await?;
            let Some(row) = row else { continue };
            let status: String = row.try_get("status")?;
            let stopped: Option<String> = row.try_get("stopped")?;
            if !matches!(status.as_str(), "published" | "failed" | "canceled")
                || (status == "canceled" && stopped.is_some())
                || unsafe_to_delete(&mut tx, task).await?
            {
                continue;
            }
            let assets =
                sqlx::query("SELECT id,storage_key FROM assets WHERE task_id=$1 FOR UPDATE")
                    .bind(task)
                    .fetch_all(&mut *tx)
                    .await?;
            let subtitles =
                sqlx::query("SELECT id,storage_key FROM subtitles WHERE task_id=$1 FOR UPDATE")
                    .bind(task)
                    .fetch_all(&mut *tx)
                    .await?;
            // Recursive filesystem enumeration is blocking; run it off the API
            // reactor. If there is a suspicious link or a huge task, retain all
            // database references and skip its physical deletion.
            let root_copy = root.clone();
            let listed = tokio::task::spawn_blocking(move || scan_orphans(&root_copy, task))
                .await
                .map_err(|err| {
                    AppError::Unavailable(format!("terminal file scan failed: {err}"))
                })?;
            let Ok(mut keys) = listed else {
                tracing::warn!(%task,"terminal resource scan blocked by unsafe path or IO error");
                continue;
            };
            for row in assets.iter().chain(subtitles.iter()) {
                let key: String = row.try_get("storage_key")?;
                if AssetRegistry::checked_key(&key).is_err() || key.len() > 1024 {
                    keys.clear();
                    tracing::warn!(%task,"invalid storage key blocked terminal cleanup");
                    break;
                }
                keys.insert(key);
            }
            if keys.is_empty() && (!assets.is_empty() || !subtitles.is_empty()) {
                continue;
            }
            if keys.is_empty() && assets.is_empty() && subtitles.is_empty() {
                continue;
            }
            if keys.len() > MAX_FILES {
                tracing::warn!(%task,"terminal resource cleanup exceeds per-task file cap");
                continue;
            }
            let mut protected = false;
            for key in &keys {
                if externally_referenced(&mut tx, key, task).await? {
                    protected = true;
                    break;
                }
                // Fails closed before any unlink if a symlink exists in the path.
                if registry.resolve_existing(key).await.is_err() {
                    protected = true;
                    break;
                }
            }
            if protected {
                tracing::warn!(%task,"terminal resource has shared refs or unsafe path; retained");
                continue;
            }
            let mut task_deleted = 0;
            let mut task_pending = 0;
            let mut ordered = keys.into_iter().collect::<Vec<_>>();
            ordered.sort();
            for key in &ordered {
                let path = registry.resolve_existing(key).await?;
                match tokio::fs::remove_file(&path).await {
                    Ok(()) => task_deleted += 1,
                    Err(err) if err.kind() == io::ErrorKind::NotFound => task_deleted += 1,
                    Err(err) => {
                        tracing::warn!(%task,storage_key=%key,error=%err,"terminal file delete deferred");
                        pending(&mut tx, key).await?;
                        task_pending += 1;
                    }
                }
            }
            let deleted_subs = sqlx::query("DELETE FROM subtitles WHERE task_id=$1")
                .bind(task)
                .execute(&mut *tx)
                .await?
                .rows_affected() as i64;
            let deleted_assets = sqlx::query("DELETE FROM assets WHERE task_id=$1")
                .bind(task)
                .execute(&mut *tx)
                .await?
                .rows_affected() as i64;
            tx.commit().await?;
            summary.matched_tasks += 1;
            summary.matched_assets += assets.len() as i64;
            summary.matched_subtitles += subtitles.len() as i64;
            summary.deleted_assets += deleted_assets;
            summary.deleted_subtitles += deleted_subs;
            summary.deleted_objects += task_deleted;
            summary.pending_objects += task_pending;
        }
    }
    Ok(summary.into_json())
}
async fn cleanup_terminal(State(state): State<AppState>) -> AppResult<Json<Value>> {
    // Explicit opt-in because unlike workdir cleanup this removes original
    // business-visible assets and orphan media. Production defaults safe.
    if !enabled() {
        return Err(AppError::Conflict(
            "terminal storage cleanup disabled; enable explicitly".into(),
        ));
    }
    let owner = acquire(&state).await?;
    let result = cleanup(&state).await;
    release(&state, &owner).await?;
    result.map(Json)
}
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/maintenance/storage/cleanup-terminal",
        post(cleanup_terminal),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prefix_scope_is_exact() {
        let task = Uuid::new_v4();
        assert!(resource_key(&format!("raw/{task}/video.mp4"), task));
        assert!(!resource_key(&format!("raw/{task}2/video.mp4"), task));
        assert!(!resource_key(&format!("other/{task}/video.mp4"), task));
        assert!(!resource_key(&format!("raw/{task}"), task));
    }
}
