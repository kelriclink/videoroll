//! Standalone Rust-compatible subset of the original Python social_publisher.
//! Native account listing, safe revocation, health and durable publish intake.
//! Browser execution remains a dedicated SAU worker.
use crate::{AppError, AppResult};
use axum::{
    Json, Router,
    extract::Request,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{delete, get},
};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::Sha256;
use sqlx::{PgPool, Row};
use std::sync::Arc;
use subtle::ConstantTimeEq;
use uuid::Uuid;

const SERVICE_CONTEXT: &[u8] = b"videoroll-internal-service:v1";
const HEADER: &str = "x-videoroll-internal-token";

#[derive(Clone)]
pub struct SauState {
    pub pool: PgPool,
    pub(crate) publish_outbox_enabled: bool,
    pub(crate) check_enabled: bool,
    pub(crate) native_publish: Option<Arc<crate::sau_worker_native_v1::PublishConfig>>,
    pub(crate) login_config: Option<Arc<crate::sau_login_native_v1::LoginConfig>>,
    token: String,
}
impl SauState {
    pub fn new(pool: PgPool, secret: &str) -> AppResult<Self> {
        let secret = secret.trim();
        if secret.len() < 16
            || matches!(
                secret.to_lowercase().as_str(),
                "change-me"
                    | "changeme"
                    | "videoroll-development-internal-secret"
                    | "videoroll-development-bootstrap-secret"
            )
        {
            return Err(AppError::Unavailable(
                "SAU requires a non-default INTERNAL_API_SECRET".into(),
            ));
        }
        let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
            .map_err(|_| AppError::Unavailable("SAU HMAC unavailable".into()))?;
        mac.update(SERVICE_CONTEXT);
        let check_enabled = std::env::var("VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK")
            .ok()
            .as_deref()
            == Some("true");
        let login_config = if std::env::var("VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN")
            .ok()
            .as_deref()
            == Some("true")
        {
            if !check_enabled {
                return Err(AppError::Invalid(
                    "native login requires native check runtime".into(),
                ));
            }
            Some(Arc::new(
                crate::sau_login_native_v1::LoginConfig::from_env()?
            ))
        } else {
            None
        };
        let publish_outbox_enabled = std::env::var("VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX")
            .ok()
            .as_deref()
            == Some("true");
        if crate::outbox_dispatcher::native_only() && publish_outbox_enabled {
            return Err(AppError::Conflict(
                "native-only outbox forbids SAU legacy Celery delivery".into(),
            ));
        }
        let native_publish = if std::env::var("VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH")
            .ok()
            .as_deref()
            == Some("true")
        {
            if !check_enabled || publish_outbox_enabled {
                return Err(AppError::Invalid(
                    "native publication requires native runtime and excludes legacy outbox mode"
                        .into(),
                ));
            }
            Some(Arc::new(
                crate::sau_worker_native_v1::PublishConfig::from_env()?,
            ))
        } else {
            None
        };
        Ok(Self {
            pool,
            publish_outbox_enabled,
            native_publish,
            check_enabled,
            login_config,
            token: format!("v1.{}", hex::encode(mac.finalize().into_bytes())),
        })
    }
    fn authenticate(&self, headers: &HeaderMap) -> Result<(), (StatusCode, Json<Value>)> {
        let presented = headers
            .get(HEADER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .trim();
        if presented.is_empty() {
            return Err((
                StatusCode::UNAUTHORIZED,
                Json(json!({"detail":"internal service credentials required"})),
            ));
        }
        if !bool::from(presented.as_bytes().ct_eq(self.token.as_bytes())) {
            return Err((
                StatusCode::FORBIDDEN,
                Json(json!({"detail":"invalid internal service credentials"})),
            ));
        }
        Ok(())
    }
}
async fn authentication(
    State(state): State<Arc<SauState>>,
    request: Request,
    next: Next,
) -> Response {
    if let Err(error) = state.authenticate(request.headers()) {
        return error.into_response();
    }
    next.run(request).await
}
async fn health() -> Json<Value> {
    Json(json!({"status":"ok","driver":"sau"}))
}
pub(crate) fn social_platform(raw: &str) -> AppResult<&'static str> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "douyin" => Ok("douyin"),
        "xiaohongshu" => Ok("xiaohongshu"),
        "kuaishou" => Ok("kuaishou"),
        _ => Err(AppError::Invalid("unsupported social platform".into())),
    }
}
#[derive(Default, Deserialize)]
struct AccountFilter {
    platform: Option<String>,
}
async fn list_accounts(
    State(state): State<Arc<SauState>>,
    Query(filter): Query<AccountFilter>,
) -> AppResult<Json<Vec<Value>>> {
    let platform = filter
        .platform
        .as_deref()
        .filter(|value| !value.is_empty())
        .map(social_platform)
        .transpose()?;
    let rows = sqlx::query(
        "SELECT id,platform::text AS platform,name,is_active,check_state,
                last_checked_at,last_check_message,rotated_at
           FROM accounts
          WHERE platform::text IN ('douyin','xiaohongshu','kuaishou')
            AND ($1::text IS NULL OR platform::text=$1)
          ORDER BY platform::text,name",
    )
    .bind(platform)
    .fetch_all(&state.pool)
    .await?;
    let mut output = Vec::with_capacity(rows.len());
    for row in rows {
        output.push(account_json(&row)?);
    }
    Ok(Json(output))
}
pub(crate) fn account_json(row: &sqlx::postgres::PgRow) -> AppResult<Value> {
    Ok(json!({
        "id":row.try_get::<Uuid,_>("id")?,
        "platform":row.try_get::<String,_>("platform")?,
        "name":row.try_get::<String,_>("name")?,
        "is_active":row.try_get::<bool,_>("is_active")?,
        "check_state":row.try_get::<String,_>("check_state")?,
        "last_checked_at":row.try_get::<Option<chrono::DateTime<chrono::Utc>>,_>("last_checked_at")?,
        "last_check_message":row.try_get::<Option<String>,_>("last_check_message")?,
        "rotated_at":row.try_get::<Option<chrono::DateTime<chrono::Utc>>,_>("rotated_at")?,
    }))
}
async fn disable_account(
    State(state): State<Arc<SauState>>,
    Path(account_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let mut tx = state.pool.begin().await?;
    // All Python SAU publish submissions take a FOR UPDATE on account first.
    // Taking the same lock prevents revocation racing with a new submission.
    let row = sqlx::query(
        "SELECT platform::text AS platform,check_state
           FROM accounts WHERE id=$1 FOR UPDATE",
    )
    .bind(account_id)
    .fetch_optional(&mut *tx)
    .await?;
    let row = row.ok_or_else(|| AppError::NotFound("social account not found".into()))?;
    let platform: String = row.try_get("platform")?;
    if social_platform(&platform).is_err() {
        return Err(AppError::NotFound("social account not found".into()));
    }
    let check_state: String = row.try_get("check_state")?;
    account_busy(&mut tx, account_id, &check_state).await?;
    sqlx::query(
        "UPDATE accounts
            SET is_active=false,secrets_encrypted='',check_state='unchecked',
                last_check_message=NULL
          WHERE id=$1",
    )
    .bind(account_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"ok":true})))
}
pub(crate) async fn account_busy(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    account_id: Uuid,
    check_state: &str,
) -> AppResult<()> {
    let account = sqlx::query("SELECT platform::text AS platform,name FROM accounts WHERE id=$1")
        .bind(account_id)
        .fetch_one(&mut **tx)
        .await?;
    crate::sau_login_native_v1::ensure_not_logging_in(
        tx,
        &account.try_get::<String, _>("platform")?,
        &account.try_get::<String, _>("name")?,
    )
    .await?;
    let publishing: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM publish_jobs
           WHERE account_id=$1 AND state::text='submitting')",
    )
    .bind(account_id)
    .fetch_one(&mut **tx)
    .await?;
    if check_state == "checking" || publishing {
        return Err(AppError::Conflict(
            "account is busy; wait for the current check or publish job".into(),
        ));
    }
    Ok(())
}
pub fn start_native_checker(state: Arc<SauState>) -> AppResult<()> {
    if state.check_enabled {
        let runtime = crate::sau_check_native_v1::Runtime::from_env()?;
        tokio::spawn(crate::sau_check_native_v1::run(state, runtime));
    }
    Ok(())
}
pub fn router(state: Arc<SauState>) -> Router {
    let protected = Router::new()
        .route("/accounts", get(list_accounts))
        .route(
            "/accounts/{account_id}",
            delete(disable_account).post(crate::sau_check_native_v1::import_account),
        )
        .route(
            "/accounts/{account_id}/check",
            axum::routing::post(crate::sau_check_native_v1::recheck),
        )
        .route(
            "/sau/{platform}/publish",
            axum::routing::post(crate::sau_publish_native_v1::publish),
        )
        .route(
            "/login-sessions/{session_id}",
            get(crate::sau_login_native_v1::get)
                .post(crate::sau_login_native_v1::start)
                .delete(crate::sau_login_native_v1::cancel),
        )
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            authentication,
        ));
    Router::new()
        .route("/health", get(health))
        .merge(protected)
        .with_state(state)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn service_token_parity_and_platform_allowlist() {
        let token = SauState::new(
            PgPool::connect_lazy("postgresql://example:example@127.0.0.1:5432/unused").unwrap(),
            "isolated-internal-service-secret",
        )
        .unwrap();
        assert!(token.token.starts_with("v1."));
        assert_eq!(token.token.len(), 67);
        let mut headers = HeaderMap::new();
        assert_eq!(
            token.authenticate(&headers).unwrap_err().0,
            StatusCode::UNAUTHORIZED
        );
        headers.insert(HEADER, token.token.parse().unwrap());
        assert!(token.authenticate(&headers).is_ok());
        assert!(social_platform("douyin").is_ok());
        assert!(social_platform("bilibili").is_err());
        assert!(
            SauState::new(
                PgPool::connect_lazy("postgresql://example:example@127.0.0.1:5432/unused").unwrap(),
                "videoroll-development-internal-secret"
            )
            .is_err()
        );
    }
}
