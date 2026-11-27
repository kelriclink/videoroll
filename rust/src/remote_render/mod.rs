//! Native Windows/Linux Render Worker v1-compatible Rust coordinator.
//! The HTTP contract is compatible with the repository's standalone Windows
//! Render Worker; credentials/executions are strictly v2-scoped.
mod protocol;
mod routes;
mod service;
pub(crate) use protocol::EnrollRequest;

pub use routes::{admin_router, worker_router};
pub use service::enqueue;
pub(crate) use service::{MAX_RENDER_ATTEMPTS, output_key, matches_output_key, output_kind, output_stage, receipt};
