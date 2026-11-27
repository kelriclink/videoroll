//! The existing agent history is the bounded live view of external model
//! reasoning. Only explicit reasoning deltas, never prompts or final replies.
use crate::{AppError, AppResult, Database, domain::WorkItem};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use std::time::{Duration, Instant};
use uuid::Uuid;

tokio::task_local! { pub(crate) static SUBTITLE_JOB: WorkItem; }
const LIMIT: usize = 16_000;
pub(crate) struct Trace {
    db: Database,
    job: WorkItem,
    id: Uuid,
    model: String,
    pending: String,
    seen: usize,
    saved: usize,
    last: Instant,
    truncated: bool,
    done: bool,
}
async fn fenced(db: &Database, job: &WorkItem) -> AppResult<Transaction<'static, Postgres>> {
    let mut tx = db.pool.begin().await?;
    let held:Option<Uuid>=sqlx::query_scalar("SELECT id FROM pipeline_runs WHERE id=$1 AND task_id=$2 AND engine='rust-v2' AND state='running' FOR UPDATE").bind(job.workflow_id).bind(job.task_id).fetch_optional(&mut *tx).await?;
    if held.is_none() {
        return Err(AppError::Conflict(
            "translation trace workflow expired".into(),
        ));
    }
    let held:Option<Uuid>=sqlx::query_scalar("SELECT id FROM operation_inbox WHERE id=$1 AND workflow_id=$2 AND task_id=$3 AND runtime_engine='rust-v2' AND stage='subtitle' AND status='running' AND lease_token=$4 AND lease_until>clock_timestamp() FOR UPDATE").bind(job.id).bind(job.workflow_id).bind(job.task_id).bind(job.lease_token).fetch_optional(&mut *tx).await?;
    if held.is_none() {
        return Err(AppError::Conflict("translation trace lease expired".into()));
    }
    Ok(tx)
}
impl Trace {
    pub(crate) async fn start(db: &Database, model: &str, operation: &str) -> Option<Self> {
        let job = SUBTITLE_JOB.try_with(Clone::clone).ok()?;
        let trace = Self {
            db: db.clone(),
            job,
            id: Uuid::new_v4(),
            model: model.chars().take(255).collect(),
            pending: String::new(),
            seen: 0,
            saved: 0,
            last: Instant::now() - Duration::from_secs(2),
            truncated: false,
            done: false,
        };
        let result=async {
            let mut tx=fenced(db,&trace.job).await?;
            // A previous attempt may have been interrupted by worker death.
            sqlx::query("UPDATE translation_agent_runs SET status='interrupted',finished_at=now(),updated_at=now(),error='worker interrupted' WHERE agent_type='subtitle_translation_thinking' AND status='running' AND checkpoint->>'native_job_id'=$1")
                .bind(trace.job.id.to_string()).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO translation_agent_runs(id,agent_type,status,task_id,term,domain,query,checkpoint,lease_owner) VALUES($1,'subtitle_translation_thinking','running',$2,'字幕翻译 Think','subtitle_translation',$3,$4,$5)")
                .bind(trace.id).bind(trace.job.task_id).bind(&trace.model).bind(json!({"native_job_id":trace.job.id,"native_lease":trace.job.lease_token,"operation":operation})).bind(trace.job.lease_token.to_string()).execute(&mut *tx).await?;
            trace.event(&mut tx,"agent_run.started","translation_thinking.started","running",json!({})).await?;
            tx.commit().await?;
            Ok::<(),AppError>(())
        }.await;
        result.ok().map(|_| trace)
    }
    async fn event(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        name: &str,
        action: &str,
        status: &str,
        output: Value,
    ) -> AppResult<()> {
        let payload = json!({"kind":"llm","action":action,"status":status,"model":self.model,"output":output,"metadata":{"thinking":true,"stream":true,"truncated":self.truncated}});
        sqlx::query("INSERT INTO translation_agent_events(id,run_id,kind,action,status,event) VALUES($1,$2,'llm',$3,$4,$5)").bind(Uuid::new_v4()).bind(self.id).bind(action).bind(status).bind(payload).execute(&mut **tx).await?;
        crate::scheduler::Scheduler::event(
            tx,
            self.job.workflow_id,
            Some(self.job.id),
            name,
            json!({"run_id":self.id,"status":status}),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn consume(&mut self, line: &[u8]) {
        let Ok(line) = std::str::from_utf8(line) else {
            return;
        };
        let Some(data) = line.trim().strip_prefix("data:") else {
            return;
        };
        if self.done {
            return;
        }
        if data.trim() == "[DONE]" {
            self.done = true;
            return;
        }
        let Ok(event) = serde_json::from_str::<Value>(data.trim()) else {
            return;
        };
        if !event["error"].is_null() {
            return;
        }
        let Some(delta) = event
            .pointer("/choices/0/delta")
            .or_else(|| event.pointer("/choices/0/message"))
        else {
            return;
        };
        let Some(text) = delta["reasoning_content"]
            .as_str()
            .or_else(|| delta["reasoning"].as_str())
        else {
            return;
        };
        self.seen = self.seen.saturating_add(text.chars().count());
        let remaining = LIMIT.saturating_sub(self.saved + self.pending.chars().count());
        self.pending.extend(text.chars().take(remaining));
        self.truncated = self.seen > LIMIT;
        if !self.pending.is_empty()
            && (self.last.elapsed() >= Duration::from_secs(1) || self.pending.len() >= 4096)
        {
            self.flush().await;
        }
    }
    async fn flush(&mut self) {
        if self.pending.is_empty() {
            return;
        }
        let result=async{
            let mut tx=fenced(&self.db,&self.job).await?;
            let n=sqlx::query("UPDATE translation_agent_runs SET updated_at=now() WHERE id=$1 AND status='running' AND lease_owner=$2").bind(self.id).bind(self.job.lease_token.to_string()).execute(&mut *tx).await?.rows_affected();
            if n!=1{return Err(AppError::Conflict("translation trace is no longer active".into()));}
            self.event(&mut tx,"agent_run.step_appended","translation_thinking.delta","running",json!({"thinking_delta":self.pending})).await?;
            tx.commit().await?;
            Ok::<(),AppError>(())
        }.await;
        // Fail-soft; never block translation because of telemetry failure.
        if result.is_ok() {
            self.saved += self.pending.chars().count();
        }
        self.pending.clear();
        self.last = Instant::now();
    }
    pub(crate) async fn finish(mut self, success: bool) {
        self.flush().await;
        let _=async{
            let mut tx=fenced(&self.db,&self.job).await?;
            let status=if success{"succeeded"}else{"failed"};
            let n=sqlx::query("UPDATE translation_agent_runs SET status=$2,result=$3,error=$4,finished_at=now(),updated_at=now() WHERE id=$1 AND status='running' AND lease_owner=$5")
                .bind(self.id).bind(status).bind(json!({"thinking":true,"thought_characters":self.seen,"stored_characters":self.saved,"truncated":self.truncated})).bind(if success{""}else{"provider request failed"}).bind(self.job.lease_token.to_string()).execute(&mut *tx).await?.rows_affected();
            if n==1{self.event(&mut tx,"agent_run.finished","translation_thinking.finished",status,json!({})).await?;}
            tx.commit().await?;
            Ok::<(),AppError>(())
        }.await;
    }
}
/// On worker completion/cancellation, settle only its own stale trace rows.
/// A new worker's trace is fenced by its own lease token.
pub(crate) async fn interrupt(db: &Database, job: &WorkItem) {
    let _=sqlx::query("UPDATE translation_agent_runs SET status='interrupted',finished_at=now(),updated_at=now(),error='worker interrupted' WHERE agent_type='subtitle_translation_thinking' AND status='running' AND checkpoint->>'native_job_id'=$1 AND lease_owner=$2").bind(job.id.to_string()).bind(job.lease_token.to_string()).execute(&db.pool).await;
}

pub(crate) async fn reap(db: &Database) -> AppResult<u64> {
    let rows=sqlx::query("WITH stale AS (
        SELECT r.id FROM translation_agent_runs r WHERE r.status='running' AND r.agent_type='subtitle_translation_thinking'
          AND r.checkpoint ? 'native_job_id' AND r.updated_at<now()-interval '15 seconds'
          AND NOT EXISTS(SELECT 1 FROM operation_inbox j JOIN pipeline_runs w ON w.id=j.workflow_id
            WHERE j.id::text=r.checkpoint->>'native_job_id' AND j.runtime_engine='rust-v2' AND j.stage='subtitle'
              AND j.lease_token::text=r.checkpoint->>'native_lease' AND j.status='running' AND j.lease_until>now() AND w.state='running')
        ORDER BY r.updated_at LIMIT 100 FOR UPDATE OF r SKIP LOCKED)
        UPDATE translation_agent_runs r SET status='interrupted',error='worker interrupted',finished_at=now(),updated_at=now() FROM stale WHERE r.id=stale.id")
        .execute(&db.pool).await?.rows_affected();
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{domain::Stage, scheduler::Scheduler};
    use axum::{Router, body::Body, extract::State, response::Response, routing::post};
    use std::{convert::Infallible, sync::Arc};
    use tokio::sync::Notify;

    #[tokio::test]
    async fn live_thinking_real_postgres_http() {
        let Ok(url) = std::env::var("VIDEOROLL_TEST_LIVE_THINKING_DB_URL") else {
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
        let (task, flow, job_id) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        sqlx::query("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES($1,'local','own','downloaded','rust',0,0)").bind(task).execute(&db.pool).await.unwrap();
        sqlx::query("INSERT INTO pipeline_runs(id,task_id,generation,engine,workflow_name,state,current_stage,request_json) VALUES($1,$2,1,'rust-v2','videoroll.rust.pipeline','running','subtitle','{}')").bind(flow).bind(task).execute(&db.pool).await.unwrap();
        sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,workflow_id,stage,generation,status,request_json) VALUES($1,$2,'rust-v2',$3,$4,'subtitle',1,'pending','{}')").bind(job_id).bind(format!("trace:{job_id}")).bind(task).bind(flow).execute(&db.pool).await.unwrap();
        let scheduler = Scheduler::new(db.clone(), Duration::from_secs(300));
        let job = scheduler.claim().await.unwrap().unwrap();
        assert_eq!(job.id, job_id);
        assert_eq!(job.stage, Stage::Subtitle);
        async fn mock(State(gate): State<Arc<Notify>>) -> Response {
            let first = format!(
                "data: {}\n\n",
                json!({"choices":[{"delta":{"reasoning_content":"实时推理"}}]})
            );
            let bytes = first.into_bytes();
            let split = bytes.iter().position(|b| *b >= 128).unwrap() + 1;
            let stream = futures_util::stream::unfold(
                (0, gate, bytes, split),
                |(i, gate, bytes, split)| async move {
                    let part = match i {
                        0 => bytes[..split].to_vec(),
                        1 => bytes[split..].to_vec(),
                        2 => {
                            gate.notified().await;
                            format!("data: {}\n\ndata: {}\n\ndata: [DONE]\n\n",json!({"choices":[{"delta":{"reasoning_content":"x".repeat(20_000)}}]}),json!({"choices":[{"delta":{"content":"{\"items\":[]}"},"finish_reason":"stop"}],"usage":{"total_tokens":42}})).into_bytes()
                        }
                        _ => return None,
                    };
                    Some((Ok::<_, Infallible>(part), (i + 1, gate, bytes, split)))
                },
            );
            Response::builder()
                .header("content-type", "text/event-stream")
                .body(Body::from_stream(stream))
                .unwrap()
        }
        let gate = Arc::new(Notify::new());
        let app = Router::new()
            .route("/chat", post(mock))
            .with_state(gate.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint =
            reqwest::Url::parse(&format!("http://{}/chat", listener.local_addr().unwrap()))
                .unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let reader_db = db.clone();
        let reading = tokio::spawn(SUBTITLE_JOB.scope(job.clone(), async move {
            let request = reqwest::Client::new()
                .post(endpoint.clone())
                .json(&json!({"stream":true}));
            crate::ai_usage::chat_json_traced(
                &reader_db,
                request,
                &endpoint,
                "fixture",
                "subtitle_translate",
                Some(task),
                1024 * 1024,
                "fixture",
            )
            .await
            .unwrap()
        }));
        let mut trace_id = None;
        for _ in 0..100 {
            trace_id=sqlx::query_scalar::<_,Uuid>("SELECT r.id FROM translation_agent_runs r JOIN translation_agent_events e ON e.run_id=r.id WHERE r.task_id=$1 AND r.status='running' AND e.action='translation_thinking.delta' AND e.event#>>'{output,thinking_delta}'='实时推理'").bind(task).fetch_optional(&db.pool).await.unwrap();
            if trace_id.is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let trace_id = trace_id.expect("reasoning must be visible before final SSE chunk");
        assert!(!reading.is_finished());
        assert_eq!(sqlx::query_scalar::<_,i64>("SELECT count(*) FROM workflow_events WHERE workflow_id=$1 AND kind='agent_run.step_appended' AND details->>'run_id'=$2").bind(flow).bind(trace_id.to_string()).fetch_one(&db.pool).await.unwrap(),1);
        gate.notify_one();
        let value = tokio::time::timeout(Duration::from_secs(10), reading)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(value["choices"][0]["message"]["content"], "{\"items\":[]}");
        assert!(!value.to_string().contains("实时推理"));
        let result: Value = sqlx::query_scalar(
            "SELECT result FROM translation_agent_runs WHERE id=$1 AND status='succeeded'",
        )
        .bind(trace_id)
        .fetch_one(&db.pool)
        .await
        .unwrap();
        assert_eq!(result["stored_characters"], LIMIT);
        assert_eq!(result["truncated"], true);
        assert_eq!(sqlx::query_scalar::<_,i64>("SELECT sum(length(event#>>'{output,thinking_delta}'))::bigint FROM translation_agent_events WHERE run_id=$1 AND action='translation_thinking.delta'").bind(trace_id).fetch_one(&db.pool).await.unwrap(),LIMIT as i64);
        let orphan = SUBTITLE_JOB
            .scope(
                job.clone(),
                Trace::start(&db, "fixture", "subtitle_translate"),
            )
            .await
            .unwrap();
        let orphan_id = orphan.id;
        drop(orphan);
        sqlx::query("UPDATE operation_inbox SET lease_until=now()-interval '1 minute' WHERE id=$1")
            .bind(job.id)
            .execute(&db.pool)
            .await
            .unwrap();
        sqlx::query(
            "UPDATE translation_agent_runs SET updated_at=now()-interval '1 minute' WHERE id=$1",
        )
        .bind(orphan_id)
        .execute(&db.pool)
        .await
        .unwrap();
        assert_eq!(reap(&db).await.unwrap(), 1);
        assert_eq!(
            sqlx::query_scalar::<_, String>(
                "SELECT status FROM translation_agent_runs WHERE id=$1"
            )
            .bind(orphan_id)
            .fetch_one(&db.pool)
            .await
            .unwrap(),
            "interrupted"
        );
        server.abort();
        println!(
            "PASS live SSE reasoning committed before final response, UTF8 split, bounded 16000 characters, workflow realtime event, final translation isolation, stale trace recovery"
        );
    }
}
