use clap::{Parser, Subcommand};
use std::sync::Arc;
use tracing_subscriber::EnvFilter;
use videoroll_v2::{
    Config, Database,
    api::{self, AppState},
    executor::Executor,
    scheduler::Scheduler,
    storage::AssetRegistry,
};
#[derive(Parser)]
#[command(
    name = "videoroll-v2",
    version,
    about = "VideoRoll v2 Rust control plane (isolated migration)"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Upgrade only v2 tables. Never modifies existing v1 table definitions.
    Migrate,
    /// Bind an independent API, without starting any media/publishing jobs.
    Api,
    /// Run an original internal service with native routes and HMAC authentication.
    Service {
        service: videoroll_v2::legacy_service_v1::LegacyService,
    },
    /// Start durable workflow workers. Requires a separate testing database.
    Worker,
    /// Process at most one native Bilibili subtitle draft (outbox remains
    /// independent of the irreversible archive submission).
    SubtitlesTick,
    /// Process at most one independently scheduled published-P1/P2 append.
    P2Tick,
    /// Process one durable SAU social publication intent and reconcile receipts.
    SocialTick,
    /// Run one due-check for the original YouTube Home recommendation scanner.
    /// Exits without side effects unless home_scan_enabled is true and due.
    HomeTick,
    /// Process expired, unreferenced v2 artifacts. Never touches v1-only assets.
    Gc {
        #[arg(long, default_value_t = 50)]
        limit: i64,
    },
}
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let cli = Cli::parse();
    let config = Arc::new(Config::from_env()?);
    let db = Database::connect(&config.database_url).await?;
    match cli.command {
        Command::Migrate => db.migrate().await?,
        Command::Api => {
            let scheduler = Scheduler::new(db.clone(), config.job_lease);
            let storage = AssetRegistry::new(db.clone(), config.storage_root.clone());
            let app = api::router(AppState {
                config: config.clone(),
                db,
                scheduler,
                storage,
            });
            let listener = tokio::net::TcpListener::bind(config.bind).await?;
            tracing::info!(address=%config.bind,"v2 API listening (worker side effects disabled)");
            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
            .await?;
        }
        Command::Service { service } => {
            if std::env::var_os("VIDEOROLL_V2_BIND").is_none() {
                anyhow::bail!("standalone service requires explicit VIDEOROLL_V2_BIND");
            }
            if !config.bind.ip().is_loopback()
                && std::env::var("VIDEOROLL_V2_SERVICE_ALLOW_CONTAINER_BIND")
                    .ok()
                    .as_deref()
                    != Some("true")
            {
                anyhow::bail!("standalone service requires loopback or explicit container bind");
            }
            let state = AppState {
                config: config.clone(),
                scheduler: Scheduler::new(db.clone(), config.job_lease),
                storage: AssetRegistry::new(db.clone(), config.storage_root.clone()),
                db,
            };
            let app = videoroll_v2::legacy_service_v1::router(service, state)?;
            let listener = tokio::net::TcpListener::bind(config.bind).await?;
            tracing::info!(address=%config.bind, ?service, "original internal service listening");
            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
            )
            .await?;
        }
        Command::Worker => {
            let scheduler = Scheduler::new(db.clone(), config.job_lease);
            let storage = AssetRegistry::new(db.clone(), config.storage_root.clone());
            let maintenance_store = storage.clone();
            // The original youtube.settings.home_scan_enabled flag is the
            // owner of this schedule. Worker startup checks due immediately;
            // API-only instances never start a scanner.
            let home_state = AppState {
                config: config.clone(),
                db: db.clone(),
                scheduler: scheduler.clone(),
                storage: storage.clone(),
            };
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(std::time::Duration::from_secs(60));
                tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                loop {
                    tick.tick().await;
                    match videoroll_v2::youtube_home_run_v1::run_due(&home_state).await {
                        Ok(true) => tracing::info!("scheduled YouTube Home scan completed"),
                        Ok(false) => {}
                        Err(error) => {
                            tracing::warn!(error=%error,"scheduled YouTube Home scan failed")
                        }
                    }
                }
            });
            if config.source_scanner_enabled {
                let scanner_config = config.clone();
                let scanner_db = db.clone();
                let scanner_scheduler = scheduler.clone();
                tokio::spawn(async move {
                    let mut tick = tokio::time::interval(std::time::Duration::from_secs(60));
                    loop {
                        tick.tick().await;
                        if let Err(error) = videoroll_v2::sources::scan_due(
                            &scanner_config,
                            &scanner_db,
                            &scanner_scheduler,
                        )
                        .await
                        {
                            tracing::warn!(error=%error,"periodic YouTube source scan unavailable");
                        }
                    }
                });
            }
            if config.native_bilibili_allowed && config.native_bilibili_subtitles {
                let subtitles_config = config.clone();
                let subtitles_db = db.clone();
                let subtitles_storage = storage.clone();
                tokio::spawn(async move {
                    let mut tick = tokio::time::interval(std::time::Duration::from_secs(45));
                    loop {
                        tick.tick().await;
                        match videoroll_v2::bili_subtitles::tick(
                            &subtitles_config,
                            &subtitles_db,
                            &subtitles_storage,
                        )
                        .await
                        {
                            Ok(true) => tracing::debug!("native Bilibili subtitle task processed"),
                            Ok(false) => {}
                            Err(error) => tracing::warn!(error=%error,
                                "native Bilibili subtitle sync unavailable (publish unaffected)"),
                        }
                    }
                });
            }
            if config.native_bilibili_allowed {
                let p2_config = config.clone();
                let p2_db = db.clone();
                let p2_registry = storage.clone();
                tokio::spawn(async move {
                    let mut tick = tokio::time::interval(std::time::Duration::from_secs(15));
                    loop {
                        tick.tick().await;
                        if let Err(error) = videoroll_v2::bili_p2::reconcile_stale(&p2_db).await {
                            tracing::warn!(error=%error,"native Bilibili P2 stale intent reconciliation failed");
                        }
                        if let Err(error) =
                            videoroll_v2::bili_p2::tick(&p2_config, &p2_db, &p2_registry).await
                        {
                            tracing::warn!(error=%error,"native Bilibili P2 processing failed");
                        }
                    }
                });
            }
            if config.social_publish_allowed {
                let social_config = config.clone();
                let social_db = db.clone();
                tokio::spawn(async move {
                    let mut tick = tokio::time::interval(std::time::Duration::from_secs(15));
                    loop {
                        tick.tick().await;
                        if let Err(error) =
                            videoroll_v2::social_publish::reconcile_stale(&social_db).await
                        {
                            tracing::warn!(error=%error,"SAU social dispatch recovery failed");
                        }
                        if let Err(error) =
                            videoroll_v2::social_publish::reconcile_receipts(&social_db).await
                        {
                            tracing::warn!(error=%error,"SAU publish receipt reconciliation failed");
                        }
                        if let Err(error) =
                            videoroll_v2::social_publish::tick(&social_config, &social_db).await
                        {
                            tracing::warn!(error=%error,"SAU social publish handoff failed");
                        }
                    }
                });
            }
            let executor = Executor::new(config.clone(), db.clone(), scheduler, storage);
            let mut workers = tokio::task::JoinSet::new();
            workers.spawn(async move {
                // Indexed due-date GC; zero directory scans on idle cycles.
                let mut tick = tokio::time::interval(std::time::Duration::from_secs(300));
                loop {
                    tick.tick().await;
                    if let Err(e) = maintenance_store
                        .reconcile_terminal_resources(50, std::time::Duration::from_secs(86_400))
                        .await
                    {
                        tracing::warn!(error=%e,"terminal resource release reconciliation failed");
                    }
                    let retention=videoroll_v2::operations_v1::published_retention(
                        &maintenance_store.db
                    ).await;
                    match retention {
                        Ok(ttl)=>{
                            if let Err(error)=maintenance_store
                                .reconcile_published_resources(50,ttl).await{
                                tracing::warn!(%error,"published resource retention backfill failed");
                            }
                        },
                        Err(error)=>tracing::warn!(%error,"published retention settings unavailable"),
                    }
                    if let Err(e) = maintenance_store.collect(100).await {
                        tracing::warn!(error=%e,"v2 object garbage collection failed");
                    }
                    if let Err(e) = maintenance_store.collect_workdirs(20).await {
                        tracing::warn!(error=%e,"v2 workdir garbage collection failed");
                    }
                }
            });
            for index in 0..config.worker_count {
                let worker = executor.clone();
                workers.spawn(async move { worker.worker_loop(index).await });
            }
            while let Some(result) = workers.join_next().await {
                result?;
            }
        }
        Command::SubtitlesTick => {
            let storage = AssetRegistry::new(db.clone(), config.storage_root.clone());
            let processed = videoroll_v2::bili_subtitles::tick(&config, &db, &storage).await?;
            tracing::info!(processed, "native subtitle sync tick finished");
        }
        Command::P2Tick => {
            let store = AssetRegistry::new(db.clone(), config.storage_root.clone());
            let stale = videoroll_v2::bili_p2::reconcile_stale(&db).await?;
            let processed = videoroll_v2::bili_p2::tick(&config, &db, &store).await?;
            tracing::info!(stale, processed, "native post-P1 P2 append tick finished");
        }
        Command::HomeTick => {
            let state = AppState {
                config: config.clone(),
                db: db.clone(),
                scheduler: Scheduler::new(db.clone(), config.job_lease),
                storage: AssetRegistry::new(db.clone(), config.storage_root.clone()),
            };
            let did_run = videoroll_v2::youtube_home_run_v1::run_due(&state).await?;
            println!("home_scan_run={did_run}");
        }
        Command::SocialTick => {
            let stale = videoroll_v2::social_publish::reconcile_stale(&db).await?;
            let receipts = videoroll_v2::social_publish::reconcile_receipts(&db).await?;
            let dispatched = videoroll_v2::social_publish::tick(&config, &db).await?;
            tracing::info!(
                stale,
                receipts,
                dispatched,
                "SAU social publish tick finished"
            );
        }
        Command::Gc { limit } => {
            let registry = AssetRegistry::new(db, config.storage_root.clone());
            let released = registry
                .reconcile_terminal_resources(limit, config.cleanup_grace)
                .await?;
            let ttl = videoroll_v2::operations_v1::published_retention(&registry.db).await?;
            let published = registry.reconcile_published_resources(limit, ttl).await?;
            let deleted = registry.collect(limit).await?;
            let directories = registry.collect_workdirs(limit).await?;
            tracing::info!(
                released,
                deleted,
                published,
                directories,
                "expired assets and workdirs deleted"
            );
        }
    }
    Ok(())
}
