//! Python V1 /subtitle/knowledge/items upsert, with online Embedding
//! vectors and source-compatible PostgreSQL deduplication.
use crate::{AppError, AppResult, api::AppState, subtitle_embedding_infer_v1 as embedding};
use axum::{Json, extract::State};
use serde::Deserialize;
use serde_json::{Value, json};
use uuid::Uuid;
#[derive(Debug, Deserialize)]
pub struct Upsert {
    #[serde(default = "term_type")]
    item_type: String,
    #[serde(default = "target_lang")]
    target_lang: String,
    #[serde(default)]
    term: String,
    #[serde(default)]
    translation: String,
    #[serde(default)]
    domain: String,
    #[serde(default)]
    aliases: Vec<String>,
    #[serde(default)]
    title: String,
    #[serde(default)]
    content: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    sources: Vec<Value>,
    #[serde(default = "confidence_one")]
    confidence: f64,
    #[serde(default = "approved")]
    status: String,
    #[serde(default = "manual")]
    created_by: String,
}
fn term_type() -> String {
    "term".into()
}
fn target_lang() -> String {
    "zh".into()
}
fn confidence_one() -> f64 {
    1.
}
fn approved() -> String {
    "approved".into()
}
fn manual() -> String {
    "manual".into()
}
fn normalize_term(raw: &str) -> String {
    raw.trim()
        .to_lowercase()
        .split(|c: char| c.is_whitespace() || c == '_' || c == '-')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
fn validate(body: &Upsert) -> AppResult<()> {
    if !matches!(body.item_type.as_str(), "term" | "document") {
        return Err(AppError::Invalid(
            "item_type must be term or document".into(),
        ));
    }
    if body.item_type == "term" && body.term.trim().is_empty() {
        return Err(AppError::Invalid("term is required for term items".into()));
    }
    if body.item_type == "term" && body.translation.trim().is_empty() {
        return Err(AppError::Invalid(
            "translation is required for term items".into(),
        ));
    }
    if body.item_type == "document"
        && body.title.trim().is_empty()
        && body.content.trim().is_empty()
    {
        return Err(AppError::Invalid(
            "title or content is required for document items".into(),
        ));
    }
    if !body.confidence.is_finite() || !(0.0..=1.0).contains(&body.confidence) {
        return Err(AppError::Invalid(
            "confidence must be between 0 and 1".into(),
        ));
    }
    if body.sources.iter().any(|s| !s.is_object()) {
        return Err(AppError::Invalid("sources must contain objects".into()));
    }
    Ok(())
}
pub async fn create(
    State(state): State<AppState>,
    Json(body): Json<Upsert>,
) -> AppResult<Json<Value>> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("1" | "true")
    ) {
        return Err(AppError::Conflict("V1 knowledge writes disabled".into()));
    }
    validate(&body)?;
    let config = embedding::load(&state).await?;
    let kind = body.item_type.as_str();
    let alias_list: Vec<String> = body
        .aliases
        .iter()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .collect();
    let sources: Vec<Value> = body.sources.into_iter().filter(Value::is_object).collect();
    let text = embedding::embed_text(
        kind,
        &body.term,
        &body.translation,
        &body.domain,
        &body.aliases,
        &body.title,
        &body.content,
        &body.description,
    );
    let vector = if text.is_empty() {
        None
    } else {
        match embedding::embed(&config, &text).await {
            Ok(v) => Some(v),
            Err(err) if kind == "term" && config.provider == "openai" => {
                // Python term writes are allowed to survive transient
                // Embedding failures. There is no false embedding_model/hash.
                tracing::warn!("term knowledge Embedding unavailable: {err}");
                None
            }
            Err(err) => return Err(err),
        }
    };
    let literal = vector.as_ref().map(|v| embedding::vector_literal(v));
    let hash = vector
        .as_ref()
        .map(|_| embedding::hash_text(&text))
        .unwrap_or_default();
    let model = vector
        .as_ref()
        .map(|_| embedding::model_key(&config))
        .unwrap_or_default();
    let term = body.term.trim().to_owned();
    let translation = body.translation.trim().to_owned();
    let domain = body.domain.trim().to_owned();
    let lang = if body.target_lang.trim().is_empty() {
        "zh".into()
    } else {
        body.target_lang.trim().to_owned()
    };
    let title = body.title.trim().to_owned();
    let content = body.content.trim().to_owned();
    let description = body.description.trim().to_owned();
    let status = if body.status.trim().is_empty() {
        "approved".into()
    } else {
        body.status.trim().to_owned()
    };
    let created_by = if body.created_by.trim().is_empty() {
        "manual".into()
    } else {
        body.created_by.trim().to_owned()
    };
    let normalized = normalize_term(&term);
    let mut tx = state.db.pool.begin().await?;
    let id = if kind == "term" && !normalized.is_empty() {
        // Serialize concurrent term writes even before an eventual unique
        // knowledge-term index. The original Python row is still authoritative.
        let key = format!("{}:{}:{}", lang, domain, normalized);
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
            .bind(&key)
            .execute(&mut *tx)
            .await?;
        let existing:Option<Uuid>=sqlx::query_scalar(
            "SELECT id FROM translation_knowledge_items WHERE
             item_type='term' AND target_lang=$1 AND domain=$2 AND normalized_term=$3 LIMIT 1 FOR UPDATE")
            .bind(&lang).bind(&domain).bind(&normalized)
            .fetch_optional(&mut *tx).await?;
        if let Some(id) = existing {
            sqlx::query(
                "UPDATE translation_knowledge_items SET
               term=$2,translation=$3,aliases=$4::jsonb,title=$5,content=$6,
               description=$7,sources=$8::jsonb,confidence=$9,status=$10,
               created_by=$11,embedding=COALESCE($12::vector,embedding),
               embedding_model=CASE WHEN $13::text<>'' THEN $13 ELSE embedding_model END,
               embedding_text_hash=CASE WHEN $14::text<>'' THEN $14 ELSE embedding_text_hash END,
               last_verified_at=now(),updated_at=now() WHERE id=$1",
            )
            .bind(id)
            .bind(&term)
            .bind(&translation)
            .bind(json!(alias_list))
            .bind(&title)
            .bind(&content)
            .bind(&description)
            .bind(json!(sources))
            .bind(body.confidence)
            .bind(&status)
            .bind(&created_by)
            .bind(literal.as_deref())
            .bind(&model)
            .bind(&hash)
            .execute(&mut *tx)
            .await?;
            id
        } else {
            insert(
                &mut tx,
                kind,
                &term,
                &normalized,
                &translation,
                &lang,
                &domain,
                &alias_list,
                &title,
                &content,
                &description,
                &sources,
                body.confidence,
                &status,
                &created_by,
                literal.as_deref(),
                &model,
                &hash,
            )
            .await?
        }
    } else {
        insert(
            &mut tx,
            kind,
            &term,
            &normalized,
            &translation,
            &lang,
            &domain,
            &alias_list,
            &title,
            &content,
            &description,
            &sources,
            body.confidence,
            &status,
            &created_by,
            literal.as_deref(),
            &model,
            &hash,
        )
        .await?
    };
    tx.commit().await?;
    Ok(Json(json!({"id":id})))
}
#[allow(clippy::too_many_arguments)]
async fn insert(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    kind: &str,
    term: &str,
    normalized: &str,
    translation: &str,
    lang: &str,
    domain: &str,
    aliases: &[String],
    title: &str,
    content: &str,
    description: &str,
    sources: &[Value],
    confidence: f64,
    status: &str,
    created_by: &str,
    vector: Option<&str>,
    model: &str,
    hash: &str,
) -> AppResult<Uuid> {
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO translation_knowledge_items (
        id,item_type,term,normalized_term,translation,target_lang,domain,
        aliases,title,content,description,sources,confidence,status,created_by,
        embedding,embedding_model,embedding_text_hash,last_verified_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12::jsonb,
          $13,$14,$15,$16::vector,$17,$18,now())",
    )
    .bind(id)
    .bind(kind)
    .bind(term)
    .bind(normalized)
    .bind(translation)
    .bind(lang)
    .bind(domain)
    .bind(json!(aliases))
    .bind(title)
    .bind(content)
    .bind(description)
    .bind(json!(sources))
    .bind(confidence)
    .bind(status)
    .bind(created_by)
    .bind(vector)
    .bind(model)
    .bind(hash)
    .execute(&mut **tx)
    .await?;
    Ok(id)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validation_and_domain_norm() {
        assert_eq!(normalize_term(" PCB/Width_TEST  "), "pcb/width test");
        let mut body = Upsert {
            item_type: "term".into(),
            target_lang: "zh".into(),
            term: "trace".into(),
            translation: "走线".into(),
            domain: "pcb".into(),
            aliases: vec![],
            title: String::new(),
            content: String::new(),
            description: String::new(),
            sources: vec![],
            confidence: 1.,
            status: "approved".into(),
            created_by: "manual".into(),
        };
        validate(&body).unwrap();
        body.translation.clear();
        assert!(validate(&body).is_err());
        body.item_type = "document".into();
        body.title = "Guide".into();
        validate(&body).unwrap();
        body.confidence = 1.1;
        assert!(validate(&body).is_err());
    }
}
