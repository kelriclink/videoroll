use super::*;
use crate::{api::AppState, domain::StageOutcome, executor::Executor, scheduler::Scheduler};
use std::{sync::Arc, time::Duration};

#[tokio::test]
async fn audio_only_real_postgres_http() {
    let Ok(url) = std::env::var("VIDEOROLL_TEST_DUB_HANDOFF_DB_URL") else {
        return;
    };
    let parsed = reqwest::Url::parse(&url).unwrap();
    assert_eq!(parsed.host_str(), Some("127.0.0.1"));
    assert_eq!(parsed.path(), "/videoroll_test");
    let db = Database::connect(&url).await.unwrap();
    sqlx::raw_sql(include_str!(
        "../../tests/bootstrap_legacy_for_integration.sql"
    ))
    .execute(&db.pool)
    .await
    .unwrap();
    db.migrate().await.unwrap();
    let temp = tempfile::tempdir().unwrap();
    let mut config = Config::from_env().unwrap();
    config.remote_renderer_enabled = true;
    config.storage_root = temp.path().join("storage");
    config.youdub_runtime = Some(temp.path().join("mock-youdub-v2"));
    config.youdub_python = Some("python3".into());
    config.youdub_bridge =
        Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/mock_youdub_bridge.py"));
    fs::create_dir_all(&config.storage_root).await.unwrap();
    let config = Arc::new(config);
    let store = AssetRegistry::new(db.clone(), config.storage_root.clone());
    let scheduler = Scheduler::new(db.clone(), Duration::from_secs(300));
    let executor = Executor::new(config.clone(), db.clone(), scheduler.clone(), store.clone());
    let (task, flow, job_id) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
    sqlx::query("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES($1,'local','own','rendered','rust',0,0)").bind(task).execute(&db.pool).await.unwrap();
    sqlx::query("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,current_stage,request_json) VALUES($1,$2,1,'rust-v2','videoroll.rust.pipeline','running','dubbing',$3)").bind(flow).bind(task).bind(json!({"dubbing":{"enabled":true}})).execute(&db.pool).await.unwrap();
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,workflow_id,stage,generation,status,request_json) VALUES($1,$2,'rust-v2',$3,$4,'dubbing',1,'pending','{}')").bind(job_id).bind(format!("fixture:{job_id}")).bind(task).bind(flow).execute(&db.pool).await.unwrap();
    let video_key = format!("v2/raw/{task}/source.mp4");
    let srt_key = format!("v2/subtitles/{task}/source.srt");
    let video = store.resolve_existing(&video_key).await.unwrap();
    let srt = store.resolve_existing(&srt_key).await.unwrap();
    fs::create_dir_all(video.parent().unwrap()).await.unwrap();
    fs::create_dir_all(srt.parent().unwrap()).await.unwrap();
    assert!(
        Command::new("ffmpeg")
            .args([
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x120:r=25:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-y"
            ])
            .arg(&video)
            .status()
            .await
            .unwrap()
            .success()
    );
    fs::write(&srt, "1\n00:00:00,000 --> 00:00:01,000\n配音测试\n\n")
        .await
        .unwrap();
    for (kind, key) in [("video_raw", &video_key), ("subtitle_srt", &srt_key)] {
        let (sha, size) = store.sha256(key).await.unwrap();
        sqlx::query("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES($1,$2,$3::asset_kind,$4,$5,$6)").bind(Uuid::new_v4()).bind(task).bind(kind).bind(key).bind(sha).bind(size).execute(&db.pool).await.unwrap();
    }
    let job = scheduler.claim().await.unwrap().unwrap();
    assert_eq!(job.id, job_id);
    let result = executor.execute(&job).await.unwrap();
    let ticket = match result {
        StageOutcome::Awaiting { ref ticket } => ticket.clone(),
        other => panic!("expected remote P2, got {other:?}"),
    };
    let execution = Uuid::parse_str(&ticket).unwrap();
    // Repeat the exact handoff before awaiting was committed.
    let again = executor.execute(&job).await.unwrap();
    assert!(matches!(again,StageOutcome::Awaiting{ticket:t} if t==ticket));
    scheduler.complete(&job, result).await.unwrap();
    assert!(
        scheduler
            .complete_external(job_id, &ticket, json!({"skipped":"forged"}))
            .await
            .is_err()
    );
    assert!(
        scheduler
            .complete_external(
                job_id,
                &ticket,
                json!({"mode":MODE,"remote_completed":true,"remote_execution_id":execution})
            )
            .await
            .is_err()
    );

    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM vr2_dubbing_runs WHERE job_id=$1")
            .bind(job_id)
            .fetch_one(&db.pool)
            .await
            .unwrap(),
        0
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM assets WHERE task_id=$1 AND kind::text='dubbing_audio'"
        )
        .bind(task)
        .fetch_one(&db.pool)
        .await
        .unwrap(),
        2
    );
    let worker = Uuid::new_v4();
    let token = "b".repeat(64);
    sqlx::query("INSERT INTO render_workers(id,worker_key,name,platform,runtime_engine,credential_hash) VALUES($1,$2,'test','linux','rust-v2',$3)").bind(worker).bind(worker.to_string()).bind({ use sha2::Digest; hex::encode(sha2::Sha256::digest(token.as_bytes())) }).execute(&db.pool).await.unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let app = crate::remote_render::worker_router().with_state(AppState {
        db: db.clone(),
        config: config.clone(),
        scheduler: scheduler.clone(),
        storage: store.clone(),
    });
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let client = reqwest::Client::new();
    let claim = || {
        client.post(format!("{base}/render-workers/v1/{worker}/claim")).bearer_auth(&token).json(&json!({"available_slots":1,"available_encoders":["mpeg4"],"accepted_transfer_modes":["http"]})).send()
    };
    let old: Value = claim()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(old["execution"].is_null(), "{old}");
    sqlx::query(
        "UPDATE render_workers SET capabilities=jsonb_build_object('dub_burn_in',true) WHERE id=$1",
    )
    .bind(worker)
    .execute(&db.pool)
    .await
    .unwrap();
    let claimed: Value = claim()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(claimed["execution"]["id"], ticket);
    assert_eq!(claimed["render_spec"]["mode"], MODE);
    assert_eq!(
        claimed["render_spec"]["artifacts"]
            .as_array()
            .unwrap()
            .len(),
        4
    );
    let fence = claimed["execution"]["fence_token"].as_str().unwrap();
    // Actual audio and subtitle transfer.
    let get = |role: &str| {
        client
            .get(format!(
                "{base}/render-workers/v1/executions/{execution}/artifacts/{role}"
            ))
            .bearer_auth(&token)
            .send()
    };
    let mixed = get("audio")
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .bytes()
        .await
        .unwrap();
    let audio_path = temp.path().join("mix.m4a");
    fs::write(&audio_path, &mixed).await.unwrap();
    let ass = get("ass")
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(ass.contains("配音测试"));
    // Produce a real P2 video (the worker-specific mux is separately tested).
    let output = temp.path().join("dubbed.mp4");
    assert!(
        Command::new("ffmpeg")
            .args(["-v", "error", "-i"])
            .arg(&video)
            .arg("-i")
            .arg(&audio_path)
            .args([
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-af",
                "apad",
                "-shortest",
                "-y"
            ])
            .arg(&output)
            .status()
            .await
            .unwrap()
            .success()
    );
    let bytes = fs::read(&output).await.unwrap();
    let upload = client
        .post(format!(
            "{base}/render-workers/v1/executions/{execution}/output"
        ))
        .bearer_auth(&token)
        .multipart(
            reqwest::multipart::Form::new()
                .text("fence_token", fence.to_owned())
                .part(
                    "file",
                    reqwest::multipart::Part::bytes(bytes).file_name("dubbed.mp4"),
                ),
        )
        .send()
        .await
        .unwrap();
    let status = upload.status();
    let receipt: Value = upload.json().await.unwrap();
    assert!(status.is_success(), "{status}: {receipt}");
    assert_eq!(receipt["kind"], "video_dubbed");
    // Pause while the worker holds a committed output, then resume only
    // after the exact fence drains. The next scan must not re-synthesize.
    scheduler.pause(flow).await.unwrap();
    scheduler.resume(flow).await.unwrap();
    crate::render_events::tick(&db, &config, 20).await.unwrap();
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT state FROM render_executions WHERE id=$1")
            .bind(execution)
            .fetch_one(&db.pool)
            .await
            .unwrap(),
        "paused"
    );
    // Lost completion callback: expire lease, recover from registered bytes.
    sqlx::query("UPDATE render_executions SET lease_until=now()-interval '1 minute' WHERE id=$1")
        .bind(execution)
        .execute(&db.pool)
        .await
        .unwrap();
    crate::render_events::tick(&db, &config, 20).await.unwrap();
    crate::render_events::tick(&db, &config, 20).await.unwrap();
    let proof: Option<String> =
        sqlx::query_scalar("SELECT output_video_key FROM vr2_dubbing_runs WHERE job_id=$1")
            .bind(job_id)
            .fetch_optional(&db.pool)
            .await
            .unwrap();
    assert_eq!(proof.as_deref(), receipt["storage_key"].as_str());
    assert_eq!(
        sqlx::query_scalar::<_, String>("SELECT state FROM vr2_jobs WHERE id=$1")
            .bind(job_id)
            .fetch_one(&db.pool)
            .await
            .unwrap(),
        "succeeded"
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM vr2_jobs WHERE workflow_id=$1 AND stage='publish'"
        )
        .bind(flow)
        .fetch_one(&db.pool)
        .await
        .unwrap(),
        1
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM assets WHERE task_id=$1 AND kind::text='video_final'"
        )
        .bind(task)
        .fetch_one(&db.pool)
        .await
        .unwrap(),
        0
    );
    server.abort();
    println!(
        "PASS audio-only mock YouDub, immutable handoff, old-worker exclusion, real HTTP audio/ASS/MP4, lost callback recovery, exactly one publish stage, no P1 overwrite"
    );
}
