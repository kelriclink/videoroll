//! Authenticated WebSocket transport compatible with the original React
//! realtime protocol. Transactional PG NOTIFY routes invalidations; clients
//! reconcile from the original authoritative tables, not timestamp cursors.
use crate::{AppResult, api::AppState, auth};
use axum::{
    Router,
    extract::{
        State,
        ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
    },
    http::{HeaderMap, header},
    response::Response,
    routing::get,
};
use chrono::Utc;
use futures_util::StreamExt;
use serde_json::{Value, json};
use sqlx::postgres::PgListener;
use std::collections::{HashMap, HashSet};
use tokio::{
    sync::broadcast,
    time::{Duration, interval},
};
use uuid::Uuid;

const MAX_CLIENT_MESSAGE_BYTES: usize = 8 * 1024;
const MAX_TOPICS: usize = 32;
const CHANNEL: &str = "videoroll_ui_commit_v1";

fn valid_topic(topic: &str) -> bool {
    matches!(
        topic,
        "tasks" | "queue" | "resources" | "agents" | "publishing"
    ) || topic
        .strip_prefix("task:")
        .is_some_and(|id| Uuid::parse_str(id).is_ok())
}
fn parse_topics(input: &Value) -> Result<HashSet<String>, &'static str> {
    let values = input.as_array().ok_or("topics must be an array")?;
    if values.len() > MAX_TOPICS {
        return Err("too many realtime subscription topics");
    }
    let mut topics = HashSet::new();
    for item in values {
        let Some(topic) = item.as_str() else {
            return Err("realtime subscription topic must be a string");
        };
        let topic = topic.trim();
        if !valid_topic(topic) {
            return Err("invalid realtime subscription topics");
        }
        topics.insert(topic.to_owned());
    }
    Ok(topics)
}
fn allowed_origin(headers: &HeaderMap) -> bool {
    let Some(origin) = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    let Some(host) = headers.get(header::HOST).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    let Ok(url) = reqwest::Url::parse(origin) else {
        return false;
    };
    if !matches!(url.scheme(), "http" | "https")
        || url.username() != ""
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return false;
    }
    let Ok(authority) = host.parse::<axum::http::uri::Authority>() else {
        return false;
    };
    let default_port = if url.scheme() == "https" { 443 } else { 80 };
    url.host_str()
        .is_some_and(|hostname| hostname.eq_ignore_ascii_case(authority.host()))
        && url.port_or_known_default() == Some(authority.port_u16().unwrap_or(default_port))
}
async fn ws(
    State(state): State<AppState>,
    headers: HeaderMap,
    upgrade: WebSocketUpgrade,
) -> Response {
    let code = if !allowed_origin(&headers) {
        Some(4403)
    } else if !auth::request_authenticated(&headers, &state.db, &state.config).await {
        Some(4401)
    } else {
        None
    };
    upgrade.on_upgrade(move |mut socket| async move {
        if let Some(code) = code {
            let _ = socket
                .send(Message::Close(Some(CloseFrame {
                    code,
                    reason: "authentication or origin rejected".into(),
                })))
                .await;
        } else {
            serve(&mut socket, state).await;
        }
    })
}
async fn send(socket: &mut WebSocket, message: Value) -> bool {
    let Ok(text) = serde_json::to_string(&message) else {
        return false;
    };
    tokio::time::timeout(
        Duration::from_secs(5),
        socket.send(Message::Text(text.into())),
    )
    .await
    .is_ok_and(|r| r.is_ok())
}

#[derive(Debug, Clone)]
enum Change {
    Invalidate {
        topics: Vec<String>,
        deleted_task: Option<Uuid>,
    },
    Gap,
}
fn decode_change(payload: &str) -> Option<Change> {
    if payload.len() > 8192 {
        return None;
    }
    let v: Value = serde_json::from_str(payload).ok()?;
    let topics = parse_topics(&v["topics"])
        .ok()?
        .into_iter()
        .collect::<Vec<_>>();
    if topics.is_empty() {
        return None;
    }
    let deleted_task = if v["kind"] == "task.deleted" {
        Some(Uuid::parse_str(v["entity_id"].as_str()?).ok()?)
    } else {
        None
    };
    Some(Change::Invalidate {
        topics,
        deleted_task,
    })
}
static BUS: std::sync::LazyLock<broadcast::Sender<Change>> =
    std::sync::LazyLock::new(|| broadcast::channel(512).0);
/// One dedicated LISTEN connection per API process, independent of clients.
/// A broken connection cannot replay NOTIFY, so send a fresh snapshot signal
/// after reconnecting. Bounded broadcast lag is also explicitly reconciled.
pub async fn listen_commits(database_url: String) {
    loop {
        let listener = tokio::time::timeout(Duration::from_secs(10), async {
            let mut listener = PgListener::connect(&database_url).await?;
            listener.listen(CHANNEL).await?;
            listener.eager_reconnect(false);
            Ok::<_, sqlx::Error>(listener)
        })
        .await;
        if let Ok(Ok(mut listener)) = listener {
            let _ = BUS.send(Change::Gap);
            loop {
                match listener.try_recv().await {
                    Ok(Some(n)) => {
                        if let Some(change) = decode_change(n.payload()) {
                            let _ = BUS.send(change);
                        } else {
                            let _ = BUS.send(Change::Gap);
                        }
                    }
                    Ok(None) | Err(_) => break,
                }
            }
        }
        // The next successful LISTEN sends a Gap *after* it is subscribed.
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
async fn login_snapshots(state: &AppState) -> AppResult<Vec<Value>> {
    let rows=sqlx::query("SELECT session.* FROM (VALUES ('douyin'),('xiaohongshu'),('kuaishou')) AS platforms(platform)
      CROSS JOIN LATERAL (
       SELECT id,status,request_json,result_json,created_at,completed_at FROM operation_inbox
       WHERE runtime_engine='rust-sau-login' AND request_json->>'platform'=platforms.platform
       ORDER BY created_at DESC,id DESC LIMIT 1
      ) session").fetch_all(&state.db.pool).await?;
    rows.iter().map(crate::sau_login_native_v1::dto).collect()
}
async fn serve(socket: &mut WebSocket, state: AppState) {
    let connection_id = Uuid::new_v4();
    if !send(
        socket,
        json!({"type":"ready","connection_id":connection_id,"heartbeat_seconds":20}),
    )
    .await
    {
        return;
    }
    let mut topics = HashSet::<String>::new();
    let mut changes = BUS.subscribe();
    let mut dirty = HashSet::<String>::new();
    let mut flush = interval(Duration::from_millis(800));
    flush.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut queue_poll = interval(Duration::from_secs(20));
    queue_poll.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut heartbeat = interval(Duration::from_secs(20));
    let mut resource_poll = interval(Duration::from_secs(3));
    heartbeat.tick().await;
    resource_poll.tick().await;
    let mut login_poll = interval(Duration::from_secs(2));
    // At most three platform snapshots per authenticated connection.
    let mut last_login = HashMap::<String, Value>::new();

    loop {
        tokio::select! {
            incoming = socket.next() => {
                match incoming {
                    Some(Ok(Message::Text(raw))) => {
                        if raw.len()>MAX_CLIENT_MESSAGE_BYTES {
                            let _=socket.send(Message::Close(Some(CloseFrame{code:1009,reason:"message too large".into()}))).await;
                            break;
                        }
                        let value:Value=match serde_json::from_str(&raw){
                            Ok(v)=>v,
                            Err(_)=>{
                                if !send(socket,json!({"type":"error","detail":"invalid JSON"})).await{break;}
                                continue;
                            }
                        };
                        match value.get("op").and_then(Value::as_str) {
                            Some("pong")=>{},
                            Some("set_subscriptions")=>{
                                match parse_topics(&value["topics"]) {
                                    Ok(updated)=>{
                                        if !updated.contains("publishing") || !topics.contains("publishing"){last_login.clear();}

                                        let mut sorted=updated.iter().cloned().collect::<Vec<_>>();
                                        sorted.sort();
                                        let added=updated.difference(&topics).cloned().collect::<Vec<_>>();
                                        topics=updated;
                                        if !send(socket,json!({"type":"subscribed","topics":sorted})).await{break;}
                                        // Snapshot after subscription closes the initial
                                        // HTTP-read/subscribe race (also on reconnect).
                                        dirty.extend(added);
                                    },
                                    Err(detail)=>{
                                        if !send(socket,json!({"type":"error","detail":detail})).await{break;}
                                    }
                                }
                            },
                            _=>if !send(socket,json!({"type":"error","detail":"unsupported operation"})).await{break;}
                        }
                    },
                    Some(Ok(Message::Close(_)))|None|Some(Err(_))=>break,
                    Some(Ok(_))=>{},
                }
            }
            _=heartbeat.tick()=>{
                if !send(socket,json!({"type":"heartbeat"})).await {break;}
            }
            _=resource_poll.tick(), if topics.contains("resources")=>{
                if let Ok(data)=crate::system_resources::sample(&state).await
                    && !send(socket,json!({
                        "v":1,"type":"event","event_id":Uuid::new_v4(),
                        "topics":["resources"],"name":"system.resources.sample","data":data,
                    })).await {break;}
            }
            _=login_poll.tick(), if topics.contains("publishing")=>{
                if let Ok(rows)=login_snapshots(&state).await {
                    let mut connected=true;
                    for data in rows{
                        let platform=data["platform"].as_str().unwrap_or("").to_owned();
                        if last_login.get(&platform)==Some(&data){continue;}
                        if !send(socket,json!({"v":1,"type":"event","event_id":Uuid::new_v4(),
                            "entity_id":data["id"],"occurred_at":Utc::now(),"topics":["publishing"],
                            "name":"login_session.updated","data":data})).await {
                            connected=false;break;
                        }
                        last_login.insert(platform,data);
                    }
                    if !connected{break;}
                }
            }
            change=changes.recv()=>{
                match change{
                    Ok(Change::Invalidate{topics:changed,deleted_task})=>{
                        let matching=changed.into_iter().filter(|topic|topics.contains(topic)).collect::<Vec<_>>();
                        if matching.is_empty(){continue;}
                        if let Some(task)=deleted_task{
                            if !send(socket,json!({"v":1,"type":"event","event_id":Uuid::new_v4(),"entity_id":task,
                                "occurred_at":Utc::now(),"topics":matching,"name":"task.deleted","data":{"id":task}})).await{break;}
                        }
                        dirty.extend(matching);
                    }
                    Ok(Change::Gap)|Err(broadcast::error::RecvError::Lagged(_))=>dirty.extend(topics.iter().cloned()),
                    Err(broadcast::error::RecvError::Closed)=>break,
                }
            }
            _=flush.tick(),if !dirty.is_empty()=>{
                let mut changed=dirty.drain().filter(|topic|topics.contains(topic)).collect::<Vec<_>>();
                changed.sort();
                if !changed.is_empty()&&!send(socket,json!({"type":"resync_required","reason":"committed_changes","topics":changed})).await{break;}
            }
            _=queue_poll.tick(),if topics.contains("queue")=>{
                // Leases/deferred jobs can change eligibility without a DB write.
                // A server-side snapshot replaces browser API interval polling.
                if let Ok(data)=crate::subtitle_queue_v1::broker_snapshot(&state).await{
                    if !send(socket,json!({"v":1,"type":"event","event_id":Uuid::new_v4(),"topics":["queue"],"name":"queue.broker.snapshot","data":data})).await{break;}
                }
            }

        }
    }
}
pub fn router() -> Router<AppState> {
    Router::new().route("/ws/events", get(ws))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn committed_realtime_and_queue_real_postgres() {
        use crate::{Config, Database, scheduler::Scheduler, storage::AssetRegistry};
        use futures_util::{SinkExt, StreamExt};
        use std::sync::Arc;
        use tokio_tungstenite::{
            connect_async,
            tungstenite::{Message as Wire, client::IntoClientRequest},
        };
        let Ok(url) = std::env::var("VIDEOROLL_TEST_REALTIME_DB_URL") else {
            return;
        };
        let parsed = reqwest::Url::parse(&url).unwrap();
        assert_eq!(parsed.host_str(), Some("127.0.0.1"));
        assert_eq!(parsed.path(), "/videoroll_test");
        let db = Database::connect(&url).await.unwrap();
        sqlx::raw_sql(include_str!(
            "../tests/bootstrap_legacy_for_integration.sql"
        ))
        .execute(&db.pool)
        .await
        .unwrap();
        db.migrate().await.unwrap();
        let secret = "isolated-websocket-test-only";
        let hash = auth::encode_password("fixture-password").unwrap();
        sqlx::query("INSERT INTO app_settings(key,value_json) VALUES('admin.auth',$1)")
            .bind(json!({"password_hash":hash}))
            .execute(&db.pool)
            .await
            .unwrap();
        let cookie = auth::mint_cookie(secret, &hash, Utc::now().timestamp());
        let tmp = tempfile::tempdir().unwrap();
        let mut config = Config::from_env().unwrap();
        config.legacy_internal_secret = Some(secret.into());
        config.storage_root = tmp.path().into();
        let config = Arc::new(config);
        let scheduler = Scheduler::new(db.clone(), Duration::from_secs(300));
        let state = AppState {
            db: db.clone(),
            config: config.clone(),
            scheduler,
            storage: AssetRegistry::new(db.clone(), config.storage_root.clone()),
        };
        let mut monitor = BUS.subscribe();
        let listener = tokio::spawn(listen_commits(url.clone()));
        assert!(matches!(
            tokio::time::timeout(Duration::from_secs(5), monitor.recv())
                .await
                .unwrap()
                .unwrap(),
            Change::Gap
        ));
        let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = socket.local_addr().unwrap();
        let app = router().with_state(state.clone());
        let server = tokio::spawn(async move { axum::serve(socket, app).await.unwrap() });
        let mut req = format!("ws://{addr}/ws/events")
            .into_client_request()
            .unwrap();
        req.headers_mut()
            .insert("origin", format!("http://{addr}").parse().unwrap());
        req.headers_mut().insert(
            "cookie",
            format!("videoroll_admin_device={cookie}").parse().unwrap(),
        );
        let (mut ws, _) = connect_async(req).await.unwrap();
        async fn recv_where<S>(
            ws: &mut tokio_tungstenite::WebSocketStream<S>,
            pred: impl Fn(&Value) -> bool,
        ) -> Value
        where
            S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
        {
            tokio::time::timeout(Duration::from_secs(6), async {
                loop {
                    let message = ws.next().await.unwrap().unwrap();
                    if let Wire::Text(raw) = message {
                        let value: Value = serde_json::from_str(&raw).unwrap();
                        if pred(&value) {
                            return value;
                        }
                    }
                }
            })
            .await
            .expect("expected WebSocket event")
        }
        recv_where(&mut ws, |v| v["type"] == "ready").await;
        let (old, new, rolled, deleted) = (
            Uuid::new_v4(),
            Uuid::new_v4(),
            Uuid::new_v4(),
            Uuid::new_v4(),
        );
        ws.send(Wire::Text(json!({"op":"set_subscriptions","topics":[format!("task:{old}"),format!("task:{new}"),format!("task:{rolled}"),format!("task:{deleted}")]}).to_string().into())).await.unwrap();
        recv_where(&mut ws, |v| v["type"] == "subscribed").await;
        recv_where(&mut ws, |v| v["type"] == "resync_required").await;
        let mut slow = db.pool.begin().await.unwrap();
        let insert = "INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES($1,'local','own','created','rust',0,0)";
        sqlx::query(insert)
            .bind(old)
            .execute(&mut *slow)
            .await
            .unwrap();
        sqlx::query(insert)
            .bind(new)
            .execute(&db.pool)
            .await
            .unwrap();
        let event = recv_where(&mut ws, |v| v["type"] == "resync_required").await;
        assert!(
            event["topics"]
                .as_array()
                .unwrap()
                .contains(&json!(format!("task:{new}")))
        );
        assert!(
            !event["topics"]
                .as_array()
                .unwrap()
                .contains(&json!(format!("task:{old}")))
        );
        slow.commit().await.unwrap();
        let event = recv_where(&mut ws, |v| v["type"] == "resync_required").await;
        assert_eq!(event["topics"], json!([format!("task:{old}")]));
        let mut rollback = db.pool.begin().await.unwrap();
        sqlx::query(insert)
            .bind(rolled)
            .execute(&mut *rollback)
            .await
            .unwrap();
        rollback.rollback().await.unwrap();
        // Insert/delete an ordinary task with no workflow. A timestamp
        // ledger join would miss both changes.
        sqlx::query(insert)
            .bind(deleted)
            .execute(&db.pool)
            .await
            .unwrap();
        let event = recv_where(&mut ws, |v| v["type"] == "resync_required").await;
        assert_eq!(event["topics"], json!([format!("task:{deleted}")]));
        sqlx::query("DELETE FROM tasks WHERE id=$1")
            .bind(deleted)
            .execute(&db.pool)
            .await
            .unwrap();
        let event = recv_where(&mut ws, |v| v["name"] == "task.deleted").await;
        assert_eq!(event["data"]["id"], json!(deleted));
        recv_where(&mut ws, |v| v["type"] == "resync_required").await;
        // Force the dedicated LISTEN connection to restart. The replacement
        // subscribes before emitting the full resync.
        sqlx::query("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND query LIKE 'LISTEN %'").execute(&db.pool).await.unwrap();
        let event = recv_where(&mut ws, |v| v["type"] == "resync_required").await;
        assert_eq!(event["topics"].as_array().unwrap().len(), 4);
        sqlx::query("UPDATE tasks SET priority=99 WHERE id=$1")
            .bind(old)
            .execute(&db.pool)
            .await
            .unwrap();
        assert_eq!(
            recv_where(&mut ws, |v| v["type"] == "resync_required").await["topics"],
            json!([format!("task:{old}")])
        );
        // Seed one of each current-stage bucket, plus stale workflow stage,
        // canceled and Python-owned rows. All counts come from real PG.
        async fn seed(
            db: &Database,
            stage: &str,
            job_stage: &str,
            state: &str,
            attempts: i32,
            max: i32,
            lease_seconds: i64,
            delay: i64,
            owner: &str,
            status: &str,
        ) {
            let task = Uuid::new_v4();
            let flow = Uuid::new_v4();
            let job = Uuid::new_v4();
            sqlx::query("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES($1,'local','own',$2::task_status,$3,0,0)").bind(task).bind(status).bind(owner).execute(&db.pool).await.unwrap();
            sqlx::query("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES($1,$2,1,'running',$3)").bind(flow).bind(task).bind(stage).execute(&db.pool).await.unwrap();
            sqlx::query("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,attempts,max_attempts,lease_token,lease_until,available_at,payload) VALUES($1,$2,$3,$4,1,$5,$6,$7,$8,$9,now()+($10*interval '1 second'),now()+($11*interval '1 second'),'{}')").bind(job).bind(flow).bind(task).bind(job_stage).bind(format!("fixture:{job}")).bind(state).bind(attempts).bind(max).bind(Uuid::new_v4()).bind(lease_seconds).bind(delay).execute(&db.pool).await.unwrap();
        }
        for (stage, job_stage, status, a, m, lease, delay, owner, task_status) in [
            (
                "download", "download", "pending", 0, 5, 0, 0, "rust", "created",
            ),
            (
                "download", "download", "running", 1, 5, -60, 0, "rust", "created",
            ),
            (
                "download", "download", "running", 5, 5, -60, 0, "rust", "created",
            ),
            (
                "download", "download", "running", 5, 5, 300, 0, "rust", "created",
            ),
            (
                "download", "download", "pending", 1, 5, 0, 300, "rust", "created",
            ),
            (
                "render",
                "render",
                "awaiting",
                1,
                5,
                0,
                0,
                "rust",
                "published",
            ),
            (
                "publish", "publish", "running", 1, 1, -60, 0, "rust", "rendered",
            ),
            (
                "subtitle", "download", "pending", 0, 5, 0, 0, "rust", "created",
            ),
            (
                "download", "download", "pending", 0, 5, 0, 0, "rust", "canceled",
            ),
            (
                "download", "download", "pending", 0, 5, 0, 0, "python", "created",
            ),
        ] {
            seed(
                &db,
                stage,
                job_stage,
                status,
                a,
                m,
                lease,
                delay,
                owner,
                task_status,
            )
            .await;
        }
        let broker = crate::subtitle_queue_v1::broker_snapshot(&state)
            .await
            .unwrap();
        let by = |name: &str| {
            broker["queues"]
                .as_array()
                .unwrap()
                .iter()
                .find(|v| v["name"] == name)
                .unwrap()
        };
        let dl = by("v2:download");
        assert_eq!(dl["ready"], 2);
        assert_eq!(dl["active"], 1);
        assert_eq!(dl["exhausted"], 1);
        assert_eq!(dl["delayed"], 1);
        assert_eq!(dl["ready_retry_attempts"], 1);
        assert_eq!(by("v2:render")["awaiting_external"], 1);
        assert_eq!(by("v2:publish")["unknown_receipt"], 1);
        let settings = crate::subtitle_queue_v1::settings_snapshot(&state)
            .await
            .unwrap();
        assert_eq!(settings["scheduler"], "rust-v2");
        assert_eq!(settings["active_downloads"], 1);
        assert_eq!(settings["pending_downloads"], 3);
        ws.send(Wire::Text(
            json!({"op":"set_subscriptions","topics":["queue"]})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
        recv_where(&mut ws, |v| v["type"] == "subscribed").await;
        let pushed = recv_where(&mut ws, |v| v["name"] == "queue.broker.snapshot").await;
        assert_eq!(pushed["data"]["scheduler"], "rust-v2");
        server.abort();
        listener.abort();
        println!(
            "PASS PostgreSQL commit order, rollback, CRUD/delete, LISTEN reconnect, targeted WS resync, queue eligibility and server-pushed snapshot"
        );
    }
    #[test]
    fn strict_ws_topics_and_bounds() {
        assert!(valid_topic("tasks"));
        assert!(valid_topic("task:00000000-0000-0000-0000-000000000000"));
        assert!(!valid_topic("task:bad"));
        assert!(!valid_topic("all"));
        assert!(parse_topics(&json!(["tasks", "resources"])).is_ok());
        assert!(parse_topics(&json!(["tasks", 5])).is_err());
        assert!(parse_topics(&json!(vec!["tasks"; 33])).is_err());
    }
    #[test]
    fn origin_has_to_match_requested_host() {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "site.example:443".parse().unwrap());
        headers.insert(header::ORIGIN, "https://site.example:443".parse().unwrap());
        assert!(allowed_origin(&headers));
        headers.insert(header::ORIGIN, "https://attacker.example".parse().unwrap());
        assert!(!allowed_origin(&headers));
        headers.remove(header::ORIGIN);
        assert!(!allowed_origin(&headers));
    }
}
