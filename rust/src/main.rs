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
    /// Start durable workflow workers. Requires a separate testing database.
    Worker,
    /// Process at most one native Bilibili subtitle draft (outbox remains
    /// independent of the irreversible archive submission).
    SubtitlesTick,
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
        Command::Worker => {
            let scheduler = Scheduler::new(db.clone(), config.job_lease);
            let storage = AssetRegistry::new(db.clone(), config.storage_root.clone());
            let maintenance_store = storage.clone();
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
        Command::Gc { limit } => {
            let registry = AssetRegistry::new(db, config.storage_root.clone());
            let released = registry
                .reconcile_terminal_resources(limit, config.cleanup_grace)
                .await?;
            let deleted = registry.collect(limit).await?;
            let directories = registry.collect_workdirs(limit).await?;
            tracing::info!(
                released,
                deleted,
                directories,
                "expired assets and workdirs deleted"
            );
        }
    }
    Ok(())
}
