//! VideoRoll v2 Rust control plane. Existing media/AI libraries remain isolated workers.
//! This crate intentionally does not modify a running v1 deployment.
pub mod api;
pub mod assets;
pub mod auth;
pub mod config;
pub mod database;
pub mod domain;
pub mod error;
pub mod executor;
pub mod ingest;
pub mod publish;
pub mod scheduler;
pub mod storage;
pub mod subtitles;
pub use config::Config;
pub use database::Database;
pub use error::{AppError, AppResult};
