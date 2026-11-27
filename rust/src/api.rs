use crate::{
    AppError, AppResult, Config, Database, auth,
    domain::{TaskInfo, WorkflowInfo},
    ingest::validate_youtube_url,
    scheduler::Scheduler,
    storage::AssetRegistry,
};
use axum::{
    Json, Router,
    extract::{ConnectInfo, Path, Query, State},
    http::{HeaderMap, Request, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{net::SocketAddr, sync::Arc, time::Duration};
use subtle::ConstantTimeEq;
use uuid::Uuid;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub db: Database,
    pub scheduler: Scheduler,
    pub storage: AssetRegistry,
}
#[derive(Debug, Deserialize)]
struct ListQuery {
    limit: Option<i64>,
}
#[derive(Debug, Deserialize)]
struct StartRequest {
    task_id: Uuid,
    #[serde(default)]
    configuration: Value,
}
#[derive(Debug, Deserialize)]
struct ImportRequest {
    url: String,
    priority: Option<i32>,
    source_license: Option<String>,
    #[serde(default)]
    configuration: Value,
}
#[derive(Debug, Deserialize)]
struct UnknownResolution {
    accepted: bool,
    external_id: Option<String>,
    evidence: String,
}
#[derive(Debug, Deserialize)]
struct ExternalCompleted {
    ticket: String,
    result: Value,
}
#[derive(Debug, Deserialize)]
struct HoldRequest {
    storage_key: String,
    seconds: u64,
}
#[derive(Debug, Serialize)]
struct StartedResponse {
    task: TaskInfo,
    workflow: Option<WorkflowInfo>,
    deduped: bool,
}

#[derive(Debug, Deserialize)]
pub(crate) struct PasswordRequest {
    password: String,
}
pub(crate) async fn admin_status(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    Ok(Json(
        auth::auth_status(&headers, &state.db, &state.config).await?,
    ))
}
pub(crate) async fn admin_login(
    State(state): State<AppState>,
    headers: HeaderMap,
    ConnectInfo(address): ConnectInfo<SocketAddr>,
    Json(body): Json<PasswordRequest>,
) -> AppResult<(HeaderMap, Json<Value>)> {
    let source = crate::trusted_proxies::caller_identity(&headers, Some(address)).await;
    let secret = state
        .config
        .legacy_internal_secret
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("native admin login not configured".into()))?;
    let stored = auth::password_hash(&state.db).await?;
    auth::authenticate_password(&state.db, &source, body.password, &stored).await?;
    let cookie = auth::mint_cookie(secret, &stored, chrono::Utc::now().timestamp());
    let mut headers = HeaderMap::new();
    headers.insert(
        header::SET_COOKIE,
        auth::login_cookie_header(&state.config, &cookie)?,
    );
    Ok((headers, Json(json!({"password_set":true,"trusted":true}))))
}
pub(crate) async fn admin_logout(State(state): State<AppState>) -> (HeaderMap, Json<Value>) {
    let mut headers = HeaderMap::new();
    headers.insert(
        header::SET_COOKIE,
        auth::logout_cookie_header(&state.config),
    );
    (headers, Json(json!({"trusted":false})))
}
pub(crate) async fn admin_setup(
    State(state): State<AppState>,
    headers: HeaderMap,
    ConnectInfo(address): ConnectInfo<SocketAddr>,
    Json(body): Json<PasswordRequest>,
) -> AppResult<(StatusCode, Json<Value>)> {
    let source=crate::trusted_proxies::caller_identity(&headers,Some(address)).await;
    let permit=auth::hash_permit()?;
    let attempt=auth::AuthAttempt::begin(&state.db,auth::AuthEndpoint::Setup,&source).await?;
    let Some(secret)=state.config.bootstrap_secret.as_deref() else {
        return Err(attempt.failure(&state.db,"bootstrap_unavailable",AppError::Unavailable("bootstrap secret not configured".into())).await)
    };
    let candidate = headers
        .get("x-videoroll-admin-bootstrap")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !bool::from(secret.as_bytes().ct_eq(candidate.as_bytes())) {
        return Err(attempt.failure(&state.db,"invalid_bootstrap_secret",AppError::Unauthorized).await);
    }
    let password=body.password;
    let encoded=match tokio::task::spawn_blocking(move||{
        let _permit=permit;
        auth::encode_password(&password)
    }).await.map_err(|_|AppError::AuthenticationUnavailable)? {
        Ok(encoded)=>encoded,
        Err(error)=>return Err(attempt.failure(&state.db,"invalid_password",error).await),
    };
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES('admin.auth','{}'::jsonb,1) ON CONFLICT(key) DO NOTHING")
        .execute(&mut *tx).await?;
    use sqlx::Row;
    let row = sqlx::query("SELECT value_json FROM app_settings WHERE key='admin.auth' FOR UPDATE")
        .fetch_one(&mut *tx)
        .await?;
    let mut stored: Value = row.try_get("value_json")?;
    if stored.get("bootstrap_consumed").and_then(Value::as_bool) == Some(true)
        || stored
            .get("password_hash")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
    {
        tx.rollback().await?;
        return Err(attempt.failure(&state.db,"password_already_set",AppError::Conflict("admin setup already completed".into())).await);
    }
    stored["bootstrap_consumed"] = json!(true);
    stored["password_hash"] = json!(encoded);
    sqlx::query("UPDATE app_settings SET value_json=$1,version=version+1,updated_at=now() WHERE key='admin.auth'")
        .bind(stored).execute(&mut *tx).await?;
    attempt.succeed(&mut tx).await?;
    tx.commit().await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({"password_set":true,"trusted":false})),
    ))
}
async fn authorize(
    State(state): State<AppState>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    let provided = request
        .headers()
        .get("x-videoroll-v2-internal-token")
        .or_else(|| request.headers().get("authorization"))
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let candidate = provided.strip_prefix("Bearer ").unwrap_or(provided);
    if bool::from(
        state
            .config
            .internal_token
            .as_bytes()
            .ct_eq(candidate.as_bytes()),
    ) || auth::request_authenticated(request.headers(), &state.db, &state.config).await
    {
        return next.run(request).await;
    }
    AppError::Unauthorized.into_response()
}
async fn health() -> impl IntoResponse {
    Json(json!({"status":"ok","service":"videoroll-v2"}))
}
async fn readiness(State(state): State<AppState>) -> AppResult<impl IntoResponse> {
    sqlx::query("SELECT 1").execute(&state.db.pool).await?;
    Ok(Json(json!({"database":"ok"})))
}
async fn list_tasks(
    State(state): State<AppState>,
    Query(q): Query<ListQuery>,
) -> AppResult<Json<Vec<TaskInfo>>> {
    Ok(Json(state.db.list_tasks(q.limit.unwrap_or(50)).await?))
}
async fn get_task(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<TaskInfo>> {
    Ok(Json(state.db.task(id).await?))
}
async fn start_workflow(
    State(state): State<AppState>,
    Json(payload): Json<StartRequest>,
) -> AppResult<Json<WorkflowInfo>> {
    let workflow = state
        .scheduler
        .start(payload.task_id, payload.configuration)
        .await?;
    Ok(Json(workflow))
}
async fn import_youtube(
    State(state): State<AppState>,
    Json(payload): Json<ImportRequest>,
) -> AppResult<(StatusCode, Json<StartedResponse>)> {
    validate_youtube_url(&payload.url)?;
    let (task, workflow, deduped) = state
        .scheduler
        .create_auto_youtube(
            &payload.url,
            payload.source_license.as_deref().unwrap_or("unknown"),
            payload.priority.unwrap_or(0),
            payload.configuration,
        )
        .await?;
    Ok((
        if deduped {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        },
        Json(StartedResponse {
            task,
            workflow,
            deduped,
        }),
    ))
}
async fn workflows_for_task(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<WorkflowInfo>>> {
    Ok(Json(state.db.workflows(id).await?))
}
async fn workflow(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<WorkflowInfo>> {
    Ok(Json(state.scheduler.workflow(id).await?))
}
async fn workflow_jobs(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<Value>>> {
    Ok(Json(state.scheduler.jobs(id).await?))
}
async fn workflow_events(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Vec<Value>>> {
    Ok(Json(state.db.job_events(id).await?))
}
async fn pause_workflow(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<WorkflowInfo>> {
    Ok(Json(state.scheduler.pause(id).await?))
}
async fn cancel_workflow(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<WorkflowInfo>> {
    Ok(Json(state.scheduler.cancel(id).await?))
}
async fn resolve_unknown(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(payload): Json<UnknownResolution>,
) -> AppResult<Json<WorkflowInfo>> {
    Ok(Json(
        state
            .scheduler
            .resolve_unknown_publish(
                id,
                payload.accepted,
                payload.external_id.as_deref(),
                &payload.evidence,
            )
            .await?,
    ))
}
async fn resume_workflow(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<WorkflowInfo>> {
    Ok(Json(state.scheduler.resume(id).await?))
}
async fn external_completed(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(payload): Json<ExternalCompleted>,
) -> AppResult<Json<Value>> {
    state
        .scheduler
        .complete_external(id, &payload.ticket, payload.result)
        .await?;
    Ok(Json(json!({"status":"committed"})))
}
async fn asset_hold(
    State(state): State<AppState>,
    Json(payload): Json<HoldRequest>,
) -> AppResult<Json<Value>> {
    state
        .storage
        .hold(&payload.storage_key, Duration::from_secs(payload.seconds))
        .await?;
    Ok(Json(json!({"status":"held"})))
}
async fn storage_stats(State(state): State<AppState>) -> AppResult<Json<Value>> {
    use sqlx::Row;
    let row=sqlx::query(
        "SELECT count(*) AS objects, COALESCE(sum(size_bytes),0)::bigint AS total_bytes, count(*) FILTER(WHERE state='live') AS live, count(*) FILTER(WHERE state='deleted') AS deleted FROM storage_objects"
    ).fetch_one(&state.db.pool).await?;
    Ok(Json(json!({
        "objects":row.get::<i64,_>("objects"),
        "total_bytes":row.get::<i64,_>("total_bytes"),
        "live":row.get::<i64,_>("live"),
        "deleted":row.get::<i64,_>("deleted")
    })))
}
pub fn router(state: AppState) -> Router {
    let protected = Router::new()
        .merge(crate::assets::router())
        .merge(crate::realtime::router())
        .merge(crate::sources::router())
        .merge(crate::telemetry::router())
        .merge(crate::parity::router())
        .merge(crate::rag::router())
        .merge(crate::playout::router())
        .merge(crate::bili_subtitles::router())
        .merge(crate::settings::router())
        .merge(crate::remote_render::admin_router())
        .route("/tasks", get(list_tasks))
        .route("/tasks/{id}", get(get_task))
        .route("/tasks/{id}/workflows", get(workflows_for_task))
        .route("/auto/youtube", post(import_youtube))
        .route("/workflows/start", post(start_workflow))
        .route("/workflows/{id}", get(workflow))
        .route("/workflows/{id}/jobs", get(workflow_jobs))
        .route("/workflows/{id}/events", get(workflow_events))
        .route("/workflows/{id}/pause", post(pause_workflow))
        .route("/workflows/{id}/cancel", post(cancel_workflow))
        .route("/jobs/{id}/resolve-unknown", post(resolve_unknown))
        .route("/workflows/{id}/resume", post(resume_workflow))
        .route("/jobs/{id}/complete", post(external_completed))
        .route("/storage/stats", get(storage_stats))
        .route("/storage/hold", post(asset_hold))
        .route_layer(middleware::from_fn_with_state(state.clone(), authorize));
    let legacy_protected = crate::legacy_contracts::core_router()
        .merge(crate::browser_gateway_v1::router(state.clone()))
        .route_layer(middleware::from_fn_with_state(state.clone(), authorize));
    // Login endpoints must remain public so the original React AuthGate can
    // establish a V2 session before entering the native V1-contract surface.
    let legacy = Router::new()
        .merge(crate::realtime::router())
        .route("/auth/status", get(admin_status))
        .route("/auth/login", post(admin_login))
        .route("/auth/logout", post(admin_logout))
        .route("/auth/setup", post(admin_setup))
        .route("/health", get(crate::legacy_contracts::health))
        .merge(legacy_protected)
        // Public Remote API owns its separate Bearer token and durable
        // Idempotency-Key, not the administrator/internal authorization layer.
        .merge(crate::remote_auto_youtube_v1::router())
        .merge(crate::desktop_grants_v1::router());
    Router::new()
        .merge(crate::remote_render::worker_router())
        .route("/api/auth/status", get(admin_status))
        .route("/api/auth/login", post(admin_login))
        .route("/api/auth/logout", post(admin_logout))
        .route("/api/auth/setup", post(admin_setup))
        .route("/health", get(health))
        .route("/ready", get(readiness))
        .route(
            "/desktop/authorize",
            get(crate::desktop_grants_v1::authorize),
        )
        .nest("/api/v1", legacy)
        .nest("/api/v2", protected)
        .fallback(crate::compat::fallback)
        .with_state(state)
}
