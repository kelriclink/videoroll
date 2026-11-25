//! Retrieval from the original approved translation knowledge corpus.
//! Native task-scoped records remain in rag.rs; never expose them globally.
use crate::{AppResult, Database, rag::SearchHit, subtitle_embedding_infer_v1 as embedding};
use serde_json::Value;
use sqlx::FromRow;
use std::collections::HashSet;
use uuid::Uuid;

#[derive(Clone)]
pub(crate) struct Settings {
    pub enabled: bool,
    pub domain: String,
    pub top_k: usize,
    pub min_score: f64,
}
pub(crate) async fn settings(db: &Database) -> AppResult<Settings> {
    let raw: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")
            .fetch_optional(&db.pool)
            .await?;
    let cfg = crate::subtitle_translate_settings_v1::view(&raw.unwrap_or(Value::Null));
    Ok(Settings {
        enabled: cfg["rag_enabled"].as_bool().unwrap_or(false),
        domain: cfg["rag_domain"].as_str().unwrap_or("").trim().into(),
        top_k: cfg["rag_top_k"].as_u64().unwrap_or(8).clamp(1, 30) as usize,
        min_score: cfg["rag_min_score"].as_f64().unwrap_or(0.68).clamp(0., 1.),
    })
}

fn normalize(s: &str) -> String {
    s.trim()
        .to_lowercase()
        .split(|c: char| c.is_whitespace() || matches!(c, '-' | '_'))
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
fn stopword(s: &str) -> bool {
    matches!(
        s.to_ascii_lowercase().as_str(),
        "the"
            | "a"
            | "an"
            | "and"
            | "or"
            | "of"
            | "to"
            | "in"
            | "is"
            | "it"
            | "this"
            | "that"
            | "for"
            | "with"
            | "on"
            | "at"
            | "are"
            | "was"
            | "be"
            | "as"
            | "by"
            | "we"
            | "you"
            | "i"
            | "they"
            | "he"
            | "she"
            | "from"
            | "but"
            | "not"
    )
}
// Bounded candidate generation; single words and multiword phrases have
// separate budgets so long videos cannot exhaust every slot on 4-grams.
// This is not the Python master agent's full linguistic candidate generator.
fn candidates(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut push = |raw: &str| {
        let v =
            normalize(raw.trim_matches(|c: char| {
                matches!(c, '.' | ',' | ':' | ';' | '!' | '?' | '\'' | '"')
            }));
        if v.chars().count() >= 2 && seen.insert(v.clone()) {
            out.push(v);
        }
    };
    let text: String = text.chars().take(3000).collect();
    let tokens = text
        .split(|c: char| {
            !c.is_ascii_alphanumeric()
                && !matches!(c, '_' | '-' | '+' | '#' | '.' | ':' | '/' | '\'')
        })
        .map(|t| t.trim_matches(|c: char| matches!(c, '.' | ':' | '\'')))
        .filter(|t| t.starts_with(|c: char| c.is_ascii_alphabetic()))
        .take(500)
        .collect::<Vec<_>>();
    for t in tokens.iter().take(100) {
        if !stopword(t) && (t.len() > 2 || t.chars().all(|c| c.is_ascii_uppercase())) {
            push(t);
        }
    }
    for n in 2..=4 {
        for chunk in tokens
            .windows(n)
            .filter(|w| !stopword(w[0]) && !stopword(w[n - 1]))
            .take(100)
        {
            push(&chunk.join(" "));
        }
    }
    for chunk in text
        .split(|c: char| !('\u{3400}'..='\u{9fff}').contains(&c))
        .filter(|s| s.chars().count() >= 2)
        .take(64)
    {
        let cs = chunk.chars().take(64).collect::<Vec<_>>();
        push(chunk);
        for n in 2..=6 {
            for w in cs.windows(n).take(24) {
                push(&w.iter().collect::<String>());
            }
        }
    }
    out.truncate(600);
    out
}

#[derive(FromRow)]
struct Row {
    id: Uuid,
    item_type: String,
    term: String,
    translation: String,
    title: String,
    content: String,
    description: String,
    domain: String,
    score: f64,
}
impl Row {
    fn hit(self) -> SearchHit {
        let term = self.item_type == "term";
        let content = if term {
            format!(
                "术语：{}\n译法：{}\n领域：{}\n说明：{}",
                self.term, self.translation, self.domain, self.description
            )
        } else {
            format!(
                "标题：{}\n领域：{}\n正文：{}\n说明：{}",
                self.title, self.domain, self.content, self.description
            )
        };
        SearchHit {
            id: self.id,
            kind: if term { "term" } else { "passage" }.into(),
            title: if term { self.term } else { self.title },
            source_ref: Some("translation_knowledge_items".into()),
            content,
            score: self.score,
            scope: "global".into(),
        }
    }
}

pub(crate) async fn reviewed_identities(
    db: &Database,
    keys: &[String],
    target: &str,
    cfg: &Settings,
) -> AppResult<Vec<(String, SearchHit)>> {
    if keys.is_empty() {
        return Ok(vec![]);
    }
    let rows=sqlx::query_as::<_,Row>(
        "SELECT DISTINCT ON (normalized_term) id,item_type,term,translation,title,content,description,domain,1.0::float8 AS score
         FROM translation_knowledge_items WHERE native_scope IS NULL AND item_type='term'
           AND target_lang=$1 AND status IN ('approved','auto_approved') AND normalized_term=ANY($2)
           AND (domain=$3 OR domain='')
         ORDER BY normalized_term,(domain=$3) DESC,(status='approved') DESC,confidence DESC,updated_at DESC,id")
        .bind(target).bind(keys).bind(&cfg.domain).fetch_all(&db.pool).await?;
    Ok(rows
        .into_iter()
        .map(|row| (normalize(&row.term), row.hit()))
        .collect())
}
pub(crate) async fn exact(
    db: &Database,
    text: &str,
    target: &str,
    cfg: &Settings,
) -> AppResult<Vec<SearchHit>> {
    let norms = candidates(text);
    if norms.is_empty() {
        return Ok(vec![]);
    }
    let rows = sqlx::query_as::<_, Row>(
        "SELECT id,item_type,term,translation,title,content,description,domain,1.0::float8 AS score
         FROM translation_knowledge_items
         WHERE native_scope IS NULL AND item_type='term'
           AND target_lang=$1 AND status IN ('approved','auto_approved')
           AND normalized_term=ANY($2) AND ($3='' OR domain='' OR domain=$3)
         ORDER BY confidence DESC,updated_at DESC,id LIMIT $4",
    )
    .bind(target)
    .bind(norms)
    .bind(&cfg.domain)
    .bind((cfg.top_k * 4).min(50) as i64)
    .fetch_all(&db.pool)
    .await?;
    Ok(rows.into_iter().map(Row::hit).collect())
}

pub(crate) async fn vector(
    db: &Database,
    text: &str,
    target: &str,
    cfg: &Settings,
) -> AppResult<Vec<SearchHit>> {
    let model = embedding::load_db(db).await?;
    let key = embedding::model_key(&model);
    // Do not send text to a remote model when no eligible vector exists.
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM translation_knowledge_items
        WHERE native_scope IS NULL AND status IN ('approved','auto_approved') AND target_lang=$1
        AND ($2='' OR domain='' OR domain=$2) AND embedding_model=$3 AND embedding IS NOT NULL
        AND vector_dims(embedding)=$4)",
    )
    .bind(target)
    .bind(&cfg.domain)
    .bind(&key)
    .bind(model.dimensions as i32)
    .fetch_one(&db.pool)
    .await?;
    if !exists {
        return Ok(vec![]);
    }
    let v = embedding::embed(&model, text).await?;
    if v.iter().all(|x| *x == 0.) {
        return Err(crate::AppError::Unavailable(
            "RAG query returned zero vector".into(),
        ));
    }
    let literal = embedding::vector_literal(&v);
    // CASE guards against mixed dimensions even if the optimizer reorders
    // filters. A read-only transaction bounds database work, including exact
    // scan of high-dimensional vectors which lack a usable HNSW index.
    let mut tx = db.pool.begin().await?;
    sqlx::query("SET LOCAL statement_timeout = '15s'")
        .execute(&mut *tx)
        .await?;
    let rows=sqlx::query_as::<_,Row>(
        "SELECT id,item_type,term,translation,title,content,description,domain,score
         FROM (SELECT id,item_type,term,translation,title,content,description,domain,
            CASE WHEN vector_dims(embedding)=$4 THEN GREATEST(0,1-(embedding <=> CAST($5 AS vector))) ELSE NULL END AS score
           FROM translation_knowledge_items
           WHERE native_scope IS NULL AND status IN ('approved','auto_approved')
             AND target_lang=$1 AND ($2='' OR domain='' OR domain=$2)
             AND embedding_model=$3 AND embedding IS NOT NULL AND vector_dims(embedding)=$4
             AND vector_norm(embedding)>0) AS eligible
         WHERE score >= $6 ORDER BY score DESC,id LIMIT $7")
        .bind(target).bind(&cfg.domain).bind(key).bind(model.dimensions as i32)
        .bind(literal).bind(cfg.min_score).bind(cfg.top_k as i64)
        .fetch_all(&mut *tx).await?;
    tx.commit().await?;
    Ok(rows.into_iter().map(Row::hit).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn phrases_and_cjk() {
        let v = candidates("The electronic circuit uses an RTX-4070 and 电路测试");
        assert!(v.contains(&"electronic circuit".into()));
        assert!(v.contains(&"rtx 4070".into()));
        assert!(v.contains(&"电路".into()));
        assert!(!v.contains(&"the".into()));
        assert!(v.len() <= 600);
    }
}
