//! Executor dispatches expensive media tasks to explicit worker gateways.
//! A 202 response is an *accepted handoff*, never a successfully processed stage.
use crate::{
    AppError, AppResult, Config, Database,
    domain::{Stage, StageOutcome, WorkItem},
    ingest::download_youtube,
    scheduler::Scheduler,
    storage::AssetRegistry,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::time::{interval, sleep};
use tracing::{error, info, warn};

#[derive(Clone)]
pub struct Executor {
    db: Database,
    registry: AssetRegistry,
    scheduler: Scheduler,
    config: Arc<Config>,
    http: reqwest::Client,
}
#[derive(Debug, Serialize)]
struct GatewayRequest<'a> {
    job_id: uuid::Uuid,
    workflow_id: uuid::Uuid,
    task_id: uuid::Uuid,
    operation_key: &'a str,
    payload: &'a Value,
}
#[derive(Debug, Deserialize)]
struct GatewayResponse {
    status: String,
    result: Option<Value>,
    ticket: Option<String>,
    detail: Option<String>,
}
impl Executor {
    pub fn new(
        config: Arc<Config>,
        db: Database,
        scheduler: Scheduler,
        registry: AssetRegistry,
    ) -> Self {
        Self {
            config,
            db,
            scheduler,
            registry,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(240))
                .build()
                .expect("reqwest client"),
        }
    }
    async fn remote(&self, item: &WorkItem) -> AppResult<StageOutcome> {
        let base = self.config.worker_gateway.as_ref().ok_or_else(|| {
            AppError::Unavailable(format!(
                "no worker gateway configured for {}",
                item.stage.as_str()
            ))
        })?;
        if !base.starts_with("http://127.0.0.1:")
            && !base.starts_with("http://[::1]:")
            && !base.starts_with("https://")
        {
            return Err(AppError::Invalid(
                "gateway must be loopback HTTP or authenticated HTTPS".into(),
            ));
        }
        let url = format!(
            "{}/v2/stages/{}",
            base.trim_end_matches('/'),
            item.stage.as_str()
        );
        let response = self
            .http
            .post(url)
            .header("x-videoroll-v2-internal-token", &self.config.internal_token)
            .json(&GatewayRequest {
                job_id: item.id,
                workflow_id: item.workflow_id,
                task_id: item.task_id,
                operation_key: &item.operation_key,
                payload: &item.payload,
            })
            .send()
            .await?;
        if !response.status().is_success() {
            return Err(AppError::Unavailable(format!(
                "gateway HTTP {}",
                response.status()
            )));
        }
        let accepted = response.status() == reqwest::StatusCode::ACCEPTED;
        let payload: GatewayResponse = response.json().await?;
        match payload.status.as_str() {
            "completed" if !accepted => {
                let result = payload.result.unwrap_or(json!({}));
                if item.stage == Stage::Publish && !crate::publish::has_confirmed_result(&result) {
                    return Err(AppError::Unavailable(
                        "publisher reported completion without a confirmed platform receipt".into(),
                    ));
                }
                Ok(StageOutcome::Completed { result })
            }
            "skipped" if !accepted => Ok(StageOutcome::Skipped {
                reason: payload
                    .detail
                    .unwrap_or_else(|| "skipped by configuration".into()),
            }),
            "accepted" => Ok(StageOutcome::Awaiting {
                ticket: payload.ticket.filter(|v| !v.is_empty()).ok_or_else(|| {
                    AppError::Invalid("accepted gateway response requires durable ticket".into())
                })?,
            }),
            value => Err(AppError::Unavailable(format!(
                "gateway returned invalid status {value}"
            ))),
        }
    }
    pub async fn execute(&self, item: &WorkItem) -> AppResult<StageOutcome> {
        match item.stage {
            Stage::Download => {
                let result = download_youtube(
                    &self.db,
                    &self.registry,
                    &self.scheduler,
                    item,
                    &self.config.download_binary,
                )
                .await?;
                Ok(StageOutcome::Completed { result })
            }
            Stage::Subtitle if self.config.asr_endpoint.is_some() => {
                let result =
                    crate::media::asr::process(&self.config, &self.db, &self.registry, item)
                        .await?;
                Ok(StageOutcome::Completed { result })
            }
            Stage::Render if self.config.remote_renderer_enabled => {
                let id = crate::remote_render::enqueue(&self.db, &self.registry, item).await?;
                Ok(StageOutcome::Awaiting {
                    ticket: id.to_string(),
                })
            }
            Stage::Render => {
                let result =
                    crate::media::ffmpeg::render(&self.config, &self.db, &self.registry, item)
                        .await?;
                Ok(StageOutcome::Completed { result })
            }
            Stage::Dubbing
                if self.config.youdub_bridge.is_some() && self.config.youdub_python.is_some() =>
            {
                let result =
                    crate::media::dubbing::process(&self.config, &self.db, &self.registry, item)
                        .await?;
                if let Some(reason) = result.get("skipped").and_then(serde_json::Value::as_str) {
                    Ok(StageOutcome::Skipped {
                        reason: reason.to_string(),
                    })
                } else {
                    Ok(StageOutcome::Completed { result })
                }
            }
            Stage::Publish => {
                let choice = crate::publish_legacy::read_selection(&self.db, item).await?;
                if !choice.enabled || choice.platforms.is_empty() {
                    return Ok(StageOutcome::Skipped {
                        reason: "automatic publishing has no enabled platform targets".into(),
                    });
                }
                if self.config.native_bilibili_allowed {
                    let result =
                        crate::bilibili::publish(&self.config, &self.db, &self.registry, item)
                            .await?;
                    Ok(StageOutcome::Completed { result })
                } else if self.config.legacy_publish_allowed {
                    let result =
                        crate::publish_legacy::publish(&self.config, &self.db, item).await?;
                    if let Some(reason) = result.get("skipped").and_then(serde_json::Value::as_str)
                    {
                        Ok(StageOutcome::Skipped {
                            reason: reason.to_string(),
                        })
                    } else {
                        Ok(StageOutcome::Completed { result })
                    }
                } else {
                    self.remote(item).await
                }
            }
            Stage::Cleanup => {
                let published: Option<Value> = sqlx::query_scalar(
                    "SELECT result FROM vr2_jobs WHERE workflow_id=$1 AND stage='publish' AND state='succeeded' LIMIT 1"
                ).bind(item.workflow_id).fetch_optional(&self.db.pool).await?.flatten();
                if published
                    .as_ref()
                    .is_some_and(crate::publish::has_confirmed_result)
                {
                    let count = self
                        .registry
                        .schedule_terminal_release(
                            item.task_id,
                            item.workflow_id,
                            self.config.cleanup_grace,
                        )
                        .await?;
                    Ok(StageOutcome::Completed {
                        result: json!({
                            "retention":"scheduled","released_references":count,
                            "grace_seconds":self.config.cleanup_grace.as_secs()
                        }),
                    })
                } else {
                    Ok(StageOutcome::Skipped {
                        reason:
                            "no verified completed publishing batch; retain the video for review"
                                .into(),
                    })
                }
            }
            _ => self.remote(item).await,
        }
    }
    pub async fn worker_loop(&self, worker_index: usize) {
        loop {
            if let Err(e) = self.scheduler.quarantine_expired_publish().await {
                warn!(worker_index,error=%e,"expired publish audit unavailable");
            }
            match self.scheduler.claim().await {
                Ok(Some(job)) => {
                    info!(worker_index,job_id=%job.id,stage=%job.stage.as_str(),"stage started");
                    let mut future = Box::pin(self.execute(&job));
                    let mut tick = interval(Duration::from_secs(30));
                    let outcome = loop {
                        tokio::select! {
                            result=&mut future=>break result,
                            _=tick.tick()=>{
                                match self.scheduler.heartbeat(&job).await {
                                    Ok(true)=>{},
                                    Ok(false)=>{
                                        break Err(AppError::Conflict("worker lease lost during stage".into()));
                                    }
                                    Err(e)=>{
                                        warn!(error=%e,job_id=%job.id,"stage lease heartbeat failed");
                                    }
                                }
                            }
                        }
                    };
                    let commit = match outcome {
                        Ok(result) => self.scheduler.complete(&job, result).await,
                        Err(err) => {
                            error!(job_id=%job.id,error=%err,"stage execution failed");
                            self.scheduler.fail(&job, &err.to_string()).await
                        }
                    };
                    if let Err(e) = commit {
                        warn!(job_id=%job.id,error=%e,"failed to settle stage; recovery will retry or quarantine");
                    }
                }
                Ok(None) => sleep(self.config.worker_poll).await,
                Err(err) => {
                    error!(worker_index,error=%err,"claim failed");
                    sleep(Duration::from_secs(5)).await;
                }
            }
        }
    }
}
