use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("database error: {0}")]
    Database(#[from] sqlx::Error),
    #[error("migration error: {0}")]
    Migration(#[from] sqlx::migrate::MigrateError),
    #[error("file error: {0}")]
    Io(#[from] std::io::Error),
    #[error("upstream error: {0}")]
    Upstream(#[from] reqwest::Error),
    #[error("invalid input: {0}")]
    Invalid(String),
    #[error("payload too large: {0}")]
    TooLarge(String),
    #[error("resource not found: {0}")]
    NotFound(String),
    #[error("conflict: {0}")]
    Conflict(String),
    #[error("forbidden: {0}")]
    Forbidden(String),
    #[error("not authorized")]
    Unauthorized,
    #[error("too many login attempts")]
    RateLimited,
    #[error("stage is not available: {0}")]
    Unavailable(String),
}
pub type AppResult<T> = std::result::Result<T, AppError>;
impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let code = match self {
            Self::Database(_) | Self::Io(_) | Self::Migration(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
            Self::Upstream(_) | Self::Unavailable(_) => StatusCode::BAD_GATEWAY,
            Self::Invalid(_) => StatusCode::BAD_REQUEST,
            Self::TooLarge(_) => StatusCode::PAYLOAD_TOO_LARGE,
            Self::NotFound(_) => StatusCode::NOT_FOUND,
            Self::Conflict(_) => StatusCode::CONFLICT,
            Self::Forbidden(_) => StatusCode::FORBIDDEN,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::RateLimited => StatusCode::TOO_MANY_REQUESTS,
        };
        let public = match code {
            StatusCode::INTERNAL_SERVER_ERROR => "internal server error".to_string(),
            StatusCode::BAD_GATEWAY => "upstream temporarily unavailable".to_string(),
            _ => self.to_string(),
        };
        (code, Json(json!({"detail": public}))).into_response()
    }
}
