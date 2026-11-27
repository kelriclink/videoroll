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
    generation: Option<i32>,
    batch: Option<Uuid>,
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
    let generation:i32=sqlx::query_scalar("SELECT restart_generation FROM tasks WHERE id=$1")
        .bind(task).fetch_one(&mut *tx).await?;
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
        generation: Some(generation),
        batch: row.try_get("batch_id")?,
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
/// Lock the same parent/account as claim and finish. Recheck the exact
/// delivery envelope and all leases before crossing the browser boundary.
/// A changed account or task may never start with a cached credential.
async fn mark_started(state:&SauState,work:&Work,cfg:&PublishConfig)->AppResult<bool>{
    let mut tx=state.pool.begin().await?;
    let task:Option<Uuid>=sqlx::query_scalar("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust'
        AND source_license::text<>'unknown' AND status::text<>'canceled' AND stopped_status IS NULL
        AND restart_generation=$2 FOR UPDATE")
        .bind(work.task).bind(work.generation).fetch_optional(&mut *tx).await?;
    if task.is_none(){return Ok(false)}
    let account:Option<Uuid>=sqlx::query_scalar("SELECT id FROM accounts WHERE id=$1 AND is_active
        AND check_state='valid' AND platform::text=$2 AND name=$3 AND secrets_encrypted=$4
        AND rotated_at IS NOT DISTINCT FROM $5 FOR UPDATE")
        .bind(work.account).bind(&work.platform).bind(&work.name).bind(&work.secret).bind(work.rotated)
        .fetch_optional(&mut *tx).await?;
    if account.is_none(){return Ok(false)}
    match crate::sau_login_native_v1::ensure_not_logging_in(&mut tx,&work.platform,work.name.as_deref().unwrap_or("")).await {
        Ok(())=>{},
        Err(AppError::Conflict(_))=>return Ok(false),
        Err(e)=>return Err(e),
    }
    let remaining=cfg.timeout.as_secs() as i64+30;
    // All three records must still belong to this execution and remain
    // leased through the bounded browser call. Lock the job first.
    let job:Option<Uuid>=sqlx::query_scalar("SELECT id FROM publish_jobs WHERE id=$1 AND task_id=$2
        AND account_id IS NOT DISTINCT FROM $3 AND batch_id IS NOT DISTINCT FROM $4
        AND platform::text=$5 AND operation_key=$6 AND meta_json=$7
        AND lease_owner=$8 AND lease_until>clock_timestamp()+($9::bigint*interval '1 second')
        AND state::text='submitting' AND started_at IS NULL AND finished_at IS NULL
        AND COALESCE(external_id,'')='' AND COALESCE(external_url,'')=''
        AND COALESCE(bvid,'')='' AND COALESCE(aid,'')='' FOR UPDATE")
        .bind(work.job).bind(work.task).bind(work.account).bind(work.batch).bind(&work.platform)
        .bind(&work.key).bind(&work.meta).bind(&work.owner).bind(remaining).fetch_optional(&mut *tx).await?;
    if job.is_none(){return Ok(false)}
    let event:Option<Uuid>=sqlx::query_scalar("SELECT id FROM outbox_events WHERE id=$1 AND operation_key=$2
        AND aggregate_type='publish_job' AND aggregate_id=$3 AND event_type='publish.dispatch'
        AND task_name='social_publisher.process_job' AND args_json->'args'=jsonb_build_array($3::text)
        AND status='native_running' AND lease_owner=$4 AND delivered_at IS NULL
        AND lease_until>clock_timestamp()+($5::bigint*interval '1 second') FOR UPDATE")
        .bind(work.event).bind(&work.key).bind(work.job.to_string()).bind(&work.owner).bind(remaining)
        .fetch_optional(&mut *tx).await?;
    if event.is_none(){return Ok(false)}
    let inbox:Option<Uuid>=sqlx::query_scalar("SELECT id FROM operation_inbox WHERE operation_key=$1
        AND runtime_engine=$2 AND status='processing' AND request_json->>'job_id'=$3
        AND lease_owner=$4 AND lease_token IS NULL AND completed_at IS NULL
        AND lease_until>clock_timestamp()+($5::bigint*interval '1 second') FOR UPDATE")
        .bind(&work.key).bind(ENGINE).bind(work.job.to_string()).bind(&work.owner).bind(remaining)
        .fetch_optional(&mut *tx).await?;
    if inbox.is_none(){return Ok(false)}
    let changed=sqlx::query("UPDATE publish_jobs SET started_at=now(),updated_at=now() WHERE id=$1")
        .bind(work.job).execute(&mut *tx).await?.rows_affected();
    tx.commit().await?;
    Ok(changed==1)
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
    match mark_started(state,work,cfg).await {
        Ok(true) => {}
        Ok(false) => return Outcome::before("SAU task, credential or delivery fence changed before browser start"),
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
      response_json=CASE WHEN response_json IS NULL THEN $4::jsonb
        WHEN jsonb_typeof(response_json)='object' THEN response_json||$4::jsonb||jsonb_build_object('previous_response',response_json)
        ELSE $4::jsonb||jsonb_build_object('previous_response',response_json) END,
      finished_at=COALESCE(finished_at,now()),lease_owner=NULL,lease_until=NULL,heartbeat_at=now(),updated_at=now()
      WHERE id=$1 AND lease_owner=$2 AND state::text='submitting' AND task_id=$5
        AND account_id IS NOT DISTINCT FROM $6 AND batch_id IS NOT DISTINCT FROM $7
        AND operation_key=$8 AND platform::text=$9
        AND COALESCE(external_id,'')='' AND COALESCE(external_url,'')='' AND COALESCE(bvid,'')='' AND COALESCE(aid,'')=''")
        .bind(work.job).bind(&work.owner).bind(outcome.status)
        .bind(json!({"driver":"sau","runtime":"rust","returncode":outcome.code,"timed_out":outcome.timed_out,
          "started":outcome.started,"message":outcome.message,"receipt_verified":false}))
        .bind(work.task).bind(work.account).bind(work.batch).bind(&work.key).bind(&work.platform)
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
    let changed=sqlx::query("UPDATE operation_inbox SET status='completed',
        result_json=CASE WHEN result_json IS NULL THEN $3::jsonb ELSE $3::jsonb||jsonb_build_object('previous_result',result_json) END,
        completed_at=COALESCE(completed_at,now()),updated_at=now(),lease_owner=NULL,lease_until=NULL
        WHERE operation_key=$1 AND lease_owner=$2 AND runtime_engine=$4 AND status='processing'
        AND lease_token IS NULL AND request_json->>'job_id'=$5")
        .bind(&work.key).bind(&work.owner).bind(&result).bind(ENGINE).bind(work.job.to_string()).execute(&mut **tx).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict("SAU operation lease lost".into()));
    }
    let changed=sqlx::query("UPDATE outbox_events SET status='native_completed',delivered_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL
        WHERE id=$1 AND lease_owner=$2 AND status='native_running' AND operation_key=$3
        AND aggregate_type='publish_job' AND aggregate_id=$4 AND event_type='publish.dispatch'
        AND task_name='social_publisher.process_job' AND args_json->'args'=jsonb_build_array($4::text)")
        .bind(work.event).bind(&work.owner).bind(&work.key).bind(work.job.to_string()).execute(&mut **tx).await?.rows_affected();
    if changed != 1 {
        return Err(AppError::Conflict("SAU event lease lost".into()));
    }
    // A terminal receipt belongs to the old execution even if the task
    // changed ownership/generation. Do not overwrite the newer task.
    let parent_valid:bool=sqlx::query_scalar("SELECT runtime_owner='rust' AND stopped_status IS NULL
        AND status::text<>'canceled' AND ($2::int IS NULL OR restart_generation=$2) FROM tasks WHERE id=$1")
        .bind(work.task).bind(work.generation).fetch_one(&mut **tx).await?;
    if !parent_valid{return Ok(())}
    if let Some(batch) = work.batch {
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
      JOIN operation_inbox i ON i.operation_key=j.operation_key
      WHERE e.task_name='social_publisher.process_job' AND e.status='native_running' AND e.lease_until<=now()
        AND j.state::text='submitting' AND j.lease_until<=now() AND j.lease_owner=e.lease_owner
        AND j.lease_owner LIKE 'rust-sau-publish:%' AND i.runtime_engine='rust-sau-publish'
        AND i.status='processing' AND i.lease_owner=e.lease_owner AND i.lease_until<=now() AND i.lease_token IS NULL
        AND i.request_json->>'job_id'=j.id::text
        AND COALESCE(j.external_id,'')='' AND COALESCE(j.external_url,'')='' AND COALESCE(j.bvid,'')='' AND COALESCE(j.aid,'')=''
      ORDER BY e.lease_until LIMIT 32").fetch_all(&state.pool).await?;
    for row in rows {
        let event: Uuid = row.try_get("event")?;
        let work = Work {
            event,
            job: row.try_get("job")?,
            task: row.try_get("task_id")?,
            generation: None, // Historical recovery has no admission generation.
            batch: row.try_get("batch_id")?,
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
        let expired_job:Option<Uuid>=sqlx::query_scalar("SELECT id FROM publish_jobs WHERE id=$1 AND lease_owner=$2 AND lease_until<=now() AND state::text='submitting' FOR UPDATE")
            .bind(work.job).bind(&work.owner).fetch_optional(&mut *tx).await?;
        if expired_job.is_none(){continue}
        let expired_event:Option<Uuid>=sqlx::query_scalar("SELECT id FROM outbox_events WHERE id=$1 AND status='native_running' AND lease_owner=$2 AND lease_until<=now() FOR UPDATE")
            .bind(event).bind(&work.owner).fetch_optional(&mut *tx).await?;
        let expired_inbox:Option<Uuid>=sqlx::query_scalar("SELECT id FROM operation_inbox WHERE operation_key=$1 AND runtime_engine=$2 AND status='processing' AND lease_owner=$3 AND lease_until<=now() FOR UPDATE")
            .bind(&work.key).bind(ENGINE).bind(&work.owner).fetch_optional(&mut *tx).await?;
        let expired=expired_event.is_some()&&expired_inbox.is_some();
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
