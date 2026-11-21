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
            let executor = Executor::new(config.clone(), db.clone(), scheduler, storage);
            let mut workers = tokio::task::JoinSet::new();
            for index in 0..config.worker_count {
                let worker = executor.clone();
                workers.spawn(async move { worker.worker_loop(index).await });
            }
            while let Some(result) = workers.join_next().await {
                result?;
            }
        }
        Command::Gc { limit } => {
            let registry = AssetRegistry::new(db, config.storage_root.clone());
            let deleted = registry.collect(limit).await?;
            tracing::info!(deleted, "expired objects deleted");
        }
    }
    Ok(())
}
