//! Explicit asset ownership. Legacy v1 data is never garbage-collected by this module.
use crate::{AppError, AppResult, Database};
use chrono::{DateTime, Utc};
use sha2::{Digest, Sha256};
use sqlx::{Postgres, Row, Transaction};
use std::{
    path::{Component, Path, PathBuf},
    time::Duration,
};
use tokio::{fs, io::AsyncReadExt};
use uuid::Uuid;

#[derive(Clone)]
pub struct AssetRegistry {
    pub db: Database,
    pub root: PathBuf,
}
/// A canceled task with a saved checkpoint is paused, not disposable.
pub(crate) fn terminal_resources_releasable(status: &str, stopped: Option<&str>) -> bool {
    status.eq_ignore_ascii_case("published")
        || status.eq_ignore_ascii_case("failed")
        || (status.eq_ignore_ascii_case("canceled") && stopped.is_none())
}
/// Revalidate publication safety under the parent task lock, at both
/// reference release and physical GC time.
pub(crate) async fn publication_cleanup_blocked(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
) -> AppResult<bool> {
    Ok(sqlx::query_scalar(r#"SELECT
      EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND state IN ('submitted','submitting','running','paused','unknown','cancel_requested'))
      OR EXISTS(SELECT 1 FROM operation_inbox WHERE task_id=$1 AND status IN ('pending','queued','awaiting','running','processing','dispatching','accepted','unknown'))
      OR EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND
         (state::text IN ('draft','submitting','unknown') OR
          (state::text='submitted' AND response_json->>'runtime'='rust' AND response_json->>'receipt_verified'='false'
           AND NULLIF(external_id,'') IS NULL AND NULLIF(external_url,'') IS NULL)))
      OR EXISTS(SELECT 1 FROM subtitle_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))
      OR EXISTS(SELECT 1 FROM render_jobs WHERE task_id=$1 AND status::text IN ('queued','running'))
      OR EXISTS(SELECT 1 FROM render_executions WHERE task_id=$1 AND runtime_engine='rust-v2' AND state IN ('running','uploading','canceled','paused') AND lease_until>now())
      OR EXISTS(SELECT 1 FROM dubbing_jobs WHERE task_id=$1 AND
        (status IN ('queued','preparing','synthesizing','aligning','mixing','rendering','pause_requested','cancel_requested')
        OR (status IN ('paused','preview_ready','needs_review') AND updated_at>now()-interval '7 days')))
      OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE task_id=$1 AND state<>'published')
      OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1 AND state NOT IN ('submitted','published','failed'))"#)
      .bind(task).fetch_one(&mut **tx).await?)
}
/// Re-read the original retention setting in the deletion transaction.
/// A batch's independent 24h cleanup permission only covers intermediates.
pub(crate) async fn deletion_policy(
    tx: &mut Transaction<'_, Postgres>, task: Uuid, status: &str,
    stopped: Option<&str>, updated: DateTime<Utc>, failed_retention: Duration,
) -> AppResult<(bool, bool)> {
    if !terminal_resources_releasable(status, stopped)
        || publication_cleanup_blocked(tx, task).await? {
        return Ok((false, false));
    }
    let now = Utc::now();
    let full = if status == "FAILED" {
        updated <= now - chrono::Duration::seconds(failed_retention.as_secs().clamp(3600, 3650*86400) as i64)
    } else {
        let settings: Option<serde_json::Value> = sqlx::query_scalar(
            "SELECT value_json FROM app_settings WHERE key='storage.retention' FOR SHARE"
        ).fetch_optional(&mut **tx).await?;
        let days = settings.as_ref().map(crate::operations_v1::ttl_from_value).unwrap_or(0);
        days > 0 && updated <= now - chrono::Duration::days(days)
    };
    if status != "PUBLISHED" { return Ok((full,false)); }
    let batches = crate::bilibili_batch_reconcile_v1::current_batches(tx,task).await?;
    if batches.iter().any(|(_,s,finished,_)| *s != crate::bilibili_batch_reconcile_v1::State::Succeeded
        || finished.is_none_or(|t|t+chrono::Duration::hours(24)>now)) {
        return Ok((false,false));
    }
    Ok((full, batches.iter().any(|(_,_,_,scheduled)|*scheduled)))
}

fn native_key_task(key: &str) -> Option<Uuid> {
    let mut parts=key.split('/');
    if parts.next()? != "v2" { return None; }
    if !matches!(parts.next()?, "raw"|"sub"|"render"|"dubbing"|"artifacts"|"youtube-meta"|"subtitles") { return None; }
    Uuid::parse_str(parts.next()?).ok()
}

fn sweep_uploads(root: &Path, age: Duration, limit: u64) -> AppResult<u64> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::time::SystemTime;
    let dirs = match std::fs::read_dir(root) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e.into()),
    };
    let cutoff = SystemTime::now()
        .checked_sub(age)
        .ok_or_else(|| AppError::Invalid("upload expiry overflow".into()))?;
    let mut count = 0;
    for dir in dirs {
        let dir = dir?;
        if !dir.file_type()?.is_dir()
            || Uuid::parse_str(&dir.file_name().to_string_lossy()).is_err()
        {
            continue;
        }
        for item in std::fs::read_dir(dir.path())? {
            let item = item?;
            let name = item.file_name();
            let name = name.to_string_lossy();
            if !name
                .strip_suffix(".vr2-upload.partial")
                .is_some_and(|id| Uuid::parse_str(id).is_ok())
            {
                continue;
            }
            // Never follow a link; recheck age and identity under exclusive
            // inode lock. Active uploads, including a stalled HTTP stream,
            // retain this lock until they rename or exit.
            let path = item.path();
            let Ok(before) = std::fs::symlink_metadata(&path) else {
                continue;
            };
            if !before.is_file()
                || before.nlink() != 1
                || !before.modified().is_ok_and(|t| t < cutoff)
            {
                continue;
            }
            let Ok(file) = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                .open(&path)
            else {
                continue;
            };
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                continue;
            }
            let actual = file.metadata()?;
            let Ok(current) = std::fs::symlink_metadata(&path) else {
                continue;
            };
            if !actual.is_file()
                || actual.nlink() != 1
                || actual.dev() != current.dev()
                || actual.ino() != current.ino()
                || !actual.modified().is_ok_and(|t| t < cutoff)
            {
                continue;
            }
            match std::fs::remove_file(&path) {
                Ok(()) => {
                    count += 1;
                    if count >= limit {
                        return Ok(count);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => tracing::warn!(error=%e,"abandoned upload cleanup deferred"),
            }
        }
    }
    Ok(count)
}
impl AssetRegistry {
    pub fn new(db: Database, root: PathBuf) -> Self {
        Self { db, root }
    }
    pub fn checked_key(key: &str) -> AppResult<&str> {
        if key.is_empty()
            || key.len() > 1024
            || key.contains('\\')
            || key.chars().any(char::is_control)
            || key
                .split('/')
                .any(|segment| segment.is_empty() || segment == "." || segment == "..")
        {
            return Err(AppError::Invalid("invalid storage key".into()));
        }
        let p = Path::new(key);
        if p.components().any(|p| !matches!(p, Component::Normal(_))) || p.is_absolute() {
            return Err(AppError::Invalid(
                "storage key must be relative and contain only normal components".into(),
            ));
        }
        Ok(key)
    }
    /// Verify all existing ancestors; symlinks cannot redirect reads/deletes outside storage.
    pub async fn resolve_existing(&self, key: &str) -> AppResult<PathBuf> {
        let key = Self::checked_key(key)?;
        let root = fs::canonicalize(&self.root).await?;
        let mut path = root.clone();
        for component in Path::new(key).components() {
            if let Component::Normal(name) = component {
                path.push(name);
                match fs::symlink_metadata(&path).await {
                    Ok(metadata) if metadata.file_type().is_symlink() => {
                        return Err(AppError::Invalid("symbolic link in storage path".into()));
                    }
                    Ok(_) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e.into()),
                }
            }
        }
        if !path.starts_with(root) {
            return Err(AppError::Invalid("storage path escapes root".into()));
        }
        Ok(path)
    }
    pub async fn sha256(&self, key: &str) -> AppResult<(String, i64)> {
        let p = self.resolve_existing(key).await?;
        let mut file = fs::File::open(p).await?;
        let mut sha = Sha256::new();
        let mut n = 0i64;
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            let count = file.read(&mut buf).await?;
            if count == 0 {
                break;
            }
            sha.update(&buf[..count]);
            n += count as i64;
        }
        Ok((hex::encode(sha.finalize()), n))
    }
    pub async fn register(
        &self,
        key: &str,
        owner_type: &str,
        owner_id: Uuid,
        role: &str,
        retain_until: DateTime<Utc>,
    ) -> AppResult<()> {
        let key = Self::checked_key(key)?;
        if owner_type.len() > 80 || owner_type.is_empty() || role.len() > 80 || role.is_empty() {
            return Err(AppError::Invalid("invalid object reference".into()));
        }
        let (sha, size) = self.sha256(key).await?;
        let mut tx = self.db.pool.begin().await?;
        let registered=sqlx::query(
            "INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',$4) ON CONFLICT(storage_key) DO UPDATE SET checksum_sha256=$2,size_bytes=$3,state='live',retain_until=GREATEST(storage_objects.retain_until,$4),updated_at=now() WHERE storage_objects.state <> 'deleting'"
        ).bind(key).bind(sha).bind(size).bind(retain_until).execute(&mut *tx).await?;
        if registered.rows_affected() != 1 {
            return Err(AppError::Conflict(
                "garbage collector has fenced this object; retry with a new key".into(),
            ));
        }
        sqlx::query(
            "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING"
        ).bind(key).bind(owner_type).bind(owner_id).bind(role).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    pub async fn detach(
        &self,
        key: &str,
        owner_type: &str,
        owner_id: Uuid,
        role: &str,
    ) -> AppResult<()> {
        let key = Self::checked_key(key)?;
        sqlx::query(
            "DELETE FROM storage_object_refs WHERE storage_key=$1 AND owner_type=$2 AND owner_id=$3 AND role=$4"
        ).bind(key).bind(owner_type).bind(owner_id).bind(role)
            .execute(&self.db.pool).await?;
        Ok(())
    }
    async fn claim_expired(
        tx: &mut Transaction<'_, Postgres>,
        limit: i64,
    ) -> AppResult<Vec<(String, DateTime<Utc>)>> {
        Ok(sqlx::query_as(
            r#"WITH eligible AS (
                SELECT o.storage_key FROM storage_objects o
                WHERE ((o.state='live' AND o.retain_until<=now())
                   OR (o.state='deleting' AND o.lease_until<now()))
                AND (o.manual_hold_until IS NULL OR o.manual_hold_until<=now())
                AND NOT EXISTS (SELECT 1 FROM storage_object_refs r WHERE r.storage_key=o.storage_key)
                ORDER BY o.retain_until ASC
                FOR UPDATE OF o SKIP LOCKED LIMIT $1
            ) UPDATE storage_objects o SET state='deleting',lease_until=now()+interval '5 minutes',
                updated_at=now() FROM eligible e WHERE o.storage_key=e.storage_key
                RETURNING o.storage_key,o.lease_until"#,
        )
        .bind(limit.clamp(1, 100))
        .fetch_all(&mut **tx)
        .await?)
    }
    /// Make storage retention an explicit terminal event, never a full-disk scan.
    /// Reconcile completed failed/canceled flows and release their own objects.
    /// Never touch a pipeline with an uncertain external publisher.
    pub async fn reconcile_terminal_resources(
        &self,
        limit: i64,
        failed_retention: Duration,
        canceled_retention: Option<Duration>,
    ) -> AppResult<u64> {
        let failed_before = Utc::now() - chrono::Duration::seconds(failed_retention.as_secs().clamp(3600, 3650 * 86400) as i64);
        let canceled_before = canceled_retention.map(|ttl|
            Utc::now() - chrono::Duration::seconds(ttl.as_secs().clamp(86400,3650 * 86400) as i64));
        let rows = sqlx::query(
            r#"SELECT w.id,w.task_id,upper(t.status::text) AS task_status FROM vr2_workflows w
               JOIN tasks t ON t.id=w.task_id
               WHERE w.state IN ('failed','canceled')
                 AND ((t.status::text='failed' AND t.updated_at <= $2)
                   OR (t.status::text='canceled' AND t.stopped_status IS NULL
                       AND $3::timestamptz IS NOT NULL AND t.updated_at <= $3))
                 AND NOT EXISTS (
                    SELECT 1 FROM workflow_events e WHERE e.workflow_id=w.id
                      AND e.kind='storage.cleanup_scheduled'
                 )
                 AND NOT EXISTS (
                    SELECT 1 FROM vr2_workflows other
                    WHERE other.task_id=w.task_id AND other.id<>w.id
                      AND other.state IN ('running','paused','unknown')
                 )
               ORDER BY w.updated_at ASC LIMIT $1"#,
        )
        .bind(limit.clamp(1, 50))
        .bind(failed_before)
        .bind(canceled_before)
        .fetch_all(&self.db.pool)
        .await?;
        let mut released = 0;
        for row in rows {
            let id: Uuid = row.try_get("id")?;
            let task_id: Uuid = row.try_get("task_id")?;
            let status: String = row.try_get("task_status")?;
            let cutoff = if status == "FAILED" { Some(failed_before) } else { canceled_before };
            let Some(cutoff) = cutoff else { continue };
            match self.schedule_terminal_release_checked(task_id, id, Duration::from_secs(3600),
                Some((status.as_str(), cutoff))).await {
                Ok(_) => released += 1,
                Err(e) => tracing::warn!(workflow_id=%id,error=%e,"terminal cleanup deferred"),
            }
        }
        Ok(released)
    }
    /// Backfill deferred cleanup for *only* V2-owned, provably published
    /// workflows. Never glob old V1 directories or delete legacy-only assets.
    /// The effective TTL is 0 by default, meaning published media is retained.
    pub async fn reconcile_published_resources(
        &self,
        limit: i64,
        retention: Option<Duration>,
    ) -> AppResult<u64> {
        let Some(ttl) = retention else {
            return Ok(0);
        };
        let age_seconds = ttl.as_secs().clamp(86_400, 3650 * 86_400) as i64;
        let cutoff = Utc::now() - chrono::Duration::seconds(age_seconds);
        let rows = sqlx::query(
            r#"SELECT w.id,w.task_id
              FROM vr2_workflows w JOIN tasks t ON t.id=w.task_id
              WHERE w.state='succeeded' AND upper(t.status::text)='PUBLISHED'
                AND t.updated_at <= $1
                AND EXISTS (
                  SELECT 1 FROM storage_object_refs r
                  WHERE ((r.owner_type='workflow' AND r.owner_id=w.id)
                      OR (r.owner_type='task' AND r.owner_id=t.id))
                    AND r.storage_key LIKE 'v2/%'
                )
                AND NOT EXISTS(
                  SELECT 1 FROM workflow_events e WHERE e.workflow_id=w.id
                   AND e.kind='storage.cleanup_scheduled'
                )
                AND NOT EXISTS(
                  SELECT 1 FROM vr2_workflows other WHERE other.task_id=t.id
                   AND other.id<>w.id AND other.state IN ('running','paused','unknown')
                )
                AND NOT EXISTS(
                  SELECT 1 FROM vr2_jobs j WHERE j.task_id=t.id
                   AND j.state IN ('pending','running','awaiting','unknown')
                )
                AND NOT EXISTS(
                  SELECT 1 FROM dubbing_jobs d WHERE d.task_id=t.id AND
                    (d.status IN ('queued','preparing','synthesizing','aligning',
                                  'mixing','rendering','pause_requested','cancel_requested')
                     OR (d.status IN ('paused','preview_ready','needs_review')
                         AND d.updated_at>now()-interval '7 days'))
                )
                AND NOT EXISTS(
                  SELECT 1 FROM vr2_bilibili_p2_appends p2 WHERE p2.task_id=t.id
                   AND p2.state<>'published'
                )
                AND NOT EXISTS(
                  SELECT 1 FROM vr2_social_publish_intents s WHERE s.task_id=t.id
                   AND s.state<>'published'
                )
              ORDER BY t.updated_at ASC LIMIT $2"#,
        )
        .bind(cutoff)
        .bind(limit.clamp(1, 50))
        .fetch_all(&self.db.pool)
        .await?;
        let mut scheduled = 0;
        for row in rows {
            let task_id: Uuid = row.try_get("task_id")?;
            let workflow_id: Uuid = row.try_get("id")?;
            // Revalidate task state + active jobs inside the release transaction.
            // One hour additional grace prevents immediate deletion on a
            // recovered process that just discovered an old completed task.
            match self
                .schedule_terminal_release_checked(task_id, workflow_id, Duration::from_secs(3600),
                    Some(("PUBLISHED", cutoff)))
                .await
            {
                Ok(_) => scheduled += 1,
                Err(e) => tracing::warn!(%task_id,%workflow_id,error=%e,
                    "published retention backfill deferred"),
            }
        }
        Ok(scheduled)
    }

    pub async fn schedule_terminal_release(
        &self,
        task_id: Uuid,
        workflow_id: Uuid,
        grace: Duration,
    ) -> AppResult<u64> {
        self.schedule_terminal_release_checked(task_id, workflow_id, grace, None).await
    }

    async fn schedule_terminal_release_checked(
        &self,
        task_id: Uuid,
        workflow_id: Uuid,
        grace: Duration,
        due: Option<(&str, DateTime<Utc>)>,
    ) -> AppResult<u64> {
        let grace_seconds = grace.as_secs().clamp(3600, 3650 * 86400) as i64;
        let mut tx = self.db.pool.begin().await?;
        let state: Option<(String, Option<String>, DateTime<Utc>)> = sqlx::query_as(
            "SELECT upper(status::text),stopped_status::text,updated_at FROM tasks WHERE id=$1 FOR UPDATE",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?;
        if !state.as_ref().is_some_and(|(status, stopped, updated)| {
            terminal_resources_releasable(status, stopped.as_deref())
                && due.is_none_or(|(expected, cutoff)| status == expected && *updated <= cutoff)
        }) {
            return Err(AppError::Conflict(
                "automatic release requires a confirmed terminal task".into(),
            ));
        }
        let active:(bool,)=sqlx::query_as(
            "SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND stage<>'cleanup' AND state IN ('pending','running','awaiting','unknown'))"
        ).bind(task_id).fetch_one(&mut *tx).await?;
        if active.0 {
            return Err(AppError::Conflict(
                "unfinished tasks still hold media artifacts".into(),
            ));
        }
        let flow:(bool,)=sqlx::query_as(
            "SELECT EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND id<>$2 AND state IN ('running','paused','unknown'))"
        ).bind(task_id).bind(workflow_id).fetch_one(&mut *tx).await?;
        if flow.0 {
            return Err(AppError::Conflict(
                "another workflow still owns this task".into(),
            ));
        }
        // Protect async sidecar handoffs and recent manual YouDub jobs even
        // when no vr2_job lease exists. Locking the parent Task above fences
        // FK-backed new intents until this transaction commits.
        let protected: bool = sqlx::query_scalar(
            r#"SELECT
              EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends p
                     WHERE p.task_id=$1 AND p.state<>'published')
              OR EXISTS(SELECT 1 FROM vr2_social_publish_intents s
                        WHERE s.task_id=$1 AND s.state<>'published')
              OR EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1 AND
                        (j.state::text IN ('submitting','unknown') OR
                        (j.state::text='submitted' AND j.response_json->>'runtime'='rust'
                          AND j.response_json->>'receipt_verified'='false'
                          AND NULLIF(j.external_id,'') IS NULL AND NULLIF(j.external_url,'') IS NULL)))
              OR EXISTS(SELECT 1 FROM dubbing_jobs d WHERE d.task_id=$1 AND
                        (d.status IN ('queued','preparing','synthesizing','aligning',
                                      'mixing','rendering','pause_requested','cancel_requested')
                         OR (d.status IN ('paused','preview_ready','needs_review')
                          AND d.updated_at>now()-interval '7 days')))"#,
        )
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;
        if protected {
            return Err(AppError::Conflict(
                "ongoing dubbing or unresolved platform publication still needs media".into(),
            ));
        }
        if state.as_ref().is_some_and(|(status, _, _)| status == "PUBLISHED") {
            let batches =
                crate::bilibili_batch_reconcile_v1::current_batches(&mut tx, task_id).await?;
            if batches.iter().any(|(_, s, finished, _)| {
                *s != crate::bilibili_batch_reconcile_v1::State::Succeeded
                    || finished.is_none_or(|t| t + chrono::Duration::hours(24) > Utc::now())
            }) {
                return Err(AppError::Conflict(
                    "current publication batches have not completed their 24h protection period"
                        .into(),
                ));
            }
        }
        let rows=sqlx::query(
            "DELETE FROM storage_object_refs WHERE (owner_type='workflow' AND owner_id=$1) OR (owner_type='task' AND owner_id=$2) RETURNING storage_key"
        ).bind(workflow_id).bind(task_id).fetch_all(&mut *tx).await?;
        for row in &rows {
            let key: String = row.try_get("storage_key")?;
            if !key.starts_with("v2/") {
                return Err(AppError::Conflict("refused release of legacy file".into()));
            }
            sqlx::query(
                "UPDATE storage_objects SET retain_until=GREATEST(now()+($1*interval '1 second'),manual_hold_until),updated_at=now() WHERE storage_key=$2 AND state='live'"
            ).bind(grace_seconds).bind(key).execute(&mut *tx).await?;
        }
        for category in ["raw", "sub", "render", "dubbing"] {
            let key = format!("v2/{category}/{task_id}");
            sqlx::query(
                "INSERT INTO storage_workdirs(path_key,task_id,workflow_id,due_at) VALUES($1,$2,$3,now()+($4*interval '1 second')) ON CONFLICT(path_key) DO UPDATE SET due_at=GREATEST(storage_workdirs.due_at,EXCLUDED.due_at),state='retained',lease_until=NULL,workflow_id=EXCLUDED.workflow_id,updated_at=now() WHERE storage_workdirs.task_id=EXCLUDED.task_id"
            ).bind(key).bind(task_id).bind(workflow_id).bind(grace_seconds)
                .execute(&mut *tx).await?;
        }
        sqlx::query(
            "INSERT INTO workflow_events(id,workflow_id,kind,details) VALUES($1,$2,'storage.cleanup_scheduled',$3)"
        ).bind(Uuid::new_v4()).bind(workflow_id)
            .bind(serde_json::json!({"task_id":task_id,"grace_seconds":grace_seconds,"released_refs":rows.len()}))
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(rows.len() as u64)
    }
    /// Check the exact claimed lease, parent task, current policy and all
    /// references in one transaction; retain business rows on unlink failure.
    async fn collect_claim(&self,key: &str, lease: DateTime<Utc>, failed_retention: Duration) -> AppResult<bool> {
        let mut tx = self.db.pool.begin().await?;
        let owners = sqlx::query_scalar::<_,Uuid>(
            "SELECT DISTINCT task_id FROM (SELECT task_id FROM assets WHERE storage_key=$1 UNION ALL SELECT task_id FROM subtitles WHERE storage_key=$1) v"
        ).bind(key).fetch_all(&mut *tx).await?;
        let task = match owners.as_slice() {
            [task] if native_key_task(key).is_none_or(|id|id==*task) => Some(*task),
            [] => native_key_task(key),
            _ => None,
        };
        let parent: Option<(String,Option<String>,DateTime<Utc>)> = if let Some(task)=task {
            sqlx::query_as("SELECT upper(status::text),stopped_status::text,updated_at FROM tasks WHERE id=$1 FOR UPDATE")
                .bind(task).fetch_optional(&mut *tx).await?
        } else { None };
        let claim: Option<bool> = sqlx::query_scalar(
            "SELECT retain_until<=now() AND (manual_hold_until IS NULL OR manual_hold_until<=now()) AND lease_until>now()
             FROM storage_objects WHERE storage_key=$1 AND state='deleting' AND lease_until=$2 FOR UPDATE"
        ).bind(key).bind(lease).fetch_optional(&mut *tx).await?;
        let Some(due) = claim else { return Ok(false) };
        let mut allowed=false;
        if key.starts_with("v2/") && due {
            if let (Some(task),Some((status,stopped,updated)))=(task,parent) {
                let (full,batch)=deletion_policy(&mut tx,task,&status,stopped.as_deref(),updated,failed_retention).await?;
                if full || batch {
                    // Short nonblocking table fence also protects against
                    // cross-task references created outside the native scheduler.
                    sqlx::query("LOCK TABLE assets, subtitles IN SHARE ROW EXCLUSIVE MODE NOWAIT")
                        .execute(&mut *tx).await?;
                    let shared:bool=sqlx::query_scalar("SELECT
                        EXISTS(SELECT 1 FROM assets WHERE storage_key=$1 AND task_id<>$2)
                        OR EXISTS(SELECT 1 FROM subtitles WHERE storage_key=$1 AND task_id<>$2)
                        OR EXISTS(SELECT 1 FROM storage_object_refs WHERE storage_key=$1)
                        OR EXISTS(SELECT 1 FROM playout_asset_links p JOIN assets a ON a.id=p.asset_id
                             WHERE a.storage_key=$1 AND (p.runtime_engine<>'rust-v2' OR p.status IN ('staging','ready')))
                        OR EXISTS(SELECT 1 FROM render_executions e JOIN assets a ON a.id=e.uploaded_asset_id
                             WHERE a.storage_key=$1)"
                    ).bind(key).bind(task).fetch_one(&mut *tx).await?;
                    let intermediate:bool=if !full && batch {
                        sqlx::query_scalar("SELECT
                            EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND storage_key=$1)
                            AND NOT EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND storage_key=$1
                                AND kind::text IN ('video_final','video_dubbed','dubbing_audio','publish_result'))
                            AND (EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND kind::text='video_final')
                                OR $1 IS DISTINCT FROM (SELECT storage_key FROM assets WHERE task_id=$2 AND kind::text='video_raw'
                                     ORDER BY created_at DESC,id DESC LIMIT 1))"
                        ).bind(key).bind(task).fetch_one(&mut *tx).await?
                    } else { false };
                    allowed=!shared && (full || intermediate);
                    if allowed {
                        // Run the relational deletes before the unlink, but
                        // roll them back on filesystem failure.
                        sqlx::query("SAVEPOINT gc_business_rows").execute(&mut *tx).await?;
                        sqlx::query("DELETE FROM subtitles WHERE task_id=$1 AND storage_key=$2").bind(task).bind(key).execute(&mut *tx).await?;
                        sqlx::query("DELETE FROM assets WHERE task_id=$1 AND storage_key=$2").bind(task).bind(key).execute(&mut *tx).await?;
                        let deletion=async {
                            let path=self.resolve_existing(key).await?;
                            match fs::remove_file(path).await {
                                Ok(())=>Ok(()),
                                Err(e) if e.kind()==std::io::ErrorKind::NotFound=>Ok(()),
                                Err(e)=>Err(AppError::Io(e)),
                            }
                        }.await;
                        if let Err(error)=deletion {
                            tracing::warn!(%key,%error,"defer GC file unlink; retained business rows");
                            sqlx::query("ROLLBACK TO SAVEPOINT gc_business_rows").execute(&mut *tx).await?;
                            allowed=false;
                        }
                    }
                }
            }
        }
        if allowed {
            sqlx::query("UPDATE storage_objects SET state='deleted',lease_until=NULL,updated_at=now() WHERE storage_key=$1")
                .bind(key).execute(&mut *tx).await?;
        } else {
            sqlx::query("UPDATE storage_objects SET state='live',lease_until=NULL,retain_until=GREATEST(retain_until,now()+interval '10 minutes',manual_hold_until),updated_at=now() WHERE storage_key=$1")
                .bind(key).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(allowed)
    }
    /// Durable lease claim followed by a short parent/object locked deletion.
    pub async fn collect(&self, limit: i64, failed_retention: Duration) -> AppResult<u64> {
        let mut tx = self.db.pool.begin().await?;
        let keys = Self::claim_expired(&mut tx, limit).await?;
        tx.commit().await?;
        let mut deleted = 0;
        for (key,lease) in keys {
            match self.collect_claim(&key,lease,failed_retention).await {
                Ok(true)=>deleted+=1,
                Ok(false)=>{},
                Err(error)=>tracing::warn!(%key,%error,"GC claim deferred until lease expiry"),
            }
        }
        Ok(deleted)
    }
    /// Clean only the new upload-specific temporary namespace. Never scan
    /// the Python sibling .partial directory or yt-dlp resumable transfers.
    pub async fn collect_abandoned_uploads(&self) -> AppResult<u64> {
        let root = self.resolve_existing("v2/artifacts").await?;
        tokio::task::spawn_blocking(move || {
            sweep_uploads(&root, std::time::Duration::from_secs(86400), 500)
        })
        .await
        .map_err(|e| AppError::Unavailable(format!("upload cleanup task failed: {e}")))?
    }
    /// Cleanup only explicit v2/{raw,sub,render,dubbing}/{UUID} directories.
    pub async fn collect_workdirs(&self, limit: i64, failed_retention: Duration) -> AppResult<u64> {
        let keys = sqlx::query(
            r#"WITH due AS (
              SELECT path_key FROM storage_workdirs
              WHERE (state='retained' AND due_at<=now())
                 OR (state='deleting' AND lease_until<now())
              ORDER BY due_at FOR UPDATE SKIP LOCKED LIMIT $1
            ) UPDATE storage_workdirs w SET state='deleting',lease_until=now()+interval '5 minutes',
              updated_at=now() FROM due WHERE w.path_key=due.path_key
              RETURNING w.path_key,w.task_id,w.workflow_id,w.lease_until"#,
        )
        .bind(limit.clamp(1, 50))
        .fetch_all(&self.db.pool)
        .await?;
        let mut cleaned = 0;
        for row in keys {
            let key: String = row.try_get("path_key")?;
            let task_id: Uuid = row.try_get("task_id")?;
            let workflow_id: Uuid = row.try_get("workflow_id")?;
            let parts = key.split('/').collect::<Vec<_>>();
            let expected = parts.len() == 3
                && parts[0] == "v2"
                && ["raw", "sub", "render", "dubbing"].contains(&parts[1])
                && parts[2] == task_id.to_string();
            let lease: DateTime<Utc>=row.try_get("lease_until")?;
            let mut safety_tx = self.db.pool.begin().await?;
            let (task_state, stopped, updated): (String, Option<String>, DateTime<Utc>) = sqlx::query_as(
                "SELECT upper(status::text),stopped_status::text,updated_at FROM tasks WHERE id=$1 FOR UPDATE",
            ).bind(task_id).fetch_one(&mut *safety_tx).await?;
            let claim: Option<bool>=sqlx::query_scalar("SELECT due_at<=now() AND lease_until>now()
                FROM storage_workdirs WHERE path_key=$1 AND workflow_id=$2 AND state='deleting' AND lease_until=$3 FOR UPDATE")
                .bind(&key).bind(workflow_id).bind(lease).fetch_optional(&mut *safety_tx).await?;
            let Some(due)=claim else {continue};
            let (full,batch)=deletion_policy(&mut safety_tx,task_id,&task_state,stopped.as_deref(),updated,failed_retention).await?;
            let referenced:bool=sqlx::query_scalar("SELECT
                EXISTS(SELECT 1 FROM storage_objects WHERE storage_key LIKE ($1 || '/%') AND state<>'deleted')
                OR EXISTS(SELECT 1 FROM assets WHERE storage_key LIKE ($1 || '/%'))
                OR EXISTS(SELECT 1 FROM subtitles WHERE storage_key LIKE ($1 || '/%'))")
                .bind(&key).fetch_one(&mut *safety_tx).await?;
            if !expected || !due || !(full || batch) || referenced {
                sqlx::query("UPDATE storage_workdirs SET state='retained',lease_until=NULL,due_at=GREATEST(due_at,now()+interval '10 minutes'),updated_at=now() WHERE path_key=$1")
                    .bind(&key).execute(&mut *safety_tx).await?;
                safety_tx.commit().await?;
                continue;
            }
            let result = async {
                let path = self.resolve_existing(&key).await?;
                match fs::remove_dir_all(path).await {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(AppError::Io(e)),
                }
            }
            .await;
            // Keep both parent and exact workdir claim locked through settlement.
            match result {
                Ok(()) => {
                    sqlx::query("UPDATE storage_workdirs SET state='deleted',lease_until=NULL,updated_at=now() WHERE path_key=$1 AND workflow_id=$2")
                        .bind(&key).bind(workflow_id).execute(&mut *safety_tx).await?;
                    cleaned += 1;
                }
                Err(e) => {
                    tracing::error!(directory=%key,error=%e,"workdir cleanup deferred");
                    sqlx::query("UPDATE storage_workdirs SET state='retained',lease_until=NULL,due_at=now()+interval '10 minutes',last_error=$2,updated_at=now() WHERE path_key=$1")
                        .bind(&key).bind(e.to_string()).execute(&mut *safety_tx).await?;
                }
            }
            safety_tx.commit().await?;
        }
        Ok(cleaned)
    }
    pub async fn hold(&self, key: &str, extra: Duration) -> AppResult<()> {
        Self::checked_key(key)?;
        sqlx::query("UPDATE storage_objects SET retain_until=GREATEST(retain_until,now()+($1*interval '1 second')),manual_hold_until=GREATEST(manual_hold_until,now()+($1*interval '1 second')),updated_at=now() WHERE storage_key=$2")
            .bind(extra.as_secs().min(31536000) as i64).bind(key)
            .execute(&self.db.pool).await?;
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn terminal_cleanup_preserves_resumable_canceled_tasks() {
        assert!(terminal_resources_releasable("published", None));
        assert!(terminal_resources_releasable("FAILED", None));
        assert!(terminal_resources_releasable("canceled", None));
        assert!(!terminal_resources_releasable("CANCELED", Some("rendering")));
        assert!(!terminal_resources_releasable("publishing", None));
        assert!(!terminal_resources_releasable("created", None));
    }
    #[test]
    fn rejects_path_traversal_and_platform_ambiguity() {
        for s in [
            "../etc/passwd",
            "/tmp/out",
            "raw/../x",
            "raw\\x",
            "",
            "./hello",
            "a//b",
            "x\u{0000}",
        ] {
            assert!(
                AssetRegistry::checked_key(s).is_err(),
                "unexpected accepted: {s:?}"
            );
        }
        assert!(AssetRegistry::checked_key("raw/550e8400.mp4").is_ok());
    }
    #[test]
    fn abandoned_upload_sweep_preserves_locked_and_unrelated_files() {
        use std::os::fd::AsRawFd;
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join(Uuid::new_v4().to_string());
        std::fs::create_dir(&dir).unwrap();
        let stale = dir.join(format!("{}.vr2-upload.partial", Uuid::new_v4()));
        let locked = dir.join(format!("{}.vr2-upload.partial", Uuid::new_v4()));
        let unrelated = dir.join("source.mp4.part");
        let symlink = dir.join(format!("{}.vr2-upload.partial", Uuid::new_v4()));
        std::fs::write(&stale, b"x").unwrap();
        std::fs::write(&locked, b"x").unwrap();
        std::fs::write(&unrelated, b"x").unwrap();
        std::os::unix::fs::symlink(&unrelated, &symlink).unwrap();
        let active = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&locked)
            .unwrap();
        assert_eq!(
            unsafe { libc::flock(active.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        std::thread::sleep(Duration::from_millis(20));
        assert_eq!(sweep_uploads(root.path(), Duration::ZERO, 500).unwrap(), 1);
        assert!(!stale.exists() && locked.exists() && unrelated.exists() && symlink.exists());
        drop(active);
        assert_eq!(sweep_uploads(root.path(), Duration::ZERO, 500).unwrap(), 1);
    }
    #[tokio::test]
    async fn rejects_symlink_without_touching_outside() {
        let dir = tempfile::tempdir().unwrap();
        let out = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("raw")).await.unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(out.path(), dir.path().join("raw/escape")).unwrap();
            // This uses no DB connections; path verification alone must be safe.
            let pool = sqlx::postgres::PgPoolOptions::new()
                .connect_lazy("postgresql://localhost/videoroll")
                .unwrap();
            let registry = AssetRegistry::new(Database { pool }, dir.path().to_path_buf());
            assert!(
                registry
                    .resolve_existing("raw/escape/test.mp4")
                    .await
                    .is_err()
            );
        }
    }
}
