//! Rust-owned SAU durable consumer. SAU is only the browser execution engine.
//! Never replay an operation after a browser may have been started.
use crate::{
    AppError, AppResult, Database,
    sau_account_native_v1::SauState,
    sau_check_native_v1::{Runtime, materialize_account_state, name, refreshed},
    storage::AssetRegistry,
};
use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use std::{path::PathBuf, time::Duration};
use uuid::Uuid;

const ENGINE: &str = "rust-sau-publish";
pub(crate) struct PublishConfig {
    root: PathBuf,
    timeout: Duration,
    display: String,
}
impl PublishConfig {
    pub(crate) fn from_env() -> AppResult<Self> {
        let root = std::env::var_os("STORAGE_ROOT")
            .or_else(|| std::env::var_os("VIDEOROLL_V2_STORAGE_ROOT"))
            .ok_or_else(|| {
                AppError::Invalid("native SAU requires explicit shared STORAGE_ROOT".into())
            })?;
        let root = std::fs::canonicalize(root)?;
        if !root.is_dir() {
            return Err(AppError::Invalid(
                "SAU storage root is not a directory".into(),
            ));
        }
        let secs = std::env::var("SAU_UPLOAD_TIMEOUT_SECONDS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(3600.0);
        if !secs.is_finite() || !(1.0..=14400.0).contains(&secs) {
            return Err(AppError::Invalid(
                "SAU upload timeout must be 1..14400 seconds".into(),
            ));
        }
        Ok(Self {
            root,
            timeout: Duration::from_secs_f64(secs),
            display: std::env::var("SOCIAL_LOGIN_DISPLAY").unwrap_or_else(|_| ":99".into()),
        })
    }
}
pub(crate) async fn enqueue(
    tx: &mut Transaction<'_, Postgres>,
    job: Uuid,
    key: &str,
) -> AppResult<()> {
    // Native statuses are deliberately outside the old Python dispatcher's
    // pending/failed/dispatching claim set. No Redis/Celery message is sent.
    sqlx::query("INSERT INTO outbox_events(id,event_type,aggregate_type,aggregate_id,task_name,args_json,operation_key,status)
        SELECT $1,'publish.dispatch','publish_job',$2,'social_publisher.process_job',$3,$4,'native_pending'
        WHERE NOT EXISTS(SELECT 1 FROM outbox_events WHERE operation_key=$4)")
        .bind(Uuid::new_v4()).bind(job.to_string()).bind(json!({"args":[job.to_string()],"queue":"social_publish"}))
        .bind(key).execute(&mut **tx).await?;
    let native:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM outbox_events WHERE operation_key=$1 AND status IN ('native_pending','native_running','native_completed'))")
        .bind(key).fetch_one(&mut **tx).await?;
    if !native {
        return Err(AppError::Conflict(
            "legacy SAU dispatch exists; drain or reconcile it before native cutover".into(),
        ));
    }
    sqlx::query(
        "INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,request_json)
      VALUES($1,$2,$3,'pending',$4) ON CONFLICT(operation_key) DO NOTHING",
    )
    .bind(Uuid::new_v4())
    .bind(key)
    .bind(ENGINE)
    .bind(json!({"job_id":job}))
    .execute(&mut **tx)
    .await?;
    let native: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key=$1 AND runtime_engine=$2)",
    )
    .bind(key)
    .bind(ENGINE)
    .fetch_one(&mut **tx)
    .await?;
    if !native {
        return Err(AppError::Conflict(
            "legacy operation owns SAU dispatch".into(),
        ));
    }
    Ok(())
}
struct Work {
    event: Uuid,
    job: Uuid,
    task: Uuid,
    account: Option<Uuid>,
    platform: String,
    key: String,
    owner: String,
    meta: Value,
    name: Option<String>,
    secret: Option<String>,
    rotated: Option<DateTime<Utc>>,
    valid: bool,
}
async fn lock_task(tx: &mut Transaction<'_, Postgres>, task: Uuid) -> AppResult<String> {
    sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE")
        .bind(task)
        .fetch_one(&mut **tx)
        .await
        .map_err(Into::into)
}
async fn claim(state: &SauState, cfg: &PublishConfig) -> AppResult<Option<Work>> {
    let candidate=sqlx::query("SELECT e.id AS event,j.id AS job,j.task_id,j.account_id
        FROM outbox_events e JOIN publish_jobs j ON j.operation_key=e.operation_key AND j.id::text=e.aggregate_id
        WHERE e.task_name='social_publisher.process_job' AND e.status='native_pending'
        AND e.available_at<=now()
        AND (j.state::text<>'submitting' OR (j.started_at IS NULL AND j.lease_owner IS NULL AND j.lease_until IS NULL))
        ORDER BY e.available_at,e.created_at,e.id LIMIT 1")
        .fetch_optional(&state.pool).await?;
    let Some(c) = candidate else { return Ok(None) };
    let event: Uuid = c.try_get("event")?;
    let job: Uuid = c.try_get("job")?;
    let task: Uuid = c.try_get("task_id")?;
    let account: Option<Uuid> = c.try_get("account_id")?;
    let mut tx = state.pool.begin().await?;
    // Serialize claims even if a second instance is accidentally pointed
    // at the same DB with a different cookie directory.
    sqlx::query(
        "SELECT pg_advisory_xact_lock(hashtextextended('videoroll:native-sau-publication',0))",
    )
    .execute(&mut *tx)
    .await?;
    let busy:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM outbox_events WHERE task_name='social_publisher.process_job' AND status='native_running')")
        .fetch_one(&mut *tx).await?;
    if busy {
        return Ok(None);
    }
    let task_status = lock_task(&mut tx, task).await?;
    let acct = if let Some(id) = account {
        sqlx::query("SELECT platform::text AS platform,name,secrets_encrypted,is_active,check_state,rotated_at
          FROM accounts WHERE id=$1 FOR UPDATE").bind(id).fetch_optional(&mut *tx).await?
    } else {
        None
    };
    let row=sqlx::query("SELECT platform::text AS platform,state::text AS state,meta_json,operation_key,batch_id,started_at,lease_owner,lease_until
      FROM publish_jobs WHERE id=$1 FOR UPDATE").bind(job).fetch_one(&mut *tx).await?;
    let queued: Option<String> =
        sqlx::query_scalar("SELECT status FROM outbox_events WHERE id=$1 FOR UPDATE")
            .bind(event)
            .fetch_optional(&mut *tx)
            .await?;
    if queued.as_deref() != Some("native_pending") {
        return Ok(None);
    }
    let key: String = row.try_get("operation_key")?;
    let status: String = row.try_get("state")?;
    if status != "submitting" {
        sqlx::query("UPDATE outbox_events SET status='native_completed',delivered_at=now(),updated_at=now() WHERE id=$1").bind(event).execute(&mut *tx).await?;
        sqlx::query("UPDATE operation_inbox SET status='completed',result_json=$2,completed_at=now(),updated_at=now()
          WHERE operation_key=$1 AND runtime_engine=$3 AND status='pending'").bind(&key).bind(json!({"status":status,"job_id":job})).bind(ENGINE).execute(&mut *tx).await?;
        tx.commit().await?;
        return Ok(None);
    }
    // Only a never-started, unleased native operation is claimable. Any
    // unexpected existing lease/started marker goes to manual reconciliation.
    if row
        .try_get::<Option<DateTime<Utc>>, _>("started_at")?
        .is_some()
        || row.try_get::<Option<String>, _>("lease_owner")?.is_some()
        || row
            .try_get::<Option<DateTime<Utc>>, _>("lease_until")?
            .is_some()
    {
        return Err(AppError::Conflict(
            "native pending SAU job already has execution evidence".into(),
        ));
    }
    let p: String = row.try_get("platform")?;
    let mut valid = task_status != "canceled";
    let (n, secret, rotated) = if let Some(a) = acct {
        valid &= a.try_get::<bool, _>("is_active")?
            && a.try_get::<String, _>("check_state")? == "valid"
            && a.try_get::<String, _>("platform")? == p;
        let n: String = a.try_get("name")?;
        if valid {
            crate::sau_login_native_v1::ensure_not_logging_in(&mut tx, &p, &n).await?;
        }
        (
            Some(n),
            Some(a.try_get::<String, _>("secrets_encrypted")?),
            a.try_get::<Option<DateTime<Utc>>, _>("rotated_at")?,
        )
    } else {
        valid = false;
        (None, None, None)
    };
    let owner = format!("rust-sau-publish:{}", Uuid::new_v4());
    let lease = cfg.timeout.as_secs() as i64 + 120;
    let changed=sqlx::query("UPDATE operation_inbox SET status='processing',lease_owner=$2,lease_until=now()+($3::bigint*interval '1 second'),
      heartbeat_at=now(),attempt_count=attempt_count+1,updated_at=now()
      WHERE operation_key=$1 AND runtime_engine=$4 AND status='pending'")
      .bind(&key).bind(&owner).bind(lease).bind(ENGINE).execute(&mut *tx).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict(
            "SAU native operation is not pending".into(),
        ));
    }
    sqlx::query("UPDATE publish_jobs SET lease_owner=$2,lease_until=now()+($3::bigint*interval '1 second'),heartbeat_at=now(),updated_at=now()
      WHERE id=$1").bind(job).bind(&owner).bind(lease).execute(&mut *tx).await?;
    sqlx::query("UPDATE outbox_events SET status='native_running',lease_owner=$2,lease_until=now()+($3::bigint*interval '1 second'),
      heartbeat_at=now(),attempt_count=attempt_count+1,updated_at=now() WHERE id=$1")
        .bind(event).bind(&owner).bind(lease).execute(&mut *tx).await?;
    let work = Work {
        event,
        job,
        task,
        account,
        platform: p,
        key,
        owner,
        meta: row.try_get("meta_json")?,
        name: n,
        secret,
        rotated,
        valid,
    };
    tx.commit().await?;
    Ok(Some(work))
}

async fn media(
    state: &SauState,
    cfg: &PublishConfig,
    work: &Work,
    key: &str,
    cover: bool,
) -> AppResult<PathBuf> {
    AssetRegistry::checked_key(key)?;
    let belongs:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND storage_key=$2
      AND (($3 AND kind::text='cover_image') OR (NOT $3 AND kind::text IN ('video_raw','video_final','video_dubbed'))))")
        .bind(work.task).bind(key).bind(cover).fetch_one(&state.pool).await?;
    if !belongs {
        return Err(AppError::Invalid(
            "SAU asset no longer belongs to task".into(),
        ));
    }
    let path = AssetRegistry::new(
        Database {
            pool: state.pool.clone(),
        },
        cfg.root.clone(),
    )
    .resolve_existing(key)
    .await?;
    let info = tokio::fs::metadata(&path).await?;
    if !info.is_file() || info.len() == 0 {
        return Err(AppError::Invalid("SAU media missing or empty".into()));
    }
    Ok(path)
}
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
async fn args(state: &SauState, cfg: &PublishConfig, work: &Work) -> AppResult<Vec<String>> {
    let p = crate::sau_account_native_v1::social_platform(&work.platform)?;
    let n = name(work.name.as_deref().unwrap_or(""))?;
    let video = media(state, cfg, work, text(&work.meta["video"]["key"]), false).await?;
    let title = text(&work.meta["meta"]["title"]).trim();
    let desc = text(&work.meta["meta"]["desc"]);
    if title.is_empty() || title.contains('\0') || desc.contains('\0') {
        return Err(AppError::Invalid("invalid SAU metadata".into()));
    }
    let mut args = vec![
        p.into(),
        "upload-video".into(),
        "--account".into(),
        n,
        "--file".into(),
        video.to_string_lossy().into_owned(),
        "--title".into(),
        title.into(),
        "--desc".into(),
        if p == "douyin" {
            desc.chars().take(1000).collect()
        } else {
            desc.into()
        },
    ];
    if let Some(tags) = work.meta["meta"]["tags"].as_array() {
        let mut seen = std::collections::HashSet::new();
        let tags = tags
            .iter()
            .filter_map(Value::as_str)
            .map(|v| v.trim().trim_start_matches('#').trim())
            .filter(|v| {
                !v.is_empty()
                    && (p != "douyin"
                        || (!v.eq_ignore_ascii_case("videoroll") && seen.insert(v.to_lowercase())))
            })
            .take(if p == "douyin" { 4 } else { 100 })
            .collect::<Vec<_>>();
        if tags.iter().any(|v| v.contains('\0')) {
            return Err(AppError::Invalid("invalid SAU tag".into()));
        }
        if !tags.is_empty() {
            args.extend(["--tags".into(), tags.join(",")]);
        }
    }
    let cover = text(&work.meta["cover"]["key"]);
    if !cover.is_empty() && p != "douyin" {
        args.extend([
            "--thumbnail".into(),
            media(state, cfg, work, cover, true)
                .await?
                .to_string_lossy()
                .into_owned(),
        ]);
    }
    let schedule = text(&work.meta["platform_options"]["schedule"]).trim();
    if !schedule.is_empty() {
        if NaiveDateTime::parse_from_str(schedule, "%Y-%m-%d %H:%M").is_err() {
            return Err(AppError::Invalid("invalid SAU schedule".into()));
        }
        args.extend(["--schedule".into(), schedule.into()]);
    }
    args.push(if p == "douyin" {
        "--headed".into()
    } else {
        "--headless".into()
    });
    Ok(args)
}
struct Outcome {
    status: &'static str,
    message: &'static str,
    code: Option<i32>,
    timed_out: bool,
    started: bool,
    secret: Option<String>,
}
impl Outcome {
    fn before(message: &'static str) -> Self {
        Self {
            status: "failed",
            message,
            code: None,
            timed_out: false,
            started: false,
            secret: None,
        }
    }
    fn uncertain(message: &'static str) -> Self {
        Self {
            status: "unknown",
            message,
            code: None,
            timed_out: false,
            started: true,
            secret: None,
        }
    }
}
async fn execute(state: &SauState, runtime: &Runtime, cfg: &PublishConfig, work: &Work) -> Outcome {
    if !work.valid {
        return Outcome::before("task canceled or account not valid");
    }
    let args = match args(state, cfg, work).await {
        Ok(args) => args,
        Err(_) => return Outcome::before("SAU media or metadata validation failed"),
    };
    let file = match materialize_account_state(
        runtime,
        &work.platform,
        work.name.as_deref().unwrap_or(""),
        work.secret.as_deref().unwrap_or(""),
    ) {
        Ok(file) => file,
        Err(_) => return Outcome::before("SAU credentials unavailable or stale cookie file"),
    };
    // Persist the irreversible boundary before invoking the browser. The
    // claim lease is long enough for the bounded CLI execution + DB commit.
    let started=sqlx::query("UPDATE publish_jobs SET started_at=now(),updated_at=now()
      WHERE id=$1 AND lease_owner=$2 AND lease_until>now() AND state::text='submitting' AND started_at IS NULL
      AND EXISTS(SELECT 1 FROM tasks WHERE id=$3 AND status::text<>'canceled')")
        .bind(work.job).bind(&work.owner).bind(work.task).execute(&state.pool).await;
    match started {
        Ok(r) if r.rows_affected() == 1 => {}
        Ok(_) => return Outcome::before("SAU execution no longer owns pending task"),
        Err(_) => return Outcome::uncertain("SAU start marker could not be confirmed"),
    }
    let mut cmd = tokio::process::Command::new(&runtime.executable);
    cmd.current_dir(&runtime.cwd)
        .args(args)
        .env("DISPLAY", &cfg.display)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .process_group(0);
    // The third-party CLI does not require VideoRoll's database/API credentials.
    for key in [
        "DATABASE_URL",
        "INTERNAL_API_SECRET",
        "VIDEOROLL_V2_INTERNAL_TOKEN",
        "ADMIN_BOOTSTRAP_SECRET",
    ] {
        cmd.env_remove(key);
    }
    let parent = std::process::id() as libc::pid_t;
    unsafe {
        cmd.pre_exec(move || {
            libc::umask(0o077);
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                return Err(std::io::Error::other("SAU parent exited"));
            }
            Ok(())
        });
    }
    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(_) => {
            return Outcome::uncertain(
                "SAU browser could not be started after durable start marker",
            );
        }
    };
    let pgid = child.id().map(|v| v as libc::pid_t);
    let result = tokio::time::timeout(cfg.timeout, child.wait()).await;
    if let Some(pgid) = pgid {
        unsafe {
            libc::kill(-pgid, libc::SIGKILL);
        }
    }
    let _ = child.wait().await;
    match result {
        Ok(Ok(exit)) if exit.success() => Outcome {
            status: "submitted",
            message: "SAU CLI completed; platform receipt not independently verified",
            code: exit.code(),
            timed_out: false,
            started: true,
            secret: refreshed(&file.0).ok(),
        },
        Ok(Ok(exit)) => Outcome {
            code: exit.code(),
            ..Outcome::uncertain(
                "SAU browser returned failure after start; verify platform before retry",
            )
        },
        Err(_) => Outcome {
            timed_out: true,
            ..Outcome::uncertain("SAU upload timed out; verify platform before retry")
        },
        _ => Outcome::uncertain("SAU browser execution interrupted"),
    }
}
async fn finalize(
    tx: &mut Transaction<'_, Postgres>,
    work: &Work,
    outcome: Outcome,
) -> AppResult<()> {
    let changed=sqlx::query("UPDATE publish_jobs SET state=CASE WHEN $3='submitted' THEN 'submitted' WHEN $3='failed' THEN 'failed' ELSE 'unknown' END::publish_state,
      response_json=$4,finished_at=now(),lease_owner=NULL,lease_until=NULL,heartbeat_at=now(),updated_at=now()
      WHERE id=$1 AND lease_owner=$2 AND state::text='submitting'")
        .bind(work.job).bind(&work.owner).bind(outcome.status)
        .bind(json!({"driver":"sau","runtime":"rust","returncode":outcome.code,"timed_out":outcome.timed_out,
          "started":outcome.started,"message":outcome.message,"receipt_verified":false}))
        .execute(&mut **tx).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict(
            "SAU job lease lost; no receipt overwritten".into(),
        ));
    }
    if let (Some(account), Some(secret)) = (work.account, outcome.secret) {
        sqlx::query("UPDATE accounts SET secrets_encrypted=$2 WHERE id=$1 AND is_active AND check_state='valid'
          AND rotated_at IS NOT DISTINCT FROM $3 AND secrets_encrypted=$4")
            .bind(account).bind(secret).bind(work.rotated).bind(&work.secret).execute(&mut **tx).await?;
    }
    let result = json!({"status":outcome.status,"job_id":work.job});
    let changed=sqlx::query("UPDATE operation_inbox SET status='completed',result_json=$3,completed_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL
        WHERE operation_key=$1 AND lease_owner=$2 AND runtime_engine=$4 AND status='processing'")
        .bind(&work.key).bind(&work.owner).bind(&result).bind(ENGINE).execute(&mut **tx).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict("SAU operation lease lost".into()));
    }
    let changed=sqlx::query("UPDATE outbox_events SET status='native_completed',delivered_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL
        WHERE id=$1 AND lease_owner=$2 AND status='native_running'")
        .bind(work.event).bind(&work.owner).execute(&mut **tx).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict("SAU event lease lost".into()));
    }
    let batch: Option<Uuid> = sqlx::query_scalar("SELECT batch_id FROM publish_jobs WHERE id=$1")
        .bind(work.job)
        .fetch_one(&mut **tx)
        .await?;
    if let Some(batch) = batch {
        // Same original batch/task evaluation, but no Python follow-up
        // dispatch for Rust-owned SAU work. Preserve unverified media.
        crate::bilibili_batch_reconcile_v1::reconcile_native_sau(tx, work.task, batch).await?;
    } else {
        let (status, code, message) = match outcome.status {
            "submitted" => (
                "publishing",
                Some("SOCIAL_PUBLISH_UNVERIFIED"),
                Some(outcome.message),
            ),
            "failed" => (
                "failed",
                Some("SOCIAL_PUBLISH_FAILED"),
                Some(outcome.message),
            ),
            _ => (
                "publishing",
                Some("SOCIAL_PUBLISH_UNKNOWN"),
                Some(outcome.message),
            ),
        };
        sqlx::query("UPDATE tasks SET status=$2::task_status,error_code=$3,error_message=$4,updated_at=now()
          WHERE id=$1 AND status::text NOT IN ('published','canceled')")
            .bind(work.task).bind(status).bind(code).bind(message).execute(&mut **tx).await?;
    }
    Ok(())
}
async fn finish(state: &SauState, work: &Work, outcome: Outcome) -> AppResult<()> {
    let mut tx = state.pool.begin().await?;
    lock_task(&mut tx, work.task).await?;
    if let Some(account) = work.account {
        sqlx::query("SELECT id FROM accounts WHERE id=$1 FOR UPDATE")
            .bind(account)
            .fetch_optional(&mut *tx)
            .await?;
    }
    finalize(&mut tx, work, outcome).await?;
    tx.commit().await?;
    Ok(())
}
async fn recover(state: &SauState) -> AppResult<()> {
    let rows=sqlx::query("SELECT e.id AS event,j.id AS job,j.task_id,j.batch_id,j.account_id,j.platform::text AS platform,
      j.operation_key,j.lease_owner FROM outbox_events e JOIN publish_jobs j ON j.operation_key=e.operation_key AND j.id::text=e.aggregate_id
      WHERE e.task_name='social_publisher.process_job' AND e.status='native_running' AND e.lease_until<=now()
      ORDER BY e.lease_until LIMIT 32").fetch_all(&state.pool).await?;
    for row in rows {
        let event: Uuid = row.try_get("event")?;
        let work = Work {
            event,
            job: row.try_get("job")?,
            task: row.try_get("task_id")?,
            account: row.try_get("account_id")?,
            platform: row.try_get("platform")?,
            key: row.try_get("operation_key")?,
            owner: row.try_get("lease_owner")?,
            meta: Value::Null,
            name: None,
            secret: None,
            rotated: None,
            valid: false,
        };
        let mut tx = state.pool.begin().await?;
        lock_task(&mut tx, work.task).await?;
        if let Some(account) = work.account {
            sqlx::query("SELECT id FROM accounts WHERE id=$1 FOR UPDATE")
                .bind(account)
                .fetch_optional(&mut *tx)
                .await?;
        }
        // Only the exact expired owner can be finalized; do not reacquire it.
        let expired:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM outbox_events WHERE id=$1 AND status='native_running' AND lease_owner=$2 AND lease_until<=now())")
            .bind(event).bind(&work.owner).fetch_one(&mut *tx).await?;
        if expired {
            finalize(
                &mut tx,
                &work,
                Outcome::uncertain("SAU worker lease expired; verify platform before retry"),
            )
            .await?;
        }
        tx.commit().await?;
    }
    Ok(())
}
pub(crate) async fn tick(state: &SauState, runtime: &Runtime) -> AppResult<bool> {
    let Some(cfg) = state.native_publish.as_ref() else {
        return Ok(false);
    };
    recover(state).await?;
    let Some(work) = claim(state, cfg).await? else {
        return Ok(false);
    };
    let outcome = execute(state, runtime, cfg, &work).await;
    finish(state, &work, outcome).await?;
    Ok(true)
}
