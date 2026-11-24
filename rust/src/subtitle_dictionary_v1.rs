//! Source-compatible dictionary management against Python's original
//! translation_dictionary_sources / translation_dictionary_entries tables.
//! Lookup deliberately persists usage_count and last_lookup_at in the same
//! transaction; successful reads aren't cosmetic.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::FromRow;
use uuid::Uuid;

#[derive(Debug, FromRow, Serialize)]
struct Source {
    id: Uuid,
    name: String,
    slug: String,
    description: String,
    source_lang: String,
    target_lang: String,
    format: String,
    license: String,
    license_url: String,
    source_url: String,
    version: String,
    attribution: String,
    domain: String,
    priority: i32,
    enabled: bool,
    metadata: Value,
    entry_count: i32,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}
const SOURCE_FIELDS: &str = "id,name,slug,description,source_lang,target_lang,format,license,license_url,source_url,version,attribution,domain,priority,enabled,metadata,entry_count,created_at,updated_at";
const SOURCE_SELECT: &str = r#"
SELECT id,name,slug,description,source_lang,target_lang,format,license,license_url,source_url,version,attribution,domain,priority,enabled,metadata,entry_count,created_at,updated_at
FROM translation_dictionary_sources
"#;
#[derive(Debug, FromRow)]
struct Entry {
    id: Uuid,
    source_id: Uuid,
    source_name: String,
    source_slug: String,
    source_lang: String,
    target_lang: String,
    term: String,
    normalized_term: String,
    translations: Value,
    translation_text: String,
    pos: String,
    definition: String,
    domain: String,
    tags: Value,
    aliases: Value,
    examples: Value,
    metadata: Value,
    quality: f64,
    enabled: bool,
    usage_count: i32,
    source_license: String,
    source_license_url: String,
    source_url: String,
    source_attribution: String,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}
impl Entry {
    fn value(self) -> Value {
        let translations: Vec<String> = self
            .translations
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .filter(|s| !s.trim().is_empty())
            .collect();
        let translation = translations.first().cloned().unwrap_or_default();
        json!({
            "id":self.id,"source_id":self.source_id,
            "source_name":self.source_name,"source_slug":self.source_slug,
            "source_lang":self.source_lang,"target_lang":self.target_lang,
            "term":self.term,"normalized_term":self.normalized_term,
            "translations":translations,"translation":translation,
            "translation_text":self.translation_text,"pos":self.pos,
            "definition":self.definition,"domain":self.domain,
            "tags":self.tags,"aliases":self.aliases,"examples":self.examples,
            "metadata":self.metadata,"quality":self.quality,
            "enabled":self.enabled,"usage_count":self.usage_count,
            "license":self.source_license,"license_url":self.source_license_url,
            "source_url":self.source_url,"attribution":self.source_attribution,
            "created_at":self.created_at,"updated_at":self.updated_at
        })
    }
}
const ENTRY_SELECT: &str = r#"
SELECT e.id,e.source_id,s.name AS source_name,s.slug AS source_slug,
e.source_lang,e.target_lang,e.term,e.normalized_term,
e.translations,e.translation_text,e.pos,e.definition,e.domain,
e.tags,e.aliases,e.examples,e.metadata,e.quality,e.enabled,
e.usage_count,s.license AS source_license,s.license_url AS source_license_url,
s.source_url,s.attribution AS source_attribution,
e.created_at,e.updated_at
FROM translation_dictionary_entries e
JOIN translation_dictionary_sources s ON s.id=e.source_id
"#;
fn writes_enabled() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    )
}
fn allow_write() -> AppResult<()> {
    if writes_enabled() {
        Ok(())
    } else {
        Err(AppError::Conflict("V1 dictionary writes disabled".into()))
    }
}
fn invalid(s: &str) -> AppError {
    AppError::Invalid(s.into())
}
fn source_value(row: Source) -> AppResult<Value> {
    serde_json::to_value(row).map_err(|_| invalid("dictionary source serialization failed"))
}
#[derive(Deserialize)]
struct SourceQuery {
    enabled: Option<bool>,
    q: Option<String>,
    limit: Option<i64>,
}
async fn sources(
    State(state): State<AppState>,
    Query(q): Query<SourceQuery>,
) -> AppResult<Json<Vec<Value>>> {
    let term = q.q.as_deref().filter(|s| !s.is_empty()).map(str::trim);
    let sql = format!(
        "{SOURCE_SELECT}
       WHERE ($1::bool IS NULL OR enabled=$1)
         AND ($2::text IS NULL OR
            name ILIKE ('%'||$2||'%') OR slug ILIKE ('%'||$2||'%') OR
            description ILIKE ('%'||$2||'%') OR domain ILIKE ('%'||$2||'%'))
       ORDER BY priority DESC,updated_at DESC,name LIMIT $3"
    );
    let rows: Vec<Source> = sqlx::query_as(&sql)
        .bind(q.enabled)
        .bind(term)
        .bind(q.limit.unwrap_or(100).clamp(1, 500))
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(
        rows.into_iter()
            .map(source_value)
            .collect::<AppResult<Vec<_>>>()?,
    ))
}
#[derive(Deserialize)]
struct SourceUpdate {
    enabled: Option<bool>,
    priority: Option<i32>,
    domain: Option<String>,
    description: Option<String>,
}
async fn put_source(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(payload): Json<SourceUpdate>,
) -> AppResult<Json<Value>> {
    allow_write()?;
    if payload
        .priority
        .is_some_and(|p| !(-1000..=1000).contains(&p))
    {
        return Err(invalid("priority out of range"));
    }
    let row: Option<Source> = if payload.enabled.is_none()
        && payload.priority.is_none()
        && payload.domain.is_none()
        && payload.description.is_none()
    {
        sqlx::query_as(&format!("{SOURCE_SELECT} WHERE id=$1"))
            .bind(id)
            .fetch_optional(&state.db.pool)
            .await?
    } else {
        sqlx::query_as(&format!(
            "UPDATE translation_dictionary_sources SET
           enabled=COALESCE($2,enabled),
           priority=COALESCE($3,priority),
           domain=COALESCE($4,domain),
           description=COALESCE($5,description),
           updated_at=now() WHERE id=$1 RETURNING {SOURCE_FIELDS}"
        ))
        .bind(id)
        .bind(payload.enabled)
        .bind(payload.priority)
        .bind(payload.domain.map(|v| v.trim().to_owned()))
        .bind(payload.description.map(|v| v.trim().to_owned()))
        .fetch_optional(&state.db.pool)
        .await?
    };
    let row = row.ok_or_else(|| AppError::NotFound("dictionary source not found".into()))?;
    Ok(Json(source_value(row)?))
}
async fn delete_source(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    allow_write()?;
    let result = sqlx::query("DELETE FROM translation_dictionary_sources WHERE id=$1")
        .bind(id)
        .execute(&state.db.pool)
        .await?;
    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("dictionary source not found".into()));
    }
    Ok(Json(json!({"deleted":true})))
}
#[derive(Deserialize)]
struct EntryQuery {
    source_id: Option<Uuid>,
    q: Option<String>,
    source_lang: Option<String>,
    target_lang: Option<String>,
    domain: Option<String>,
    enabled: Option<bool>,
    limit: Option<i64>,
    offset: Option<i64>,
}
async fn entries(
    State(state): State<AppState>,
    Query(q): Query<EntryQuery>,
) -> AppResult<Json<Vec<Value>>> {
    let term = q.q.as_deref().filter(|s| !s.is_empty()).map(str::trim);
    let source_lang = q
        .source_lang
        .as_deref()
        .filter(|s| !s.is_empty())
        .map(str::trim);
    let target_lang = q
        .target_lang
        .as_deref()
        .filter(|s| !s.is_empty())
        .map(str::trim);
    let domain = q.domain.as_deref().filter(|s| !s.is_empty()).map(str::trim);
    let sql = format!(
        "{ENTRY_SELECT}
      WHERE ($1::uuid IS NULL OR e.source_id=$1)
        AND ($2::text IS NULL OR
          e.term ILIKE ('%'||$2||'%') OR e.translation_text ILIKE ('%'||$2||'%')
          OR e.definition ILIKE ('%'||$2||'%') OR e.domain ILIKE ('%'||$2||'%'))
        AND ($3::text IS NULL OR e.source_lang=$3)
        AND ($4::text IS NULL OR e.target_lang=$4)
        AND ($5::text IS NULL OR e.domain ILIKE ('%'||$5||'%') OR s.domain ILIKE ('%'||$5||'%'))
        AND ($6::bool IS NULL OR e.enabled=$6)
      ORDER BY s.priority DESC,e.updated_at DESC LIMIT $7 OFFSET $8"
    );
    let rows: Vec<Entry> = sqlx::query_as(&sql)
        .bind(q.source_id)
        .bind(term)
        .bind(source_lang)
        .bind(target_lang)
        .bind(domain)
        .bind(q.enabled)
        .bind(q.limit.unwrap_or(100).clamp(1, 500))
        .bind(q.offset.unwrap_or(0).max(0))
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(rows.into_iter().map(Entry::value).collect()))
}
#[derive(Deserialize)]
struct EntryUpdate {
    enabled: bool,
}
async fn put_entry(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(body): Json<EntryUpdate>,
) -> AppResult<Json<Value>> {
    allow_write()?;
    let mut tx = state.db.pool.begin().await?;
    let updated = sqlx::query(
        "UPDATE translation_dictionary_entries SET enabled=$2,updated_at=now() WHERE id=$1",
    )
    .bind(id)
    .bind(body.enabled)
    .execute(&mut *tx)
    .await?;
    if updated.rows_affected() == 0 {
        return Err(AppError::NotFound("dictionary entry not found".into()));
    }
    let row: Entry = sqlx::query_as(&format!("{ENTRY_SELECT} WHERE e.id=$1"))
        .bind(id)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(row.value()))
}
async fn delete_entry(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    allow_write()?;
    let result = sqlx::query("DELETE FROM translation_dictionary_entries WHERE id=$1")
        .bind(id)
        .execute(&state.db.pool)
        .await?;
    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("dictionary entry not found".into()));
    }
    Ok(Json(json!({"deleted":true})))
}
#[derive(Deserialize)]
struct Lookup {
    term: String,
    #[serde(default)]
    source_lang: String,
    #[serde(default = "default_zh")]
    target_lang: String,
    #[serde(default)]
    domain: String,
    #[serde(default = "default_true")]
    exact: bool,
    #[serde(default)]
    min_quality: f64,
    #[serde(default = "default_eight")]
    limit: i64,
}
fn default_eight() -> i64 {
    8
}
fn default_zh() -> String {
    "zh".into()
}
fn default_true() -> bool {
    true
}
fn normalize_term(text: &str) -> String {
    text.trim()
        .to_lowercase()
        .split(|c: char| c.is_whitespace() || matches!(c, '_' | '-' | ':' | '/' | '|'))
        .filter(|p| !p.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
async fn lookup(
    State(state): State<AppState>,
    Json(input): Json<Lookup>,
) -> AppResult<Json<Value>> {
    if !(0.0..=1.0).contains(&input.min_quality) || !(1..=50).contains(&input.limit) {
        return Err(invalid("dictionary lookup quality or limit out of range"));
    }
    let term = input.term.split_whitespace().collect::<Vec<_>>().join(" ");
    if term.is_empty() {
        return Ok(Json(json!({"count":0,"results":[]})));
    }
    let norm = normalize_term(&term);
    let src = input.source_lang.trim();
    let dest = if input.target_lang.trim().is_empty() {
        "zh"
    } else {
        input.target_lang.trim()
    };
    let domain = input.domain.trim();
    let sql = format!(
        "{ENTRY_SELECT}
     WHERE s.enabled=TRUE AND e.enabled=TRUE AND e.quality >= $1
       AND e.target_lang=$2
       AND ($3='' OR e.source_lang='' OR e.source_lang=$3)
       AND ($4='' OR e.domain='' OR e.domain=$4 OR s.domain='' OR s.domain=$4)
       AND (($5 AND e.normalized_term=$6)
         OR (NOT $5 AND (e.normalized_term=$6 OR e.term ILIKE $7
              OR e.translation_text ILIKE $7 OR e.definition ILIKE $7)))
     ORDER BY CASE WHEN e.normalized_term=$6 THEN 0 ELSE 1 END,
         s.priority DESC,e.quality DESC,e.usage_count DESC,e.updated_at DESC
     LIMIT $8"
    );
    let mut tx = state.db.pool.begin().await?;
    let rows: Vec<Entry> = sqlx::query_as(&sql)
        .bind(input.min_quality)
        .bind(dest)
        .bind(src)
        .bind(domain)
        .bind(input.exact)
        .bind(norm)
        .bind(format!("%{term}%"))
        .bind(input.limit)
        .fetch_all(&mut *tx)
        .await?;
    let count = rows.len();
    let mut results = Vec::with_capacity(count);
    for row in rows {
        let id = row.id;
        results.push(row.value());
        sqlx::query(
            "UPDATE translation_dictionary_entries SET
             usage_count=usage_count+1,last_lookup_at=now(),updated_at=now()
             WHERE id=$1",
        )
        .bind(id)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(Json(json!({"count":count,"results":results})))
}

fn default_promote_status() -> String {
    "approved".into()
}
fn default_promote_confidence() -> f64 {
    0.85
}
#[derive(Deserialize)]
struct PromoteRequest {
    entry_id: Uuid,
    #[serde(default = "default_promote_status")]
    status: String,
    #[serde(default = "default_promote_confidence")]
    confidence: f64,
}
fn knowledge_normalize_term(term: &str) -> String {
    // Python RAG normalize_term uses [\\s\\-_]+; unlike dictionary lookup,
    // its normalization deliberately does not split ':' or '/'.
    term.trim()
        .to_lowercase()
        .split(|c: char| c.is_whitespace() || matches!(c, '-' | '_'))
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
async fn promote(
    State(state): State<AppState>,
    Json(body): Json<PromoteRequest>,
) -> AppResult<Json<Value>> {
    allow_write()?;
    if !body.confidence.is_finite() || !(0.0..=1.0).contains(&body.confidence) {
        return Err(invalid("confidence must be between 0 and 1"));
    }
    let row: Option<Entry> = sqlx::query_as(&format!("{ENTRY_SELECT} WHERE e.id=$1"))
        .bind(body.entry_id)
        .fetch_optional(&state.db.pool)
        .await?;
    let item = row
        .ok_or_else(|| AppError::NotFound("dictionary entry not found".into()))?
        .value();
    let translation = item["translations"]
        .as_array()
        .and_then(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .find(|text| !text.trim().is_empty())
        })
        .map(str::trim)
        .unwrap_or("")
        .to_string();
    if translation.is_empty() {
        return Err(invalid("dictionary entry has no translation"));
    }
    let raw: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")
            .fetch_optional(&state.db.pool)
            .await?;
    let cfg = crate::subtitle_translate_settings_v1::view(&raw.unwrap_or(Value::Null));
    let embedding_cfg = crate::subtitle_embedding_infer_v1::load(&state).await?;
    let term = item["term"].as_str().unwrap_or("").trim().to_owned();
    let normalized = knowledge_normalize_term(&term);
    let target_lang = item["target_lang"]
        .as_str()
        .unwrap_or("zh")
        .trim()
        .to_owned();
    let target_lang = if target_lang.is_empty() {
        "zh".to_owned()
    } else {
        target_lang
    };
    let domain = item["domain"]
        .as_str()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| cfg["rag_domain"].as_str().unwrap_or(""))
        .trim()
        .to_owned();
    let description = item["definition"]
        .as_str()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| item["pos"].as_str())
        .unwrap_or("")
        .trim()
        .to_owned();
    let aliases: Vec<String> = item["aliases"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    let embedding_text = crate::subtitle_embedding_infer_v1::embed_text(
        "term",
        &term,
        &translation,
        &domain,
        &aliases,
        "",
        "",
        &description,
    );
    let embedding_vec =
        match crate::subtitle_embedding_infer_v1::embed(&embedding_cfg, &embedding_text).await {
            Ok(vector) => Some(vector),
            Err(err) if embedding_cfg.provider == "openai" => {
                // Python's original promote endpoint accepts a glossary term
                // when the online Embedding service is temporarily unavailable.
                tracing::warn!("dictionary promotion Embedding unavailable: {err}");
                None
            }
            Err(err) => return Err(err),
        };
    let vector = embedding_vec
        .as_ref()
        .map(|values| crate::subtitle_embedding_infer_v1::vector_literal(values));
    let embedding_model = if vector.is_some() {
        crate::subtitle_embedding_infer_v1::model_key(&embedding_cfg)
    } else {
        String::new()
    };
    let embedding_hash = if vector.is_some() {
        crate::subtitle_embedding_infer_v1::hash_text(&embedding_text)
    } else {
        String::new()
    };
    let source = item["source_name"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| item["source_slug"].as_str())
        .unwrap_or("dictionary");
    let url = item["source_url"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| item["license_url"].as_str())
        .unwrap_or("");
    let sources = json!([{
        "source": source,
        "url": url,
        "license": item["license"].as_str().unwrap_or(""),
        "attribution": item["attribution"].as_str().unwrap_or(""),
        "dictionary_entry_id": body.entry_id.to_string()
    }]);
    let confidence = item["quality"]
        .as_f64()
        .unwrap_or(0.0)
        .max(body.confidence)
        .clamp(0.0, 1.0);
    let status = if body.status.trim().is_empty() {
        "approved"
    } else {
        body.status.trim()
    };
    let mut tx = state.db.pool.begin().await?;
    let existing: Option<Uuid> = sqlx::query_scalar(
        "SELECT id FROM translation_knowledge_items
         WHERE item_type='term' AND target_lang=$1 AND domain=$2 AND normalized_term=$3
         LIMIT 1 FOR UPDATE",
    )
    .bind(&target_lang)
    .bind(&domain)
    .bind(&normalized)
    .fetch_optional(&mut *tx)
    .await?;
    let id = existing.unwrap_or_else(Uuid::new_v4);
    if existing.is_some() {
        sqlx::query(
            "UPDATE translation_knowledge_items SET
              term=$2, translation=$3, aliases=$4::jsonb, title='', content='',
              description=$5, sources=$6::jsonb, confidence=$7,
              status=$8, created_by='dictionary_import',
              embedding=COALESCE($9::vector,embedding),
              embedding_model=CASE WHEN $10::text<>'' THEN $10 ELSE embedding_model END,
              embedding_text_hash=CASE WHEN $11::text<>'' THEN $11 ELSE embedding_text_hash END,
              last_verified_at=now(), updated_at=now()
             WHERE id=$1",
        )
        .bind(id)
        .bind(&term)
        .bind(&translation)
        .bind(json!(aliases))
        .bind(&description)
        .bind(sources)
        .bind(confidence)
        .bind(status)
        .bind(vector.as_deref())
        .bind(&embedding_model)
        .bind(&embedding_hash)
        .execute(&mut *tx)
        .await?;
    } else {
        sqlx::query(
            "INSERT INTO translation_knowledge_items
             (id,item_type,term,normalized_term,translation,target_lang,domain,
              aliases,title,content,description,sources,confidence,status,
              created_by,embedding,embedding_model,embedding_text_hash,last_verified_at)
             VALUES($1,'term',$2,$3,$4,$5,$6,$7::jsonb,'','',$8,$9::jsonb,
              $10,$11,'dictionary_import',$12::vector,$13,$14,now())",
        )
        .bind(id)
        .bind(&term)
        .bind(&normalized)
        .bind(&translation)
        .bind(&target_lang)
        .bind(&domain)
        .bind(json!(aliases))
        .bind(&description)
        .bind(sources)
        .bind(confidence)
        .bind(status)
        .bind(vector.as_deref())
        .bind(&embedding_model)
        .bind(&embedding_hash)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(Json(json!({"knowledge_item_id":id})))
}
async fn presets() -> Json<Value> {
    Json(json!([{
        "key":"ecdict","label":"ECDICT","name":"ECDICT","slug":"ecdict",
        "description":"Free English to Chinese dictionary database.",
        "source_lang":"en","target_lang":"zh","format_name":"ecdict",
        "license":"MIT",
        "license_url":"https://github.com/skywind3000/ECDICT/blob/master/LICENSE",
        "source_url":"https://github.com/skywind3000/ECDICT",
        "version":"","domain":"","priority":80,
        "recommended_full_import":true,"recommended_max_entries":0
    }]))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/dictionaries/sources", get(sources))
        .route(
            "/subtitle/dictionaries/sources/{source_id}",
            axum::routing::put(put_source).delete(delete_source),
        )
        .route("/subtitle/dictionaries/entries", get(entries))
        .route(
            "/subtitle/dictionaries/entries/{entry_id}",
            axum::routing::put(put_entry).delete(delete_entry),
        )
        .route("/subtitle/dictionaries/lookup", post(lookup))
        .route("/subtitle/dictionaries/import-presets", get(presets))
        .route("/subtitle/dictionaries/promote", post(promote))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn norm_and_defaults() {
        assert_eq!(
            normalize_term("  PCB_Trace / Width :01  "),
            "pcb trace width 01"
        );
        assert_eq!(normalize_term("PCB-Trace"), "pcb trace");
        assert_eq!(default_zh(), "zh");
        assert_eq!(
            knowledge_normalize_term(" PCB_Width / Test  "),
            "pcb width / test"
        );
        assert_eq!(default_promote_status(), "approved");
    }
}
