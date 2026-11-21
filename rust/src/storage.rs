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
            "INSERT INTO vr2_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',$4) ON CONFLICT(storage_key) DO UPDATE SET checksum_sha256=$2,size_bytes=$3,state='live',retain_until=GREATEST(vr2_objects.retain_until,$4),updated_at=now() WHERE vr2_objects.state <> 'deleting'"
        ).bind(key).bind(sha).bind(size).bind(retain_until).execute(&mut *tx).await?;
        if registered.rows_affected() != 1 {
            return Err(AppError::Conflict(
                "garbage collector has fenced this object; retry with a new key".into(),
            ));
        }
        sqlx::query(
            "INSERT INTO vr2_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING"
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
            "DELETE FROM vr2_object_refs WHERE storage_key=$1 AND owner_type=$2 AND owner_id=$3 AND role=$4"
        ).bind(key).bind(owner_type).bind(owner_id).bind(role)
            .execute(&self.db.pool).await?;
        Ok(())
    }
    async fn claim_expired(
        tx: &mut Transaction<'_, Postgres>,
        limit: i64,
    ) -> AppResult<Vec<String>> {
        let rows = sqlx::query(
            r#"WITH eligible AS (
                SELECT o.storage_key FROM vr2_objects o
                WHERE ((o.state='live' AND o.retain_until<=now())
                   OR (o.state='deleting' AND o.lease_until<now()))
                AND NOT EXISTS (SELECT 1 FROM vr2_object_refs r WHERE r.storage_key=o.storage_key)
                ORDER BY o.retain_until ASC
                FOR UPDATE OF o SKIP LOCKED LIMIT $1
            ) UPDATE vr2_objects o SET state='deleting',lease_until=now()+interval '5 minutes',
                updated_at=now() FROM eligible e WHERE o.storage_key=e.storage_key
                RETURNING o.storage_key"#,
        )
        .bind(limit.clamp(1, 100))
        .fetch_all(&mut **tx)
        .await?;
        Ok(rows
            .into_iter()
            .map(|row| row.get::<String, _>("storage_key"))
            .collect())
    }
    /// Make storage retention an explicit terminal event, never a full-disk scan.
    /// Reconcile completed failed/canceled flows and release their own objects.
    /// Never touch a pipeline with an uncertain external publisher.
    pub async fn reconcile_terminal_resources(
        &self,
        limit: i64,
        grace: Duration,
    ) -> AppResult<u64> {
        let rows = sqlx::query(
            r#"SELECT w.id,w.task_id FROM vr2_workflows w
               JOIN tasks t ON t.id=w.task_id
               WHERE w.state IN ('failed','canceled')
                 AND t.status::text IN ('FAILED','CANCELED')
                 AND NOT EXISTS (
                    SELECT 1 FROM vr2_events e WHERE e.workflow_id=w.id
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
        .fetch_all(&self.db.pool)
        .await?;
        let mut released = 0;
        for row in rows {
            let id: Uuid = row.try_get("id")?;
            let task_id: Uuid = row.try_get("task_id")?;
            match self.schedule_terminal_release(task_id, id, grace).await {
                Ok(_) => released += 1,
                Err(e) => tracing::warn!(workflow_id=%id,error=%e,"terminal cleanup deferred"),
            }
        }
        Ok(released)
    }
    pub async fn schedule_terminal_release(
        &self,
        task_id: Uuid,
        workflow_id: Uuid,
        grace: Duration,
    ) -> AppResult<u64> {
        let grace_seconds = grace.as_secs().clamp(3600, 30 * 86400) as i64;
        let mut tx = self.db.pool.begin().await?;
        let state: Option<String> =
            sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE")
                .bind(task_id)
                .fetch_optional(&mut *tx)
                .await?;
        if !matches!(state.as_deref(), Some("PUBLISHED" | "FAILED" | "CANCELED")) {
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
        let rows=sqlx::query(
            "DELETE FROM vr2_object_refs WHERE (owner_type='workflow' AND owner_id=$1) OR (owner_type='task' AND owner_id=$2) RETURNING storage_key"
        ).bind(workflow_id).bind(task_id).fetch_all(&mut *tx).await?;
        for row in &rows {
            let key: String = row.try_get("storage_key")?;
            if !key.starts_with("v2/") {
                return Err(AppError::Conflict("refused release of legacy file".into()));
            }
            sqlx::query(
                "UPDATE vr2_objects SET retain_until=GREATEST(now()+($1*interval '1 second'),manual_hold_until),updated_at=now() WHERE storage_key=$2 AND state='live'"
            ).bind(grace_seconds).bind(key).execute(&mut *tx).await?;
        }
        for category in ["raw", "sub", "render", "dubbing"] {
            let key = format!("v2/{category}/{task_id}");
            sqlx::query(
                "INSERT INTO vr2_workdirs(path_key,task_id,workflow_id,due_at) VALUES($1,$2,$3,now()+($4*interval '1 second')) ON CONFLICT(path_key) DO UPDATE SET due_at=GREATEST(vr2_workdirs.due_at,EXCLUDED.due_at),updated_at=now()"
            ).bind(key).bind(task_id).bind(workflow_id).bind(grace_seconds)
                .execute(&mut *tx).await?;
        }
        sqlx::query(
            "INSERT INTO vr2_events(id,workflow_id,kind,details) VALUES($1,$2,'storage.cleanup_scheduled',$3)"
        ).bind(Uuid::new_v4()).bind(workflow_id)
            .bind(serde_json::json!({"task_id":task_id,"grace_seconds":grace_seconds,"released_refs":rows.len()}))
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(rows.len() as u64)
    }
    /// Drop only v2-owned legacy display rows, after every relevant workflow is
    /// terminal and no ffplayout hardlink/copy relationship is using this asset.
    async fn remove_legacy_refs_if_safe(&self, key: &str) -> AppResult<bool> {
        let mut tx = self.db.pool.begin().await?;
        let rows=sqlx::query(
            "SELECT DISTINCT task_id FROM (SELECT task_id FROM assets WHERE storage_key=$1 UNION ALL SELECT task_id FROM subtitles WHERE storage_key=$1) v"
        ).bind(key).fetch_all(&mut *tx).await?;
        if rows.len() > 1 {
            return Ok(false);
        }
        if let Some(row) = rows.first() {
            let task_id: Uuid = row.try_get("task_id")?;
            let state: String =
                sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE")
                    .bind(task_id)
                    .fetch_one(&mut *tx)
                    .await?;
            if !matches!(state.as_str(), "PUBLISHED" | "FAILED" | "CANCELED") {
                return Ok(false);
            }
            let unfinished:(bool,)=sqlx::query_as(
                "SELECT EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown'))"
            ).bind(task_id).fetch_one(&mut *tx).await?;
            if unfinished.0 {
                return Ok(false);
            }
            let playout:(bool,)=sqlx::query_as(
                "SELECT EXISTS(SELECT 1 FROM playout_asset_links p JOIN assets a ON p.asset_id=a.id WHERE a.storage_key=$1)"
            ).bind(key).fetch_one(&mut *tx).await?;
            if playout.0 {
                return Ok(false);
            }
            let refs: (bool,) =
                sqlx::query_as("SELECT EXISTS(SELECT 1 FROM vr2_object_refs WHERE storage_key=$1)")
                    .bind(key)
                    .fetch_one(&mut *tx)
                    .await?;
            if refs.0 {
                return Ok(false);
            }
            sqlx::query("DELETE FROM subtitles WHERE task_id=$1 AND storage_key=$2")
                .bind(task_id)
                .bind(key)
                .execute(&mut *tx)
                .await?;
            sqlx::query("DELETE FROM assets WHERE task_id=$1 AND storage_key=$2")
                .bind(task_id)
                .bind(key)
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
        Ok(true)
    }
    /// A two-phase fenced deletion. Failed/deferred objects are retried by an
    /// indexed due-date query rather than periodic filesystem traversal.
    pub async fn collect(&self, limit: i64) -> AppResult<u64> {
        let mut tx = self.db.pool.begin().await?;
        let keys = Self::claim_expired(&mut tx, limit).await?;
        tx.commit().await?;
        let mut deleted = 0;
        for key in keys {
            let remaining: (bool,) =
                sqlx::query_as("SELECT EXISTS(SELECT 1 FROM vr2_object_refs WHERE storage_key=$1)")
                    .bind(&key)
                    .fetch_one(&self.db.pool)
                    .await?;
            if !key.starts_with("v2/")
                || remaining.0
                || !self.remove_legacy_refs_if_safe(&key).await?
            {
                sqlx::query("UPDATE vr2_objects SET state='live',lease_until=NULL,retain_until=GREATEST(now()+interval '10 minutes',manual_hold_until),updated_at=now() WHERE storage_key=$1")
                    .bind(&key).execute(&self.db.pool).await?;
                continue;
            }
            let deletion = async {
                let path = self.resolve_existing(&key).await?;
                match fs::remove_file(path).await {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(AppError::Io(e)),
                }
            }
            .await;
            match deletion {
                Ok(()) => {
                    sqlx::query("UPDATE vr2_objects SET state='deleted',lease_until=NULL,updated_at=now() WHERE storage_key=$1 AND state='deleting'")
                        .bind(&key).execute(&self.db.pool).await?;
                    deleted += 1;
                }
                Err(error) => {
                    tracing::warn!(key=%key,error=%error,"defer GC file unlink");
                    sqlx::query("UPDATE vr2_objects SET state='live',lease_until=NULL,retain_until=now()+interval '10 minutes',updated_at=now() WHERE storage_key=$1")
                        .bind(&key).execute(&self.db.pool).await?;
                }
            }
        }
        Ok(deleted)
    }
    /// Cleanup only explicit v2/{raw,sub,render,dubbing}/{UUID} directories.
    pub async fn collect_workdirs(&self, limit: i64) -> AppResult<u64> {
        let keys = sqlx::query(
            r#"WITH due AS (
              SELECT path_key FROM vr2_workdirs
              WHERE (state='retained' AND due_at<=now())
                 OR (state='deleting' AND lease_until<now())
              ORDER BY due_at FOR UPDATE SKIP LOCKED LIMIT $1
            ) UPDATE vr2_workdirs w SET state='deleting',lease_until=now()+interval '5 minutes',
              updated_at=now() FROM due WHERE w.path_key=due.path_key
              RETURNING w.path_key,w.task_id,w.workflow_id"#,
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
            let parents: (bool,)=sqlx::query_as(
                "SELECT EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown'))"
            ).bind(task_id).fetch_one(&self.db.pool).await?;
            let unfinished:(bool,)=sqlx::query_as(
                "SELECT EXISTS(SELECT 1 FROM vr2_objects WHERE storage_key LIKE ($1 || '/%') AND state<>'deleted')"
            ).bind(&key).fetch_one(&self.db.pool).await?;
            let legacy_ref:(bool,)=sqlx::query_as(
                "SELECT EXISTS(SELECT 1 FROM assets WHERE storage_key LIKE ($1 || '/%') UNION ALL SELECT 1 FROM subtitles WHERE storage_key LIKE ($1 || '/%'))"
            ).bind(&key).fetch_one(&self.db.pool).await?;
            let task_state: String =
                sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1")
                    .bind(task_id)
                    .fetch_one(&self.db.pool)
                    .await?;
            if !expected
                || parents.0
                || unfinished.0
                || legacy_ref.0
                || !matches!(task_state.as_str(), "PUBLISHED" | "FAILED" | "CANCELED")
            {
                sqlx::query("UPDATE vr2_workdirs SET state='retained',lease_until=NULL,due_at=now()+interval '10 minutes',updated_at=now() WHERE path_key=$1")
                    .bind(&key).execute(&self.db.pool).await?;
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
            match result {
                Ok(()) => {
                    sqlx::query("UPDATE vr2_workdirs SET state='deleted',lease_until=NULL,updated_at=now() WHERE path_key=$1 AND workflow_id=$2")
                        .bind(&key).bind(workflow_id).execute(&self.db.pool).await?;
                    cleaned += 1;
                }
                Err(e) => {
                    tracing::error!(directory=%key,error=%e,"workdir cleanup deferred");
                    sqlx::query("UPDATE vr2_workdirs SET state='retained',lease_until=NULL,due_at=now()+interval '10 minutes',last_error=$2,updated_at=now() WHERE path_key=$1")
                        .bind(&key).bind(e.to_string()).execute(&self.db.pool).await?;
                }
            }
        }
        Ok(cleaned)
    }
    pub async fn hold(&self, key: &str, extra: Duration) -> AppResult<()> {
        Self::checked_key(key)?;
        sqlx::query("UPDATE vr2_objects SET retain_until=GREATEST(retain_until,now()+($1*interval '1 second')),manual_hold_until=GREATEST(manual_hold_until,now()+($1*interval '1 second')),updated_at=now() WHERE storage_key=$2")
            .bind(extra.as_secs().min(31536000) as i64).bind(key)
            .execute(&self.db.pool).await?;
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
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
