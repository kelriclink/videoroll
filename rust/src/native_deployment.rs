//! Read-only standalone deployment admission. Schema migration and legacy
//! ownership changes remain explicit offline operations.
use crate::{AppError, AppResult, Config, Database};
use serde_json::{Value, json};
use sqlx::Row;

pub fn enabled() -> bool {
    std::env::var("VIDEOROLL_V2_STANDALONE").ok().as_deref() == Some("true")
}
pub fn validate(cfg: &Config) -> AppResult<()> {
    if !enabled() {
        return Ok(());
    }
    if !crate::outbox_dispatcher::native_only()
        || cfg.legacy_publish_allowed
        || cfg.compat_writes_allowed
        || cfg.compat_orchestrator_url.is_some()
        || cfg.legacy_orchestrator_url.is_some()
        || cfg.worker_gateway.is_some()
    {
        return Err(AppError::Conflict("standalone deployment requires native-only mode without legacy API, publisher or worker gateway".into()));
    }
    if cfg.social_publish_allowed
        && cfg.social_publisher_url.as_deref() != Some("http://127.0.0.1:18087")
    {
        return Err(AppError::Conflict(
            "standalone social publication requires the native loopback SAU service on port 18087"
                .into(),
        ));
    }
    if std::env::var("VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX")
        .ok()
        .as_deref()
        == Some("true")
    {
        return Err(AppError::Conflict(
            "standalone deployment excludes legacy SAU broker".into(),
        ));
    }
    Ok(())
}
pub async fn preflight(db: &Database, cfg: &Config, startup: bool) -> AppResult<Value> {
    if !enabled() {
        return Err(AppError::Conflict(
            "standalone preflight requires VIDEOROLL_V2_STANDALONE=true".into(),
        ));
    }
    validate(cfg)?;
    // Check every compiled migration, including checksums. Never perform DDL
    // or auto-mark missing historical schema as migrated.
    let migrator = sqlx::migrate!("./migrations");
    let ledger: bool =
        sqlx::query_scalar("SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
            .fetch_one(&db.pool)
            .await?;
    let mut missing = vec![];
    let mut invalid = vec![];
    if ledger {
        let applied = sqlx::query("SELECT version,success,checksum FROM _sqlx_migrations")
            .fetch_all(&db.pool)
            .await?;
        for migration in migrator.iter() {
            match applied
                .iter()
                .find(|r| r.get::<i64, _>("version") == migration.version)
            {
                None => missing.push(migration.version),
                Some(r)
                    if !r.get::<bool, _>("success")
                        || r.get::<Vec<u8>, _>("checksum").as_slice()
                            != migration.checksum.as_ref() =>
                {
                    invalid.push(migration.version)
                }
                _ => {}
            }
        }
        for row in applied {
            let version: i64 = row.try_get("version")?;
            if !migrator.iter().any(|m| m.version == version) {
                invalid.push(version);
            }
        }
    } else {
        missing.extend(migrator.iter().map(|m| m.version));
    }
    if !missing.is_empty() || !invalid.is_empty() {
        return Ok(
            json!({"ready":false,"missing_migrations":missing,"invalid_migrations":invalid,"note":"explicit offline migration required; no schema writes performed"}),
        );
    }
    let mut tx = db.pool.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        .execute(&mut *tx)
        .await?;
    // Do not admit a deployment with stale libc/ICU sorting metadata.
    // NULL/NULL is valid for versionless locales such as C.
    let collation = sqlx::query("SELECT datcollversion AS recorded, pg_database_collation_actual_version(oid) AS actual FROM pg_database WHERE datname=current_database()")
        .fetch_one(&mut *tx).await?;
    let recorded: Option<String> = collation.try_get("recorded")?;
    let actual: Option<String> = collation.try_get("actual")?;
    let collation_ok = recorded == actual;
    let row=sqlx::query(r#"SELECT
      (SELECT count(*) FROM tasks WHERE runtime_owner IS DISTINCT FROM 'rust') AS legacy_tasks,
      (SELECT count(*) FROM pipeline_runs WHERE engine<>'rust-v2'
        AND state NOT IN ('succeeded','failed','canceled','completed','superseded')) AS legacy_workflows,
      (SELECT count(*) FROM operation_inbox i WHERE i.runtime_engine='python'
        AND (i.status NOT IN ('completed','succeeded','failed','canceled') OR i.lease_owner IS NOT NULL OR i.lease_until IS NOT NULL)
        AND NOT (i.status='processing' AND i.lease_owner='rust-v2'
          AND EXISTS(SELECT 1 FROM publish_jobs j JOIN tasks t ON t.id=j.task_id
            JOIN pipeline_runs w ON w.task_id=t.id AND w.engine='rust-v2'
              AND w.request_json->'bilibili_direct_v1'->>'publish_job_id'=j.id::text
            WHERE j.operation_key=i.operation_key AND j.platform::text='bilibili' AND t.runtime_owner='rust')))
        AS legacy_operations,
      (SELECT count(*) FROM render_executions WHERE runtime_engine='python'
        AND state NOT IN ('completed','failed','canceled','lost')
        AND NOT (state='succeeded' AND finished_at IS NOT NULL)) AS legacy_render_executions,
      (SELECT count(*) FROM publish_jobs WHERE state::text='unknown'
        OR (state::text='submitted' AND COALESCE(NULLIF(external_id,''),NULLIF(external_url,''),NULLIF(bvid,''),NULLIF(aid,'')) IS NULL)) AS uncertain_publications"#)
      .fetch_one(&mut *tx).await?;
    let counts = json!({"legacy_tasks":row.try_get::<i64,_>("legacy_tasks")?,
        "legacy_workflows":row.try_get::<i64,_>("legacy_workflows")?,
        "legacy_operations":row.try_get::<i64,_>("legacy_operations")?,
        "legacy_render_executions":row.try_get::<i64,_>("legacy_render_executions")?,
        "uncertain_publications":row.try_get::<i64,_>("uncertain_publications")?});
    tx.commit().await?;
    let outbox = crate::outbox_dispatcher::audit(db).await?;
    let storage_ok = cfg.storage_root.is_absolute()
        && tokio::fs::metadata(&cfg.storage_root)
            .await
            .is_ok_and(|m| m.is_dir());
    let secret_ok = cfg.legacy_internal_secret.as_deref().is_some_and(|s| {
        s.trim().len() >= 16
            && !matches!(
                s.trim().to_ascii_lowercase().as_str(),
                "change-me"
                    | "changeme"
                    | "videoroll-development-internal-secret"
                    | "videoroll-development-bootstrap-secret"
            )
    });
    let sau_enabled = std::env::var("VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH")
        .ok()
        .as_deref()
        == Some("true");
    let sau_required = cfg.social_publish_allowed
        || outbox["groups"].as_array().is_some_and(|groups| {
            groups
                .iter()
                .any(|g| g["disposition"] == "native_sau_queue")
        });
    let sau_ok = !sau_required
        || (sau_enabled
            && std::env::var("VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK")
                .ok()
                .as_deref()
                == Some("true"));
    let legacy_ledger_clear = outbox["groups"].as_array().is_some_and(|groups| {
        groups.iter().all(|g| {
            g["disposition"] != "legacy_unattempted"
                && (g["disposition"] != "requires_reconciliation"
                    || g["status"]
                        .as_str()
                        .is_some_and(|s| s.starts_with("native_")))
        })
    });
    let ready = counts.as_object().is_some_and(|m| {
        m.iter()
            .all(|(k, v)| (startup && k == "uncertain_publications") || v.as_i64() == Some(0))
    }) && (if startup {
        legacy_ledger_clear
    } else {
        outbox["delivery_ledger_ready"] == true
    }) && storage_ok
        && secret_ok
        && sau_ok
        && collation_ok;
    Ok(
        json!({"ready":ready,"mode":if startup{"startup"}else{"cutover"}, "historical_blockers":counts,"database_collation":{"recorded":recorded,"actual":actual,"compatible":collation_ok},"outbox":outbox,"storage_directory_exists":storage_ok,"internal_secret_configured":secret_ok,"native_sau_configured_if_required":sau_ok,
      "note":"read-only admission, not proof of external runtime shutdown, credential validity, GPU availability or platform parity"}),
    )
}
