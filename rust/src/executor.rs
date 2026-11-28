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
                    &self.config,
                )
                .await?;
                Ok(StageOutcome::Completed { result })
            }
            Stage::Subtitle => {
                crate::social_publish::explicit_input(&self.db,&self.registry,item.task_id,item.workflow_id).await?;
                // Selective retranslation uses preserved aligned source cues
                // and must execute natively even when the configured ASR
                // engine itself is delegated to an external worker.
                let selective: bool = sqlx::query_scalar(
                    "SELECT configuration ? 'selective_retranslate' FROM vr2_workflows WHERE id=$1",
                )
                .bind(item.workflow_id)
                .fetch_one(&self.db.pool)
                .await?;
                let resume:bool=sqlx::query_scalar("SELECT configuration ? 'subtitle_resume_plan' FROM vr2_workflows WHERE id=$1").bind(item.workflow_id).fetch_one(&self.db.pool).await?;
                if selective || resume {
                    let result =
                        crate::media::asr::process(&self.config, &self.db, &self.registry, item,None)
                            .await?;
                    Ok(StageOutcome::Completed { result })
                } else if let Some(captions)=crate::media::asr::preferred_source(&self.config,&self.db,&self.registry,item).await?{
                    let result=crate::media::asr::process(&self.config,&self.db,&self.registry,item,Some(captions)).await?;
                    Ok(StageOutcome::Completed{result})
                } else if let Some(runtime) =
                    crate::media::asr::runtime_asr_config(&self.config, &self.db,item).await?
                {
                    let result =
                        crate::media::asr::process(&runtime, &self.db, &self.registry, item,None)
                            .await?;
                    Ok(StageOutcome::Completed { result })
                } else {
                    self.remote(item).await
                }
            }
            Stage::Render if self.config.remote_renderer_enabled => {
                let profile=crate::media::ffmpeg::profile(&self.db,item).await?;
                crate::media::ffmpeg::prepare_render_ass(&self.config,&self.db,&self.registry,item,&profile).await?;
                let id = crate::remote_render::enqueue(&self.db,item,&profile).await?;
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
            Stage::Dubbing if !crate::media::dubbing::auto_enabled(&self.db,item).await? => {
                Ok(StageOutcome::Skipped{reason:"automatic P2 dubbing is disabled".into()})
            }
            Stage::Dubbing if sqlx::query_scalar::<_,bool>("SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE workflow_id=$1 AND stage='subtitle' AND state='succeeded' AND result->'no_speech'='true'::jsonb)")
                .bind(item.workflow_id).fetch_one(&self.db.pool).await? => {
                Ok(StageOutcome::Skipped{reason:"no credible speech: P2 was not generated".into()})
            }
            Stage::Dubbing
                if self.config.youdub_bridge.is_some() && self.config.youdub_python.is_some() =>
            {
                let result =
                    crate::media::dubbing::process(&self.config, &self.db, &self.registry, item)
                        .await?;
                if let Some(ticket)=result.get("remote_render_ticket").and_then(serde_json::Value::as_str) {
                    Ok(StageOutcome::Awaiting {ticket:ticket.to_owned()})
                } else if let Some(reason) = result.get("skipped").and_then(serde_json::Value::as_str) {
                    Ok(StageOutcome::Skipped {
                        reason: reason.to_string(),
                    })
                } else {
                    Ok(StageOutcome::Completed { result })
                }
            }
            Stage::Publish => {
                let explicit:Option<serde_json::Value>=sqlx::query_scalar("SELECT configuration->'after_render_publish' FROM vr2_workflows WHERE id=$1").bind(item.workflow_id).fetch_one(&self.db.pool).await?;
                if let Some(payload)=explicit{
                    let state=crate::api::AppState{config:self.config.clone(),db:self.db.clone(),scheduler:self.scheduler.clone(),storage:self.registry.clone()};
                    return crate::social_publish::explicit_after_render(&state,item,payload).await;
                }
                let choice = crate::publish_legacy::read_selection(&self.db, item).await?;
                if !choice.enabled || choice.platforms.is_empty() {
                    return Ok(StageOutcome::Skipped {
                        reason: "automatic publishing has no enabled platform targets".into(),
                    });
                }
                // A dynamically enabled automatic policy must never bypass
                // source authorization, even on the direct native Bili path.
                let authorized:bool=sqlx::query_scalar("SELECT source_license::text<>'unknown' AND runtime_owner='rust' AND status::text<>'canceled' AND stopped_status IS NULL FROM tasks WHERE id=$1")
                    .bind(item.task_id).fetch_one(&self.db.pool).await?;
                if !authorized{return Err(AppError::Conflict("automatic publication requires authorized unstopped source".into()))}
                if choice.platforms.iter().any(|p|p!="bilibili") && crate::social_publish::mixed_enabled(){
                    let state=crate::api::AppState{config:self.config.clone(),db:self.db.clone(),scheduler:self.scheduler.clone(),storage:self.registry.clone()};
                    return crate::social_publish::automatic(&state,item,choice.platforms).await;
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
                // Batch cleanup is independently due after ALL current targets
                // settle. This stage must not fail an already confirmed Bilibili
                // publication merely because another target is still running.
                let batched: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM vr2_workflows w JOIN publish_jobs j
                    ON j.id::text=w.configuration->'bilibili_direct_v1'->>'publish_job_id'
                    WHERE w.id=$1 AND j.task_id=$2 AND j.batch_id IS NOT NULL)",
                )
                .bind(item.workflow_id)
                .bind(item.task_id)
                .fetch_one(&self.db.pool)
                .await?;
                if batched {
                    return Ok(StageOutcome::Skipped{reason:"batch cleanup handled by durable publication events and retention policy".into()});
                }

                let published: Option<Value> = sqlx::query_scalar(
                    "SELECT result FROM vr2_jobs WHERE workflow_id=$1 AND stage='publish' AND state='succeeded' LIMIT 1"
                ).bind(item.workflow_id).fetch_optional(&self.db.pool).await?.flatten();
                if published
                    .as_ref()
                    .is_some_and(crate::publish::has_confirmed_result)
                {
                    // Python's published asset TTL defaults to 0 (disabled).
                    // The V2 task must NOT auto-delete a successfully posted
                    // video when the owner has not opted into retention.
                    if let Some(retention) =
                        crate::operations_v1::published_retention(&self.db).await?
                    {
                        let count = self
                            .registry
                            .schedule_terminal_release(item.task_id, item.workflow_id, retention)
                            .await?;
                        Ok(StageOutcome::Completed {
                            result: json!({
                                "retention":"scheduled","released_references":count,
                                "grace_seconds":retention.as_secs()
                            }),
                        })
                    } else {
                        Ok(StageOutcome::Skipped {
                            reason: "published asset retention disabled (asset_ttl_days=0)".into(),
                        })
                    }
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
        self.worker_loop_inner(worker_index, None).await;
    }
    /// Execute the original synchronous manual-download operation with the same
    /// durable claim and heartbeat as the automatic worker. No new HTTP route.
    pub(crate) async fn manual_download(&self, task: uuid::Uuid, workflow: uuid::Uuid) -> AppResult<()> {
        let f = self.scheduler.workflow(workflow).await?;
        if f.task_id != task || f.configuration["manual_download_only"].as_bool() != Some(true) || f.current_stage != "download" {
            return Err(AppError::Conflict("not a manual download workflow".into()));
        }
        self.worker_loop_inner(0, Some((task, workflow))).await;
        let f = self.scheduler.workflow(workflow).await?;
        if f.state == "succeeded" { Ok(()) } else {
            Err(AppError::Unavailable(f.last_error.unwrap_or_else(|| "YouTube download did not complete".into())))
        }
    }
    /// A single explicitly identified workflow, without global maintenance or publication.
    pub async fn trial_worker(&self, task: uuid::Uuid, workflow: uuid::Uuid) -> AppResult<()> {
        let f = self.scheduler.workflow(workflow).await?;
        if f.task_id != task || f.configuration["auto_publish"].as_bool() != Some(false)
            || f.configuration["auto_dub_p2"].as_bool() != Some(false)
            || f.configuration.get("after_render_publish").is_some()
            || f.configuration.get("bilibili_direct_v1").is_some() {
            return Err(AppError::Conflict("trial requires explicit media-only workflow configuration".into()));
        }
        self.worker_loop_inner(0, Some((task, workflow))).await;
        let f = self.scheduler.workflow(workflow).await?;
        if f.state == "succeeded" { Ok(()) } else {
            Err(AppError::Conflict(format!("trial ended in {}: {:?}",f.state,f.last_error)))
        }
    }
    async fn worker_loop_inner(&self, worker_index: usize, scope: Option<(uuid::Uuid,uuid::Uuid)>) {
        loop {
            if let Some((task,workflow))=scope {
                match self.scheduler.workflow(workflow).await {
                    Ok(f) if f.task_id==task && f.state=="running" => {},
                    _ => return,
                }
            } else {
                if let Err(e) = self.scheduler.quarantine_expired_publish().await {
                    warn!(worker_index,error=%e,"expired publish audit unavailable");
                }
                if let Err(e) = self.scheduler.reconcile_exhausted_jobs().await {
                    warn!(worker_index,error=%e,"expired stage recovery unavailable");
                }
            }
            match self.scheduler.claim_scoped(scope).await {
                Ok(Some(job)) => {
                    info!(worker_index,job_id=%job.id,stage=%job.stage.as_str(),"stage started");
                    let mut future = Box::pin(async {
                        if scope.is_some() && matches!(job.stage, Stage::Dubbing | Stage::Publish | Stage::Cleanup) {
                            Ok(StageOutcome::Skipped { reason: "media-only trial: retain output, no external submission".into() })
                        } else { self.execute(&job).await }
                    });
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
                    // Release process/HTTP guards before recording terminal state.
                    drop(future);
                    if job.stage == Stage::Subtitle {
                        crate::media::translation_trace::interrupt(&self.db, &job).await;
                    }
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
