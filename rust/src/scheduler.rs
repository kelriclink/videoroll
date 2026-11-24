use crate::{
    AppError, AppResult, Database,
    domain::{Stage, StageOutcome, TaskInfo, WorkItem, WorkflowInfo},
};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use std::time::Duration;
use uuid::Uuid;

// Duplicate operator clicks are idempotent ONLY when their actual effective
// settings match. A second click with different options must not claim that
// the original already-queued work adopted the new settings.
fn requested_settings_match(saved: &Value, changes: &serde_json::Map<String, Value>) -> bool {
    changes.iter().all(|(key, requested)| {
        if (key == "subtitle" || key == "render")
            && let Some(fields) = requested.as_object()
        {
            return fields.iter().all(|(name, value)| {
                saved.get(key).and_then(|parent| parent.get(name)) == Some(value)
            });
        }
        saved.get(key) == Some(requested)
    })
}

#[derive(Clone)]
pub struct Scheduler {
    db: Database,
    lease: Duration,
}
impl Scheduler {
    pub fn new(db: Database, lease: Duration) -> Self {
        Self { db, lease }
    }
    fn lease_seconds(&self) -> i64 {
        self.lease.as_secs().clamp(30, 3600) as i64
    }
    /// One transaction owns the source deduplication, Task, Workflow and first stage.
    /// The existing v1 ingested_videos unique index prevents duplicates across generations.
    pub async fn create_auto_youtube(
        &self,
        source_url: &str,
        source_license: &str,
        priority: i32,
        configuration: Value,
    ) -> AppResult<(TaskInfo, Option<WorkflowInfo>, bool)> {
        self.create_youtube_source_task(source_url, source_license, priority, configuration, true)
            .await
    }
    pub async fn create_youtube_source_task(
        &self,
        source_url: &str,
        source_license: &str,
        priority: i32,
        configuration: Value,
        auto_process: bool,
    ) -> AppResult<(TaskInfo, Option<WorkflowInfo>, bool)> {
        let video_id = crate::ingest::youtube_video_id(source_url)?;
        let license = match source_license {
            "own" | "authorized" | "cc" | "unknown" => source_license,
            _ => return Err(AppError::Invalid("unsupported source license".into())),
        };
        let mut tx = self.db.pool.begin().await?;
        // Serializes concurrent source intake with v2 peers; v1 coordination is
        // additionally guaranteed by ingested_videos(platform,source_id) UNIQUE.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))")
            .bind(format!("youtube:{video_id}"))
            .execute(&mut *tx)
            .await?;
        if let Some(task_id) = sqlx::query_scalar::<_, Uuid>(
            "SELECT task_id FROM ingested_videos WHERE platform='youtube' AND source_id=$1",
        )
        .bind(&video_id)
        .fetch_optional(&mut *tx)
        .await?
        {
            tx.commit().await?;
            return Ok((self.db.task(task_id).await?, None, true));
        }
        let task_id = Uuid::new_v4();
        let workflow_id = Uuid::new_v4();
        let run_id = Uuid::new_v4().simple().to_string();
        let home_scan = auto_process
            && configuration.get("origin").and_then(Value::as_str) == Some("youtube_home_scan");
        let origin = if home_scan {
            "youtube_home_scan"
        } else if auto_process {
            "auto_youtube"
        } else {
            "source_scan"
        };
        let created_by = if home_scan {
            format!("youtube_home_scan;auto_publish=;run_id={run_id}")
        } else {
            origin.to_string()
        };
        let canonical_url = format!("https://www.youtube.com/watch?v={video_id}");
        sqlx::query(
            "INSERT INTO tasks(id,source_type,source_url,source_license,status,priority,origin_kind,origin_run_id,restart_generation,created_by) VALUES($1,'youtube'::source_type,$2,$3::source_license,'created'::task_status,$4,$6,$5,0,$6)"
        ).bind(task_id).bind(canonical_url).bind(license).bind(priority.clamp(-100,100))
            .bind(run_id).bind(created_by)
            .execute(&mut *tx).await?;
        if home_scan {
            sqlx::query("UPDATE tasks SET origin_kind='youtube_home_scan' WHERE id=$1")
                .bind(task_id)
                .execute(&mut *tx)
                .await?;
        }
        let inserted=sqlx::query(
            "INSERT INTO ingested_videos(id,platform,source_id,task_id) VALUES($1,'youtube',$2,$3) ON CONFLICT(platform,source_id) DO NOTHING"
        ).bind(Uuid::new_v4()).bind(&video_id).bind(task_id).execute(&mut *tx).await?;
        if inserted.rows_affected() != 1 {
            // Concurrent v1 intake won. The new Task must not be committed.
            tx.rollback().await?;
            let winner: Uuid = sqlx::query_scalar(
                "SELECT task_id FROM ingested_videos WHERE platform='youtube' AND source_id=$1",
            )
            .bind(&video_id)
            .fetch_one(&self.db.pool)
            .await?;
            return Ok((self.db.task(winner).await?, None, true));
        }
        sqlx::query("UPDATE tasks SET runtime_owner='rust',runtime_origin=$2 WHERE id=$1")
            .bind(task_id)
            .bind(if auto_process {
                "youtube_auto"
            } else {
                "youtube_manual"
            })
            .execute(&mut *tx)
            .await?;
        if auto_process {
            sqlx::query(
            "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES($1,$2,1,'running','download',$3)"
        ).bind(workflow_id).bind(task_id).bind(configuration).execute(&mut *tx).await?;
            Self::insert_job(&mut tx, workflow_id, task_id, 1, Stage::Download).await?;
            Self::event(
                &mut tx,
                workflow_id,
                None,
                "workflow.started",
                json!({"generation":1,"video_id":video_id}),
            )
            .await?;
        }
        tx.commit().await?;
        Ok((
            self.db.task(task_id).await?,
            if auto_process {
                Some(self.workflow(workflow_id).await?)
            } else {
                None
            },
            false,
        ))
    }
    pub async fn start(&self, task_id: Uuid, configuration: Value) -> AppResult<WorkflowInfo> {
        let mut tx = self.db.pool.begin().await?;
        let task = sqlx::query(
            "SELECT upper(t.status::text) AS status FROM tasks t
             JOIN tasks m ON m.id=t.id AND m.runtime_owner='rust'
             WHERE t.id=$1 FOR UPDATE OF t",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("V2-managed task not found".into()))?;
        let status: String = task.try_get("status")?;
        if status == "CANCELED" || status == "PUBLISHED" || status == "PUBLISHING" {
            return Err(AppError::Conflict(format!(
                "task status {status} forbids a new workflow"
            )));
        }
        if let Some(row) = sqlx::query(
            "SELECT id, state FROM vr2_workflows WHERE task_id=$1 AND state IN ('running','paused','unknown') ORDER BY generation DESC LIMIT 1 FOR UPDATE"
        ).bind(task_id).fetch_optional(&mut *tx).await? {
            let id: Uuid = row.try_get("id")?;
            let state: String = row.try_get("state")?;
            if state == "unknown" {
                return Err(AppError::Conflict("previous pipeline contains an uncertain publish; reconcile it before restarting".into()));
            }
            tx.commit().await?;
            return self.workflow(id).await;
        }
        let generation: i32 = sqlx::query_scalar::<_, i32>(
            "SELECT COALESCE(MAX(generation),0)+1 FROM vr2_workflows WHERE task_id=$1",
        )
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;
        let workflow_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES($1,$2,$3,'running','download',$4)"
        ).bind(workflow_id).bind(task_id).bind(generation).bind(configuration)
            .execute(&mut *tx).await?;
        Self::insert_job(&mut tx, workflow_id, task_id, generation, Stage::Download).await?;
        Self::event(
            &mut tx,
            workflow_id,
            None,
            "workflow.started",
            json!({"generation": generation}),
        )
        .await?;
        tx.commit().await?;
        self.workflow(workflow_id).await
    }
    /// Restart a V2-owned task at an exact stage without reissuing earlier work.
    ///
    /// Every restart creates a new workflow generation and independent operation
    /// keys, except while an identical stage is already running. A pending,
    /// uncertain, or successful external publication is NEVER automatically
    /// repeated, including after a process or machine restart.
    pub async fn restart_at(
        &self,
        task_id: Uuid,
        stage: Stage,
        overrides: Value,
    ) -> AppResult<WorkflowInfo> {
        if stage == Stage::Cleanup {
            return Err(AppError::Invalid(
                "cleanup is not a restartable user stage".into(),
            ));
        }
        let changes = overrides
            .as_object()
            .ok_or_else(|| AppError::Invalid("workflow overrides must be a JSON object".into()))?;
        if serde_json::to_vec(&overrides)
            .map_err(|_| AppError::Invalid("invalid overrides".into()))?
            .len()
            > 64 * 1024
        {
            return Err(AppError::Invalid("workflow overrides exceed 64KB".into()));
        }
        let mut tx = self.db.pool.begin().await?;
        let status: Option<String> = sqlx::query_scalar(
            "SELECT upper(t.status::text) FROM tasks t
             JOIN tasks m ON m.id=t.id AND m.runtime_owner='rust'
             WHERE t.id=$1 FOR UPDATE OF t",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?;
        let status = status.ok_or_else(|| AppError::NotFound("V2-owned task not found".into()))?;
        if stage == Stage::Dubbing
            && let Some(key) = changes
                .get("manual_dub_operation_key")
                .and_then(Value::as_str)
        {
            // The parent task row lock serializes concurrent operator calls.
            // Repeating an operation key returns the original receipt even if
            // that workflow has since failed; retry requires a new generation.
            if let Some(existing) = sqlx::query(
                "SELECT id,configuration->'manual_dub_request' AS request
                 FROM vr2_workflows WHERE task_id=$1
                 AND configuration->>'manual_dub_operation_key'=$2
                 ORDER BY generation ASC LIMIT 1",
            )
            .bind(task_id)
            .bind(key)
            .fetch_optional(&mut *tx)
            .await?
            {
                let originally: Option<Value> = existing.try_get("request")?;
                if originally.as_ref() != changes.get("manual_dub_request") {
                    return Err(AppError::Conflict(
                        "YouDub operation key has been used for a different subtitle or raw video"
                            .into(),
                    ));
                }
                let flow_id: Uuid = existing.try_get("id")?;
                tx.commit().await?;
                return self.workflow(flow_id).await;
            }
        }
        if matches!(status.as_str(), "CANCELED" | "PUBLISHED" | "PUBLISHING") {
            return Err(AppError::Conflict(format!(
                "task status {status} forbids stage restart"
            )));
        }
        let unsafe_publish: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM vr2_jobs
             WHERE task_id=$1 AND (
               (stage='publish' AND (
                  state IN ('pending','running','awaiting','unknown')
                  OR (state='succeeded' AND COALESCE(result->>'skipped','')='')
               ))
               OR (stage='sau_publish' AND state IN
                 ('queued','dispatching','accepted','submitted','published','unknown'))
             ))",
        )
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;
        if unsafe_publish {
            return Err(AppError::Conflict(
                "a queued, successful or uncertain publication blocks restart; verify external outcome first".into()
            ));
        }
        let latest = sqlx::query(
            "SELECT id,state,current_stage,configuration
             FROM vr2_workflows WHERE task_id=$1
             ORDER BY generation DESC LIMIT 1 FOR UPDATE",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?;
        let mut configuration = json!({});
        if let Some(ref previous) = latest {
            let prev_state: String = previous.try_get("state")?;
            let prev_stage: String = previous.try_get("current_stage")?;
            let prev_id: Uuid = previous.try_get("id")?;
            if prev_state == "unknown" {
                return Err(AppError::Conflict(
                    "uncertain external workflow must be reconciled first".into(),
                ));
            }
            if prev_state == "running" && prev_stage == stage.as_str() {
                let current: Value = previous.try_get("configuration")?;
                if !requested_settings_match(&current, changes) {
                    return Err(AppError::Conflict(
                        "same stage already running with different settings; cannot mutate an active generation".into()
                    ));
                }
                tx.commit().await?;
                return self.workflow(prev_id).await;
            }
            if matches!(prev_state.as_str(), "running" | "paused") {
                return Err(AppError::Conflict(
                    "another workflow is active or paused; stop or resume it before restarting"
                        .into(),
                ));
            }
            configuration = previous.try_get("configuration")?;
        }
        if let (Some(base), Some(overrides)) = (configuration.as_object_mut(), Some(changes)) {
            for (key, value) in overrides {
                if (key == "subtitle" || key == "render")
                    && let (Some(src), Some(dst)) = (
                        value.as_object(),
                        base.get_mut(key).and_then(Value::as_object_mut),
                    )
                {
                    for (field, content) in src {
                        dst.insert(field.clone(), content.clone());
                    }
                    continue;
                }
                base.insert(key.clone(), value.clone());
            }
        }
        // Preconditions are checked in the same transaction as workflow creation.
        // Source files are separately validated by the stage worker before use.
        let asset_needed = match stage {
            Stage::Download => None,
            Stage::Subtitle | Stage::Render | Stage::Dubbing => Some("video_raw"),
            Stage::Publish => Some("video_final"),
            Stage::Cleanup => unreachable!(),
        };
        if let Some(kind) = asset_needed {
            let has_asset: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1
                  AND kind::text=$2 AND storage_key LIKE 'v2/%')",
            )
            .bind(task_id)
            .bind(kind)
            .fetch_one(&mut *tx)
            .await?;
            if !has_asset
                && !(stage == Stage::Publish
                    && sqlx::query_scalar::<_, bool>(
                        "SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1
                 AND kind='video_raw'::asset_kind AND storage_key LIKE 'v2/%')",
                    )
                    .bind(task_id)
                    .fetch_one(&mut *tx)
                    .await?)
            {
                return Err(AppError::Conflict(format!(
                    "stage {} requires an existing V2 {kind} asset",
                    stage.as_str()
                )));
            }
        }
        if matches!(stage, Stage::Render | Stage::Dubbing) {
            let has_subtitle: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1
                 AND kind='subtitle_srt'::asset_kind AND storage_key LIKE 'v2/%')",
            )
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
            if !has_subtitle {
                return Err(AppError::Conflict(
                    "requested stage needs a translated SRT".into(),
                ));
            }
        }
        if stage == Stage::Publish {
            let has_meta: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM vr2_publish_meta WHERE task_id=$1)",
            )
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
            let inline = configuration.pointer("/publish/bilibili").is_some();
            if !has_meta && !inline {
                return Err(AppError::Conflict(
                    "manual publication requires validated Bilibili metadata".into(),
                ));
            }
        }
        let generation: i32 = sqlx::query_scalar(
            "SELECT COALESCE(MAX(generation),0)+1 FROM vr2_workflows WHERE task_id=$1",
        )
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;
        let workflow_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration)
             VALUES($1,$2,$3,'running',$4,$5)",
        )
        .bind(workflow_id)
        .bind(task_id)
        .bind(generation)
        .bind(stage.as_str())
        .bind(&configuration)
        .execute(&mut *tx)
        .await?;
        Self::insert_job(&mut tx, workflow_id, task_id, generation, stage).await?;
        // Clear only internal error status, never platform history or artifacts.
        let task_status = match stage {
            Stage::Download => "CREATED",
            Stage::Subtitle => "DOWNLOADED",
            Stage::Render => "SUBTITLE_READY",
            Stage::Dubbing | Stage::Publish => "RENDERED",
            Stage::Cleanup => unreachable!(),
        };
        sqlx::query(
            "UPDATE tasks SET status=lower($1)::task_status,error_code=NULL,error_message=NULL,
             updated_at=now() WHERE id=$2",
        )
        .bind(task_status)
        .bind(task_id)
        .execute(&mut *tx)
        .await?;
        Self::event(
            &mut tx,
            workflow_id,
            None,
            "workflow.restarted_from_stage",
            json!({"generation":generation,"stage":stage.as_str()}),
        )
        .await?;
        tx.commit().await?;
        self.workflow(workflow_id).await
    }

    async fn insert_job(
        tx: &mut Transaction<'_, Postgres>,
        workflow_id: Uuid,
        task_id: Uuid,
        generation: i32,
        stage: Stage,
    ) -> AppResult<()> {
        let operation_key = format!("vr2:{workflow_id}:{}:{generation}", stage.as_str());
        // The operation key, not the broker task ID, identifies a business side effect.
        sqlx::query(
            "INSERT INTO operation_inbox(id,workflow_id,task_id,stage,generation,operation_key,request_json,max_attempts,runtime_engine) VALUES($1,$2,$3,$4,$5,$6,'{}'::jsonb,$7,'rust-v2') ON CONFLICT(operation_key) DO NOTHING"
        ).bind(Uuid::new_v4()).bind(workflow_id).bind(task_id)
            .bind(stage.as_str()).bind(generation).bind(operation_key)
            .bind(if stage == Stage::Publish {1i32} else {5i32})
            .execute(&mut **tx).await?;
        Ok(())
    }
    async fn event(
        tx: &mut Transaction<'_, Postgres>,
        workflow_id: Uuid,
        job_id: Option<Uuid>,
        kind: &str,
        details: Value,
    ) -> AppResult<()> {
        sqlx::query(
            "INSERT INTO workflow_events(id,workflow_id,job_id,kind,details) VALUES($1,$2,$3,$4,$5)",
        )
        .bind(Uuid::new_v4())
        .bind(workflow_id)
        .bind(job_id)
        .bind(kind)
        .bind(details)
        .execute(&mut **tx)
        .await?;
        Ok(())
    }
    pub async fn workflow(&self, id: Uuid) -> AppResult<WorkflowInfo> {
        sqlx::query_as(
            "SELECT id,task_id,generation,state,current_stage,configuration,last_error,created_at,updated_at FROM vr2_workflows WHERE id=$1"
        ).bind(id).fetch_optional(&self.db.pool).await?
            .ok_or_else(|| AppError::NotFound(format!("workflow {id}")))
    }
    pub async fn jobs(&self, id: Uuid) -> AppResult<Vec<Value>> {
        let rows = sqlx::query(
            "SELECT id,stage,state,attempts,max_attempts,operation_key,external_ticket,lease_until,available_at,last_error,result,created_at,updated_at FROM vr2_jobs WHERE workflow_id=$1 ORDER BY created_at,id"
        ).bind(id).fetch_all(&self.db.pool).await?;
        Ok(rows
            .into_iter()
            .map(|r| {
                json!({
                    "id":r.get::<Uuid,_>("id"),"stage":r.get::<String,_>("stage"),
                    "state":r.get::<String,_>("state"),"attempts":r.get::<i32,_>("attempts"),
                    "max_attempts":r.get::<i32,_>("max_attempts"),
                    "operation_key":r.get::<String,_>("operation_key"),
                    "external_ticket":r.get::<Option<String>,_>("external_ticket"),
                    "lease_until":r.get::<Option<chrono::DateTime<chrono::Utc>>,_>("lease_until"),
                    "available_at":r.get::<chrono::DateTime<chrono::Utc>,_>("available_at"),
                    "last_error":r.get::<Option<String>,_>("last_error"),
                    "result":r.get::<Option<Value>,_>("result"),
                    "created_at":r.get::<chrono::DateTime<chrono::Utc>,_>("created_at"),
                    "updated_at":r.get::<chrono::DateTime<chrono::Utc>,_>("updated_at")
                })
            })
            .collect())
    }
    /// Quarantine a bounded set of expired irreversible publishes. Acquire
    /// locks in the same order as normal transitions and Python's original
    /// publisher reconciliation: Task -> Workflow -> Job -> PublishBatch.
    /// The earlier bulk UPDATE locked Jobs before Tasks, which could deadlock
    /// concurrent Python Task -> Batch settlement during a rolling migration.
    pub async fn quarantine_expired_publish(&self) -> AppResult<u64> {
        let candidates = sqlx::query(
            "SELECT id,task_id,workflow_id FROM vr2_jobs
            WHERE stage='publish' AND ((state='running' AND lease_until<now())
                OR (state='awaiting' AND updated_at<now()-interval '48 hours'))
            ORDER BY updated_at,id LIMIT 100",
        )
        .fetch_all(&self.db.pool)
        .await?;
        let mut count = 0;
        for candidate in candidates {
            let task_id: Uuid = candidate.try_get("task_id")?;
            let workflow_id: Uuid = candidate.try_get("workflow_id")?;
            let job_id: Uuid = candidate.try_get("id")?;
            let mut tx = self.db.pool.begin().await?;
            sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
                .bind(task_id)
                .fetch_one(&mut *tx)
                .await?;
            sqlx::query("SELECT id FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(workflow_id)
                .fetch_one(&mut *tx)
                .await?;
            let fenced=sqlx::query("UPDATE vr2_jobs SET state='unknown',lease_token=NULL,
                lease_until=NULL,last_error='publish ownership expired; external outcome must be verified',
                updated_at=now() WHERE id=$1 AND stage='publish'
                AND ((state='running' AND lease_until<now())
                    OR (state='awaiting' AND updated_at<now()-interval '48 hours'))
                RETURNING id")
                .bind(job_id).fetch_optional(&mut *tx).await?;
            if fenced.is_none() {
                tx.commit().await?;
                continue;
            }
            crate::bilibili_publish_v1::mark_uncertain(
                &mut tx,
                workflow_id,
                "native publish lease expired; confirm external Bilibili archive manually",
            )
            .await?;
            sqlx::query(
                "UPDATE vr2_workflows SET state='unknown',
                last_error='publish outcome unconfirmed',updated_at=now()
                WHERE id=$1 AND state='running'",
            )
            .bind(workflow_id)
            .execute(&mut *tx)
            .await?;
            Self::event(
                &mut tx,
                workflow_id,
                Some(job_id),
                "publish.unknown",
                json!({"reason":"lease_expired"}),
            )
            .await?;
            tx.commit().await?;
            count += 1;
        }
        Ok(count)
    }
    /// Claims are atomically fenced by a new UUID and a renewed PostgreSQL lease.
    /// The claim query can be safely used by multiple processes.
    pub async fn claim(&self) -> AppResult<Option<WorkItem>> {
        let mut tx = self.db.pool.begin().await?;
        // Serialise admission across V2 workers: the queue settings must
        // actually govern downloads rather than only change a UI number.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtext('videoroll:v2:job-claim'))")
            .execute(&mut *tx)
            .await?;
        let raw_limit: Option<Value> = sqlx::query_scalar(
            "SELECT value_json FROM app_settings WHERE key='workflow.youtube_download_concurrency'",
        )
        .fetch_optional(&mut *tx)
        .await?;
        let max_downloads =
            crate::subtitle_queue_v1::read_concurrency(&raw_limit.unwrap_or(Value::Null));
        let row = sqlx::query(
            r#"WITH next AS (
                SELECT j.id FROM vr2_jobs j
                JOIN vr2_workflows w ON w.id=j.workflow_id
                JOIN tasks t ON t.id=j.task_id
                WHERE w.state='running' AND t.status <> 'canceled'::task_status
                  AND (
                     (j.state='pending' AND j.available_at<=now())
                     OR (j.state='running' AND j.lease_until<now() AND j.stage <> 'publish')
                  )
                  AND (
                    j.stage <> 'download' OR (
                      (SELECT count(*) FROM vr2_jobs dj
                        JOIN vr2_workflows dw ON dw.id=dj.workflow_id
                        WHERE dj.stage='download' AND dj.state='running'
                          AND dj.lease_until>now() AND dw.state='running')
                      +
                      (SELECT count(*) FROM operation_inbox op
                        WHERE op.operation_key LIKE 'youtube-download:%'
                          AND op.status='processing' AND op.lease_until>now())
                    ) < $3
                  )
                ORDER BY t.priority DESC, j.available_at, j.created_at
                FOR UPDATE OF j SKIP LOCKED LIMIT 1
            ) UPDATE vr2_jobs j SET state='running',lease_token=$1,
                lease_until=now()+($2 * interval '1 second'),
                attempts=attempts+1,started_at=COALESCE(started_at,now()),
                updated_at=now() FROM next WHERE j.id=next.id
                RETURNING j.id,j.workflow_id,j.task_id,j.stage,j.generation,j.operation_key,
                          j.lease_token,j.attempts,j.max_attempts,j.payload"#,
        )
        .bind(Uuid::new_v4())
        .bind(self.lease_seconds())
        .bind(max_downloads)
        .fetch_optional(&mut *tx)
        .await?;
        tx.commit().await?;
        let Some(row) = row else { return Ok(None) };
        let name: String = row.try_get("stage")?;
        let stage = Stage::from_name(&name)
            .ok_or_else(|| AppError::Invalid(format!("unrecognized stage: {name}")))?;
        Ok(Some(WorkItem {
            id: row.try_get("id")?,
            workflow_id: row.try_get("workflow_id")?,
            task_id: row.try_get("task_id")?,
            stage,
            generation: row.try_get("generation")?,
            operation_key: row.try_get("operation_key")?,
            lease_token: row.try_get("lease_token")?,
            attempts: row.try_get("attempts")?,
            max_attempts: row.try_get("max_attempts")?,
            payload: row.try_get("payload")?,
        }))
    }
    pub async fn heartbeat(&self, item: &WorkItem) -> AppResult<bool> {
        let result = sqlx::query(
            "UPDATE vr2_jobs SET lease_until=now()+($1 * interval '1 second'),updated_at=now() WHERE id=$2 AND lease_token=$3 AND state='running' AND lease_until>now() AND EXISTS(SELECT 1 FROM vr2_workflows w WHERE w.id=vr2_jobs.workflow_id AND w.state='running')"
        ).bind(self.lease_seconds()).bind(item.id).bind(item.lease_token)
            .execute(&self.db.pool).await?;
        Ok(result.rows_affected() == 1)
    }
    async fn transition(
        &self,
        item: &WorkItem,
        outcome: StageOutcome,
        allow_awaiting: bool,
    ) -> AppResult<()> {
        // Strict lock order: parent task -> workflow -> child job.
        let mut tx = self.db.pool.begin().await?;
        let task_state: Option<String> =
            sqlx::query_scalar("SELECT upper(status::text) FROM tasks WHERE id=$1 FOR UPDATE")
                .bind(item.task_id)
                .fetch_optional(&mut *tx)
                .await?;
        let Some(task_state) = task_state else {
            return Err(AppError::NotFound("task disappeared".into()));
        };
        let workflow_state: Option<String> =
            sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(item.workflow_id)
                .fetch_optional(&mut *tx)
                .await?;
        let Some(workflow_state) = workflow_state else {
            return Err(AppError::NotFound("workflow disappeared".into()));
        };
        let job = sqlx::query(
            "SELECT state,lease_token,external_ticket FROM vr2_jobs WHERE id=$1 FOR UPDATE",
        )
        .bind(item.id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("job disappeared".into()))?;
        let job_state: String = job.try_get("state")?;
        let lease_token: Option<Uuid> = job.try_get("lease_token")?;
        if job_state == "succeeded" {
            tx.commit().await?;
            return Ok(());
        }
        if job_state != "running" || lease_token != Some(item.lease_token) {
            return Err(AppError::Conflict(
                "stale or superseded job completion".into(),
            ));
        }
        if workflow_state != "running" || task_state == "CANCELED" {
            return Err(AppError::Conflict(
                "parent workflow stopped; side-effect completion requires reconciliation".into(),
            ));
        }
        match outcome {
            StageOutcome::Awaiting { ticket } => {
                if !allow_awaiting || ticket.is_empty() || ticket.len() > 1024 {
                    return Err(AppError::Invalid("invalid external ticket".into()));
                }
                sqlx::query("UPDATE vr2_jobs SET state='awaiting',external_ticket=$1,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$2")
                    .bind(&ticket).bind(item.id).execute(&mut *tx).await?;
                Self::event(
                    &mut tx,
                    item.workflow_id,
                    Some(item.id),
                    "job.awaiting",
                    json!({"ticket":ticket}),
                )
                .await?;
            }
            completed @ (StageOutcome::Completed { .. } | StageOutcome::Skipped { .. }) => {
                Self::settle_locked(&mut tx, item, completed.result_payload()).await?;
            }
        }
        tx.commit().await?;
        Ok(())
    }
    async fn settle_locked(
        tx: &mut Transaction<'_, Postgres>,
        item: &WorkItem,
        result: Value,
    ) -> AppResult<()> {
        if item.stage == Stage::Publish && result.get("skipped").is_none() {
            if !crate::publish::has_confirmed_result(&result) {
                return Err(AppError::Invalid(
                    "publish completion requires a verified receipt".into(),
                ));
            }
            let standalone = crate::bilibili_publish_v1::mark_confirmed(tx, item, &result).await?;
            // Subtitle drafts are reconciled from confirmed publish receipts by
            // the independent outbox worker. NEVER execute subtitle SQL inside
            // the publication settlement transaction: a failed PostgreSQL
            // statement would poison it and obscure a successful upload.
            if standalone {
                sqlx::query("UPDATE tasks SET status='published'::task_status,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1 AND status <> 'canceled'::task_status")
                    .bind(item.task_id).execute(&mut **tx).await?;
            }
        }
        sqlx::query("UPDATE vr2_jobs SET state='succeeded',result=$1,lease_token=NULL,lease_until=NULL,finished_at=now(),updated_at=now() WHERE id=$2")
            .bind(&result).bind(item.id).execute(&mut **tx).await?;
        Self::event(
            tx,
            item.workflow_id,
            Some(item.id),
            "job.succeeded",
            json!({"stage":item.stage,"result":result}),
        )
        .await?;
        if let Some(next) = item.stage.after() {
            Self::insert_job(tx, item.workflow_id, item.task_id, item.generation, next).await?;
            sqlx::query("UPDATE vr2_workflows SET current_stage=$1,updated_at=now() WHERE id=$2")
                .bind(next.as_str())
                .bind(item.workflow_id)
                .execute(&mut **tx)
                .await?;
        } else {
            sqlx::query("UPDATE vr2_workflows SET state='succeeded',updated_at=now() WHERE id=$1")
                .bind(item.workflow_id)
                .execute(&mut **tx)
                .await?;
            Self::event(
                tx,
                item.workflow_id,
                Some(item.id),
                "workflow.succeeded",
                json!({}),
            )
            .await?;
        }
        Ok(())
    }
    pub async fn complete(&self, item: &WorkItem, outcome: StageOutcome) -> AppResult<()> {
        self.transition(item, outcome, true).await
    }
    /// An acknowledged external operation is never assumed to have completed.
    /// A matching callback ticket and idempotent duplicate callback are required.
    pub async fn complete_external(
        &self,
        job_id: Uuid,
        ticket: &str,
        result: Value,
    ) -> AppResult<()> {
        if ticket.len() > 1024 || ticket.is_empty() {
            return Err(AppError::Invalid("external ticket required".into()));
        }
        let mut tx = self.db.pool.begin().await?;
        let peek = sqlx::query(
            "SELECT workflow_id,task_id,stage,generation,operation_key FROM vr2_jobs WHERE id=$1",
        )
        .bind(job_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("job not found".into()))?;
        let task_id: Uuid = peek.try_get("task_id")?;
        let workflow_id: Uuid = peek.try_get("workflow_id")?;
        let stage = Stage::from_name(peek.try_get::<String, _>("stage")?.as_str())
            .ok_or_else(|| AppError::Invalid("bad stage".into()))?;
        let task_state: String =
            sqlx::query_scalar("SELECT upper(status::text) FROM tasks WHERE id=$1 FOR UPDATE")
                .bind(task_id)
                .fetch_one(&mut *tx)
                .await?;
        let flow_state: String =
            sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(workflow_id)
                .fetch_one(&mut *tx)
                .await?;
        let job =
            sqlx::query("SELECT state,external_ticket,result FROM vr2_jobs WHERE id=$1 FOR UPDATE")
                .bind(job_id)
                .fetch_one(&mut *tx)
                .await?;
        let old_ticket: Option<String> = job.try_get("external_ticket")?;
        if old_ticket.as_deref() != Some(ticket) {
            return Err(AppError::Conflict("external ticket mismatch".into()));
        }
        let state: String = job.try_get("state")?;
        if state == "succeeded" {
            let prev: Option<Value> = job.try_get("result")?;
            if prev.as_ref() != Some(&result) {
                return Err(AppError::Conflict(
                    "different result was previously committed".into(),
                ));
            }
            tx.commit().await?;
            return Ok(());
        }
        if state != "awaiting" || flow_state != "running" || task_state == "CANCELED" {
            return Err(AppError::Conflict(
                "workflow not awaiting this external operation".into(),
            ));
        }
        let item = WorkItem {
            id: job_id,
            workflow_id,
            task_id,
            stage,
            generation: peek.try_get("generation")?,
            operation_key: peek.try_get("operation_key")?,
            lease_token: Uuid::nil(),
            attempts: 0,
            max_attempts: 0,
            payload: Value::Null,
        };
        Self::settle_locked(&mut tx, &item, result).await?;
        tx.commit().await?;
        Ok(())
    }
    /// External rendering failures preserve the original durable operation ID
    /// and are idempotently resolved only while the exact ticket is awaiting.
    pub async fn fail_external(&self, job_id: Uuid, ticket: &str, error: &str) -> AppResult<()> {
        if ticket.is_empty() || ticket.len() > 1024 {
            return Err(AppError::Invalid("external render ticket missing".into()));
        }
        let mut tx = self.db.pool.begin().await?;
        let peek = sqlx::query("SELECT task_id,workflow_id,stage FROM vr2_jobs WHERE id=$1")
            .bind(job_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::NotFound("external stage job missing".into()))?;
        let task_id: Uuid = peek.try_get("task_id")?;
        let workflow_id: Uuid = peek.try_get("workflow_id")?;
        if peek.try_get::<String, _>("stage")? != "render" {
            return Err(AppError::Invalid(
                "only external renderer failure accepted".into(),
            ));
        }
        sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
        let flow: String =
            sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(workflow_id)
                .fetch_one(&mut *tx)
                .await?;
        let row = sqlx::query("SELECT state,external_ticket FROM vr2_jobs WHERE id=$1 FOR UPDATE")
            .bind(job_id)
            .fetch_one(&mut *tx)
            .await?;
        let state: String = row.try_get("state")?;
        let old_ticket: Option<String> = row.try_get("external_ticket")?;
        if old_ticket.as_deref() != Some(ticket) || state != "awaiting" || flow != "running" {
            return Err(AppError::Conflict(
                "external render no longer awaiting this ticket".into(),
            ));
        }
        let message = error.chars().take(1000).collect::<String>();
        sqlx::query("UPDATE vr2_jobs SET state='failed',last_error=$1,finished_at=now(),updated_at=now() WHERE id=$2")
            .bind(&message).bind(job_id).execute(&mut *tx).await?;
        sqlx::query(
            "UPDATE vr2_workflows SET state='failed',last_error=$1,updated_at=now() WHERE id=$2",
        )
        .bind(&message)
        .bind(workflow_id)
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE tasks SET status='failed'::task_status,error_code='V2_REMOTE_RENDER_FAILED',error_message=$1,updated_at=now() WHERE id=$2 AND status<>'canceled'::task_status")
            .bind(&message).bind(task_id).execute(&mut *tx).await?;
        Self::event(
            &mut tx,
            workflow_id,
            Some(job_id),
            "render.remote_failed",
            json!({"error":message}),
        )
        .await?;
        tx.commit().await?;
        Ok(())
    }
    pub async fn fail(&self, item: &WorkItem, error: &str) -> AppResult<()> {
        let mut tx = self.db.pool.begin().await?;
        // Maintain the global transaction lock order: Task -> Workflow -> Job.
        sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(item.task_id)
            .fetch_one(&mut *tx)
            .await?;
        sqlx::query("SELECT id FROM vr2_workflows WHERE id=$1 FOR UPDATE")
            .bind(item.workflow_id)
            .fetch_one(&mut *tx)
            .await?;
        let row = sqlx::query(
            "SELECT state,lease_token,attempts,max_attempts FROM vr2_jobs WHERE id=$1 FOR UPDATE",
        )
        .bind(item.id)
        .fetch_one(&mut *tx)
        .await?;
        let state: String = row.try_get("state")?;
        let owner: Option<Uuid> = row.try_get("lease_token")?;
        if state != "running" || owner != Some(item.lease_token) {
            return Err(AppError::Conflict("stale stage failure ignored".into()));
        }
        let attempts: i32 = row.try_get("attempts")?;
        let max: i32 = row.try_get("max_attempts")?;
        let unknown = item.stage.has_irreversible_external_effect();
        let failed = attempts >= max || unknown;
        let target = if unknown {
            "unknown"
        } else if failed {
            "failed"
        } else {
            "pending"
        };
        // Retry delay is bounded; stage execution itself is lease-protected.
        let retry_seconds = std::cmp::min(900, 15 * 2i64.pow(attempts.clamp(0, 5) as u32));
        sqlx::query("UPDATE vr2_jobs SET state=$1,last_error=$2,lease_token=NULL,lease_until=NULL,available_at=now()+($3*interval '1 second'),updated_at=now(),finished_at=CASE WHEN $4 THEN now() ELSE NULL END WHERE id=$5")
            .bind(target).bind(error.chars().take(2000).collect::<String>())
            .bind(retry_seconds).bind(failed).bind(item.id).execute(&mut *tx).await?;
        if failed {
            if item.stage == Stage::Publish {
                crate::bilibili_publish_v1::mark_uncertain(
                    &mut tx,
                    item.workflow_id,
                    "native publisher outcome unknown; verify Bilibili before retry",
                )
                .await?;
            }
            sqlx::query("UPDATE vr2_workflows SET state=$1,last_error=$2,updated_at=now() WHERE id=$3 AND state='running'")
                .bind(target).bind(error.chars().take(2000).collect::<String>())
                .bind(item.workflow_id).execute(&mut *tx).await?;
            if !unknown {
                sqlx::query("UPDATE tasks SET status='failed'::task_status,error_code='V2_WORKFLOW_FAILED',error_message=$1,updated_at=now() WHERE id=$2 AND status NOT IN ('published'::task_status,'canceled'::task_status)")
                    .bind(error.chars().take(1000).collect::<String>())
                    .bind(item.task_id).execute(&mut *tx).await?;
            }
        }
        Self::event(&mut tx,item.workflow_id,Some(item.id), if unknown {"job.unknown"} else {"job.failed_attempt"},
            json!({"stage":item.stage,"attempts":attempts,"state":target,"error":error.chars().take(800).collect::<String>()})).await?;
        tx.commit().await?;
        Ok(())
    }
    pub async fn pause(&self, workflow_id: Uuid) -> AppResult<WorkflowInfo> {
        let mut tx = self.db.pool.begin().await?;
        let peek = sqlx::query("SELECT task_id FROM vr2_workflows WHERE id=$1")
            .bind(workflow_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::NotFound("workflow not found".into()))?;
        let task_id: Uuid = peek.try_get("task_id")?;
        sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
        let state: String =
            sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(workflow_id)
                .fetch_one(&mut *tx)
                .await?;
        if !matches!(state.as_str(), "running" | "paused") {
            return Err(AppError::Conflict(format!(
                "cannot pause workflow in state {state}"
            )));
        }
        sqlx::query("UPDATE vr2_workflows SET state='paused',updated_at=now() WHERE id=$1")
            .bind(workflow_id)
            .execute(&mut *tx)
            .await?;
        Self::event(&mut tx, workflow_id, None, "workflow.paused", json!({})).await?;
        tx.commit().await?;
        self.workflow(workflow_id).await
    }
    /// Original UI's per-task stop control, implemented as a reversible
    /// durable pause. The running stage retains its fenced lease and will be
    /// eligible for resumption when that lease expires. Never pause external
    /// publisher results that may already have caused irreversible effects.
    pub async fn stop_task_compat(&self, task_id: Uuid) -> AppResult<()> {
        let mut tx = self.db.pool.begin().await?;
        let row = sqlx::query(
            "SELECT upper(status::text) AS status,upper(stopped_status::text) AS stopped
             FROM tasks WHERE id=$1
             AND EXISTS(SELECT 1 FROM tasks WHERE id=$1 AND runtime_owner='rust')
             FOR UPDATE",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("V2 task missing".into()))?;
        let status: String = row.try_get("status")?;
        let stopped: Option<String> = row.try_get("stopped")?;
        if status == "CANCELED" && stopped.is_some() {
            tx.commit().await?;
            return Ok(());
        }
        if !matches!(
            status.as_str(),
            "CREATED"
                | "INGESTED"
                | "DOWNLOADED"
                | "AUDIO_EXTRACTED"
                | "ASR_DONE"
                | "TRANSLATED"
                | "SUBTITLE_READY"
                | "RENDERED"
                | "READY_FOR_REVIEW"
                | "APPROVED"
        ) {
            return Err(AppError::Conflict(
                "V2 task cannot be stopped in its current state".into(),
            ));
        }
        let publishing: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1
             AND stage='publish' AND state IN ('pending','running','awaiting','unknown'))",
        )
        .bind(task_id)
        .fetch_one(&mut *tx)
        .await?;
        if publishing {
            return Err(AppError::Conflict(
                "potential Bilibili publish must be verified before stopping task".into(),
            ));
        }
        let flow=sqlx::query(
            "SELECT id,state FROM vr2_workflows WHERE task_id=$1
             AND state IN ('running','paused','unknown') ORDER BY generation DESC LIMIT 1 FOR UPDATE"
        ).bind(task_id).fetch_optional(&mut *tx).await?;
        if let Some(flow) = flow {
            let state: String = flow.try_get("state")?;
            if state == "unknown" {
                return Err(AppError::Conflict(
                    "external workflow outcome unknown".into(),
                ));
            }
            let id: Uuid = flow.try_get("id")?;
            sqlx::query("UPDATE vr2_workflows SET state='paused',updated_at=now() WHERE id=$1")
                .bind(id)
                .execute(&mut *tx)
                .await?;
            Self::event(
                &mut tx,
                id,
                None,
                "workflow.legacy_task_stopped",
                json!({"from_status":status}),
            )
            .await?;
        }
        sqlx::query(
            "UPDATE tasks SET stopped_status=lower($1)::task_status,status='canceled'::task_status,updated_at=now() WHERE id=$2"
        ).bind(status).bind(task_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    /// Restore the original task stage and resume the same workflow generation.
    /// No completed or uncertain external publisher is ever automatically reissued.
    pub async fn resume_task_compat(&self, task_id: Uuid) -> AppResult<()> {
        let mut tx = self.db.pool.begin().await?;
        let row = sqlx::query(
            "SELECT upper(status::text) AS status,upper(stopped_status::text) AS stopped FROM tasks
             WHERE id=$1 AND EXISTS(SELECT 1 FROM tasks WHERE id=$1 AND runtime_owner='rust')
             FOR UPDATE",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("V2 task missing".into()))?;
        let status: String = row.try_get("status")?;
        let stopped: Option<String> = row.try_get("stopped")?;
        if status != "CANCELED" || stopped.is_none() {
            return Err(AppError::Conflict(
                "V2 task was not stopped by task controls".into(),
            ));
        }
        let flow = sqlx::query(
            "SELECT id,state FROM vr2_workflows WHERE task_id=$1
             ORDER BY generation DESC LIMIT 1 FOR UPDATE",
        )
        .bind(task_id)
        .fetch_optional(&mut *tx)
        .await?;
        if let Some(flow) = flow {
            let state: String = flow.try_get("state")?;
            if state != "paused" {
                return Err(AppError::Conflict(
                    "V2 task cannot resume a completed, canceled or uncertain workflow".into(),
                ));
            }
            let id: Uuid = flow.try_get("id")?;
            sqlx::query("UPDATE vr2_workflows SET state='running',updated_at=now() WHERE id=$1")
                .bind(id)
                .execute(&mut *tx)
                .await?;
            Self::event(&mut tx, id, None, "workflow.legacy_task_resumed", json!({})).await?;
        }
        sqlx::query(
            "UPDATE tasks SET status=lower($1)::task_status,stopped_status=NULL,updated_at=now() WHERE id=$2"
        ).bind(stopped).bind(task_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    /// Stop a workflow without pretending that an in-flight external publish was rejected.
    pub async fn cancel(&self, workflow_id: Uuid) -> AppResult<WorkflowInfo> {
        let mut tx = self.db.pool.begin().await?;
        let peek = sqlx::query("SELECT task_id FROM vr2_workflows WHERE id=$1")
            .bind(workflow_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::NotFound("workflow not found".into()))?;
        let task_id: Uuid = peek.try_get("task_id")?;
        sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
        let state: String =
            sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(workflow_id)
                .fetch_one(&mut *tx)
                .await?;
        if !matches!(state.as_str(), "running" | "paused") {
            return Err(AppError::Conflict(format!(
                "cannot cancel {state} workflow"
            )));
        }
        let risky:Option<Uuid>=sqlx::query_scalar(
            "SELECT id FROM vr2_jobs WHERE workflow_id=$1 AND stage='publish' AND state IN ('running','awaiting') FOR UPDATE"
        ).bind(workflow_id).fetch_optional(&mut *tx).await?;
        if let Some(job_id) = risky {
            sqlx::query("UPDATE vr2_jobs SET state='unknown',lease_token=NULL,lease_until=NULL,last_error='canceled during external publish: verify platform result',updated_at=now() WHERE id=$1")
                .bind(job_id).execute(&mut *tx).await?;
            crate::bilibili_publish_v1::mark_uncertain(
                &mut tx,
                workflow_id,
                "native publisher canceled while external result uncertain",
            )
            .await?;
            sqlx::query("UPDATE vr2_workflows SET state='unknown',last_error='external publish outcome uncertain',updated_at=now() WHERE id=$1")
                .bind(workflow_id).execute(&mut *tx).await?;
            Self::event(
                &mut tx,
                workflow_id,
                Some(job_id),
                "workflow.cancel_publish_unknown",
                json!({}),
            )
            .await?;
        } else {
            sqlx::query("UPDATE vr2_jobs SET state='canceled',lease_token=NULL,lease_until=NULL,updated_at=now() WHERE workflow_id=$1 AND state IN ('pending','running','awaiting')")
                .bind(workflow_id).execute(&mut *tx).await?;
            sqlx::query("UPDATE tasks SET status='canceled'::task_status,updated_at=now() WHERE id=$1 AND status<>'published'::task_status")
                .bind(task_id).execute(&mut *tx).await?;
            sqlx::query("UPDATE vr2_workflows SET state='canceled',updated_at=now() WHERE id=$1")
                .bind(workflow_id)
                .execute(&mut *tx)
                .await?;
            Self::event(&mut tx, workflow_id, None, "workflow.canceled", json!({})).await?;
        }
        tx.commit().await?;
        self.workflow(workflow_id).await
    }
    /// A human must verify the external archive outcome before resolving UNKNOWN.
    /// Recording evidence is part of the transaction and never silently resubmits.
    pub async fn resolve_unknown_publish(
        &self,
        job_id: Uuid,
        accepted: bool,
        external_id: Option<&str>,
        evidence: &str,
    ) -> AppResult<WorkflowInfo> {
        if evidence.trim().chars().count() < 12 || evidence.chars().count() > 4000 {
            return Err(AppError::Invalid(
                "verification evidence must contain 12–4000 characters".into(),
            ));
        }
        if accepted && !external_id.is_some_and(|v| (6..=200).contains(&v.len())) {
            return Err(AppError::Invalid(
                "confirmed publish requires a platform archive ID".into(),
            ));
        }
        let mut tx = self.db.pool.begin().await?;
        let peek = sqlx::query(
            "SELECT workflow_id,task_id,stage,generation,operation_key FROM vr2_jobs WHERE id=$1",
        )
        .bind(job_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("publish job not found".into()))?;
        let workflow_id: Uuid = peek.try_get("workflow_id")?;
        let task_id: Uuid = peek.try_get("task_id")?;
        if peek.try_get::<String, _>("stage")? != "publish" {
            return Err(AppError::Invalid(
                "only an uncertain publish job can be reconciled".into(),
            ));
        }
        sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_one(&mut *tx)
            .await?;
        let flow: String =
            sqlx::query_scalar("SELECT state FROM vr2_workflows WHERE id=$1 FOR UPDATE")
                .bind(workflow_id)
                .fetch_one(&mut *tx)
                .await?;
        let job_state: String =
            sqlx::query_scalar("SELECT state FROM vr2_jobs WHERE id=$1 FOR UPDATE")
                .bind(job_id)
                .fetch_one(&mut *tx)
                .await?;
        if flow != "unknown" || job_state != "unknown" {
            return Err(AppError::Conflict("publish is not in UNKNOWN state".into()));
        }
        sqlx::query(
            "UPDATE vr2_workflows SET state='running',last_error=NULL,updated_at=now() WHERE id=$1",
        )
        .bind(workflow_id)
        .execute(&mut *tx)
        .await?;
        Self::event(
            &mut tx,
            workflow_id,
            Some(job_id),
            "publish.manually_reconciled",
            json!({"accepted":accepted,"external_id":external_id,"evidence":evidence}),
        )
        .await?;
        if accepted {
            let item = WorkItem {
                id: job_id,
                workflow_id,
                task_id,
                stage: Stage::Publish,
                generation: peek.try_get("generation")?,
                operation_key: peek.try_get("operation_key")?,
                lease_token: Uuid::nil(),
                attempts: 0,
                max_attempts: 0,
                payload: Value::Null,
            };
            Self::settle_locked(
                &mut tx,
                &item,
                json!({
                    "confirmed":true,"external_id":external_id,"verification":"manual"
                }),
            )
            .await?;
        } else {
            sqlx::query("UPDATE vr2_jobs SET state='pending',attempts=0,lease_token=NULL,lease_until=NULL,available_at=now(),last_error=NULL,updated_at=now() WHERE id=$1")
                .bind(job_id).execute(&mut *tx).await?;
            sqlx::query(
                "UPDATE vr2_workflows SET current_stage='publish',updated_at=now() WHERE id=$1",
            )
            .bind(workflow_id)
            .execute(&mut *tx)
            .await?;
        }
        tx.commit().await?;
        self.workflow(workflow_id).await
    }
    pub async fn resume(&self, workflow_id: Uuid) -> AppResult<WorkflowInfo> {
        let mut tx = self.db.pool.begin().await?;
        let peek = sqlx::query("SELECT task_id FROM vr2_workflows WHERE id=$1")
            .bind(workflow_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or_else(|| AppError::NotFound("workflow not found".into()))?;
        let task_id: Uuid = peek.try_get("task_id")?;
        let task_status: String =
            sqlx::query_scalar("SELECT upper(status::text) FROM tasks WHERE id=$1 FOR UPDATE")
                .bind(task_id)
                .fetch_one(&mut *tx)
                .await?;
        if task_status == "CANCELED" {
            return Err(AppError::Conflict("task is stopped in legacy state".into()));
        }
        let updated=sqlx::query("UPDATE vr2_workflows SET state='running',updated_at=now() WHERE id=$1 AND state='paused'")
            .bind(workflow_id).execute(&mut *tx).await?;
        if updated.rows_affected() != 1 {
            return Err(AppError::Conflict("workflow is not paused".into()));
        }
        Self::event(&mut tx, workflow_id, None, "workflow.resumed", json!({})).await?;
        tx.commit().await?;
        self.workflow(workflow_id).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn publish_stages_have_no_automatic_retry() {
        assert!(Stage::Publish.has_irreversible_external_effect());
        assert!(!Stage::Subtitle.has_irreversible_external_effect());
    }
}

#[cfg(test)]
mod stage_restart_tests {
    use super::*;
    #[test]
    fn reusing_a_stage_does_not_silently_change_settings() {
        let saved = json!({"subtitle":{"translate_enabled":false,"asr_language":"en"},
            "render":{"burn_in":true},"auto_publish":false});
        let exact = json!({"subtitle":{"translate_enabled":false},"render":{"burn_in":true},
            "auto_publish":false});
        assert!(requested_settings_match(&saved, exact.as_object().unwrap()));
        let changed = json!({"subtitle":{"translate_enabled":true}});
        assert!(!requested_settings_match(
            &saved,
            changed.as_object().unwrap()
        ));
        let newkey = json!({"subtitle":{"model_id":"missing"}});
        assert!(!requested_settings_match(
            &saved,
            newkey.as_object().unwrap()
        ));
    }
}
