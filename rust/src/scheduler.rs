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

/// Snapshot of a bulk recovery decision, checked again under the Task lock.
pub(crate) struct BulkResumeGuard {
    pub updated_at: chrono::DateTime<chrono::Utc>,
    pub native_job: Option<Uuid>,
    pub subtitle_job: Option<(Uuid, Value)>,
    pub raw_key: Option<String>,
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
    pub async fn reconcile_translation_traces(&self) -> AppResult<u64> {
        crate::media::translation_trace::reap(&self.db).await
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
    /// Persist optional source proof together with original Task and workflow.
    pub async fn create_auto_youtube_with_proof(
        &self,
        source_url: &str,
        source_license: &str,
        priority: i32,
        configuration: Value,
        source_proof_url: Option<&str>,
    ) -> AppResult<(TaskInfo, Option<WorkflowInfo>, bool)> {
        self.create_youtube_source_task_inner(
            source_url,
            source_license,
            priority,
            configuration,
            true,
            source_proof_url,
        )
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
        self.create_youtube_source_task_inner(
            source_url,
            source_license,
            priority,
            configuration,
            auto_process,
            None,
        )
        .await
    }
    async fn create_youtube_source_task_inner(
        &self,
        source_url: &str,
        source_license: &str,
        priority: i32,
        configuration: Value,
        auto_process: bool,
        source_proof_url: Option<&str>,
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
        // Original Task columns are distinct: origin_kind has a 32-char
        // enum-like value; created_by can contain the original metadata.
        // Both must be committed atomically with the durable workflow.
        sqlx::query(
            "INSERT INTO tasks(id,source_type,source_url,source_license,status,priority,origin_kind,origin_run_id,restart_generation,created_by,source_proof_url,retry_count) VALUES($1,'youtube'::source_type,$2,$3::source_license,'created'::task_status,$4,$6,$5,0,$7,$8,0)"
        ).bind(task_id).bind(canonical_url).bind(license).bind(priority.clamp(-100,100))
            .bind(run_id).bind(origin).bind(created_by).bind(source_proof_url)
            .execute(&mut *tx).await?;
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
        let historical_publish:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox i WHERE i.task_id=$1 AND i.runtime_engine='rust-task-cutover' AND i.status='completed') AND EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1)")
            .bind(task_id).fetch_one(&mut *tx).await?;
        if historical_publish || crate::maintenance_publish_timeout_v1::publication_fence(&mut tx, task_id).await? {
            return Err(AppError::Conflict(
                "historical publication exists; use explicit reconciled publication controls"
                    .into(),
            ));
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
        if configuration["legacy_auto_runtime_profile"].as_bool()==Some(true){
            // Explicit V1 auto-start: keep the old origin contract. Never
            // silently switch an already-running or paused generation.
            if !crate::task_ownership::blockers(&mut tx,task_id).await?.is_empty(){
                return Err(AppError::Conflict("historical or active work must be settled before automatic restart".into()))
            }
            let run_id=Uuid::new_v4().simple().to_string();
            let changed=sqlx::query("UPDATE tasks SET runtime_origin='youtube_auto',origin_kind='youtube_task_restart',
                origin_run_id=$2,created_by=$3 WHERE id=$1 AND source_type::text='youtube'
                AND source_url IS NOT NULL AND stopped_status IS NULL")
                .bind(task_id).bind(&run_id).bind(format!("youtube_task_restart;auto_publish=;run_id={run_id}"))
                .execute(&mut *tx).await?.rows_affected();
            if changed!=1{return Err(AppError::Conflict("automatic restart needs an unstopped YouTube source".into()))}
        }
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
    /// Restore a proven missing native source without resetting its history.
    /// The old download receipt remains immutable; the new generation ends at
    /// download, never inheriting publication or dubbing intent.
    pub(crate) async fn recover_missing_download(
        &self,
        store: &crate::storage::AssetRegistry,
        task_id: Uuid,
    ) -> AppResult<bool> {
        let mut tx = self.db.pool.begin().await?;
        let task = sqlx::query(
            "SELECT source_url,source_license::text AS license,status::text AS status,
                (to_jsonb(t)->>'lock_owner' IS NOT NULL OR to_jsonb(t)->>'lock_until' IS NOT NULL) AS locked
             FROM tasks t WHERE id=$1 AND runtime_owner='rust' AND source_type::text='youtube' FOR UPDATE",
        ).bind(task_id).fetch_optional(&mut *tx).await?;
        let Some(task) = task else { return Ok(false) };
        let status: String = task.try_get("status")?;
        let url: Option<String> = task.try_get("source_url")?;
        if matches!(status.as_str(), "published" | "publishing" | "canceled")
            || task.try_get::<bool,_>("locked")?
            || task.try_get::<String,_>("license")? == "unknown"
        {
            return Ok(false);
        }
        let Some(video_id) = url.as_deref().and_then(|s| crate::ingest::youtube_video_id(s).ok()) else {
            return Ok(false);
        };
        let latest = sqlx::query(
            "SELECT id,state,configuration FROM vr2_workflows WHERE task_id=$1
             ORDER BY generation DESC LIMIT 1 FOR UPDATE",
        ).bind(task_id).fetch_optional(&mut *tx).await?;
        let Some(latest) = latest else { return Ok(false) };
        let flow_state: String = latest.try_get("state")?;
        let config: Value = latest.try_get("configuration")?;
        if flow_state == "running" && config.get("source_recovery").is_some() {
            let active: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE workflow_id=$1 AND stage='download'
                 AND state IN ('pending','running'))",
            ).bind(latest.try_get::<Uuid,_>("id")?).fetch_one(&mut *tx).await?;
            return Ok(active);
        }
        if !matches!(flow_state.as_str(), "succeeded" | "failed" | "canceled")
            || !crate::task_ownership::blockers(&mut tx, task_id).await?.is_empty()
            || crate::storage::publication_cleanup_blocked(&mut tx, task_id).await?
        {
            return Ok(false);
        }
        let blocked: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1)
              OR EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1)
              OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND stage IN ('publish','sau_publish')
                AND NOT (state='succeeded' AND COALESCE(result->>'skipped','')<>''))
              OR EXISTS(SELECT 1 FROM operation_inbox WHERE operation_key='youtube-download:'||$1::text
                AND (status NOT IN ('completed','succeeded','failed','canceled') OR lease_owner IS NOT NULL OR lease_until IS NOT NULL))
              OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE task_id=$1)
              OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1)",
        ).bind(task_id).fetch_one(&mut *tx).await?;
        if blocked { return Ok(false); }
        let receipt = sqlx::query(
            "SELECT id,state,result FROM vr2_jobs WHERE task_id=$1 AND stage='download'
             ORDER BY generation DESC,created_at DESC,id DESC LIMIT 1",
        ).bind(task_id).fetch_optional(&mut *tx).await?;
        let Some(receipt) = receipt else { return Ok(false) };
        if receipt.try_get::<String,_>("state")? != "succeeded" { return Ok(false); }
        let result: Option<Value> = receipt.try_get("result")?;
        let raw = sqlx::query_as::<_, crate::domain::AssetInfo>(
            "SELECT id,task_id,kind::text AS kind,storage_key,sha256,size_bytes,duration_ms,created_at
             FROM assets WHERE task_id=$1 AND kind::text='video_raw'
             ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE",
        ).bind(task_id).fetch_optional(&mut *tx).await?;
        let Some(raw) = raw else { return Ok(false) };
        let Some(proof) = crate::ingest::SourceRecovery::from_asset(&raw, video_id) else { return Ok(false) };
        if result.as_ref().and_then(|r| r.get("storage_key")).and_then(Value::as_str)
            != Some(proof.storage_key.as_str())
            || result.as_ref().and_then(|r|r.get("sha256")).and_then(Value::as_str)
                .is_some_and(|sha| !sha.eq_ignore_ascii_case(&proof.sha256))
            || result.as_ref().and_then(|r|r.get("size_bytes")).and_then(Value::as_i64)
                .is_some_and(|size| size!=proof.size_bytes)
        {
            return Ok(false);
        }
        let path = store.resolve_existing(&proof.storage_key).await?;
        match tokio::fs::symlink_metadata(path).await {
            Err(e) if e.kind()==std::io::ErrorKind::NotFound => {},
            Err(e) => return Err(e.into()),
            Ok(_) => return Ok(false),
        }
        // Existing GC claims must finish before recovery. A live registered
        // object is pinned by the new workflow reference before committing.
        let object: Option<String> = sqlx::query_scalar(
            "SELECT state FROM storage_objects WHERE storage_key=$1 FOR UPDATE",
        ).bind(&proof.storage_key).fetch_optional(&mut *tx).await?;
        let workdir: Option<String> = sqlx::query_scalar(
            "SELECT state FROM storage_workdirs WHERE path_key=$1 FOR UPDATE",
        ).bind(format!("v2/raw/{task_id}")).fetch_optional(&mut *tx).await?;
        if object.as_deref()==Some("deleting") || workdir.as_deref()==Some("deleting") {
            return Ok(false);
        }
        let generation: i32 = sqlx::query_scalar(
            "SELECT COALESCE(MAX(generation),0)+1 FROM vr2_workflows WHERE task_id=$1",
        ).bind(task_id).fetch_one(&mut *tx).await?;
        let flow = Uuid::new_v4();
        let configuration = json!({"source_recovery":proof,"auto_publish":false,
            "auto_publish_platforms":[],"auto_dub_p2":false});
        sqlx::query("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration)
            VALUES($1,$2,$3,'running','download',$4)")
            .bind(flow).bind(task_id).bind(generation).bind(&configuration).execute(&mut *tx).await?;
        Self::insert_job(&mut tx, flow, task_id, generation, Stage::Download).await?;
        if object.as_deref()==Some("live") {
            sqlx::query("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
                VALUES($1,'workflow',$2,'video_raw') ON CONFLICT DO NOTHING")
                .bind(&raw.storage_key).bind(flow).execute(&mut *tx).await?;
        }
        sqlx::query("UPDATE tasks SET status='created'::task_status,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1")
            .bind(task_id).execute(&mut *tx).await?;
        Self::event(&mut tx,flow,None,"workflow.source_recovery_started",
            json!({"previous_download_job":receipt.try_get::<Uuid,_>("id")?,"asset_id":raw.id,"generation":generation})).await?;
        tx.commit().await?;
        Ok(true)
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
        self.restart_at_inner(task_id, stage, overrides, None).await
    }

    pub(crate) async fn restart_recent(
        &self, task_id:Uuid, stage:Stage, overrides:Value, guard:BulkResumeGuard,
    )->AppResult<WorkflowInfo>{
        self.restart_at_inner(task_id,stage,overrides,Some(guard)).await
    }

    async fn restart_at_inner(
        &self,task_id:Uuid,stage:Stage,overrides:Value,bulk:Option<BulkResumeGuard>,
    )->AppResult<WorkflowInfo>{
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
        if let Some(guard)=bulk {
            // Never turn a stale bulk list into a restart of a newer task.
            // Nonblocking ledger locks also fence old direct writers.
            if status!="FAILED" {return Err(AppError::Conflict("task is no longer failed".into()))}
            sqlx::query("LOCK TABLE pipeline_runs,operation_inbox,assets,subtitles,subtitle_jobs,render_jobs,render_executions,dubbing_jobs,publish_jobs,publish_batches,outbox_events IN SHARE ROW EXCLUSIVE MODE NOWAIT")
                .execute(&mut *tx).await.map_err(|e|if e.as_database_error().and_then(|e|e.code()).as_deref()==Some("55P03"){
                    AppError::Conflict("another worker is changing a recovery ledger".into())
                }else{e.into()})?;
            let eligible:bool=sqlx::query_scalar("SELECT updated_at=$2 AND source_license::text<>'unknown'
                AND stopped_status IS NULL AND to_jsonb(t)->>'lock_owner' IS NULL AND to_jsonb(t)->>'lock_until' IS NULL
                AND active_publish_batch_id IS NULL FROM tasks t WHERE id=$1")
                .bind(task_id).bind(guard.updated_at).fetch_one(&mut *tx).await?;
            let latest:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_jobs WHERE task_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1")
                .bind(task_id).fetch_optional(&mut *tx).await?;
            let native_failed=if let Some(job)=latest{
                sqlx::query_scalar::<_,bool>("SELECT state='failed' AND stage=$2 AND lease_token IS NULL AND lease_until IS NULL FROM vr2_jobs WHERE id=$1")
                    .bind(job).bind(stage.as_str()).fetch_one(&mut *tx).await?
            }else{true};
            if !eligible || !native_failed || latest!=guard.native_job || !crate::task_ownership::blockers(&mut tx,task_id).await?.is_empty()
                || crate::storage::publication_cleanup_blocked(&mut tx,task_id).await? {
                return Err(AppError::Conflict("task or recovery ledger changed or is unsettled".into()));
            }
            if latest.is_none(){
                let old:Option<(Uuid,Value)>=sqlx::query_as("SELECT id,request_json FROM subtitle_jobs WHERE task_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1")
                    .bind(task_id).fetch_optional(&mut *tx).await?;
                let raw:Option<String>=sqlx::query_scalar("SELECT storage_key FROM assets WHERE task_id=$1 AND kind::text='video_raw' ORDER BY created_at DESC,id DESC LIMIT 1")
                    .bind(task_id).fetch_optional(&mut *tx).await?;
                let history:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1) OR EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1) OR EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND engine='rust-v2')")
                    .bind(task_id).fetch_one(&mut *tx).await?;
                if old!=guard.subtitle_job || raw!=guard.raw_key || history{
                    return Err(AppError::Conflict("historical recovery evidence changed or publication history exists".into()))
                }
            }
        }
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
        let historical_publish:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox i WHERE i.task_id=$1 AND i.runtime_engine='rust-task-cutover' AND i.status='completed') AND EXISTS(SELECT 1 FROM publish_jobs j WHERE j.task_id=$1)")
            .bind(task_id).fetch_one(&mut *tx).await?;
        if unsafe_publish || historical_publish || crate::maintenance_publish_timeout_v1::publication_fence(&mut tx,task_id).await? {
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
        // A source-only recovery is an independent generation. Ordinary
        // manual stage restarts must not inherit its download-only policy.
        if let Some(base) = configuration.as_object_mut() {
            base.remove("source_recovery");
        }
        if changes.contains_key("source_recovery") {
            return Err(AppError::Invalid("source recovery is an internal operation".into()));
        }
        // A new manual stage cannot inherit a past explicit posting intent.
        if matches!(stage, Stage::Subtitle | Stage::Publish)
            && !changes.contains_key("after_render_publish")
        {
            if let Some(base) = configuration.as_object_mut() {
                base.remove("after_render_publish");
                base.remove("after_render_input");
            }
        }
        if stage == Stage::Subtitle && !changes.contains_key("subtitle_resume_plan") {
            if let Some(base) = configuration.as_object_mut() {
                base.remove("subtitle_resume_plan");
            }
        }
        // A fresh standalone subtitle request is a new execution policy,
        // not a merge with stale manual parameters or prior selective plan.
        if stage == Stage::Subtitle
            && changes.contains_key("standalone_subtitle_runtime_profile")
            && !changes.contains_key("selective_retranslate")
        {
            if let Some(base) = configuration.as_object_mut() {
                for key in [
                    "legacy_auto_runtime_profile",
                    "subtitle",
                    "render",
                    "selective_retranslate",
                    "subtitle_resume_plan",
                    "auto_publish",
                    "auto_publish_platforms",
                    "auto_dub_p2",
                    "dubbing",
                    "publish",
                    "auto_publish_dubbing_job_id",
                ] {
                    base.remove(key);
                }
            }
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
                  AND kind::text=$2 AND (storage_key LIKE 'v2/%' OR EXISTS(SELECT 1 FROM operation_inbox i WHERE i.task_id=$1 AND i.runtime_engine='rust-task-cutover' AND i.status='completed')))",
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
                 AND kind='subtitle_srt'::asset_kind AND (storage_key LIKE 'v2/%' OR EXISTS(SELECT 1 FROM operation_inbox i WHERE i.task_id=$1 AND i.runtime_engine='rust-task-cutover' AND i.status='completed')))",
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

    /// Called only under the ownership-cutover transaction, after validating
    /// all historical side effects and media. Never creates a publish job.
    pub(crate) async fn adopt_resume(
        tx: &mut Transaction<'_, Postgres>,
        task: Uuid,
        stage: Stage,
    ) -> AppResult<Uuid> {
        let status = match stage {
            Stage::Download => "created",
            Stage::Subtitle => "downloaded",
            Stage::Render => "subtitle_ready",
            _ => return Err(AppError::Invalid("unsafe adoption resume stage".into())),
        };
        let generation: i32 = sqlx::query_scalar(
            "SELECT COALESCE(MAX(generation),0)+1 FROM vr2_workflows WHERE task_id=$1",
        )
        .bind(task)
        .fetch_one(&mut **tx)
        .await?;
        let id = Uuid::new_v4();
        sqlx::query("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage,configuration) VALUES($1,$2,$3,'running',$4,$5)")
            .bind(id).bind(task).bind(generation).bind(stage.as_str())
            .bind(json!({"auto_publish":false,"auto_publish_platforms":[],"auto_dub_p2":false,"historical_adoption":true}))
            .execute(&mut **tx).await?;
        Self::insert_job(tx, id, task, generation, stage).await?;
        sqlx::query("UPDATE tasks SET status=$2::task_status,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1")
            .bind(task).bind(status).execute(&mut **tx).await?;
        Self::event(
            tx,
            id,
            None,
            "workflow.historical_adopted",
            json!({"stage":stage.as_str(),"generation":generation,"automatic_publish":false}),
        )
        .await?;
        Ok(id)
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
    pub(crate) async fn event(
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
                OR (state='awaiting' AND COALESCE(external_ticket,'') NOT LIKE 'native-batch:%' AND updated_at<now()-interval '48 hours'))
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
                    OR (state='awaiting' AND COALESCE(external_ticket,'') NOT LIKE 'native-batch:%' AND updated_at<now()-interval '48 hours'))
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
    /// A crashed worker consumes its claimed attempt, just like a normal
    /// stage failure. Terminalize only the current reversible stage after
    /// the last lease expired; paused/canceled/older generations are untouched.
    pub async fn reconcile_exhausted_jobs(&self) -> AppResult<u64> {
        let candidates=sqlx::query("SELECT j.id,j.task_id,j.workflow_id FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id JOIN tasks t ON t.id=j.task_id
            WHERE t.runtime_owner='rust' AND t.status<>'canceled'::task_status
              AND w.state='running' AND w.current_stage=j.stage AND w.generation=j.generation
              AND j.stage<>'publish' AND j.attempts>=j.max_attempts
              AND (j.state='pending' OR (j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=clock_timestamp())))
            ORDER BY j.updated_at,j.id LIMIT 100").fetch_all(&self.db.pool).await?;
        let mut changed = 0;
        for candidate in candidates {
            let id: Uuid = candidate.try_get("id")?;
            let task: Uuid = candidate.try_get("task_id")?;
            let flow: Uuid = candidate.try_get("workflow_id")?;
            let mut tx = self.db.pool.begin().await?;
            let active:Option<Uuid>=sqlx::query_scalar("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust' AND status<>'canceled'::task_status FOR UPDATE").bind(task).fetch_optional(&mut *tx).await?;
            if active.is_none() {
                continue;
            }
            let active:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_workflows WHERE id=$1 AND task_id=$2 AND state='running' FOR UPDATE").bind(flow).bind(task).fetch_optional(&mut *tx).await?;
            if active.is_none() {
                continue;
            }
            let row=sqlx::query("UPDATE vr2_jobs j SET state='failed',last_error='stage attempt budget exhausted after worker lease expiry',lease_token=NULL,lease_until=NULL,finished_at=now(),updated_at=now()
                WHERE j.id=$1 AND j.task_id=$2 AND j.workflow_id=$3 AND j.stage<>'publish' AND j.attempts>=j.max_attempts
                 AND (j.state='pending' OR (j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=clock_timestamp())))
                 AND EXISTS(SELECT 1 FROM vr2_workflows w WHERE w.id=j.workflow_id AND w.state='running' AND w.current_stage=j.stage AND w.generation=j.generation)
                RETURNING j.stage,j.attempts").bind(id).bind(task).bind(flow).fetch_optional(&mut *tx).await?;
            let Some(row) = row else {
                continue;
            };
            let stage: String = row.try_get("stage")?;
            let attempts: i32 = row.try_get("attempts")?;
            let error = "stage attempt budget exhausted after worker lease expiry";
            sqlx::query("UPDATE vr2_workflows SET state='failed',last_error=$2,updated_at=now() WHERE id=$1").bind(flow).bind(error).execute(&mut *tx).await?;
            sqlx::query("UPDATE tasks SET status='failed'::task_status,error_code='V2_WORKFLOW_FAILED',error_message=$2,updated_at=now() WHERE id=$1 AND status NOT IN ('published'::task_status,'canceled'::task_status)").bind(task).bind(error).execute(&mut *tx).await?;
            Self::event(&mut tx,flow,Some(id),"job.failed_attempt",json!({"stage":stage,"attempts":attempts,"state":"failed","reason":"lease_attempt_budget_exhausted"})).await?;
            tx.commit().await?;
            changed += 1;
        }
        Ok(changed)
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
                WHERE w.state='running' AND t.runtime_owner='rust' AND t.status <> 'canceled'::task_status
                  AND w.current_stage=j.stage AND w.generation=j.generation AND j.attempts<j.max_attempts
                  AND (
                     (j.state='pending' AND j.available_at<=now())
                     OR (j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=clock_timestamp()) AND j.stage <> 'publish')
                  )
                  AND (
                    j.stage <> 'download' OR (
                      (SELECT count(*) FROM vr2_jobs dj
                        JOIN vr2_workflows dw ON dw.id=dj.workflow_id
                        JOIN tasks dt ON dt.id=dj.task_id
                        WHERE dt.runtime_owner='rust' AND dt.status<>'canceled'::task_status
                          AND dw.current_stage=dj.stage AND dw.generation=dj.generation
                          AND dj.stage='download' AND dj.state='running'
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
                lease_until=clock_timestamp()+($2 * interval '1 second'),
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
             "UPDATE vr2_jobs SET lease_until=clock_timestamp()+($1 * interval '1 second'),updated_at=now() WHERE id=$2 AND lease_token=$3 AND state='running' AND lease_until>clock_timestamp() AND EXISTS(SELECT 1 FROM vr2_workflows w JOIN tasks t ON t.id=w.task_id WHERE w.id=vr2_jobs.workflow_id AND w.state='running' AND w.current_stage=vr2_jobs.stage AND w.generation=vr2_jobs.generation AND t.runtime_owner='rust' AND t.status<>'canceled'::task_status)"
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
            "SELECT state,lease_token,external_ticket,COALESCE(lease_until>clock_timestamp(),false) AS lease_active FROM vr2_jobs WHERE id=$1 FOR UPDATE",
        )
        .bind(item.id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| AppError::NotFound("job disappeared".into()))?;
        let job_state: String = job.try_get("state")?;
        let lease_token: Option<Uuid> = job.try_get("lease_token")?;
        if let StageOutcome::Awaiting { ticket } = &outcome {
            if ticket.starts_with("native-batch:")
                && job
                    .try_get::<Option<String>, _>("external_ticket")?
                    .as_deref()
                    == Some(ticket.as_str())
                && matches!(job_state.as_str(), "awaiting" | "unknown" | "succeeded")
            {
                tx.commit().await?;
                return Ok(());
            }
        }
        if job_state == "succeeded" {
            tx.commit().await?;
            return Ok(());
        }
        if job_state != "running"
            || lease_token != Some(item.lease_token)
            || !job.try_get::<bool, _>("lease_active")?
        {
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
        let p2_pending = if item.stage == Stage::Dubbing {
            sqlx::query_scalar::<_,bool>("SELECT EXISTS(SELECT 1 FROM render_executions WHERE job_id=$1 AND runtime_engine='rust-v2' AND mode='dub_burn_in')").bind(item.id).fetch_one(&mut **tx).await?
        } else {
            false
        };
        if p2_pending
            || (item.stage == Stage::Dubbing
                && result["mode"] == crate::media::dubbing_handoff::MODE)
        {
            if result["mode"] != crate::media::dubbing_handoff::MODE
                || result["remote_completed"] != true
            {
                return Err(AppError::Conflict(
                    "P2 requires exact completed renderer receipt".into(),
                ));
            }
            let execution = result["remote_execution_id"]
                .as_str()
                .and_then(|s| Uuid::parse_str(s).ok())
                .ok_or_else(|| AppError::Conflict("P2 execution identity missing".into()))?;
            let row=sqlx::query("SELECT render_spec,output_json,uploaded_asset_id FROM render_executions WHERE id=$1 AND job_id=$2 AND task_id=$3 AND workflow_id=$4 AND runtime_engine='rust-v2' AND state='completed' AND mode='dub_burn_in' FOR UPDATE").bind(execution).bind(item.id).bind(item.task_id).bind(item.workflow_id).fetch_optional(&mut **tx).await?.ok_or_else(||AppError::Conflict("P2 render receipt missing".into()))?;
            let stored: Value = row.try_get("output_json")?;
            let spec: Value = row.try_get("render_spec")?;
            let asset: Uuid = row.try_get("uploaded_asset_id")?;
            let key = result["storage_key"]
                .as_str()
                .ok_or_else(|| AppError::Conflict("P2 output key missing".into()))?;
            let expected = crate::remote_render::output_key(item.task_id, execution);
            let owned:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE id=$1 AND task_id=$2 AND kind::text='video_dubbed' AND storage_key=$3)").bind(asset).bind(item.task_id).bind(key).fetch_one(&mut **tx).await?;
            if stored != result || key != expected || !owned || result["asset_id"] != json!(asset) {
                return Err(AppError::Conflict("P2 output receipt differs".into()));
            }
            let request = &spec["request"];
            let input = request["input_key"]
                .as_str()
                .ok_or_else(|| AppError::Conflict("P2 input missing".into()))?;
            let subtitle = request["dubbing_source_srt_key"]
                .as_str()
                .ok_or_else(|| AppError::Conflict("P2 source subtitles missing".into()))?;
            let native = request["dubbing_model_job_id"]
                .as_str()
                .and_then(|s| Uuid::parse_str(s).ok())
                .ok_or_else(|| AppError::Conflict("P2 model job missing".into()))?;
            let written=sqlx::query("UPDATE operation_inbox SET dubbing_source_video_key=$2,dubbing_source_srt_key=$3,dubbing_output_video_key=$4,dubbing_model_job_id=$5,dubbing_model_status='succeeded',dubbing_run_created_at=now() WHERE id=$1 AND stage='dubbing' AND status='awaiting' AND dubbing_model_status IS NULL").bind(item.id).bind(input).bind(subtitle).bind(key).bind(native).execute(&mut **tx).await?.rows_affected();
            if written != 1 {
                return Err(AppError::Conflict("P2 completion ownership lost".into()));
            }
        }
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
        let recovery: Option<Value> = if item.stage == Stage::Download {
            sqlx::query_scalar("SELECT configuration->'source_recovery' FROM vr2_workflows WHERE id=$1")
                .bind(item.workflow_id).fetch_one(&mut **tx).await?
        } else { None };
        if let Some(recovery) = recovery {
            let proof: crate::ingest::SourceRecovery = serde_json::from_value(recovery)
                .map_err(|_| AppError::Conflict("invalid source recovery proof".into()))?;
            if result["storage_key"] != json!(proof.storage_key)
                || result["sha256"] != json!(proof.sha256)
                || result["size_bytes"] != json!(proof.size_bytes) {
                return Err(AppError::Conflict("source recovery receipt differs from original asset".into()));
            }
            sqlx::query("UPDATE vr2_workflows SET state='succeeded',updated_at=now() WHERE id=$1")
                .bind(item.workflow_id).execute(&mut **tx).await?;
            sqlx::query("UPDATE tasks SET status='downloaded'::task_status,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1")
                .bind(item.task_id).execute(&mut **tx).await?;
            Self::event(tx,item.workflow_id,Some(item.id),"workflow.source_recovered",json!({"asset_id":proof.asset_id})).await?;
            return Ok(());
        }
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
        if ticket.starts_with("native-batch:") {
            return Err(AppError::Conflict(
                "native batch settles only from original target receipts".into(),
            ));
        }
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
        if !matches!(
            peek.try_get::<String, _>("stage")?.as_str(),
            "render" | "dubbing"
        ) {
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
        let row = sqlx::query(
            "SELECT state,external_ticket,last_error FROM vr2_jobs WHERE id=$1 FOR UPDATE",
        )
        .bind(job_id)
        .fetch_one(&mut *tx)
        .await?;
        if peek.try_get::<String, _>("stage")? == "dubbing" {
            let valid:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM render_executions WHERE job_id=$1 AND id::text=$2 AND runtime_engine='rust-v2' AND mode='dub_burn_in' AND state='failed')").bind(job_id).bind(ticket).fetch_one(&mut *tx).await?;
            if !valid {
                return Err(AppError::Conflict("P2 failure receipt missing".into()));
            }
        }
        let state: String = row.try_get("state")?;
        let old_ticket: Option<String> = row.try_get("external_ticket")?;
        let message = error.chars().take(1000).collect::<String>();
        if old_ticket.as_deref() == Some(ticket)
            && state == "failed"
            && flow == "failed"
            && row.try_get::<Option<String>, _>("last_error")?.as_deref() == Some(message.as_str())
        {
            tx.commit().await?;
            return Ok(());
        }
        if old_ticket.as_deref() != Some(ticket) || state != "awaiting" || flow != "running" {
            return Err(AppError::Conflict(
                "external render no longer awaiting this ticket".into(),
            ));
        }
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
        let task:Option<Uuid>=sqlx::query_scalar("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust' AND status<>'canceled'::task_status FOR UPDATE").bind(item.task_id).fetch_optional(&mut *tx).await?;
        if task.is_none() {
            return Err(AppError::Conflict("stale stage failure ignored".into()));
        }
        let flow:Option<Uuid>=sqlx::query_scalar("SELECT id FROM vr2_workflows WHERE id=$1 AND task_id=$2 AND state='running' AND generation=$3 AND current_stage=$4 FOR UPDATE")
            .bind(item.workflow_id).bind(item.task_id).bind(item.generation).bind(item.stage.as_str()).fetch_optional(&mut *tx).await?;
        if flow.is_none() {
            return Err(AppError::Conflict("stale stage failure ignored".into()));
        }
        let row=sqlx::query("SELECT state,lease_token,attempts,max_attempts,COALESCE(lease_until>clock_timestamp(),false) AS lease_active FROM vr2_jobs WHERE id=$1 AND task_id=$2 AND workflow_id=$3 AND generation=$4 AND stage=$5 FOR UPDATE")
            .bind(item.id).bind(item.task_id).bind(item.workflow_id).bind(item.generation).bind(item.stage.as_str()).fetch_optional(&mut *tx).await?.ok_or_else(||AppError::Conflict("stale stage failure ignored".into()))?;
        let state: String = row.try_get("state")?;
        let owner: Option<Uuid> = row.try_get("lease_token")?;
        if state != "running"
            || owner != Some(item.lease_token)
            || !row.try_get::<bool, _>("lease_active")?
        {
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
    // Parent task and workflow are already locked. Lock the original awaiting
    // render job before its execution, matching the upload lock order. A
    // paused execution retains its evidence/fence and reserves a live slot;
    // only the resumed workflow may requeue it after the worker drains.
    async fn stop_remote_render(
        tx: &mut Transaction<'_, Postgres>,
        workflow: Uuid,
        terminal: bool,
    ) -> AppResult<()> {
        sqlx::query("SELECT id FROM operation_inbox WHERE workflow_id=$1 AND runtime_engine='rust-v2' AND stage IN ('render','dubbing') AND status='awaiting' ORDER BY id FOR UPDATE")
            .bind(workflow).fetch_all(&mut **tx).await?;
        sqlx::query("UPDATE render_executions e SET state=$2,lease_until=CASE WHEN e.lease_until>now() THEN e.lease_until ELSE NULL END,
          error_message=$3,finished_at=CASE WHEN $2='canceled' THEN now() ELSE NULL END,updated_at=now()
          FROM operation_inbox j,pipeline_runs w,tasks t
          WHERE e.runtime_engine='rust-v2' AND e.workflow_id=$1 AND e.state IN ('queued','running','uploading','paused')
          AND j.id=e.job_id AND j.runtime_engine='rust-v2' AND j.stage IN ('render','dubbing') AND j.status='awaiting'
          AND j.task_id=e.task_id AND j.workflow_id=e.workflow_id AND j.external_ticket=e.id::text
          AND w.id=e.workflow_id AND w.engine='rust-v2' AND w.task_id=e.task_id
          AND t.id=e.task_id AND t.runtime_owner='rust'")
          .bind(workflow).bind(if terminal {"canceled"} else {"paused"})
          .bind(if terminal {"workflow canceled"} else {"workflow paused"}).execute(&mut **tx).await?;
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
        let batch_parent:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE workflow_id=$1 AND external_ticket LIKE 'native-batch:%' AND state IN ('awaiting','unknown'))").bind(workflow_id).fetch_one(&mut *tx).await?;
        if batch_parent {
            return Err(AppError::Conflict(
                "native batch publication already admitted; verify individual target receipts"
                    .into(),
            ));
        }
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
        Self::stop_remote_render(&mut tx, workflow_id, false).await?;
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
        let mut tx=self.db.pool.begin().await?;
        Self::lock_control_task(&mut tx,task_id).await?;
        Self::lock_control_ledgers(&mut tx).await?;
        Self::stop_task_locked(&mut tx,task_id,false).await?;
        tx.commit().await?;
        Ok(())
    }

    const STOPPABLE:&str = "t.status::text IN ('created','ingested','downloaded','audio_extracted','asr_done','translated','subtitle_ready','rendered','ready_for_review','approved')";
    // Python's bulk stop includes failed tasks with live subtitle/render
    // jobs. Also cover the equivalent reversible native operation.
    const FAILED_LIVE:&str = "t.status::text='failed' AND (
        EXISTS(SELECT 1 FROM subtitle_jobs j WHERE j.task_id=t.id AND j.status::text IN ('queued','running'))
        OR EXISTS(SELECT 1 FROM render_jobs j WHERE j.task_id=t.id AND j.status::text IN ('queued','running'))
        OR EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
          WHERE j.task_id=t.id AND w.task_id=t.id AND w.state IN ('running','paused')
          AND w.current_stage=j.stage AND w.generation=j.generation
          AND j.stage IN ('download','subtitle','render','dubbing') AND j.state IN ('pending','running','awaiting')))";

    async fn lock_control_task(tx:&mut Transaction<'_,Postgres>,task:Uuid)->AppResult<()>{
        sqlx::query_scalar::<_,Uuid>("SELECT id FROM tasks WHERE id=$1 AND runtime_owner='rust' FOR UPDATE")
            .bind(task).fetch_optional(&mut **tx).await?
            .ok_or_else(||AppError::NotFound("V2 task missing".into()))?;
        Ok(())
    }
    async fn lock_control_ledgers(tx:&mut Transaction<'_,Postgres>)->AppResult<()>{
        sqlx::query("LOCK TABLE pipeline_runs,operation_inbox,subtitle_jobs,render_jobs,render_executions,dubbing_jobs,publish_jobs,publish_batches,outbox_events IN SHARE ROW EXCLUSIVE MODE NOWAIT")
            .execute(&mut **tx).await.map_err(|e|if e.as_database_error().and_then(|e|e.code()).as_deref()==Some("55P03"){
                AppError::Conflict("another worker is updating task control ledgers; retry".into())
            }else{e.into()})?;
        Ok(())
    }

    /// All eligible changes commit together. A business precondition failure
    /// leaves that task unchanged; database errors roll back the whole batch.
    pub(crate) async fn bulk_task_control(&self,stop:bool)->AppResult<(usize,usize)>{
        let mut tx=self.db.pool.begin().await?;
        let predicate=if stop{format!("({} OR ({}))",Self::STOPPABLE,Self::FAILED_LIVE)}
            else{"t.status::text='canceled' AND t.stopped_status IS NOT NULL".into()};
        let sql=format!("SELECT t.id FROM tasks t WHERE t.runtime_owner='rust' AND {predicate} ORDER BY t.id LIMIT 1001 FOR UPDATE OF t");
        let ids:Vec<Uuid>=sqlx::query_scalar(&sql).fetch_all(&mut *tx).await?;
        if ids.len()>1000{return Err(AppError::Conflict("more than 1000 V2 tasks selected; split the operation".into()))}
        if !ids.is_empty(){Self::lock_control_ledgers(&mut tx).await?;}
        let mut changed=0;
        for id in &ids{
            let result=if stop{Self::stop_task_locked(&mut tx,*id,true).await}
                else{Self::resume_task_locked(&mut tx,*id).await};
            match result{
                Ok(true)=>changed+=1,
                Ok(false)=>{},
                Err(AppError::Conflict(reason))=>tracing::debug!(task_id=%id,%reason,"bulk task control deferred"),
                Err(e)=>return Err(e),
            }
        }
        tx.commit().await?;
        Ok((ids.len(),changed))
    }

    async fn task_control_fence(tx:&mut Transaction<'_,Postgres>,task:Uuid)->AppResult<()>{
        let blocked:bool=sqlx::query_scalar("SELECT
          EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND stage IN ('publish','sau_publish') AND state IN ('pending','queued','running','awaiting','dispatching','accepted','unknown'))
          OR EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND (state::text IN ('draft','submitting','unknown') OR lease_owner IS NOT NULL OR lease_until IS NOT NULL OR upload_active
            OR (state::text='submitted' AND NULLIF(external_id,'') IS NULL AND NULLIF(external_url,'') IS NULL AND NULLIF(bvid,'') IS NULL AND NULLIF(aid,'') IS NULL)))
          OR EXISTS(SELECT 1 FROM publish_batches WHERE task_id=$1 AND state NOT IN ('succeeded','failed','partial_failed'))
          OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE task_id=$1 AND state<>'published')
          OR EXISTS(SELECT 1 FROM vr2_social_publish_intents WHERE task_id=$1 AND state NOT IN ('submitted','published','failed'))
          OR EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND state='unknown')")
            .bind(task).fetch_one(&mut **tx).await?;
        if blocked{return Err(AppError::Conflict("unsettled publication or external workflow prevents task control".into()))}
        Ok(())
    }
    // Only the unique current native workflow may be paused or resumed.
    // Terminal history is not an active workflow and must remain untouched.
    async fn control_flow(tx:&mut Transaction<'_,Postgres>,task:Uuid)->AppResult<Option<(Uuid,String,String)>>{
        let rows=sqlx::query("SELECT id,state,current_stage,generation FROM vr2_workflows WHERE task_id=$1
            AND state IN ('running','paused','unknown') ORDER BY generation DESC FOR UPDATE")
            .bind(task).fetch_all(&mut **tx).await?;
        if rows.len()>1{return Err(AppError::Conflict("multiple active native workflows require reconciliation".into()))}
        let Some(row)=rows.first() else{return Ok(None)};
        let id:Uuid=row.try_get("id")?;
        let state:String=row.try_get("state")?;
        let stage:String=row.try_get("current_stage")?;
        let generation:i32=row.try_get("generation")?;
        let current:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE workflow_id=$1 AND task_id=$2
          AND stage=$3 AND generation=$4 AND state IN ('pending','running','awaiting'))
          AND NOT EXISTS(SELECT 1 FROM vr2_workflows WHERE task_id=$2 AND generation>$4)")
            .bind(id).bind(task).bind(&stage).bind(generation).fetch_one(&mut **tx).await?;
        if state=="unknown" || !current || !matches!(stage.as_str(),"download"|"subtitle"|"render"|"dubbing"){
            return Err(AppError::Conflict("native workflow lacks a current reversible job".into()))
        }
        Ok(Some((id,state,stage)))
    }

    async fn stop_task_locked(tx:&mut Transaction<'_,Postgres>,task_id:Uuid,bulk:bool)->AppResult<bool>{
        let (status,stopped):(String,Option<String>)=sqlx::query_as("SELECT upper(status::text),upper(stopped_status::text) FROM tasks WHERE id=$1 AND runtime_owner='rust'")
            .bind(task_id).fetch_one(&mut **tx).await?;
        if status=="CANCELED" && stopped.is_some(){return Ok(false)}
        let predicate=if bulk{format!("({} OR ({}))",Self::STOPPABLE,Self::FAILED_LIVE)}else{Self::STOPPABLE.into()};
        let allowed:bool=sqlx::query_scalar(&format!("SELECT {predicate} FROM tasks t WHERE t.id=$1"))
            .bind(task_id).fetch_one(&mut **tx).await?;
        if !allowed || stopped.is_some(){return Err(AppError::Conflict("V2 task cannot be stopped in its current state".into()))}
        Self::task_control_fence(tx,task_id).await?;
        if let Some((id,_,_))=Self::control_flow(tx,task_id).await?{
            Self::stop_remote_render(tx,id,false).await?;
            sqlx::query("UPDATE vr2_workflows SET state='paused',updated_at=now() WHERE id=$1")
                .bind(id).execute(&mut **tx).await?;
            Self::event(tx,id,None,"workflow.legacy_task_stopped",json!({"from_status":status})).await?;
        }
        sqlx::query("UPDATE tasks SET stopped_status=lower($1)::task_status,status='canceled'::task_status,updated_at=now() WHERE id=$2")
            .bind(status).bind(task_id).execute(&mut **tx).await?;
        Ok(true)
    }

    /// Restore a checkpoint, not an old terminal workflow.
    pub async fn resume_task_compat(&self,task_id:Uuid)->AppResult<()>{
        let mut tx=self.db.pool.begin().await?;
        Self::lock_control_task(&mut tx,task_id).await?;
        Self::lock_control_ledgers(&mut tx).await?;
        Self::resume_task_locked(&mut tx,task_id).await?;
        tx.commit().await?;
        Ok(())
    }
    async fn resume_task_locked(tx:&mut Transaction<'_,Postgres>,task_id:Uuid)->AppResult<bool>{
        let (status,stopped):(String,Option<String>)=sqlx::query_as("SELECT upper(status::text),upper(stopped_status::text) FROM tasks WHERE id=$1 AND runtime_owner='rust'")
            .bind(task_id).fetch_one(&mut **tx).await?;
        let Some(mut restored)=stopped.filter(|_|status=="CANCELED") else{
            return Err(AppError::Conflict("V2 task was not stopped by task controls".into()))
        };
        Self::task_control_fence(tx,task_id).await?;
        if let Some((id,state,stage))=Self::control_flow(tx,task_id).await?{
            if state!="paused"{return Err(AppError::Conflict("native workflow is not paused".into()))}
            // Remote render explicitly excludes FAILED. A verified current
            // paused operation must return to its actual stage checkpoint.
            if restored=="FAILED"{
                restored=match stage.as_str(){
                    "download"=>"CREATED","subtitle"=>"DOWNLOADED","render"=>"SUBTITLE_READY","dubbing"=>"RENDERED",
                    _=>return Err(AppError::Conflict("failed task has no reversible paused stage".into()))
                }.into();
            }
            sqlx::query("UPDATE vr2_workflows SET state='running',updated_at=now() WHERE id=$1")
                .bind(id).execute(&mut **tx).await?;
            Self::event(tx,id,None,"workflow.legacy_task_resumed",json!({"restored_status":restored})).await?;
        }else{
            // Historical live rows cannot be relaunched safely without a
            // native owner. A quiescent terminal flow needs no revival.
            if !crate::task_ownership::blockers(tx,task_id).await?.is_empty(){
                return Err(AppError::Conflict("stopped task has unsettled historical work; reconcile before resuming".into()))
            }
        }
        sqlx::query("UPDATE tasks SET status=lower($1)::task_status,stopped_status=NULL,updated_at=now() WHERE id=$2")
            .bind(restored).bind(task_id).execute(&mut **tx).await?;
        Ok(true)
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
        let batch_parent:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE workflow_id=$1 AND external_ticket LIKE 'native-batch:%' AND state IN ('awaiting','unknown'))").bind(workflow_id).fetch_one(&mut *tx).await?;
        if batch_parent {
            return Err(AppError::Conflict(
                "native batch publication already admitted; verify individual target receipts"
                    .into(),
            ));
        }
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
            Self::stop_remote_render(&mut tx, workflow_id, true).await?;
            sqlx::query("UPDATE vr2_jobs SET state='canceled',lease_token=NULL,lease_until=NULL,updated_at=now() WHERE workflow_id=$1 AND state IN ('pending','running','awaiting')")
                .bind(workflow_id).execute(&mut *tx).await?;
            sqlx::query("UPDATE tasks SET status='canceled'::task_status,stopped_status=NULL,updated_at=now() WHERE id=$1 AND status<>'published'::task_status")
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
        let batch_parent:bool=sqlx::query_scalar("SELECT COALESCE(external_ticket LIKE 'native-batch:%',false) FROM vr2_jobs WHERE id=$1").bind(job_id).fetch_one(&mut *tx).await?;
        if batch_parent {
            return Err(AppError::Conflict(
                "resolve individual original publication receipts instead".into(),
            ));
        }
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
    #[tokio::test]
    async fn exhausted_lease_and_asset_real_postgres() {
        let Ok(url) = std::env::var("VIDEOROLL_TEST_EXHAUSTED_LEASE_DB_URL") else {
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
        let scheduler = Scheduler::new(db.clone(), Duration::from_secs(300));
        async fn seed(
            db: &Database,
            stage: Stage,
            flow_state: &str,
            attempts: i32,
            max: i32,
            expired: bool,
        ) -> WorkItem {
            let (task, flow, id, lease) = (
                Uuid::new_v4(),
                Uuid::new_v4(),
                Uuid::new_v4(),
                Uuid::new_v4(),
            );
            let operation_key = format!("exhausted-lease:{id}");
            sqlx::query("INSERT INTO tasks(id,source_type,source_license,status,runtime_owner,priority,retry_count) VALUES($1,'local','own','downloaded','rust',0,0)").bind(task).execute(&db.pool).await.unwrap();
            sqlx::query("INSERT INTO vr2_workflows(id,task_id,generation,state,current_stage) VALUES($1,$2,1,$3,$4)").bind(flow).bind(task).bind(flow_state).bind(stage.as_str()).execute(&db.pool).await.unwrap();
            sqlx::query("INSERT INTO vr2_jobs(id,workflow_id,task_id,stage,generation,operation_key,state,attempts,max_attempts,lease_token,lease_until,payload) VALUES($1,$2,$3,$4,1,$5,'running',$6,$7,$8,now()+($9*interval '1 second'),'{}')").bind(id).bind(flow).bind(task).bind(stage.as_str()).bind(&operation_key).bind(attempts).bind(max).bind(lease).bind(if expired{-60i64}else{300}).execute(&db.pool).await.unwrap();
            WorkItem {
                id,
                workflow_id: flow,
                task_id: task,
                stage,
                generation: 1,
                operation_key,
                lease_token: lease,
                attempts,
                max_attempts: max,
                payload: json!({}),
            }
        }
        async fn state(db: &Database, id: Uuid) -> String {
            sqlx::query_scalar("SELECT state FROM vr2_jobs WHERE id=$1")
                .bind(id)
                .fetch_one(&db.pool)
                .await
                .unwrap()
        }
        let expired = seed(&db, Stage::Subtitle, "running", 5, 5, true).await;
        let paused = seed(&db, Stage::Subtitle, "paused", 5, 5, true).await;
        let live = seed(&db, Stage::Render, "running", 5, 5, false).await;
        let retry = seed(&db, Stage::Download, "running", 4, 5, true).await;
        let publish = seed(&db, Stage::Publish, "running", 1, 1, true).await;
        let replacement = scheduler.claim().await.unwrap().unwrap();
        assert_eq!(replacement.id, retry.id);
        assert_eq!(replacement.attempts, 5);
        assert_ne!(replacement.lease_token, retry.lease_token);
        assert!(
            scheduler.claim().await.unwrap().is_none(),
            "exhausted stage must not be reclaimed"
        );
        assert!(scheduler.fail(&expired, "late failure").await.is_err());
        assert!(scheduler.fail(&paused, "paused failure").await.is_err());
        let a = scheduler.clone();
        let b = scheduler.clone();
        let (a, b) = tokio::join!(a.reconcile_exhausted_jobs(), b.reconcile_exhausted_jobs());
        assert_eq!(a.unwrap() + b.unwrap(), 1);
        assert_eq!(state(&db, expired.id).await, "failed");
        assert_eq!(state(&db, paused.id).await, "running");
        assert_eq!(state(&db, live.id).await, "running");
        assert_eq!(state(&db, publish.id).await, "running");
        let event_count:i64=sqlx::query_scalar("SELECT count(*) FROM workflow_events WHERE job_id=$1 AND details->>'reason'='lease_attempt_budget_exhausted'").bind(expired.id).fetch_one(&db.pool).await.unwrap();
        assert_eq!(event_count, 1);
        assert_eq!(scheduler.reconcile_exhausted_jobs().await.unwrap(), 0);
        assert!(
            scheduler
                .fail(&retry, "previous worker failed")
                .await
                .is_err()
        );
        let dir = tempfile::tempdir().unwrap();
        let store = crate::storage::AssetRegistry::new(db.clone(), dir.path().to_path_buf());
        let key = "fixture/output.mp4";
        let path = store.resolve_existing(key).await.unwrap();
        tokio::fs::create_dir_all(path.parent().unwrap())
            .await
            .unwrap();
        tokio::fs::write(&path, b"original bytes").await.unwrap();
        assert!(
            crate::media::ffmpeg::ensure_task_asset(
                &db,
                &store,
                expired.task_id,
                &expired,
                "video_final",
                key
            )
            .await
            .is_err()
        );
        assert!(
            crate::media::ffmpeg::ensure_task_asset(
                &db,
                &store,
                paused.task_id,
                &paused,
                "video_final",
                key
            )
            .await
            .is_err()
        );
        let asset = crate::media::ffmpeg::ensure_task_asset(
            &db,
            &store,
            live.task_id,
            &live,
            "video_final",
            key,
        )
        .await
        .unwrap();
        assert_eq!(
            crate::media::ffmpeg::ensure_task_asset(
                &db,
                &store,
                live.task_id,
                &live,
                "video_final",
                key
            )
            .await
            .unwrap(),
            asset
        );
        let wrong = WorkItem {
            lease_token: Uuid::new_v4(),
            ..live.clone()
        };
        assert!(
            crate::media::ffmpeg::ensure_task_asset(
                &db,
                &store,
                live.task_id,
                &wrong,
                "video_final",
                key
            )
            .await
            .is_err()
        );
        // Stale workers are rejected before the file rename, even when
        // the old canonical object already has live references.
        assert!(
            crate::media::ffmpeg::write_task_asset(
                &db,
                &store,
                &wrong,
                "video_final",
                key,
                b"stale overwrite"
            )
            .await
            .is_err()
        );
        assert_eq!(tokio::fs::read(&path).await.unwrap(), b"original bytes");
        assert!(
            crate::media::ffmpeg::write_task_asset(
                &db,
                &store,
                &live,
                "video_final",
                key,
                b"conflicting bytes"
            )
            .await
            .is_err()
        );
        assert_eq!(tokio::fs::read(&path).await.unwrap(), b"original bytes");
        assert_eq!(
            crate::media::ffmpeg::write_task_asset(
                &db,
                &store,
                &live,
                "video_final",
                key,
                b"original bytes"
            )
            .await
            .unwrap(),
            asset
        );
        let new_key = "fixture/new.mp4";
        assert!(
            crate::media::ffmpeg::write_task_asset(
                &db,
                &store,
                &wrong,
                "video_final",
                new_key,
                b"stale"
            )
            .await
            .is_err()
        );
        assert!(!store.resolve_existing(new_key).await.unwrap().exists());
        assert!(
            crate::media::ffmpeg::write_task_asset(
                &db,
                &store,
                &live,
                "video_final",
                new_key,
                b"valid"
            )
            .await
            .is_ok()
        );
        assert_eq!(
            tokio::fs::read(store.resolve_existing(new_key).await.unwrap())
                .await
                .unwrap(),
            b"valid"
        );
        tokio::fs::write(&path, b"different bytes").await.unwrap();
        assert!(
            crate::media::ffmpeg::ensure_task_asset(
                &db,
                &store,
                live.task_id,
                &live,
                "video_final",
                key
            )
            .await
            .is_err()
        );
        tokio::fs::write(&path, b"original bytes").await.unwrap();
        sqlx::query("UPDATE storage_objects SET state='deleting' WHERE storage_key=$1")
            .bind(key)
            .execute(&db.pool)
            .await
            .unwrap();
        assert!(
            crate::media::ffmpeg::ensure_task_asset(
                &db,
                &store,
                live.task_id,
                &live,
                "video_final",
                key
            )
            .await
            .is_err()
        );
        let count: i64 = sqlx::query_scalar("SELECT count(*) FROM assets WHERE storage_key=$1")
            .bind(key)
            .fetch_one(&db.pool)
            .await
            .unwrap();
        assert_eq!(count, 1);
        assert!(scheduler.heartbeat(&live).await.unwrap());
        println!(
            "PASS exhausted lease budget and concurrent reconciliation; paused and live retained; stale failure rejected; publish kept for separate quarantine; asset lease/hash/GC fencing"
        );
    }
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
