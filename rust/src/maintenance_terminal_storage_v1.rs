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
async fn acquire(db: &crate::Database) -> AppResult<String> {
    let owner = format!("rust:{}:{}", std::process::id(), Uuid::new_v4());
    let mut tx = db.pool.begin().await?;
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
async fn release(db: &crate::Database, owner: &str) -> AppResult<()> {
    sqlx::query("UPDATE app_settings SET value_json=value_json-'lock_owner'-'lock_until',updated_at=now() WHERE key=$1 AND value_json->>'lock_owner'=$2")
        .bind(LOCK).bind(owner).execute(&db.pool).await?;
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
    if crate::storage::publication_cleanup_blocked(tx, task).await? {
        return Ok(true);
    }
    let busy:bool=sqlx::query_scalar(r#"
      SELECT EXISTS(SELECT 1 FROM subtitle_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))
       OR EXISTS(SELECT 1 FROM render_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))
       OR EXISTS(SELECT 1 FROM render_executions WHERE task_id=$1 AND runtime_engine='rust-v2' AND state IN ('running','uploading','canceled','paused') AND lease_until>now())
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
           OR EXISTS(SELECT 1 FROM storage_objects WHERE storage_key=$1)
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
// Only claim original filesystem records with an identifiable task owner.
// Native v2 objects belong to the separate registry collector.
fn pending_identity(setting: &str, value: &Value) -> Option<(Uuid, String, DateTime<Utc>)> {
    let key = value.get("storage_key")?.as_str()?;
    AssetRegistry::checked_key(key).ok()?;
    let bucket = match value.get("bucket") {
        None | Some(Value::Null) => "",
        Some(Value::String(s)) if s.is_empty() || s == "filesystem" => s.as_str(),
        _ => return None,
    };
    let expected = format!(
        "storage.pending_delete.{}",
        hex::encode(Sha256::digest(format!("{bucket}\0{key}").as_bytes()))
    );
    if setting != expected {
        return None;
    }
    let task = Uuid::parse_str(key.split('/').nth(1)?).ok()?;
    if !resource_key(key, task) {
        return None;
    }
    let requested = DateTime::parse_from_rfc3339(value.get("requested_at")?.as_str()?)
        .ok()?
        .with_timezone(&Utc);
    if requested > Utc::now() {
        return None;
    }
    Some((task, key.to_owned(), requested))
}

/// Retry the original durable pending-delete queue without traversing storage.
/// Only opt in after the Python retention worker has been retired.
pub async fn retry_pending_deletes(registry: &AssetRegistry, limit: i64) -> AppResult<u64> {
    if !enabled() {
        return Ok(0);
    }
    let candidates = sqlx::query_as::<_, (String, Value)>(
        "SELECT key,value_json FROM app_settings
         WHERE starts_with(key,'storage.pending_delete.') AND updated_at<=now()-interval '5 minutes'
         ORDER BY updated_at,key LIMIT $1",
    )
    .bind(limit.clamp(1, 200))
    .fetch_all(&registry.db.pool)
    .await?;
    let mut removed = 0;
    for (setting, snapshot) in candidates {
        let mut tx = registry.db.pool.begin().await?;
        let identity = pending_identity(&setting, &snapshot);
        // Match terminal cleanup's task -> settings lock order.
        let parent: Option<(String, Option<String>)> = if let Some((task, _, _)) = &identity {
            sqlx::query_as(
                "SELECT status::text,stopped_status::text FROM tasks WHERE id=$1 FOR UPDATE SKIP LOCKED",
            )
            .bind(task)
            .fetch_optional(&mut *tx)
            .await?
        } else {
            None
        };
        let current: Option<Value> = sqlx::query_scalar(
            "SELECT value_json FROM app_settings WHERE key=$1 AND updated_at<=now()-interval '5 minutes' FOR UPDATE SKIP LOCKED",
        )
        .bind(&setting)
        .fetch_optional(&mut *tx)
        .await?;
        if current.as_ref() != Some(&snapshot) {
            continue;
        }
        let mut completed = false;
        if let (Some((task, key, requested)), Some((status, stopped))) = (identity, parent) {
            if crate::storage::terminal_resources_releasable(&status, stopped.as_deref())
                && !unsafe_to_delete(&mut tx, task).await?
            {
                // A short nonblocking table lock fences cross-task references
                // too. Never wait behind a writer while holding a task lock.
                if let Err(error) = sqlx::query(
                    "LOCK TABLE assets, subtitles, storage_objects, storage_object_refs IN SHARE MODE NOWAIT",
                )
                .execute(&mut *tx)
                .await
                {
                    if error.as_database_error().and_then(|e| e.code()).as_deref() == Some("55P03") {
                        continue;
                    }
                    return Err(error.into());
                }
                let referenced: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM assets WHERE storage_key=$1)
                     OR EXISTS(SELECT 1 FROM subtitles WHERE storage_key=$1)
                     OR EXISTS(SELECT 1 FROM storage_objects WHERE storage_key=$1)
                     OR EXISTS(SELECT 1 FROM storage_object_refs WHERE storage_key=$1)",
                )
                .bind(&key)
                .fetch_one(&mut *tx)
                .await?;
                if !referenced {
                    let deletion: AppResult<bool> = async {
                        let path = registry.resolve_existing(&key).await?;
                        match tokio::fs::symlink_metadata(&path).await {
                            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(true),
                            Err(err) => Err(err.into()),
                            Ok(meta) if meta.is_file()
                                && DateTime::<Utc>::from(meta.modified()?) <= requested =>
                            {
                                match tokio::fs::remove_file(path).await {
                                    Ok(()) => Ok(true),
                                    Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(true),
                                    Err(err) => Err(err.into()),
                                }
                            }
                            // Never delete a replacement created after the old
                            // request, directories, or non-regular objects.
                            Ok(_) => Ok(false),
                        }
                    }
                    .await;
                    match deletion {
                        Ok(done) => completed = done,
                        Err(error) => tracing::warn!(%task,%key,%error,"legacy pending delete deferred"),
                    }
                }
            }
        }
        if completed {
            sqlx::query("DELETE FROM app_settings WHERE key=$1")
                .bind(&setting)
                .execute(&mut *tx)
                .await?;
            removed += 1;
        } else {
            // Rotate blocked/malformed records behind other due requests.
            // Preserve their original payload for operator reconciliation.
            sqlx::query("UPDATE app_settings SET updated_at=now() WHERE key=$1")
                .bind(&setting)
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
    }
    Ok(removed)
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
            if !crate::storage::terminal_resources_releasable(&status, stopped.as_deref())
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
            // Fence cross-task reference writes before the per-key decision.
            sqlx::query("LOCK TABLE assets, subtitles, storage_objects, storage_object_refs IN SHARE ROW EXCLUSIVE MODE NOWAIT")
                .execute(&mut *tx).await?;
            // A mixed-layout task is not indivisible: leave registry-owned
            // and shared keys for their own collectors, without blocking
            // eligible original-layout files or deleting their business rows.
            let mut ordered=Vec::new();
            for key in keys {
                // Nonstandard keys are permitted only from the exact original
                // asset/subtitle rows, as in Python. Known foreign task paths
                // and every registry-managed object remain protected.
                let explicit=assets.iter().chain(subtitles.iter()).any(|r|r.try_get::<String,_>("storage_key").ok().as_deref()==Some(key.as_str()));
                let known_prefix=key.split('/').next().is_some_and(|p|PREFIXES.contains(&p));
                if key.starts_with("v2/") || (known_prefix && !resource_key(&key,task)) || (!resource_key(&key,task) && !explicit) || externally_referenced(&mut tx,&key,task).await? || registry.resolve_existing(&key).await.is_err(){continue;}
                ordered.push(key);
            }
            if ordered.is_empty(){continue;}
            let mut task_deleted = 0;
            let mut task_pending = 0;
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
            let deleted_subs = sqlx::query("DELETE FROM subtitles WHERE task_id=$1 AND storage_key=ANY($2::text[])")
                .bind(task).bind(&ordered)
                .execute(&mut *tx)
                .await?
                .rows_affected() as i64;
            let deleted_assets = sqlx::query("DELETE FROM assets WHERE task_id=$1 AND storage_key=ANY($2::text[])")
                .bind(task).bind(&ordered)
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
// Scheduled legacy cleanup is intentionally a second opt-in: an operator
// who enabled manual cleanup or pending-delete retries did not necessarily
// consent to periodic deletion of all eligible historical task media.
pub fn scheduled_enabled() -> bool {
    enabled() && matches!(std::env::var("VIDEOROLL_V2_ALLOW_V1_SCHEDULED_CLEANUP").ok().as_deref(), Some("true"|"1"))
}
const SCAN_LIMIT: i64 = 128;
fn scan_interval() -> i64 {
    std::env::var("STORAGE_CLEANUP_INTERVAL_SECONDS").ok()
        .and_then(|v|v.parse::<i64>().ok()).unwrap_or(3600).clamp(60,86400)
}
fn timestamp(value: &Value, key: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value.get(key)?.as_str()?).ok().map(|t|t.with_timezone(&Utc))
}
fn scan_cursor(value: &Value) -> Option<(DateTime<Utc>,Uuid)> {
    Some((timestamp(value,"rust_scan_after_at")?,Uuid::parse_str(value.get("rust_scan_after_id")?.as_str()?).ok()?))
}

/// Lock the original maintenance lease, then refresh it before every task.
/// No destructive action proceeds after a competing cleanup took ownership.
async fn renew_scan(tx:&mut Transaction<'_,Postgres>, owner:&str) -> AppResult<()> {
    let current:Value=sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
        .bind(LOCK).fetch_one(&mut **tx).await?;
    if current.get("lock_owner").and_then(Value::as_str)!=Some(owner)
        || timestamp(&current,"lock_until").is_none_or(|until|until<=Utc::now()) {
        return Err(AppError::Conflict("legacy resource scan lease lost".into()));
    }
    let ttl=std::env::var("STORAGE_RESOURCE_CLEANUP_LOCK_TTL_SECONDS").ok()
        .and_then(|v|v.parse::<i64>().ok()).unwrap_or(3600).clamp(60,7200);
    sqlx::query("UPDATE app_settings SET value_json=jsonb_set(value_json,'{lock_until}',to_jsonb($2::text)),updated_at=now() WHERE key=$1")
        .bind(LOCK).bind((Utc::now()+Duration::seconds(ttl)).to_rfc3339()).execute(&mut **tx).await?;
    Ok(())
}

/// Bounded original namespace only. Unlike explicit manual purge, a recent
/// replacement, registered object, noncanonical key or failed unlink is
/// retained, including its original database rows.
async fn scheduled_task(registry:&AssetRegistry, owner:&str, task:Uuid, failed:std::time::Duration) -> AppResult<u64> {
    let mut tx=registry.db.pool.begin().await?;
    renew_scan(&mut tx,owner).await?;
    let parent:Option<(String,Option<String>,DateTime<Utc>)>=sqlx::query_as(
        "SELECT upper(status::text),stopped_status::text,updated_at FROM tasks WHERE id=$1 FOR UPDATE SKIP LOCKED")
        .bind(task).fetch_optional(&mut *tx).await?;
    let Some((status,stopped,updated))=parent else {return Ok(0)};
    let (full,_)=crate::storage::deletion_policy(&mut tx,task,&status,stopped.as_deref(),updated,failed).await?;
    if !full || unsafe_to_delete(&mut tx,task).await? {return Ok(0)}
    let root=registry.root.clone();
    let listed=tokio::task::spawn_blocking(move||scan_orphans(&root,task)).await
        .map_err(|e|AppError::Unavailable(format!("legacy scan join failed: {e}")))?;
    let Ok(mut keys)=listed else {
        tracing::warn!(%task,"unsafe or oversized original directory retained");
        return Ok(0)
    };
    // Fence cross-task changes only after enumeration; never wait for a
    // writer while holding the parent. Registry-managed objects are excluded.
    sqlx::query("LOCK TABLE assets, subtitles, storage_objects, storage_object_refs IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        .execute(&mut *tx).await?;
    let referenced:Vec<String>=sqlx::query_scalar(
        "SELECT storage_key FROM assets WHERE task_id=$1 UNION SELECT storage_key FROM subtitles WHERE task_id=$1")
        .bind(task).fetch_all(&mut *tx).await?;
    for key in referenced {
        if resource_key(&key,task) && AssetRegistry::checked_key(&key).is_ok() {keys.insert(key);}
    }
    if keys.len()>MAX_FILES {return Ok(0)}
    let mut keys:Vec<String>=keys.into_iter().collect();
    keys.sort();
    let mut removed=0;
    for key in keys {
        if externally_referenced(&mut tx,&key,task).await? {continue}
        // Do not bypass the stricter pending-delete identity/mtime checks.
        let queued:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_settings WHERE starts_with(key,'storage.pending_delete.') AND value_json->>'storage_key'=$1)")
            .bind(&key).fetch_one(&mut *tx).await?;
        if queued {continue}
        let path=match registry.resolve_existing(&key).await {
            Ok(p)=>p,Err(error)=>{tracing::warn!(%task,%key,%error,"legacy path retained");continue}
        };
        let present=match tokio::fs::symlink_metadata(&path).await {
            Ok(meta) if meta.is_file() && meta.modified().is_ok_and(|t|DateTime::<Utc>::from(t)<=updated)=>true,
            Err(e) if e.kind()==io::ErrorKind::NotFound=>false,
            _=>continue,
        };
        sqlx::query("SAVEPOINT original_resource").execute(&mut *tx).await?;
        let delete_rows:AppResult<()> = async {
            sqlx::query("DELETE FROM subtitles WHERE task_id=$1 AND storage_key=$2").bind(task).bind(&key).execute(&mut *tx).await?;
            sqlx::query("DELETE FROM assets WHERE task_id=$1 AND storage_key=$2").bind(task).bind(&key).execute(&mut *tx).await?;
            Ok(())
        }.await;
        if let Err(error)=delete_rows {
            sqlx::query("ROLLBACK TO SAVEPOINT original_resource").execute(&mut *tx).await?;
            sqlx::query("RELEASE SAVEPOINT original_resource").execute(&mut *tx).await?;
            tracing::warn!(%task,%key,%error,"legacy relational references retained");
            continue;
        }
        if present {
            match tokio::fs::remove_file(&path).await {
                Ok(())=>{},
                Err(e) if e.kind()==io::ErrorKind::NotFound=>{},
                Err(error)=>{
                    sqlx::query("ROLLBACK TO SAVEPOINT original_resource").execute(&mut *tx).await?;
                    sqlx::query("RELEASE SAVEPOINT original_resource").execute(&mut *tx).await?;
                    tracing::warn!(%task,%key,%error,"legacy unlink failed; rows retained");
                    continue;
                }
            }
        }
        sqlx::query("RELEASE SAVEPOINT original_resource").execute(&mut *tx).await?;
        removed+=1;
    }
    tx.commit().await?;
    Ok(removed)
}

async fn scheduled_page(registry:&AssetRegistry, owner:&str, failed:std::time::Duration, force:bool) -> AppResult<u64> {
    let value:Value=sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(LOCK).fetch_one(&registry.db.pool).await?;
    if !force && timestamp(&value,"rust_scan_next_due_at").is_some_and(|t|t>Utc::now()) {return Ok(0)}
    let cursor=scan_cursor(&value);
    let now=Utc::now();
    let failed_before=now-Duration::seconds(failed.as_secs().clamp(3600,3650*86400) as i64);
    let published_before=crate::operations_v1::published_retention(&registry.db).await?
        .map(|ttl|now-Duration::seconds(ttl.as_secs() as i64));
    let rows:Vec<(DateTime<Utc>,Uuid)>=sqlx::query_as(
        "SELECT updated_at,id FROM tasks WHERE
          ((status::text='failed' AND updated_at<=$1)
          OR ((status::text='published' OR (status::text='canceled' AND stopped_status IS NULL))
              AND $2::timestamptz IS NOT NULL AND updated_at<=$2))
          AND ($3::timestamptz IS NULL OR (updated_at,id)>($3,$4::uuid))
          ORDER BY updated_at,id LIMIT $5")
        .bind(failed_before).bind(published_before).bind(cursor.map(|c|c.0)).bind(cursor.map(|c|c.1))
        .bind(SCAN_LIMIT).fetch_all(&registry.db.pool).await?;
    let mut next=None;
    let mut removed=0;
    let mut processed=0;
    let mut matched=0;
    for &(updated,task) in &rows {
        if matched>=MAX_TASKS {break}
        match scheduled_task(registry,owner,task,failed).await {
            Ok(n)=>{removed+=n;if n>0{matched+=1}},
            Err(AppError::Conflict(detail))=>return Err(AppError::Conflict(detail)),
            Err(error)=>tracing::warn!(%task,%error,"legacy scheduled task deferred"),
        }
        processed+=1;
        next=Some((updated,task));
    }
    // Resume after this page across restarts. Once the end is reached, wrap
    // back to the start on a subsequent sweep, so skipped locks and new
    // tasks that sort before the cursor eventually get another chance.
    if processed==rows.len() && (rows.len() as i64)<SCAN_LIMIT {next=None}
    let mut tx=registry.db.pool.begin().await?;
    renew_scan(&mut tx,owner).await?;
    sqlx::query("UPDATE app_settings SET value_json=(value_json-'rust_scan_after_at'-'rust_scan_after_id') || $2::jsonb,updated_at=now() WHERE key=$1")
        .bind(LOCK).bind(json!({
            "rust_scan_after_at":next.map(|c|c.0.to_rfc3339()),
            "rust_scan_after_id":next.map(|c|c.1),
            "rust_scan_next_due_at":(Utc::now()+Duration::seconds(scan_interval())).to_rfc3339()
        })).execute(&mut *tx).await?;
    tx.commit().await?;
    tracing::info!(scanned=processed,removed,more=next.is_some(),"legacy retention page complete");
    Ok(removed)
}

pub async fn scheduled_tick(registry:&AssetRegistry, failed:std::time::Duration, force:bool)->AppResult<u64> {
    if !scheduled_enabled(){return Ok(0)}
    if !force {
        let value:Option<Value>=sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(LOCK).fetch_optional(&registry.db.pool).await?;
        if value.as_ref().and_then(|v|timestamp(v,"rust_scan_next_due_at")).is_some_and(|t|t>Utc::now()) {
            return Ok(0)
        }
    }
    let root=fs::canonicalize(&registry.root)?;
    let registry=AssetRegistry::new(registry.db.clone(),root);
    let owner=match acquire(&registry.db).await {
        Ok(owner)=>owner,
        Err(AppError::Conflict(_))=>return Ok(0),
        Err(error)=>return Err(error),
    };
    let result=scheduled_page(&registry,&owner,failed,force).await;
    release(&registry.db,&owner).await?;
    result
}

async fn cleanup_terminal(State(state): State<AppState>) -> AppResult<Json<Value>> {
    // Explicit opt-in because unlike workdir cleanup this removes original
    // business-visible assets and orphan media. Production defaults safe.
    if !enabled() {
        return Err(AppError::Conflict(
            "terminal storage cleanup disabled; enable explicitly".into(),
        ));
    }
    let owner = acquire(&state.db).await?;
    let result = cleanup(&state).await;
    release(&state.db, &owner).await?;
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
    fn pending_identity_checks_original_hash_and_namespace() {
        let task = Uuid::new_v4();
        let key = format!("raw/{task}/video.mp4");
        let requested = (Utc::now() - Duration::minutes(10)).to_rfc3339();
        for bucket in [json!(null), json!("filesystem")] {
            let name = bucket.as_str().unwrap_or("");
            let setting = format!(
                "storage.pending_delete.{}",
                hex::encode(Sha256::digest(format!("{name}\0{key}").as_bytes()))
            );
            let value = json!({"storage_key":key,"bucket":bucket,"requested_at":requested});
            assert_eq!(pending_identity(&setting, &value).unwrap().0, task);
            assert!(pending_identity("storage.pending_delete.fake", &value).is_none());
        }
        for key in [format!("v2/raw/{task}/video.mp4"), format!("raw/{task}/../x"), format!("raw/{task}")] {
            let setting = format!("storage.pending_delete.{}", hex::encode(Sha256::digest(format!("filesystem\0{key}").as_bytes())));
            assert!(pending_identity(&setting,&json!({"storage_key":key,"bucket":"filesystem","requested_at":requested})).is_none());
        }
    }
    #[test]
    fn prefix_scope_is_exact() {
        let task = Uuid::new_v4();
        assert!(resource_key(&format!("raw/{task}/video.mp4"), task));
        assert!(!resource_key(&format!("raw/{task}2/video.mp4"), task));
        assert!(!resource_key(&format!("other/{task}/video.mp4"), task));
        assert!(!resource_key(&format!("raw/{task}"), task));
    }
}
