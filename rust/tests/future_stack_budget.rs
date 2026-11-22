use serde_json::json;
use std::{mem::size_of_val, path::PathBuf, sync::Arc, time::Duration};
use uuid::Uuid;
use videoroll_v2::{
    Config, Database,
    domain::{Stage, WorkItem},
    executor::Executor,
    scheduler::Scheduler,
    storage::AssetRegistry,
};

#[tokio::test]
async fn async_job_futures_fit_default_tokio_worker_stack() {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect_lazy("postgresql://test:test@127.0.0.1/test")
        .unwrap();
    let db = Database { pool };
    let cfg = Arc::new(Config {
        bind: "127.0.0.1:18080".parse().unwrap(),
        database_url: "postgresql://test:test@127.0.0.1/test".into(),
        storage_root: PathBuf::from("/tmp/videoroll-rust-stack-test"),
        internal_token: "test-only-super-long-internal-token-1".into(),
        legacy_internal_secret: None,
        bootstrap_secret: None,
        secure_cookies: false,
        worker_gateway: None,
        download_binary: "yt-dlp".into(),
        ffmpeg_binary: "ffmpeg".into(),
        ffprobe_binary: "ffprobe".into(),
        asr_endpoint: None,
        asr_api_key: None,
        asr_model: "test".into(),
        translate_endpoint: None,
        translate_api_key: None,
        translate_model: None,
        youdub_python: None,
        youdub_bridge: None,
        youdub_runtime: None,
        legacy_orchestrator_url: None,
        legacy_internal_token: None,
        legacy_publish_allowed: false,
        native_bilibili_allowed: false,
        native_bilibili_cookie: None,
        native_bilibili_csrf: None,
        native_bilibili_api_base: None,
        native_bilibili_subtitles: true,
        source_scanner_enabled: false,
        remote_renderer_enabled: false,
        compat_orchestrator_url: None,
        compat_internal_token: None,
        compat_writes_allowed: false,
        playout_root: None,
        embedding_endpoint: None,
        embedding_model: None,
        embedding_api_key: None,
        worker_count: 1,
        worker_poll: Duration::from_secs(1),
        job_lease: Duration::from_secs(180),
        cleanup_grace: Duration::from_secs(86400),
    });
    let scheduler = Scheduler::new(db.clone(), Duration::from_secs(180));
    let storage = AssetRegistry::new(db.clone(), cfg.storage_root.clone());
    let executor = Executor::new(cfg, db, scheduler, storage);
    let job = WorkItem {
        id: Uuid::new_v4(),
        workflow_id: Uuid::new_v4(),
        task_id: Uuid::new_v4(),
        stage: Stage::Download,
        generation: 1,
        operation_key: "example".into(),
        lease_token: Uuid::new_v4(),
        attempts: 1,
        max_attempts: 5,
        payload: json!({}),
    };
    let execute_future = executor.execute(&job);
    let worker_future = executor.worker_loop(0);
    let execute_size = size_of_val(&execute_future);
    let worker_size = size_of_val(&worker_future);
    println!("execute_future_bytes={execute_size} worker_loop_future_bytes={worker_size}");
    assert!(
        execute_size < 512 * 1024,
        "execution state future exceeds 512KiB stack budget"
    );
    assert!(
        worker_size < 512 * 1024,
        "worker-loop future exceeds 512KiB stack budget"
    );
}
