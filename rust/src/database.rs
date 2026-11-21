use crate::{
    AppError, AppResult,
    domain::{AssetInfo, TaskInfo, WorkflowInfo},
};
use serde_json::Value;
use sqlx::{PgPool, postgres::PgPoolOptions};
use uuid::Uuid;

#[derive(sqlx::FromRow)]
struct EventRow {
    id: Uuid,
    job_id: Option<Uuid>,
    kind: String,
    details: Value,
    created_at: chrono::DateTime<chrono::Utc>,
}

#[derive(Clone)]
pub struct Database {
    pub pool: PgPool,
}
impl Database {
    pub async fn connect(url: &str) -> AppResult<Self> {
        let pool = PgPoolOptions::new()
            .max_connections(12)
            .acquire_timeout(std::time::Duration::from_secs(10))
            .connect(url)
            .await?;
        Ok(Self { pool })
    }
    /// Explicit migration. Never auto-migrate a production v1 database on API startup.
    pub async fn migrate(&self) -> AppResult<()> {
        sqlx::migrate!("./migrations").run(&self.pool).await?;
        Ok(())
    }
    pub async fn task(&self, id: Uuid) -> AppResult<TaskInfo> {
        sqlx::query_as::<_, TaskInfo>(
            "SELECT id, source_type::text, source_url, status::text, error_code, error_message, priority, created_at, updated_at FROM tasks WHERE id=$1"
        ).bind(id).fetch_optional(&self.pool).await?
            .ok_or_else(|| AppError::NotFound(format!("task {id}")))
    }
    pub async fn list_tasks(&self, limit: i64) -> AppResult<Vec<TaskInfo>> {
        Ok(sqlx::query_as::<_, TaskInfo>(
            "SELECT id, source_type::text, source_url, status::text, error_code, error_message, priority, created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT $1"
        ).bind(limit.clamp(1, 200)).fetch_all(&self.pool).await?)
    }
    pub async fn list_assets(&self, task_id: Uuid) -> AppResult<Vec<AssetInfo>> {
        Ok(sqlx::query_as::<_,AssetInfo>(
            "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at FROM assets WHERE task_id=$1 ORDER BY created_at DESC"
        ).bind(task_id).fetch_all(&self.pool).await?)
    }
    pub async fn asset(&self, id: Uuid) -> AppResult<AssetInfo> {
        sqlx::query_as::<_,AssetInfo>(
            "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at FROM assets WHERE id=$1"
        ).bind(id).fetch_optional(&self.pool).await?
            .ok_or_else(||AppError::NotFound("asset not found".into()))
    }
    pub async fn workflows(&self, task_id: Uuid) -> AppResult<Vec<WorkflowInfo>> {
        Ok(sqlx::query_as::<_, WorkflowInfo>(
            "SELECT id, task_id, generation, state, current_stage, configuration, last_error, created_at, updated_at FROM vr2_workflows WHERE task_id=$1 ORDER BY generation DESC"
        ).bind(task_id).fetch_all(&self.pool).await?)
    }
    pub async fn job_events(&self, workflow_id: Uuid) -> AppResult<Vec<Value>> {
        let rows: Vec<EventRow> = sqlx::query_as(
            "SELECT id, job_id, kind, details, created_at FROM vr2_events WHERE workflow_id=$1 ORDER BY created_at ASC LIMIT 500"
        ).bind(workflow_id).fetch_all(&self.pool).await?;
        Ok(rows
            .into_iter()
            .map(|EventRow{id, job_id, kind, details, created_at}| {
                serde_json::json!({
                    "id":id,"job_id":job_id,"kind":kind,"details":details,"created_at":created_at
                })
            })
            .collect())
    }
}
