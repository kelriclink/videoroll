//! Authenticated WebSocket transport compatible with the original React
//! realtime protocol. Events are read exclusively from the V2 event ledger;
//! no legacy Redis subscriber or guessed V1 task ownership is involved.
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
use chrono::{DateTime, Utc};
use futures_util::StreamExt;
use serde_json::{Value, json};
use sqlx::Row;
use std::collections::HashSet;
use tokio::time::{Duration, interval};
use uuid::Uuid;

const MAX_CLIENT_MESSAGE_BYTES: usize = 8 * 1024;
const MAX_TOPICS: usize = 32;
const QUERY_BATCH: i64 = 256;

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
    socket.send(Message::Text(text.into())).await.is_ok()
}

#[derive(Clone, Copy)]
struct Cursor {
    timestamp: DateTime<Utc>,
    id: Uuid,
}
async fn event_batch(state: &AppState, cursor: Cursor) -> AppResult<Vec<sqlx::postgres::PgRow>> {
    // The timestamp + UUID key is a stable compound cursor even for events
    // committed at the same time. New sessions start with a short look-back.
    let rows = sqlx::query(
        "SELECT e.id AS event_id,e.job_id,e.kind,e.details,e.created_at,e.workflow_id,w.task_id,
                jsonb_build_object('id',t.id,'status',upper(t.status::text),'source_type',t.source_type::text,
                'source_url',t.source_url,'source_license',t.source_license::text,
                'source_proof_url',t.source_proof_url,'stopped_status',upper(t.stopped_status::text),
                'created_by',t.created_by,'retry_count',t.retry_count,'bilibili_upload',NULL,
                'priority',t.priority,
                'display_title',COALESCE(
                  NULLIF(trim(title.value_json->>'translated_title'),''),
                  NULLIF(trim(title.value_json->>'source_title'),'')
                ),
                'error_code',t.error_code,'error_message',t.error_message,
                'created_at',t.created_at,'updated_at',t.updated_at) AS task_data
         FROM workflow_events e
         JOIN vr2_workflows w ON w.id=e.workflow_id
         JOIN tasks m ON m.id=w.task_id AND m.runtime_owner='rust'
         JOIN tasks t ON t.id=w.task_id
         LEFT JOIN app_settings title ON title.key=('task.title.'||t.id::text)
         WHERE (e.created_at,e.id)>($1,$2)
         ORDER BY e.created_at,e.id LIMIT $3",
    )
    .bind(cursor.timestamp)
    .bind(cursor.id)
    .bind(QUERY_BATCH + 1)
    .fetch_all(&state.db.pool)
    .await?;
    Ok(rows)
}
fn event_topics(task: Uuid, kind: &str) -> Vec<String> {
    let mut topics = vec!["tasks".into(), "queue".into(), format!("task:{task}")];
    if kind.contains("publish") || kind.contains("bilibili") {
        topics.push("publishing".into());
    }
    topics
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
    let mut cursor = Cursor {
        timestamp: Utc::now() - chrono::Duration::seconds(2),
        id: Uuid::nil(),
    };
    let mut event_poll = interval(Duration::from_secs(1));
    let mut heartbeat = interval(Duration::from_secs(20));
    let mut resource_poll = interval(Duration::from_secs(3));
    heartbeat.tick().await;
    resource_poll.tick().await;
    let mut failure_notified = false;
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
                                        let mut sorted=updated.iter().cloned().collect::<Vec<_>>();
                                        sorted.sort();
                                        topics=updated;
                                        if !send(socket,json!({"type":"subscribed","topics":sorted})).await{break;}
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
                if let Ok(data)=crate::system_resources::sample().await
                    && !send(socket,json!({
                        "v":1,"type":"event","event_id":Uuid::new_v4(),
                        "topics":["resources"],"name":"system.resources.sample","data":data,
                    })).await {break;}
            }
            _=event_poll.tick(), if !topics.is_empty()=>{
                match event_batch(&state,cursor).await{
                    Ok(rows)=>{
                        failure_notified=false;
                        if rows.len()>QUERY_BATCH as usize{
                            if let Some(row)=rows.last(){
                                cursor=Cursor{timestamp:row.get("created_at"),id:row.get("event_id")};
                            }
                            if !send(socket,json!({"type":"resync_required","reason":"event_backlog"})).await{break;}
                            continue;
                        }
                        for row in rows{
                            let id:Uuid=row.get("event_id");
                            let task:Uuid=row.get("task_id");
                            let timestamp:DateTime<Utc>=row.get("created_at");
                            cursor=Cursor{timestamp,id};
                            let kind:String=row.get("kind");
                            let all_topics=event_topics(task,&kind);
                            if !all_topics.iter().any(|topic|topics.contains(topic)){continue;}
                            let data:Value=row.get("task_data");
                            if !send(socket,json!({
                                "v":1,"type":"event","event_id":id,
                                "entity_id":task,"occurred_at":timestamp,
                                "topics":all_topics,"name":"task.updated","data":data
                            })).await{break;}
                        }
                    },
                    Err(error)=>{
                        tracing::warn!(%error,"native realtime event polling failed");
                        if !failure_notified {
                            failure_notified=true;
                            if !send(socket,json!({"type":"resync_required","reason":"event_source_unavailable"})).await{break;}
                        }
                    }
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
