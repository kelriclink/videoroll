//! Persistent SAU browser login sessions. The original CLI and authenticated
//! noVNC bridge remain separate execution/display components.
use crate::{
    AppError, AppResult,
    sau_account_native_v1::{SauState, account_busy, social_platform},
    sau_check_native_v1::{Runtime, name, refreshed},
};
use axum::{
    Json,
    extract::{Path, State},
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use std::{path::PathBuf, sync::Arc, time::Duration};
use uuid::Uuid;

const ENGINE: &str = "rust-sau-login";
const BROWSER_URL: &str =
    "/social-login/vnc.html?autoconnect=1&resize=scale&path=/social-login/websockify";
#[derive(Deserialize)]
pub(crate) struct Start {
    account_name: String,
}
pub(crate) struct LoginConfig {
    timeout: Duration,
    display: String,
    browser_url: String,
}
impl LoginConfig {
    pub(crate) fn from_env() -> AppResult<Self> {
        let secs = std::env::var("SOCIAL_LOGIN_TIMEOUT_SECONDS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(900.0);
        if !secs.is_finite() || !(1.0..=3600.0).contains(&secs) {
            return Err(AppError::Invalid(
                "login timeout must be 1..3600 seconds".into(),
            ));
        }
        let display = std::env::var("SOCIAL_LOGIN_DISPLAY").unwrap_or_else(|_| ":99".into());
        let browser_url =
            std::env::var("SOCIAL_LOGIN_BROWSER_URL").unwrap_or_else(|_| BROWSER_URL.into());
        if display.is_empty()
            || display.chars().any(char::is_control)
            || !browser_url.starts_with("/social-login/")
            || browser_url.contains('\\')
            || browser_url.chars().any(char::is_control)
        {
            return Err(AppError::Invalid(
                "invalid SAU login display or browser URL".into(),
            ));
        }
        Ok(Self {
            timeout: Duration::from_secs_f64(secs),
            display,
            browser_url,
        })
    }
}
pub(crate) async fn lock_name(
    tx: &mut Transaction<'_, Postgres>,
    p: &str,
    n: &str,
) -> AppResult<()> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
        .bind(format!("videoroll:sau-account:{p}:{n}"))
        .execute(&mut **tx)
        .await?;
    Ok(())
}
pub(crate) async fn ensure_not_logging_in(
    tx: &mut Transaction<'_, Postgres>,
    p: &str,
    n: &str,
) -> AppResult<()> {
    let busy: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM operation_inbox
       WHERE runtime_engine='rust-sau-login' AND status IN ('pending','processing')
       AND request_json->>'platform'=$1 AND request_json->>'account_name'=$2)",
    )
    .bind(p)
    .bind(n)
    .fetch_one(&mut **tx)
    .await?;
    if busy {
        return Err(AppError::Conflict(
            "account is busy; wait for browser login".into(),
        ));
    }
    Ok(())
}
pub(crate) fn dto(row: &PgRow) -> AppResult<Value> {
    let req: Value = row.try_get("request_json")?;
    let result: Option<Value> = row.try_get("result_json")?;
    let result = result.unwrap_or(json!({}));
    Ok(json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "platform":req["platform"],
        "account_name":req["account_name"],
        "state":result.get("state").and_then(Value::as_str).unwrap_or("starting"),
        "message":result.get("message"),
        "browser_url":req["browser_url"],
        "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
        "finished_at":row.try_get::<Option<DateTime<Utc>>,_>("completed_at")?,
    }))
}
async fn session(tx: &mut Transaction<'_, Postgres>, id: Uuid) -> AppResult<PgRow> {
    sqlx::query(
        "SELECT id,status,request_json,result_json,created_at,completed_at
      FROM operation_inbox WHERE id=$1 AND runtime_engine=$2 FOR UPDATE",
    )
    .bind(id)
    .bind(ENGINE)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or_else(|| AppError::NotFound("login session not found".into()))
}
pub(crate) async fn start(
    State(state): State<Arc<SauState>>,
    Path(raw): Path<String>,
    Json(body): Json<Start>,
) -> AppResult<Json<Value>> {
    let cfg = state.login_config.as_ref().ok_or_else(|| {
        AppError::Conflict("native SAU browser login not explicitly enabled".into())
    })?;
    let p = social_platform(&raw)?;
    let n = name(&body.account_name)?;
    let mut tx = state.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('videoroll:sau-login-global',0))")
        .execute(&mut *tx)
        .await?;
    let active:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE runtime_engine='rust-sau-login' AND status IN ('pending','processing'))")
        .fetch_one(&mut *tx).await?;
    if active {
        return Err(AppError::Conflict(
            "another browser login session is already active".into(),
        ));
    }
    lock_name(&mut tx, p, &n).await?;
    let existing=sqlx::query("SELECT id,rotated_at,is_active,check_state FROM accounts WHERE platform::text=$1 AND name=$2 FOR UPDATE")
        .bind(p).bind(&n).fetch_optional(&mut *tx).await?;
    let snapshot = if let Some(row) = existing {
        let id: Uuid = row.try_get("id")?;
        let check: String = row.try_get("check_state")?;
        account_busy(&mut tx, id, &check).await?;
        if check == "queued" {
            return Err(AppError::Conflict("account has a queued check".into()));
        }
        json!({"id":id,"rotated_at":row.try_get::<Option<DateTime<Utc>>,_>("rotated_at")?,"is_active":row.try_get::<bool,_>("is_active")?,"check_state":check})
    } else {
        Value::Null
    };
    let id = Uuid::new_v4();
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,request_json,result_json)
        VALUES($1,$2,$3,'pending',$4,$5)")
        .bind(id).bind(format!("sau-login:{id}")).bind(ENGINE)
        .bind(json!({"platform":p,"account_name":n,"browser_url":cfg.browser_url,"snapshot":snapshot}))
        .bind(json!({"state":"starting","message":null})).execute(&mut *tx).await?;
    let response = dto(&session(&mut tx, id).await?)?;
    tx.commit().await?;
    Ok(Json(response))
}
pub(crate) async fn get(
    State(state): State<Arc<SauState>>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let row=sqlx::query("SELECT id,status,request_json,result_json,created_at,completed_at FROM operation_inbox WHERE id=$1 AND runtime_engine=$2")
        .bind(id).bind(ENGINE).fetch_optional(&state.pool).await?.ok_or_else(||AppError::NotFound("login session not found".into()))?;
    Ok(Json(dto(&row)?))
}
pub(crate) async fn cancel(
    State(state): State<Arc<SauState>>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let mut tx = state.pool.begin().await?;
    let row = session(&mut tx, id).await?;
    match row.try_get::<String, _>("status")?.as_str() {
        "pending" => {
            sqlx::query("UPDATE operation_inbox SET status='completed',result_json=$2,completed_at=now(),updated_at=now() WHERE id=$1")
            .bind(id).bind(json!({"state":"canceled","message":"login canceled"})).execute(&mut *tx).await?;
        }
        "processing" => {
            sqlx::query("UPDATE operation_inbox SET result_json=$2,updated_at=now() WHERE id=$1")
                .bind(id)
                .bind(json!({"state":"canceling","message":"login canceled"}))
                .execute(&mut *tx)
                .await?;
        }
        _ => {}
    }
    let response = dto(&session(&mut tx, id).await?)?;
    tx.commit().await?;
    Ok(Json(response))
}
async fn recover(state: &SauState) -> AppResult<()> {
    // A killed browser cannot be resumed. Do not remove stale cookie files:
    // they may still be owned by a surviving browser child.
    sqlx::query("UPDATE operation_inbox SET status='completed',
      result_json=CASE WHEN result_json->>'state'='canceling'
       THEN '{\"state\":\"canceled\",\"message\":\"login canceled\"}'::jsonb
       ELSE '{\"state\":\"failed\",\"message\":\"browser login interrupted; verify stale cookie file before retry\"}'::jsonb END,
      completed_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL
      WHERE runtime_engine='rust-sau-login' AND status='processing' AND lease_until<=now()")
        .execute(&state.pool).await?;
    Ok(())
}
struct Work {
    id: Uuid,
    req: Value,
}
async fn claim(state: &SauState, cfg: &LoginConfig) -> AppResult<Option<Work>> {
    let row=sqlx::query("UPDATE operation_inbox SET status='processing',
      result_json=$1,attempt_count=attempt_count+1,lease_owner='rust-sau-login',
      lease_until=now()+($2::bigint * interval '1 second'),heartbeat_at=now(),updated_at=now()
      WHERE id=(SELECT id FROM operation_inbox WHERE runtime_engine='rust-sau-login' AND status='pending'
      ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED)
      RETURNING id,request_json")
        .bind(json!({"state":"running","message":"browser opening; complete login in the new window"}))
        .bind(cfg.timeout.as_secs() as i64+60).fetch_optional(&state.pool).await?;
    row.map(|r| {
        Ok(Work {
            id: r.try_get("id")?,
            req: r.try_get("request_json")?,
        })
    })
    .transpose()
}
fn fields(work: &Work) -> AppResult<(&str, &str)> {
    let p = social_platform(work.req["platform"].as_str().unwrap_or(""))?;
    let n = work.req["account_name"].as_str().unwrap_or("");
    if name(n)? != n {
        return Err(AppError::Invalid("invalid stored account name".into()));
    }
    Ok((p, n))
}
struct CookieFile(PathBuf);
impl Drop for CookieFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
async fn canceled(state: &SauState, id: Uuid) -> AppResult<bool> {
    let value:Option<String>=sqlx::query_scalar("SELECT result_json->>'state' FROM operation_inbox WHERE id=$1 AND runtime_engine=$2 AND status='processing'")
        .bind(id).bind(ENGINE).fetch_optional(&state.pool).await?;
    Ok(value.as_deref() != Some("running"))
}
async fn execute(
    state: &SauState,
    runtime: &Runtime,
    cfg: &LoginConfig,
    work: &Work,
) -> (&'static str, &'static str, Option<String>) {
    let Ok((p, n)) = fields(work) else {
        return ("failed", "invalid stored login request", None);
    };
    let path = runtime.cookies.join(format!("{p}_{n}.json"));
    match std::fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        _ => {
            return (
                "failed",
                "SAU cookie file already exists; verify stale file before retry",
                None,
            );
        }
    }
    if canceled(state, work.id).await.unwrap_or(true) {
        return ("canceled", "login canceled", None);
    }
    let file = CookieFile(path);
    let mut cmd = tokio::process::Command::new(&runtime.executable);
    cmd.current_dir(&runtime.cwd)
        .args([p, "login", "--account", n, "--headed"])
        .env("DISPLAY", &cfg.display)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .process_group(0);
    if std::env::var_os("DOUYIN_COOKIE_AUTH_HEADLESS").is_none() {
        cmd.env("DOUYIN_COOKIE_AUTH_HEADLESS", "true");
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
        Ok(c) => c,
        Err(_) => return ("failed", "SAU login process could not start", None),
    };
    let pgid = child.id().map(|v| v as libc::pid_t);
    let deadline = tokio::time::Instant::now() + cfg.timeout;
    let outcome = loop {
        if tokio::time::Instant::now() >= deadline {
            break ("failed", "login timed out");
        }
        match canceled(state, work.id).await {
            Ok(true) => break ("canceled", "login canceled"),
            Err(_) => break ("failed", "login state unavailable"),
            Ok(false) => {}
        }
        match child.try_wait() {
            Ok(Some(s)) if s.success() => {
                break ("succeeded", "login succeeded and account was saved");
            }
            Ok(Some(_)) => break ("failed", "SAU login failed"),
            Ok(None) => {}
            Err(_) => break ("failed", "SAU login process failed"),
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    if let Some(pgid) = pgid {
        unsafe {
            libc::kill(-pgid, libc::SIGKILL);
        }
    }
    let _ = child.wait().await;
    if outcome.0 == "succeeded" {
        match refreshed(&file.0) {
            Ok(secret) => (outcome.0, outcome.1, Some(secret)),
            Err(_) => (
                "failed",
                "SAU returned missing or invalid storage_state",
                None,
            ),
        }
    } else {
        (outcome.0, outcome.1, None)
    }
}
async fn finish(
    state: &SauState,
    work: &Work,
    outcome: (&str, &str, Option<String>),
) -> AppResult<()> {
    let (p, n) = fields(work)?;
    let (mut status, mut message, secret) = outcome;
    let mut tx = state.pool.begin().await?;
    lock_name(&mut tx, p, n).await?;
    let row=sqlx::query("SELECT id,rotated_at,is_active,check_state FROM accounts WHERE platform::text=$1 AND name=$2 FOR UPDATE")
        .bind(p).bind(n).fetch_optional(&mut *tx).await?;
    let current = session(&mut tx, work.id).await?;
    if current.try_get::<String, _>("status")? != "processing" {
        return Ok(());
    }
    let result: Option<Value> = current.try_get("result_json")?;
    if result.as_ref().and_then(|v| v["state"].as_str()) == Some("canceling") {
        status = "canceled";
        message = "login canceled";
    }
    if status == "succeeded" {
        let snapshot = if let Some(row) = &row {
            json!({"id":row.try_get::<Uuid,_>("id")?,"rotated_at":row.try_get::<Option<DateTime<Utc>>,_>("rotated_at")?,
              "is_active":row.try_get::<bool,_>("is_active")?,"check_state":row.try_get::<String,_>("check_state")?})
        } else {
            Value::Null
        };
        let publishing = if let Some(row) = &row {
            sqlx::query_scalar::<_,bool>("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE account_id=$1 AND state::text='submitting')")
                .bind(row.try_get::<Uuid,_>("id")?).fetch_one(&mut *tx).await?
        } else {
            false
        };
        if snapshot != work.req["snapshot"] || publishing {
            status = "failed";
            message = "account changed during browser login; credentials not saved";
        } else if let Some(secret) = secret {
            let updated=sqlx::query("INSERT INTO accounts(id,platform,name,secrets_encrypted,rotated_at,is_active,check_state,last_checked_at,last_check_message)
                VALUES($1,$2::platform,$3,$4,now(),true,'valid',now(),'browser login completed')
                ON CONFLICT(platform,name) DO UPDATE SET secrets_encrypted=excluded.secrets_encrypted,
                rotated_at=now(),is_active=true,check_state='valid',last_checked_at=now(),last_check_message='browser login completed'
                WHERE accounts.id=$5")
                .bind(Uuid::new_v4()).bind(p).bind(n).bind(secret)
                .bind(row.as_ref().map(|r|r.try_get::<Uuid,_>("id")).transpose()?)
                .execute(&mut *tx).await?.rows_affected();
            if updated != 1 {
                status = "failed";
                message = "account changed during browser login; credentials not saved";
            }
        } else {
            status = "failed";
            message = "SAU returned missing storage_state";
        }
    }
    sqlx::query("UPDATE operation_inbox SET status='completed',result_json=$2,completed_at=now(),updated_at=now(),
      lease_owner=NULL,lease_until=NULL WHERE id=$1")
        .bind(work.id).bind(json!({"state":status,"message":message})).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}
pub(crate) async fn tick(state: &SauState, runtime: &Runtime) -> AppResult<bool> {
    recover(state).await?;
    let Some(cfg) = state.login_config.as_ref() else {
        return Ok(false);
    };
    let Some(work) = claim(state, cfg).await? else {
        return Ok(false);
    };
    let result = execute(state, runtime, cfg, &work).await;
    finish(state, &work, result).await?;
    Ok(true)
}
