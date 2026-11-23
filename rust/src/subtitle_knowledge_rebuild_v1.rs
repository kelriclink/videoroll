//! Rebuild Python V1 knowledge vectors against the same original PostgreSQL
//! knowledge table. Each successful row commits after the loop; failed rows
//! are reported (not silently ignored) and preserve previous embeddings.
use crate::{AppError, AppResult, api::AppState, subtitle_embedding_infer_v1 as embedding};
use axum::{Json, extract::State};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::FromRow;
use uuid::Uuid;
#[derive(Debug, Deserialize)]
pub struct RebuildRequest {
    #[serde(default)]
    item_type: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default = "default_limit")]
    limit: i64,
}
fn default_limit() -> i64 {
    1000
}
#[derive(FromRow)]
struct Knowledge {
    id: Uuid,
    item_type: String,
    term: String,
    translation: String,
    domain: String,
    aliases: Value,
    title: String,
    content: String,
    description: String,
}
pub async fn rebuild(
    State(state): State<AppState>,
    Json(body): Json<RebuildRequest>,
) -> AppResult<Json<Value>> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        return Err(AppError::Conflict(
            "V1 knowledge embedding rebuild disabled".into(),
        ));
    }
    if body
        .item_type
        .as_deref()
        .is_some_and(|s| s != "term" && s != "document")
    {
        return Err(AppError::Invalid(
            "item_type must be term or document".into(),
        ));
    }
    if !(1..=10000).contains(&body.limit) {
        return Err(AppError::Invalid(
            "limit must be between 1 and 10000".into(),
        ));
    }
    let config = embedding::load(&state).await?;
    let model = embedding::model_key(&config);
    let rows: Vec<Knowledge> = sqlx::query_as(
        "SELECT id,item_type,term,translation,domain,aliases,title,content,description
         FROM translation_knowledge_items
         WHERE ($1::text IS NULL OR item_type=$1)
           AND ($2::text IS NULL OR status=$2)
         ORDER BY CASE WHEN last_verified_at IS NULL THEN 0 ELSE 1 END,
           last_verified_at ASC,created_at ASC,id ASC LIMIT $3",
    )
    .bind(body.item_type.as_deref().filter(|s| !s.is_empty()))
    .bind(body.status.as_deref().filter(|s| !s.is_empty()))
    .bind(body.limit)
    .fetch_all(&state.db.pool)
    .await?;
    let mut updated = 0usize;
    let mut failed = 0usize;
    let mut skipped = 0usize;
    let mut errors = Vec::<Value>::new();
    let total = rows.len();
    let mut tx = state.db.pool.begin().await?;
    for row in rows {
        let aliases = row
            .aliases
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .filter(|s| !s.trim().is_empty())
            .map(str::to_owned)
            .collect::<Vec<_>>();
        let text = embedding::embed_text(
            &row.item_type,
            &row.term,
            &row.translation,
            &row.domain,
            &aliases,
            &row.title,
            &row.content,
            &row.description,
        );
        if text.is_empty() {
            skipped += 1;
            continue;
        }
        let result = embedding::embed(&config, &text).await;
        match result {
            Err(err) => {
                failed += 1;
                if errors.len() < 20 {
                    errors.push(json!({"id":row.id,"error":err.to_string()}));
                }
            }
            Ok(vector) => {
                let literal = embedding::vector_literal(&vector);
                let hash = embedding::hash_text(&text);
                // Python uses a nested savepoint for each row, so preserve
                // successful updates even if one row has a DB constraint issue.
                sqlx::query("SAVEPOINT rebuild_embedding_row")
                    .execute(&mut *tx)
                    .await?;
                let result = sqlx::query(
                    "UPDATE translation_knowledge_items SET embedding=$2::vector,
                     embedding_model=$3,embedding_text_hash=$4,
                     last_verified_at=now(),updated_at=now() WHERE id=$1",
                )
                .bind(row.id)
                .bind(&literal)
                .bind(&model)
                .bind(&hash)
                .execute(&mut *tx)
                .await;
                match result {
                    Ok(res) => {
                        sqlx::query("RELEASE SAVEPOINT rebuild_embedding_row")
                            .execute(&mut *tx)
                            .await?;
                        if res.rows_affected() > 0 {
                            updated += 1;
                        } else {
                            failed += 1;
                            if errors.len() < 20 {
                                errors.push(json!({"id":row.id,"error":"knowledge row disappeared during rebuild"}));
                            }
                        }
                    }
                    Err(err) => {
                        sqlx::query("ROLLBACK TO SAVEPOINT rebuild_embedding_row")
                            .execute(&mut *tx)
                            .await?;
                        sqlx::query("RELEASE SAVEPOINT rebuild_embedding_row")
                            .execute(&mut *tx)
                            .await?;
                        failed += 1;
                        if errors.len() < 20 {
                            errors.push(json!({"id":row.id,"error":err.to_string()}));
                        }
                    }
                }
            }
        }
    }
    tx.commit().await?;
    Ok(Json(
        json!({"total":total,"updated":updated,"failed":failed,"skipped":skipped,
        "embedding_model":model,"dimensions":config.dimensions,"errors":errors}),
    ))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn limits() {
        assert_eq!(default_limit(), 1000);
        let body: RebuildRequest =
            serde_json::from_value(json!({"item_type":"document","limit":10})).unwrap();
        assert_eq!(body.item_type.as_deref(), Some("document"));
        assert_eq!(body.limit, 10);
    }
}
