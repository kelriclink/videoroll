//! Original knowledge corpus integration with mixed model/dimension fixtures.
use axum::{Json, Router, routing::post};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use uuid::Uuid;
use videoroll_v2::{
    Config, Database,
    domain::{Stage, WorkItem},
    rag, subtitle_embedding_infer_v1 as embedding,
    subtitles::SubtitleCue,
};

async fn setting(db: &Database, key: &str, value: Value) {
    sqlx::query("INSERT INTO app_settings(key,value_json) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json")
        .bind(key).bind(value).execute(&db.pool).await.unwrap();
}
async fn doc(
    db: &Database,
    title: &str,
    lang: &str,
    domain: &str,
    status: &str,
    model: &str,
    v: &[f32],
) -> Uuid {
    let id = Uuid::new_v4();
    sqlx::query("INSERT INTO translation_knowledge_items(id,item_type,title,content,target_lang,domain,status,embedding_model,embedding,confidence) VALUES($1,'document',$2,$2,$3,$4,$5,$6,CAST($7 AS vector),0.9)")
        .bind(id).bind(title).bind(lang).bind(domain).bind(status).bind(model).bind(embedding::vector_literal(v)).execute(&db.pool).await.unwrap();
    id
}
async fn term(
    db: &Database,
    term: &str,
    translation: &str,
    lang: &str,
    domain: &str,
    status: &str,
) -> Uuid {
    let id = Uuid::new_v4();
    sqlx::query("INSERT INTO translation_knowledge_items(id,item_type,term,normalized_term,translation,target_lang,domain,status,confidence) VALUES($1,'term',$2,$2,$3,$4,$5,$6,1)")
        .bind(id).bind(term).bind(translation).bind(lang).bind(domain).bind(status).execute(&db.pool).await.unwrap();
    id
}

#[tokio::test]
async fn original_settings_and_vector_filters() {
    if std::env::var("VIDEOROLL_V2_TEST_ORIGINAL_RAG").as_deref() != Ok("1") {
        return;
    }
    let config = Config::from_env().unwrap();
    let url = reqwest::Url::parse(&config.database_url).unwrap();
    assert_eq!(url.host_str(), Some("127.0.0.1"));
    assert_eq!(url.path(), "/videoroll_test");
    assert_eq!(
        config.internal_token,
        "isolated-original-rag-token-0123456789"
    );
    let db = Database::connect(&config.database_url).await.unwrap();
    db.migrate().await.unwrap();
    let count = Arc::new(AtomicUsize::new(0));
    let mode = Arc::new(AtomicUsize::new(0));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let calls = count.clone();
    let status = mode.clone();
    let mock = tokio::spawn(async move {
        axum::serve(
            listener,
            Router::new().route(
                "/v1/embeddings",
                post(
                    move |headers: axum::http::HeaderMap, Json(body): Json<Value>| {
                        let calls = calls.clone();
                        let status = status.clone();
                        async move {
                            assert_eq!(headers["authorization"], "Bearer isolated-embedding-key");
                            assert_eq!(body["model"], "test-model");
                            assert_eq!(body["dimensions"], 3);
                            assert!(
                                body["input"]
                                    .as_str()
                                    .unwrap()
                                    .contains("electronic circuit")
                            );
                            calls.fetch_add(1, Ordering::SeqCst);
                            if status.load(Ordering::SeqCst) == 0 {
                                (
                                    axum::http::StatusCode::OK,
                                    Json(json!({"data":[{"embedding":[1,0,0]}]})),
                                )
                            } else {
                                (
                                    axum::http::StatusCode::BAD_REQUEST,
                                    Json(json!({"error":"fixture failure"})),
                                )
                            }
                        }
                    },
                ),
            ),
        )
        .await
        .unwrap();
    });
    let mut raw = json!({"rag_enabled":true,"rag_domain":"electronics","rag_top_k":8,"rag_min_score":0.8,
        "rag_embedding_provider":"openai","rag_embedding_model":"test-model","rag_embedding_dimensions":3,
        "rag_embedding_openai":{"base_url":format!("http://127.0.0.1:{port}/v1"),"api_key_enc":"dbplain:v1:isolated-embedding-key","timeout_seconds":5}});
    setting(&db, "subtitle.translate", raw.clone()).await;
    let exact = term(
        &db,
        "electronic circuit",
        "电路精确术语",
        "zh",
        "electronics",
        "approved",
    )
    .await;
    let semantic = doc(
        &db,
        "semantic vector evidence",
        "zh",
        "electronics",
        "approved",
        "openai:test-model",
        &[1., 0., 0.],
    )
    .await;
    let generic = doc(
        &db,
        "generic approved evidence",
        "zh",
        "",
        "auto_approved",
        "openai:test-model",
        &[0.9, 0.1, 0.],
    )
    .await;
    let wrong_dim = doc(
        &db,
        "wrong dimension",
        "zh",
        "electronics",
        "approved",
        "openai:test-model",
        &[1., 0.],
    )
    .await;
    let wrong_model = doc(
        &db,
        "wrong model",
        "zh",
        "electronics",
        "approved",
        "local:test-model",
        &[1., 0., 0.],
    )
    .await;
    let wrong_lang = doc(
        &db,
        "wrong language",
        "ja",
        "electronics",
        "approved",
        "openai:test-model",
        &[1., 0., 0.],
    )
    .await;
    let wrong_domain = doc(
        &db,
        "wrong domain",
        "zh",
        "gaming",
        "approved",
        "openai:test-model",
        &[1., 0., 0.],
    )
    .await;
    let pending = doc(
        &db,
        "pending evidence",
        "zh",
        "electronics",
        "pending",
        "openai:test-model",
        &[1., 0., 0.],
    )
    .await;
    let opposite = doc(
        &db,
        "opposite evidence",
        "zh",
        "electronics",
        "approved",
        "openai:test-model",
        &[-1., 0., 0.],
    )
    .await;
    let zero = doc(
        &db,
        "zero vector",
        "zh",
        "electronics",
        "approved",
        "openai:test-model",
        &[0., 0., 0.],
    )
    .await;
    let denied_term = term(
        &db,
        "circuit",
        "未审批术语",
        "zh",
        "electronics",
        "archived",
    )
    .await;
    let task_id = Uuid::new_v4();
    sqlx::query("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner) VALUES($1,'local','own','downloaded','rust')").bind(task_id).execute(&db.pool).await.unwrap();
    // A task-private row must never enter the global V1 corpus.
    let private = doc(
        &db,
        "private task evidence",
        "zh",
        "electronics",
        "approved",
        "openai:test-model",
        &[1., 0., 0.],
    )
    .await;
    sqlx::query("UPDATE translation_knowledge_items SET created_by='rust-v2',native_scope='task',native_collection='translation',native_kind='passage',native_task_id=$2 WHERE id=$1").bind(private).bind(task_id).execute(&db.pool).await.unwrap();
    let job = WorkItem {
        id: Uuid::new_v4(),
        workflow_id: Uuid::new_v4(),
        task_id: Uuid::new_v4(),
        stage: Stage::Subtitle,
        generation: 1,
        operation_key: "original-rag-fixture".into(),
        lease_token: Uuid::new_v4(),
        attempts: 1,
        max_attempts: 3,
        payload: json!({}),
    };
    let cues = [SubtitleCue {
        start_ms: 0,
        end_ms: 1000,
        text: "The electronic circuit uses a capacitor.".into(),
        secondary_text: None,
    }];
    let context = rag::prepare(&config, &db, &job, &cues, "zh").await.unwrap();
    assert_eq!(context.evidence_ids, vec![exact, semantic, generic]);
    assert!(
        context.prompt.contains("电路精确术语")
            && context.prompt.contains("semantic vector evidence")
    );
    for id in [
        wrong_dim,
        wrong_model,
        wrong_lang,
        wrong_domain,
        pending,
        opposite,
        zero,
        denied_term,
        private,
    ] {
        assert!(!context.evidence_ids.contains(&id));
    }
    assert_eq!(count.load(Ordering::SeqCst), 1);
    println!(
        "PASS: original V1 flag, exact terms, pgvector score/rank, mixed model/dimension/language/domain/status/scope filters"
    );
    mode.store(1, Ordering::SeqCst);
    let degraded = rag::prepare(&config, &db, &job, &cues, "zh").await.unwrap();
    assert_eq!(degraded.evidence_ids, vec![exact]);
    setting(&db, "subtitle.rag", json!({"research_blocking":true})).await;
    assert!(rag::prepare(&config, &db, &job, &cues, "zh").await.is_err());
    setting(&db, "subtitle.rag", json!({})).await;
    raw["rag_enabled"] = json!(false);
    setting(&db, "subtitle.translate", raw).await;
    let before = count.load(Ordering::SeqCst);
    assert!(
        rag::prepare(&config, &db, &job, &cues, "zh")
            .await
            .unwrap()
            .evidence_ids
            .is_empty()
    );
    assert_eq!(count.load(Ordering::SeqCst), before);
    mock.abort();
    println!(
        "PASS: failed vector request retains exact terms; blocking policy fails closed; disabled flag avoids external request"
    );

    // Real existing local BGE model, no online calls and no writes to model files.
    if let Ok(model_dir) = std::env::var("SUBTITLE_RAG_EMBEDDING_MODEL_DIR") {
        let local = json!({"rag_enabled":true,"rag_domain":"electronics","rag_top_k":8,"rag_min_score":0.85,
          "rag_embedding_provider":"local","rag_embedding_model":"BAAI/bge-small-zh-v1.5","rag_embedding_dimensions":512,
          "rag_embedding_model_dir":model_dir,"rag_embedding_device":"openvino:CPU","rag_embedding_timeout_seconds":90});
        setting(&db, "subtitle.translate", local).await;
        let cfg = embedding::load_db(&db).await.unwrap();
        let text = "电子电路使用电阻和电容";
        let v = embedding::embed(&cfg, text).await.unwrap();
        assert_eq!(v.len(), 512);
        let local_id = doc(
            &db,
            "本地语义向量证据",
            "zh",
            "electronics",
            "approved",
            &embedding::model_key(&cfg),
            &v,
        )
        .await;
        let source = [SubtitleCue {
            start_ms: 0,
            end_ms: 1000,
            text: text.into(),
            secondary_text: None,
        }];
        let actual = rag::prepare(&config, &db, &job, &source, "zh")
            .await
            .unwrap();
        assert_eq!(actual.evidence_ids, vec![local_id]);
        assert!(actual.prompt.contains("本地语义向量证据"));
        println!(
            "PASS: real BGE OpenVINO 512-dimensional query retrieves original pgvector row and renders translation context"
        );
    }
}
