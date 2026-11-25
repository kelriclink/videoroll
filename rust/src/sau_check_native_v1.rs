//! Original SAU account API, using the original accounts and operation_inbox
//! tables. Browser checks invoke only the configured third-party SAU CLI.
use crate::{
    AppError, AppResult,
    sau_account_native_v1::{SauState, account_busy, account_json, social_platform},
};
use axum::{
    Json,
    extract::{Multipart, Path, State},
};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use std::{
    path::{Path as FsPath, PathBuf},
    sync::Arc,
    time::Duration,
};
use uuid::Uuid;

const ENGINE: &str = "rust-sau-check";
const MAX_STATE: usize = 1024 * 1024;

pub(crate) fn name(raw: &str) -> AppResult<String> {
    let n = raw.trim();
    if n.is_empty()
        || n.len() > 64
        || !n.as_bytes()[0].is_ascii_alphanumeric()
        || !n
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
    {
        return Err(AppError::Invalid("invalid account name".into()));
    }
    Ok(n.into())
}
fn canonical(bytes: &[u8]) -> AppResult<String> {
    if bytes.len() > MAX_STATE {
        return Err(AppError::Invalid("storage_state exceeds 1 MiB".into()));
    }
    let mut obj: Value = serde_json::from_slice(bytes)
        .map_err(|_| AppError::Invalid("storage_state must be valid UTF-8 JSON".into()))?;
    let map = obj
        .as_object_mut()
        .ok_or_else(|| AppError::Invalid("storage_state root must be an object".into()))?;
    if !map.get("cookies").is_some_and(Value::is_array) {
        return Err(AppError::Invalid(
            "storage_state must contain a cookies array".into(),
        ));
    }
    if !map.entry("origins").or_insert(json!([])).is_array() {
        return Err(AppError::Invalid(
            "storage_state origins must be an array".into(),
        ));
    }
    Ok(obj.to_string())
}
fn enabled(state: &SauState) -> AppResult<()> {
    if !state.check_enabled {
        return Err(AppError::Conflict(
            "native SAU account check worker not explicitly enabled".into(),
        ));
    }
    Ok(())
}

async fn pending(tx: &mut Transaction<'_, Postgres>, id: Uuid) -> AppResult<Option<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT id FROM operation_inbox WHERE runtime_engine='rust-sau-check' AND status='pending'
      AND request_json->>'account_id'=$1 ORDER BY created_at DESC LIMIT 1",
    )
    .bind(id.to_string())
    .fetch_optional(&mut **tx)
    .await?)
}
async fn enqueue(tx: &mut Transaction<'_, Postgres>, id: Uuid) -> AppResult<Uuid> {
    if let Some(op) = pending(tx, id).await? {
        return Ok(op);
    }
    let op = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,request_json)
        VALUES($1,$2,$3,'pending',$4)",
    )
    .bind(op)
    .bind(format!("sau-account-check:{id}:{op}"))
    .bind(ENGINE)
    .bind(json!({"account_id":id}))
    .execute(&mut **tx)
    .await?;
    Ok(op)
}
async fn response(
    tx: &mut Transaction<'_, Postgres>,
    id: Uuid,
    op: Uuid,
) -> AppResult<Json<Value>> {
    let row=sqlx::query("SELECT id,platform::text AS platform,name,is_active,check_state,last_checked_at,last_check_message,rotated_at FROM accounts WHERE id=$1")
        .bind(id).fetch_one(&mut **tx).await?;
    Ok(Json(
        json!({"account":account_json(&row)?,"check_job_id":op.to_string()}),
    ))
}
pub(crate) async fn import_account(
    State(state): State<Arc<SauState>>,
    Path(raw): Path<String>,
    mut form: Multipart,
) -> AppResult<Json<Value>> {
    enabled(&state)?;
    let platform = social_platform(&raw)?;
    let mut account_name = None;
    let mut file = None;
    while let Some(field) = form
        .next_field()
        .await
        .map_err(|_| AppError::Invalid("invalid multipart form".into()))?
    {
        match field.name() {
            Some("account_name") if account_name.is_none() => {
                account_name =
                    Some(name(&field.text().await.map_err(|_| {
                        AppError::Invalid("invalid account name".into())
                    })?)?)
            }
            Some("file") if file.is_none() => {
                file = Some(canonical(&field.bytes().await.map_err(|_| {
                    AppError::Invalid("invalid storage_state upload".into())
                })?)?)
            }
            _ => {
                return Err(AppError::Invalid(
                    "unexpected or duplicate multipart field".into(),
                ));
            }
        }
    }
    let name = account_name.ok_or_else(|| AppError::Invalid("account_name is required".into()))?;
    let secret = format!(
        "dbplain:v1:{}",
        file.ok_or_else(|| AppError::Invalid("file is required".into()))?
    );
    let mut tx = state.pool.begin().await?;
    crate::sau_login_native_v1::lock_name(&mut tx, platform, &name).await?;
    crate::sau_login_native_v1::ensure_not_logging_in(&mut tx, platform, &name).await?;
    // Same unique constraint as Python; serialize simultaneous new imports
    // before examining the existing account (including a disabled account).
    sqlx::query(
        "INSERT INTO accounts(id,platform,name,secrets_encrypted,is_active,check_state)
        VALUES($1,$2::platform,$3,'',false,'unchecked') ON CONFLICT(platform,name) DO NOTHING",
    )
    .bind(Uuid::new_v4())
    .bind(platform)
    .bind(&name)
    .execute(&mut *tx)
    .await?;
    let row=sqlx::query("SELECT id,check_state,is_active,secrets_encrypted FROM accounts WHERE platform::text=$1 AND name=$2 FOR UPDATE")
        .bind(platform).bind(name).fetch_one(&mut *tx).await?;
    let id: Uuid = row.try_get("id")?;
    account_busy(&mut tx, id, &row.try_get::<String, _>("check_state")?).await?;
    // Rotate credentials only when necessary. A duplicate queued upload
    // reuses its durable check ID instead of creating another browser check.
    if row.try_get::<String, _>("check_state")? == "queued"
        && row.try_get::<bool, _>("is_active")?
        && row.try_get::<String, _>("secrets_encrypted")? == secret
    {
        if let Some(op) = pending(&mut tx, id).await? {
            let res = response(&mut tx, id, op).await?;
            tx.commit().await?;
            return Ok(res);
        }
    }
    sqlx::query("UPDATE operation_inbox SET status='completed',result_json='{\"status\":\"superseded\"}'::jsonb,completed_at=now(),updated_at=now()
      WHERE runtime_engine=$1 AND status='pending' AND request_json->>'account_id'=$2")
        .bind(ENGINE).bind(id.to_string()).execute(&mut *tx).await?;
    sqlx::query("UPDATE accounts SET secrets_encrypted=$2,rotated_at=now(),is_active=true,check_state='queued',last_checked_at=NULL,last_check_message=NULL WHERE id=$1")
        .bind(id).bind(secret).execute(&mut *tx).await?;
    let op = enqueue(&mut tx, id).await?;
    let res = response(&mut tx, id, op).await?;
    tx.commit().await?;
    Ok(res)
}
pub(crate) async fn recheck(
    State(state): State<Arc<SauState>>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    enabled(&state)?;
    let mut tx = state.pool.begin().await?;
    let row=sqlx::query("SELECT platform::text AS platform,is_active,secrets_encrypted,check_state FROM accounts WHERE id=$1 FOR UPDATE")
        .bind(id).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::NotFound("social account not found".into()))?;
    if social_platform(&row.try_get::<String, _>("platform")?).is_err() {
        return Err(AppError::NotFound("social account not found".into()));
    }
    if !row.try_get::<bool, _>("is_active")?
        || row.try_get::<String, _>("secrets_encrypted")?.is_empty()
    {
        return Err(AppError::Invalid("social account is inactive".into()));
    }
    account_busy(&mut tx, id, &row.try_get::<String, _>("check_state")?).await?;
    sqlx::query("UPDATE accounts SET check_state='queued',last_check_message=NULL WHERE id=$1")
        .bind(id)
        .execute(&mut *tx)
        .await?;
    let op = enqueue(&mut tx, id).await?;
    let res = response(&mut tx, id, op).await?;
    tx.commit().await?;
    Ok(res)
}

// One native checker process per SAU cookie directory. Acquire an advisory
// file lock at startup; no credentials are put into operation payloads.
pub(crate) struct Runtime {
    pub(crate) executable: PathBuf,
    pub(crate) cwd: PathBuf,
    pub(crate) cookies: PathBuf,
    timeout: Duration,
    _lock: std::fs::File,
}
impl Runtime {
    pub(crate) fn from_env() -> AppResult<Self> {
        use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd};
        let cwd = std::fs::canonicalize(
            std::env::var("SAU_RUNTIME_DIR").unwrap_or_else(|_| "/opt/social-auto-upload".into()),
        )?;
        let cookies = std::fs::canonicalize(
            std::env::var("SAU_COOKIES_DIR")
                .unwrap_or_else(|_| cwd.join("cookies").to_string_lossy().into_owned()),
        )?;
        // SAU uses its cwd/cookies directory. Never create credential files
        // in a different directory from the configured CLI.
        if std::fs::canonicalize(cwd.join("cookies"))? != cookies {
            return Err(AppError::Invalid(
                "SAU_COOKIES_DIR must match runtime cookies directory".into(),
            ));
        }
        let executable =
            PathBuf::from(std::env::var("SAU_EXECUTABLE").unwrap_or_else(|_| "sau".into()));
        let secs = std::env::var("SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(120.0);
        if !secs.is_finite() || !(1.0..=600.0).contains(&secs) {
            return Err(AppError::Invalid(
                "SAU check timeout must be 1..600 seconds".into(),
            ));
        }
        let lock = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(cookies.join(".videoroll-native-check.lock"))?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(AppError::Conflict(
                "another native SAU checker owns the cookie directory".into(),
            ));
        }
        Ok(Self {
            executable,
            cwd,
            cookies,
            timeout: Duration::from_secs_f64(secs),
            _lock: lock,
        })
    }
}
struct Work {
    op: Uuid,
    id: Uuid,
    platform: String,
    name: String,
    secret: String,
    rotated: Option<DateTime<Utc>>,
}
async fn claim(state: &SauState, runtime: &Runtime) -> AppResult<Option<Work>> {
    // Lock order is account -> operation, as in the HTTP import/recheck path.
    // Select candidate without locking, then recheck status under row lock.
    let candidate = sqlx::query(
        "SELECT id,request_json->>'account_id' AS account_id FROM operation_inbox
       WHERE runtime_engine='rust-sau-check' AND status='pending' ORDER BY created_at,id LIMIT 1",
    )
    .fetch_optional(&state.pool)
    .await?;
    let Some(candidate) = candidate else {
        return Ok(None);
    };
    let op: Uuid = candidate.try_get("id")?;
    let id = Uuid::parse_str(&candidate.try_get::<String, _>("account_id")?)
        .map_err(|_| AppError::Invalid("invalid SAU check record".into()))?;
    let mut tx = state.pool.begin().await?;
    let account=sqlx::query("SELECT platform::text AS platform,name,secrets_encrypted,is_active,check_state,rotated_at FROM accounts WHERE id=$1 FOR UPDATE")
        .bind(id).fetch_optional(&mut *tx).await?;
    let pending: Option<String> = sqlx::query_scalar(
        "SELECT status FROM operation_inbox WHERE id=$1 AND runtime_engine=$2 FOR UPDATE",
    )
    .bind(op)
    .bind(ENGINE)
    .fetch_optional(&mut *tx)
    .await?;
    if pending.as_deref() != Some("pending") {
        return Ok(None);
    }
    let work = if let Some(row) = account {
        let active: bool = row.try_get("is_active")?;
        let status: String = row.try_get("check_state")?;
        let p: String = row.try_get("platform")?;
        if active && status == "queued" && social_platform(&p).is_ok() {
            account_busy(&mut tx, id, &status).await?;
            Some(Work {
                op,
                id,
                platform: p,
                name: row.try_get("name")?,
                secret: row.try_get("secrets_encrypted")?,
                rotated: row.try_get("rotated_at")?,
            })
        } else {
            None
        }
    } else {
        None
    };
    if work.is_some() {
        sqlx::query(
            "UPDATE accounts SET check_state='checking',last_check_message=NULL WHERE id=$1",
        )
        .bind(id)
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE operation_inbox SET status='processing',attempt_count=attempt_count+1,lease_owner='rust-sau-check',
          lease_until=now()+($2::bigint * interval '1 second'),heartbeat_at=now(),updated_at=now() WHERE id=$1")
            .bind(op).bind(runtime.timeout.as_secs() as i64+60).execute(&mut *tx).await?;
    } else {
        sqlx::query("UPDATE operation_inbox SET status='completed',result_json='{\"status\":\"skipped\"}'::jsonb,completed_at=now(),updated_at=now() WHERE id=$1")
            .bind(op).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(work)
}

// A crash can leave a browser or cookie file behind. Never automatically
// replay an in-flight check. Mark expired checks as error and require explicit
// retry; create_new on the cookie file fences any leftover credential file.
async fn recover(state: &SauState) -> AppResult<()> {
    let rows = sqlx::query(
        "SELECT id,request_json->>'account_id' AS account_id FROM operation_inbox
      WHERE runtime_engine='rust-sau-check' AND status='processing' AND lease_until<=now() LIMIT 32",
    )
    .fetch_all(&state.pool)
    .await?;
    for row in rows {
        let op: Uuid = row.try_get("id")?;
        let id = Uuid::parse_str(&row.try_get::<String, _>("account_id")?)
            .map_err(|_| AppError::Invalid("invalid SAU check record".into()))?;
        let mut tx = state.pool.begin().await?;
        sqlx::query("SELECT id FROM accounts WHERE id=$1 FOR UPDATE")
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?;
        let changed=sqlx::query("UPDATE operation_inbox SET status='completed',result_json='{\"status\":\"error\"}'::jsonb,
          last_error='SAU check interrupted; verify stale cookie file before retry',completed_at=now(),lease_owner=NULL,lease_until=NULL,updated_at=now()
          WHERE id=$1 AND runtime_engine=$2 AND status='processing' AND lease_until<=now()")
            .bind(op).bind(ENGINE).execute(&mut *tx).await?.rows_affected();
        if changed == 1 {
            sqlx::query("UPDATE accounts SET check_state='error',last_checked_at=now(),last_check_message='SAU check interrupted; verify stale cookie file before retry'
              WHERE id=$1 AND check_state='checking'").bind(id).execute(&mut *tx).await?;
        }
        tx.commit().await?;
    }
    Ok(())
}
pub(crate) struct CookieFile(pub(crate) PathBuf);
impl Drop for CookieFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
fn materialize(runtime: &Runtime, work: &Work) -> AppResult<CookieFile> {
    materialize_account_state(runtime, &work.platform, &work.name, &work.secret)
}
pub(crate) fn materialize_account_state(
    runtime: &Runtime,
    platform: &str,
    account_name: &str,
    secret: &str,
) -> AppResult<CookieFile> {
    use std::{io::Write, os::unix::fs::OpenOptionsExt};
    let raw = crate::secret_compat::decode(secret)?
        .ok_or_else(|| AppError::Invalid("SAU state is empty".into()))?;
    let canonical = canonical(raw.as_bytes())?;
    let file = runtime.cookies.join(format!(
        "{}_{}.json",
        social_platform(platform)?,
        name(account_name)?
    ));
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&file)
        .map_err(|_| {
            AppError::Conflict("SAU cookie file already exists or cannot be created".into())
        })?;
    let guard = CookieFile(file);
    f.write_all(canonical.as_bytes())?;
    f.sync_all()?;
    Ok(guard)
}
pub(crate) fn refreshed(path: &FsPath) -> AppResult<String> {
    use std::{io::Read, os::unix::fs::OpenOptionsExt};
    let f = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    if !f.metadata()?.is_file() {
        return Err(AppError::Invalid("invalid SAU state file".into()));
    }
    let mut bytes = Vec::new();
    f.take((MAX_STATE + 1) as u64).read_to_end(&mut bytes)?;
    Ok(format!("dbplain:v1:{}", canonical(&bytes)?))
}
async fn execute(runtime: &Runtime, work: &Work) -> (String, String, Option<String>) {
    let file = match materialize(runtime, work) {
        Ok(file) => file,
        Err(_) => {
            return (
                "error".into(),
                "SAU state unavailable; check credentials or stale cookie file".into(),
                None,
            );
        }
    };
    let parent = std::process::id() as libc::pid_t;
    let mut command = tokio::process::Command::new(&runtime.executable);
    command
        .current_dir(&runtime.cwd)
        .args([
            work.platform.as_str(),
            "check",
            "--account",
            work.name.as_str(),
        ])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .process_group(0);
    // Linux parent-death signal prevents the SAU leader surviving a killed
    // checker; process group cleanup on normal completion/timeout.
    unsafe {
        command.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                return Err(std::io::Error::other("SAU parent exited"));
            }
            Ok(())
        });
    }
    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(_) => {
            return (
                "error".into(),
                "SAU check process could not start".into(),
                None,
            );
        }
    };
    let pgid = child.id().map(|v| v as libc::pid_t);
    let result = tokio::time::timeout(runtime.timeout, child.wait()).await;
    if let Some(pgid) = pgid {
        unsafe {
            libc::kill(-pgid, libc::SIGKILL);
        }
    }
    let _ = child.wait().await;
    let (status, message) = match result {
        Ok(Ok(s)) if s.success() => ("valid", "cookie valid"),
        Ok(Ok(_)) => ("invalid", "SAU check failed"),
        Err(_) => ("invalid", "SAU check timed out"),
        _ => ("error", "SAU check process failed"),
    };
    // Only persist a valid replacement from a successful browser check.
    // Raw CLI stdout/stderr are never stored (they can contain cookies).
    if status == "valid" {
        match refreshed(&file.0) {
            Ok(secret) => (status.into(), message.into(), Some(secret)),
            Err(_) => (
                "error".into(),
                "SAU returned invalid storage_state".into(),
                None,
            ),
        }
    } else {
        (status.into(), message.into(), None)
    }
}
async fn finish(
    state: &SauState,
    work: &Work,
    result: (String, String, Option<String>),
) -> AppResult<()> {
    let (status, message, secret) = result;
    let mut tx = state.pool.begin().await?;
    sqlx::query("SELECT id FROM accounts WHERE id=$1 FOR UPDATE")
        .bind(work.id)
        .fetch_optional(&mut *tx)
        .await?;
    let changed=sqlx::query("UPDATE operation_inbox SET status='completed',result_json=$2,completed_at=now(),updated_at=now(),
      lease_owner=NULL,lease_until=NULL WHERE id=$1 AND runtime_engine=$3 AND status='processing' AND lease_owner='rust-sau-check'")
        .bind(work.op).bind(json!({"status":status})).bind(ENGINE).execute(&mut *tx).await?.rows_affected();
    if changed == 1 {
        sqlx::query("UPDATE accounts SET check_state=$2,last_checked_at=now(),last_check_message=$3,secrets_encrypted=COALESCE($4,secrets_encrypted)
          WHERE id=$1 AND is_active AND check_state='checking' AND rotated_at IS NOT DISTINCT FROM $5 AND secrets_encrypted=$6")
            .bind(work.id).bind(status).bind(message).bind(secret).bind(work.rotated).bind(&work.secret).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(())
}
pub async fn run(state: Arc<SauState>, runtime: Runtime) {
    loop {
        let result: AppResult<bool> = async {
            recover(&state).await?;
            if crate::sau_login_native_v1::tick(&state, &runtime).await? {
                return Ok(true);
            }
            if let Some(work) = claim(&state, &runtime).await? {
                let outcome = execute(&runtime, &work).await;
                finish(&state, &work, outcome).await?;
                Ok(true)
            } else {
                crate::sau_worker_native_v1::tick(&state, &runtime).await
            }
        }
        .await;
        match result {
            Ok(true) => continue,
            Ok(false) => {}
            Err(_) => tracing::warn!("SAU native check tick failed; retrying durable queue scan"),
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validate_original_storage_contract() {
        assert_eq!(
            canonical(br#"{"cookies":[]}"#).unwrap(),
            r#"{"cookies":[],"origins":[]}"#
        );
        for bad in [
            br#"[]"#.as_slice(),
            br#"{"cookies":{}}"#,
            br#"{"cookies":[],"origins":null}"#,
            b"\xff",
        ] {
            assert!(canonical(bad).is_err());
        }
        assert!(canonical(&vec![0; MAX_STATE + 1]).is_err());
        assert_eq!(name(" foo-1 ").unwrap(), "foo-1");
        for bad in ["../a", "a/b", "a\\b", ".a", "中文", ""] {
            assert!(name(bad).is_err());
        }
    }
}
