//! Legacy workdir visibility against the original job/task tables.
//! This endpoint is read-only: listing a directory never authorizes deletion.
use crate::{AppError, AppResult, api::AppState};
use axum::{Json, Router, extract::State, routing::get};
use chrono::{DateTime, Duration, Utc};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::Row;
use std::{
    collections::{HashMap, HashSet},
    fs, io,
    path::{Path, PathBuf},
    time::SystemTime,
};
use uuid::Uuid;

#[derive(Clone, Debug)]
struct DiskEntry {
    kind: &'static str,
    owner_id: String,
    rel_path: String,
    size_bytes: u64,
    modified_at: DateTime<Utc>,
}
#[derive(Clone, Debug, Serialize)]
struct Entry {
    kind: &'static str,
    owner_id: String,
    rel_path: String,
    size_bytes: u64,
    modified_at: DateTime<Utc>,
    reclaimable: bool,
    reason: String,
    task_id: Option<Uuid>,
}
#[derive(Debug)]
struct Job {
    task_id: Option<Uuid>,
    status: String,
}
fn root_path() -> PathBuf {
    std::env::var_os("VIDEOROLL_V2_WORK_DIR")
        .or_else(|| std::env::var_os("WORK_DIR"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp/videoroll"))
}
fn scan_tree(path: &Path, total: &mut u64, latest: &mut SystemTime) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if meta.file_type().is_symlink() {
        return Ok(());
    }
    if let Ok(modified) = meta.modified() {
        *latest = (*latest).max(modified);
    }
    if meta.is_file() {
        *total = total.saturating_add(meta.len());
    } else if meta.is_dir() {
        for child in fs::read_dir(path)? {
            let child = child?;
            // Never follow symlinks, even when a former worker left them in a
            // workdir. An unreadable entry cannot become deletion eligibility.
            scan_tree(&child.path(), total, latest)?;
        }
    }
    Ok(())
}

// Targeted rescan: cleanup must not recurse through every other video
// directory for each candidate in a large backlog.
fn disk_one(root: &Path, relative: &str) -> io::Result<Option<DiskEntry>> {
    let parts = relative.split('/').collect::<Vec<_>>();
    if parts.len() != 2
        || parts[1].is_empty()
        || parts[1] == "."
        || parts[1] == ".."
        || parts[1].contains('\\')
    {
        return Ok(None);
    }
    let kind = match parts[0] {
        "subtitle" => "subtitle",
        "render" => "render",
        "youtube" => "youtube",
        _ => return Ok(None),
    };
    for path in [root.to_path_buf(), root.join(kind), root.join(relative)] {
        let meta = match fs::symlink_metadata(&path) {
            Ok(meta) => meta,
            Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(err) => return Err(err),
        };
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Ok(None);
        }
    }
    let full = root.join(relative);
    let mut size_bytes = 0;
    let mut modified = fs::symlink_metadata(&full)?.modified()?;
    scan_tree(&full, &mut size_bytes, &mut modified)?;
    Ok(Some(DiskEntry {
        kind,
        owner_id: parts[1].into(),
        rel_path: relative.into(),
        size_bytes,
        modified_at: DateTime::<Utc>::from(modified),
    }))
}
fn disk_entries(root: &Path) -> io::Result<Vec<DiskEntry>> {
    if !root.exists() {
        return Ok(Vec::new());
    }
    let meta = fs::symlink_metadata(root)?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "WORK_DIR must be a normal directory",
        ));
    }
    let mut found = Vec::new();
    for kind in ["subtitle", "render", "youtube"] {
        let dir = root.join(kind);
        let Ok(meta) = fs::symlink_metadata(&dir) else {
            continue;
        };
        if !meta.is_dir() || meta.file_type().is_symlink() {
            continue;
        }
        let mut children: Vec<_> = fs::read_dir(&dir)?.collect::<io::Result<Vec<_>>>()?;
        children.sort_by_key(|entry| entry.file_name());
        for child in children {
            let meta = fs::symlink_metadata(child.path())?;
            if !meta.is_dir() || meta.file_type().is_symlink() {
                continue;
            }
            if found.len() >= 20_000 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "workdir scan exceeds 20000 directories",
                ));
            }
            let mut size_bytes = 0;
            let mut modified = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
            scan_tree(&child.path(), &mut size_bytes, &mut modified)?;
            let owner_id = child.file_name().to_string_lossy().to_string();
            found.push(DiskEntry {
                kind,
                rel_path: format!("{kind}/{owner_id}"),
                owner_id,
                size_bytes,
                modified_at: DateTime::<Utc>::from(modified),
            });
        }
    }
    Ok(found)
}

fn classify(
    entry: DiskEntry,
    now: DateTime<Utc>,
    grace: Duration,
    subtitle: &HashMap<Uuid, Job>,
    render: &HashMap<Uuid, Job>,
    tasks: &HashSet<Uuid>,
    active: &HashSet<Uuid>,
) -> Entry {
    let recent = entry.modified_at >= now - grace;
    let owner = Uuid::parse_str(&entry.owner_id).ok();
    let mut reclaimable = false;
    let mut task_id = None;
    let reason = match entry.kind {
        "subtitle" | "render" => {
            let jobs = if entry.kind == "subtitle" {
                subtitle
            } else {
                render
            };
            match owner.and_then(|id| jobs.get(&id)) {
                Some(job) => {
                    task_id = job.task_id;
                    if matches!(job.status.as_str(), "queued" | "running") {
                        format!("{} job active ({})", entry.kind, job.status)
                    } else if job.task_id.is_some_and(|id| active.contains(&id)) {
                        "task active".into()
                    } else {
                        reclaimable = true;
                        format!("{} job {}", entry.kind, job.status)
                    }
                }
                None if owner.is_none() => {
                    reclaimable = !recent;
                    if recent {
                        format!("recent invalid {} directory", entry.kind)
                    } else {
                        format!("invalid {} job directory name", entry.kind)
                    }
                }
                None => {
                    reclaimable = !recent;
                    if recent {
                        format!("recent unknown {} job directory", entry.kind)
                    } else {
                        format!("{} job missing", entry.kind)
                    }
                }
            }
        }
        "youtube" => {
            task_id = owner;
            match owner {
                None => {
                    reclaimable = !recent;
                    if recent {
                        "recent invalid youtube temp directory"
                    } else {
                        "invalid youtube task directory name"
                    }
                    .into()
                }
                Some(id) if active.contains(&id) => "task active".into(),
                Some(id) if !tasks.contains(&id) => {
                    reclaimable = !recent;
                    if recent {
                        "recent unknown youtube temp directory"
                    } else {
                        "youtube task missing"
                    }
                    .into()
                }
                Some(_) if recent => "recent youtube temp directory".into(),
                Some(_) => {
                    reclaimable = true;
                    "youtube temp directory idle".into()
                }
            }
        }
        _ => unreachable!(),
    };
    Entry {
        kind: entry.kind,
        owner_id: entry.owner_id,
        rel_path: entry.rel_path,
        size_bytes: entry.size_bytes,
        modified_at: entry.modified_at,
        reclaimable,
        reason,
        task_id,
    }
}
fn to_response(root: &Path, mut entries: Vec<Entry>) -> Value {
    entries.sort_by(|a, b| {
        b.reclaimable
            .cmp(&a.reclaimable)
            .then_with(|| b.size_bytes.cmp(&a.size_bytes))
            .then_with(|| a.rel_path.cmp(&b.rel_path))
    });
    let total_bytes: u64 = entries.iter().map(|v| v.size_bytes).sum();
    let reclaimable_bytes: u64 = entries
        .iter()
        .filter(|v| v.reclaimable)
        .map(|v| v.size_bytes)
        .sum();
    let reclaimable_dirs = entries.iter().filter(|v| v.reclaimable).count();
    json!({"work_dir":root.to_string_lossy(),"scanned_dirs":entries.len(),
        "reclaimable_dirs":reclaimable_dirs,"total_bytes":total_bytes,
        "reclaimable_bytes":reclaimable_bytes,"deleted_dirs":0,
        "deleted_bytes":0,"deleted_paths":[],"errors":[],"entries":entries})
}
async fn scan(state: &AppState) -> AppResult<Value> {
    let root = root_path();
    let root_scan = root.clone();
    let found = tokio::task::spawn_blocking(move || disk_entries(&root_scan))
        .await
        .map_err(|err| AppError::Unavailable(format!("workdir scanner failed: {err}")))?
        .map_err(AppError::Io)?;
    classify_found(state, &root, found).await
}
async fn classify_found(state: &AppState, root: &Path, found: Vec<DiskEntry>) -> AppResult<Value> {
    if found.is_empty() {
        return Ok(to_response(&root, Vec::new()));
    }
    let mut subtitle_ids = Vec::new();
    let mut render_ids = Vec::new();
    let mut youtube_ids = Vec::new();
    for entry in &found {
        if let Ok(id) = Uuid::parse_str(&entry.owner_id) {
            match entry.kind {
                "subtitle" => subtitle_ids.push(id),
                "render" => render_ids.push(id),
                "youtube" => youtube_ids.push(id),
                _ => (),
            }
        }
    }
    let mut subtitles = HashMap::new();
    for row in
        sqlx::query("SELECT id,task_id,status::text AS status FROM subtitle_jobs WHERE id=ANY($1)")
            .bind(&subtitle_ids)
            .fetch_all(&state.db.pool)
            .await?
    {
        subtitles.insert(
            row.try_get::<Uuid, _>("id")?,
            Job {
                task_id: Some(row.try_get("task_id")?),
                status: row.try_get::<String, _>("status")?.to_ascii_lowercase(),
            },
        );
    }
    let mut renders = HashMap::new();
    for row in
        sqlx::query("SELECT id,task_id,status::text AS status FROM render_jobs WHERE id=ANY($1)")
            .bind(&render_ids)
            .fetch_all(&state.db.pool)
            .await?
    {
        renders.insert(
            row.try_get::<Uuid, _>("id")?,
            Job {
                task_id: row.try_get("task_id")?,
                status: row.try_get::<String, _>("status")?.to_ascii_lowercase(),
            },
        );
    }
    let tasks: HashSet<Uuid> = sqlx::query_scalar("SELECT id FROM tasks WHERE id=ANY($1)")
        .bind(&youtube_ids)
        .fetch_all(&state.db.pool)
        .await?
        .into_iter()
        .collect();
    let mut all_ids = youtube_ids;
    all_ids.extend(subtitles.values().filter_map(|j| j.task_id));
    all_ids.extend(renders.values().filter_map(|j| j.task_id));
    all_ids.sort_unstable();
    all_ids.dedup();
    let mut active: HashSet<Uuid> = HashSet::new();
    for id in sqlx::query_scalar::<_,Uuid>("SELECT DISTINCT task_id FROM subtitle_jobs WHERE task_id=ANY($1) AND status::text IN('queued','running')")
        .bind(&all_ids).fetch_all(&state.db.pool).await? { active.insert(id); }
    for id in sqlx::query_scalar::<_,Uuid>("SELECT DISTINCT task_id FROM render_jobs WHERE task_id=ANY($1) AND status::text IN('queued','running') AND task_id IS NOT NULL")
        .bind(&all_ids).fetch_all(&state.db.pool).await? { active.insert(id); }
    // Rust native workflows also own files; a completed legacy stage does
    // not mean it can be deleted while the replacement pipeline is active.
    for id in sqlx::query_scalar::<_,Uuid>("SELECT DISTINCT task_id FROM vr2_workflows WHERE task_id=ANY($1) AND state IN('running','paused','unknown')")
        .bind(&all_ids).fetch_all(&state.db.pool).await? { active.insert(id); }
    let download_ops: Vec<String> = all_ids
        .iter()
        .map(|id| format!("youtube-download:{id}"))
        .collect();
    for key in sqlx::query_scalar::<_,String>("SELECT operation_key FROM operation_inbox WHERE operation_key=ANY($1) AND status='processing' AND lease_until>now()")
        .bind(&download_ops).fetch_all(&state.db.pool).await? {
        if let Some(id)=key.strip_prefix("youtube-download:").and_then(|s|Uuid::parse_str(s).ok()) {
            active.insert(id);
        }
    }
    let grace_secs = std::env::var("WORKDIR_RECENT_GRACE_SECONDS")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(900)
        .clamp(0, 86400);
    let now = Utc::now();
    let entries = found
        .into_iter()
        .map(|v| {
            classify(
                v,
                now,
                Duration::seconds(grace_secs),
                &subtitles,
                &renders,
                &tasks,
                &active,
            )
        })
        .collect();
    Ok(to_response(&root, entries))
}

// Share the original Python app_settings row lease with V1 maintenance.
// The row-level lock ensures a Rust and Python cleanup cannot both acquire.
const LOCK_KEY: &str = "orchestrator.workdir_maintenance";
async fn acquire_lock(state: &AppState) -> AppResult<String> {
    let owner = format!("rust:{}:{}", std::process::id(), Uuid::new_v4());
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("INSERT INTO app_settings(key,value_json) VALUES($1,'{}'::jsonb) ON CONFLICT(key) DO NOTHING")
        .bind(LOCK_KEY).execute(&mut *tx).await?;
    let old: Value =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(LOCK_KEY)
            .fetch_one(&mut *tx)
            .await?;
    let previous_owner = old.get("lock_owner").and_then(Value::as_str).unwrap_or("");
    let locked = old
        .get("lock_until")
        .and_then(Value::as_str)
        .and_then(|v| DateTime::parse_from_rfc3339(v).ok())
        .is_some_and(|v| v > Utc::now());
    if !previous_owner.is_empty() && locked {
        return Err(AppError::Conflict("workdir cleanup already running".into()));
    }
    let ttl = std::env::var("WORKDIR_MAINTENANCE_LOCK_TTL_SECONDS")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(900)
        .clamp(30, 3600);
    let until = (Utc::now() + Duration::seconds(ttl)).to_rfc3339();
    sqlx::query("UPDATE app_settings SET value_json=jsonb_set(jsonb_set(COALESCE(value_json,'{}'::jsonb),'{lock_owner}',to_jsonb($2::text)),'{lock_until}',to_jsonb($3::text)),updated_at=now() WHERE key=$1")
        .bind(LOCK_KEY).bind(&owner).bind(until).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(owner)
}
async fn release_lock(state: &AppState, owner: &str) -> AppResult<()> {
    sqlx::query("UPDATE app_settings SET value_json=value_json-'lock_owner'-'lock_until',updated_at=now() WHERE key=$1 AND value_json->>'lock_owner'=$2")
        .bind(LOCK_KEY).bind(owner).execute(&state.db.pool).await?;
    Ok(())
}
async fn cleanup_inner(state: &AppState) -> AppResult<Value> {
    let before = scan(state).await?;
    let before_entries = before
        .get("entries")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Unavailable("workdir scanner missing entries".into()))?;
    let mut deleted_dirs = 0usize;
    let mut deleted_bytes = 0u64;
    let mut deleted_paths = Vec::<String>::new();
    let mut errors = Vec::<String>::new();
    let root = root_path();
    for entry in before_entries.iter().filter(|v| v["reclaimable"] == true) {
        if deleted_dirs >= 50 {
            errors
                .push("cleanup batch limited to 50 directories; retry for remaining paths".into());
            break;
        }
        let path = entry["rel_path"].as_str().unwrap_or("");
        let parts = path.split('/').collect::<Vec<_>>();
        if parts.len() != 2
            || !["subtitle", "render", "youtube"].contains(&parts[0])
            || parts[1].is_empty()
            || parts[1] == "."
            || parts[1] == ".."
            || parts[1].contains('\\')
        {
            errors.push(format!("{path}: invalid workdir path"));
            continue;
        }
        // Refresh the live DB/worker state and recursive disk fingerprint before
        // each unlink, not just once at the beginning of a long cleanup.
        let root_recheck = root.clone();
        let path_recheck = path.to_owned();
        let updated = tokio::task::spawn_blocking(move || disk_one(&root_recheck, &path_recheck))
            .await
            .map_err(|err| AppError::Unavailable(format!("workdir recheck failed: {err}")))?
            .map_err(AppError::Io)?;
        let current = match updated {
            Some(found) => classify_found(state, &root, vec![found]).await?,
            None => {
                errors.push(format!("{path}: disappeared or replaced; retained"));
                continue;
            }
        };
        let matching = current
            .get("entries")
            .and_then(Value::as_array)
            .and_then(|rows| rows.iter().find(|v| v["rel_path"] == path));
        if !matching.is_some_and(|v| {
            v["reclaimable"] == true
                && v["size_bytes"] == entry["size_bytes"]
                && v["modified_at"] == entry["modified_at"]
        }) {
            errors.push(format!("{path}: changed during cleanup; retained"));
            continue;
        }
        let directory = root.join(path);
        let is_normal = fs::symlink_metadata(&directory)
            .is_ok_and(|m| m.is_dir() && !m.file_type().is_symlink());
        let parent = root.join(parts[0]);
        let parent_normal =
            fs::symlink_metadata(&parent).is_ok_and(|m| m.is_dir() && !m.file_type().is_symlink());
        if !is_normal || !parent_normal {
            errors.push(format!("{path}: directory replaced or unsafe; retained"));
            continue;
        }
        match tokio::fs::remove_dir_all(&directory).await {
            Ok(()) => {
                deleted_dirs += 1;
                deleted_bytes =
                    deleted_bytes.saturating_add(entry["size_bytes"].as_u64().unwrap_or(0));
                deleted_paths.push(path.to_string());
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => (),
            Err(err) => errors.push(format!("{path}: {err}")),
        }
    }
    let mut after = scan(state).await?;
    let result = after
        .as_object_mut()
        .ok_or_else(|| AppError::Unavailable("invalid scanner reply".into()))?;
    result.insert("deleted_dirs".into(), json!(deleted_dirs));
    result.insert("deleted_bytes".into(), json!(deleted_bytes));
    result.insert("deleted_paths".into(), json!(deleted_paths));
    result.insert("errors".into(), json!(errors));
    Ok(after)
}
async fn post_cleanup(State(state): State<AppState>) -> AppResult<Json<Value>> {
    // Migration defaults to read-only; explicitly authorized deployments may
    // enable manual cleanup after validating the existing WORK_DIR mount.
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_WORKDIR_CLEANUP")
            .ok()
            .as_deref(),
        Some("1" | "true")
    ) {
        return Err(AppError::Conflict(
            "V1 workdir cleanup disabled; enable explicitly".into(),
        ));
    }
    let owner = acquire_lock(&state).await?;
    let result = cleanup_inner(&state).await;
    let release = release_lock(&state, &owner).await;
    release?;
    result.map(Json)
}
async fn get_workdir(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(scan(&state).await?))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/maintenance/workdir", get(get_workdir))
        .route(
            "/maintenance/workdir/cleanup",
            axum::routing::post(post_cleanup),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_reclaim_classes_and_active_defense() {
        let now = Utc::now();
        let fixture = |kind, owner: &str, age: i64| DiskEntry {
            kind,
            owner_id: owner.into(),
            rel_path: format!("{kind}/{owner}"),
            size_bytes: 123,
            modified_at: now - Duration::seconds(age),
        };
        let id = Uuid::new_v4();
        let task = Uuid::new_v4();
        let mut subtitle = HashMap::new();
        subtitle.insert(
            id,
            Job {
                task_id: Some(task),
                status: "succeeded".into(),
            },
        );
        let e = classify(
            fixture("subtitle", &id.to_string(), 2000),
            now,
            Duration::seconds(900),
            &subtitle,
            &HashMap::new(),
            &HashSet::new(),
            &HashSet::new(),
        );
        assert!(e.reclaimable);
        assert_eq!(e.task_id, Some(task));
        let mut active = HashSet::new();
        active.insert(task);
        let e = classify(
            fixture("subtitle", &id.to_string(), 2000),
            now,
            Duration::seconds(900),
            &subtitle,
            &HashMap::new(),
            &HashSet::new(),
            &active,
        );
        assert!(!e.reclaimable);
        assert_eq!(e.reason, "task active");
        let e = classify(
            fixture("youtube", "not-a-uuid", 300),
            now,
            Duration::seconds(900),
            &subtitle,
            &HashMap::new(),
            &HashSet::new(),
            &active,
        );
        assert!(!e.reclaimable);
        let e = classify(
            fixture("youtube", "not-a-uuid", 1800),
            now,
            Duration::seconds(900),
            &subtitle,
            &HashMap::new(),
            &HashSet::new(),
            &active,
        );
        assert!(e.reclaimable);
    }
    #[test]
    fn symlinks_are_not_scanned() {
        let root = std::env::temp_dir().join(format!("videoroll-workdir-test-{}", Uuid::new_v4()));
        fs::create_dir_all(root.join("youtube").join("invalid")).unwrap();
        fs::write(root.join("youtube").join("invalid").join("file"), b"hello").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(
            "/tmp",
            root.join("youtube").join("invalid").join("external"),
        )
        .unwrap();
        let rows = disk_entries(&root).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].size_bytes, 5);
        fs::remove_dir_all(root).unwrap();
    }
}
