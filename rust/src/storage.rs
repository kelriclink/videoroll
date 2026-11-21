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
        let mut buf = [0u8; 1024 * 256];
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
    /// Two-phase cleanup: lease the candidate, remove the verified path, then commit a tombstone.
    /// Existing v1 assets and subtitles always block deletion even if v2 references are missing.
    pub async fn collect(&self, limit: i64) -> AppResult<u64> {
        let mut tx = self.db.pool.begin().await?;
        let keys = Self::claim_expired(&mut tx, limit).await?;
        tx.commit().await?;
        let mut deleted = 0;
        for key in keys {
            // No deletion of old files tracked by the Python schema.
            let v1_ref:(bool,)=sqlx::query_as(
                "SELECT EXISTS(SELECT 1 FROM assets WHERE storage_key=$1 UNION ALL SELECT 1 FROM subtitles WHERE storage_key=$1)"
            ).bind(&key).fetch_one(&self.db.pool).await?;
            let active_refs: (bool,) =
                sqlx::query_as("SELECT EXISTS(SELECT 1 FROM vr2_object_refs WHERE storage_key=$1)")
                    .bind(&key)
                    .fetch_one(&self.db.pool)
                    .await?;
            if !key.starts_with("v2/") || v1_ref.0 || active_refs.0 {
                sqlx::query("UPDATE vr2_objects SET state='live',lease_until=NULL,retain_until=GREATEST(retain_until,now()+interval '1 day') WHERE storage_key=$1")
                    .bind(&key).execute(&self.db.pool).await?;
                continue;
            }
            let removal = async {
                let path = self.resolve_existing(&key).await?;
                match fs::remove_file(path).await {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(AppError::Io(e)),
                }
            }
            .await;
            match removal {
                Ok(()) => {
                    sqlx::query("UPDATE vr2_objects SET state='deleted',lease_until=NULL,updated_at=now() WHERE storage_key=$1 AND state='deleting'")
                        .bind(&key).execute(&self.db.pool).await?;
                    deleted += 1;
                }
                Err(e) => {
                    tracing::error!(storage_key=%key,error=%e,"asset deletion deferred");
                    sqlx::query("UPDATE vr2_objects SET state='live',lease_until=NULL,retain_until=now()+interval '10 minutes',updated_at=now() WHERE storage_key=$1")
                        .bind(&key).execute(&self.db.pool).await?;
                }
            }
        }
        Ok(deleted)
    }
    pub async fn hold(&self, key: &str, extra: Duration) -> AppResult<()> {
        Self::checked_key(key)?;
        sqlx::query("UPDATE vr2_objects SET retain_until=GREATEST(retain_until,now()+($1*interval '1 second')),updated_at=now() WHERE storage_key=$2")
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
