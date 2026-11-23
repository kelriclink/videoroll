//! Real PostgreSQL scheduler download-admission regression.
//! Enabled only inside the disposable videoroll_test created by smoke-isolated.sh.
use serde_json::{Value, json};
use std::time::Duration;
use uuid::Uuid;
use videoroll_v2::{Database, scheduler::Scheduler};

#[tokio::test(flavor = "multi_thread", worker_threads = 6)]
async fn simultaneous_worker_claims_respect_download_setting() {
    if std::env::var("VIDEOROLL_V2_TEST_QUEUE_CLAIM")
        .ok()
        .as_deref()
        != Some("1")
    {
        return;
    }
    let uri = std::env::var("DATABASE_URL").expect("test database URL");
    assert_eq!(
        uri.rsplit('/').next(),
        Some("videoroll_test"),
        "refuse scheduler mutation on any other database"
    );
    let db = Database::connect(&uri).await.unwrap();
    let original: Option<Value> = sqlx::query_scalar(
        "SELECT value_json FROM app_settings WHERE key='workflow.youtube_download_concurrency'",
    )
    .fetch_optional(&db.pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES
       ('workflow.youtube_download_concurrency',$1,1)
       ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json",
    )
    .bind(json!({"max_concurrent":1}))
    .execute(&db.pool)
    .await
    .unwrap();
    let mut ids = Vec::<Uuid>::new();
    for idx in 0..4 {
        let task = Uuid::new_v4();
        let workflow = Uuid::new_v4();
        let job = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO tasks(id,source_type,source_license,status,priority)
             VALUES($1,'local','own','created',$2)",
        )
        .bind(task)
        .bind(idx)
        .execute(&db.pool)
        .await
        .unwrap();
        sqlx::query(
            "UPDATE tasks SET runtime_owner='rust',runtime_origin='local_upload' WHERE id=$1",
        )
        .bind(task)
        .execute(&db.pool)
        .await
        .unwrap();
        sqlx::query(
            "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage)
             VALUES($1,$2,1,'running','download')",
        )
        .bind(workflow)
        .bind(task)
        .execute(&db.pool)
        .await
        .unwrap();
        sqlx::query(
            "INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state)
             VALUES($1,$2,$3,'download',1,$4,'pending')",
        )
        .bind(job)
        .bind(workflow)
        .bind(task)
        .bind(format!("queue-claim-cap-{task}"))
        .execute(&db.pool)
        .await
        .unwrap();
        ids.push(task);
    }
    let s = Scheduler::new(db.clone(), Duration::from_secs(300));
    let mut handles = Vec::new();
    for _ in 0..6 {
        let scheduler = s.clone();
        handles.push(tokio::spawn(
            async move { scheduler.claim().await.unwrap() },
        ));
    }
    let mut claimed = 0;
    for h in handles {
        if h.await.unwrap().is_some() {
            claimed += 1;
        }
    }
    assert_eq!(
        claimed, 1,
        "cap=1 must admit exactly one concurrent download"
    );
    let running: i64 = sqlx::query_scalar(
        "SELECT count(*)::BIGINT FROM vr2_jobs
        WHERE task_id=ANY($1) AND stage='download' AND state='running'
        AND lease_until>now()",
    )
    .bind(&ids)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(running, 1);
    sqlx::query(
        "UPDATE app_settings SET value_json=$1
        WHERE key='workflow.youtube_download_concurrency'",
    )
    .bind(json!({"max_concurrent":2}))
    .execute(&db.pool)
    .await
    .unwrap();
    let mut handles = Vec::new();
    for _ in 0..5 {
        let scheduler = s.clone();
        handles.push(tokio::spawn(
            async move { scheduler.claim().await.unwrap() },
        ));
    }
    let mut second = 0;
    for h in handles {
        if h.await.unwrap().is_some() {
            second += 1;
        }
    }
    assert_eq!(second, 1, "cap increase should admit one more download");
    let running: i64 = sqlx::query_scalar(
        "SELECT count(*)::BIGINT FROM vr2_jobs
        WHERE task_id=ANY($1) AND stage='download' AND state='running'
          AND lease_until>now()",
    )
    .bind(&ids)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(running, 2);
    // Keep test fixtures and config out of subsequent smoke stages.
    sqlx::query("DELETE FROM vr2_jobs WHERE task_id=ANY($1)")
        .bind(&ids)
        .execute(&db.pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM vr2_workflows WHERE task_id=ANY($1)")
        .bind(&ids)
        .execute(&db.pool)
        .await
        .unwrap();
    sqlx::query("UPDATE tasks SET runtime_owner='python',runtime_origin=NULL WHERE id=ANY($1)")
        .bind(&ids)
        .execute(&db.pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM tasks WHERE id=ANY($1)")
        .bind(&ids)
        .execute(&db.pool)
        .await
        .unwrap();
    if let Some(value) = original {
        sqlx::query(
            "UPDATE app_settings SET value_json=$1
           WHERE key='workflow.youtube_download_concurrency'",
        )
        .bind(value)
        .execute(&db.pool)
        .await
        .unwrap();
    } else {
        sqlx::query("DELETE FROM app_settings WHERE key='workflow.youtube_download_concurrency'")
            .execute(&db.pool)
            .await
            .unwrap();
    }
    println!("PASS: simultaneous PostgreSQL leases enforce shared download cap 1→2");
}
