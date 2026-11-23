//! Native V1 knowledge read/delete against Python's translation_knowledge_items.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::get,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::FromRow;
use uuid::Uuid;

#[derive(Deserialize)]
struct Filters {
    item_type: Option<String>,
    status: Option<String>,
    q: Option<String>,
    domain: Option<String>,
    limit: Option<i64>,
    offset: Option<i64>,
}
#[derive(Serialize, FromRow)]
struct Knowledge {
    id: Uuid,
    item_type: String,
    term: String,
    translation: String,
    target_lang: String,
    domain: String,
    aliases: Value,
    title: String,
    content: String,
    description: String,
    sources: Value,
    confidence: f64,
    status: String,
    created_by: String,
    usage_count: i32,
    embedding_model: String,
    last_verified_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}
fn nonempty(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.is_empty())
}
async fn list(
    State(state): State<AppState>,
    Query(filters): Query<Filters>,
) -> AppResult<Json<Vec<Value>>> {
    let item_type = nonempty(filters.item_type);
    let status = nonempty(filters.status);
    let q = nonempty(filters.q).map(|v| format!("%{}%", v.trim()));
    let domain = nonempty(filters.domain).map(|v| format!("%{}%", v.trim()));
    let rows: Vec<Knowledge> = sqlx::query_as(
        "SELECT id,item_type,term,translation,target_lang,domain,aliases,title,
         content,description,sources,confidence,status,created_by,usage_count,
         embedding_model,last_verified_at,created_at,updated_at
         FROM translation_knowledge_items WHERE
         ($1::text IS NULL OR item_type=$1) AND
         ($2::text IS NULL OR status=$2) AND
         ($3::text IS NULL OR term ILIKE $3 OR translation ILIKE $3 OR
           title ILIKE $3 OR description ILIKE $3 OR content ILIKE $3 OR domain ILIKE $3)
         AND ($4::text IS NULL OR domain ILIKE $4)
         ORDER BY updated_at DESC,created_at DESC LIMIT $5 OFFSET $6",
    )
    .bind(item_type)
    .bind(status)
    .bind(q)
    .bind(domain)
    .bind(filters.limit.unwrap_or(100).clamp(1, 500))
    .bind(filters.offset.unwrap_or(0).max(0))
    .fetch_all(&state.db.pool)
    .await?;
    let mut records = Vec::with_capacity(rows.len());
    for row in rows {
        let mut value = serde_json::to_value(row)
            .map_err(|_| AppError::Invalid("knowledge item cannot be serialized".into()))?;
        for field in ["aliases", "sources"] {
            if !value[field].is_array() {
                value[field] = json!([]);
            }
        }
        records.push(value);
    }
    Ok(Json(records))
}
async fn remove(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        return Err(AppError::Conflict("V1 knowledge deletion disabled".into()));
    }
    let changed = sqlx::query("DELETE FROM translation_knowledge_items WHERE id=$1")
        .bind(id)
        .execute(&state.db.pool)
        .await?
        .rows_affected();
    if changed == 0 {
        return Err(AppError::NotFound("knowledge item not found".into()));
    }
    Ok(Json(json!({"deleted":true})))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/subtitle/knowledge/items",
            get(list).post(crate::subtitle_knowledge_write_v1::create),
        )
        .route(
            "/subtitle/knowledge/rebuild-embeddings",
            axum::routing::post(crate::subtitle_knowledge_rebuild_v1::rebuild),
        )
        .route(
            "/subtitle/knowledge/items/{item_id}",
            axum::routing::delete(remove),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nonempty_keeps_python_empty_filter_semantics() {
        assert!(nonempty(Some("".into())).is_none());
        assert_eq!(nonempty(Some("PCB".into())), Some("PCB".into()));
        assert_eq!(nonempty(None), None);
    }
}
