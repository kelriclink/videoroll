//! Python operations.py AI usage/pricing and settings.py storage-retention
//! V1 contracts. Writes use the original app_settings as the only source of truth.
use crate::{AppError, AppResult, Database, api::AppState, settings};
use axum::{
    Json, Router,
    extract::{Query, State},
    routing::get,
};
use chrono::{DateTime, Duration, Utc};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::Row;
use uuid::Uuid;

const STORAGE_KEY: &str = "storage.retention";
const PRICING_KEY: &str = "ai.usage.pricing";

async fn persist(db: &Database, key: &str, data: Value) -> AppResult<()> {
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,
           version=app_settings.version+1, updated_at=now()",
    )
    .bind(key)
    .bind(data)
    .execute(&db.pool)
    .await?;
    Ok(())
}

fn ttl_from_value(value: &Value) -> i64 {
    value
        .get("asset_ttl_days")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .clamp(0, 3650)
}
/// Python treats 0 as 'do not automatically remove published media'.
/// Return a duration only when the user has opted into published retention.
pub async fn published_retention(db: &Database) -> AppResult<Option<std::time::Duration>> {
    let days = ttl_from_value(&settings::effective(db, STORAGE_KEY).await?);
    Ok((days > 0).then(|| std::time::Duration::from_secs(days as u64 * 86_400)))
}
async fn storage_read(db: &Database) -> AppResult<Value> {
    Ok(json!({"asset_ttl_days":ttl_from_value(&settings::effective(db, STORAGE_KEY).await?)}))
}
async fn storage_get(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(storage_read(&state.db).await?))
}
async fn storage_put(
    State(state): State<AppState>,
    Json(value): Json<Value>,
) -> AppResult<Json<Value>> {
    let body = value
        .as_object()
        .ok_or_else(|| AppError::Invalid("storage settings must be an object".into()))?;
    let next = match body.get("asset_ttl_days") {
        Some(Value::Null) | None => None,
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| AppError::Invalid("asset_ttl_days must be an integer".into()))?
            .into(),
        _ => {
            return Err(AppError::Invalid(
                "asset_ttl_days must be an integer".into(),
            ));
        }
    };
    if let Some(days) = next {
        persist(
            &state.db,
            STORAGE_KEY,
            json!({"asset_ttl_days":days.clamp(0,3650)}),
        )
        .await?;
    }
    Ok(Json(storage_read(&state.db).await?))
}

fn validate_pricing(value: &Value) -> AppResult<Map<String, Value>> {
    let raw = value
        .get("models")
        .and_then(Value::as_object)
        .ok_or_else(|| AppError::Invalid("models must be an object".into()))?;
    let mut valid = Map::new();
    for (name, price) in raw {
        let trimmed = name.trim();
        if trimmed.is_empty() || trimmed.len() > 320 {
            return Err(AppError::Invalid(
                "each pricing model must have a non-empty model key".into(),
            ));
        }
        let entry = price
            .as_object()
            .ok_or_else(|| AppError::Invalid("each pricing entry must be an object".into()))?;
        let number = |field: &str| -> AppResult<f64> {
            let Some(value) = entry.get(field) else {
                return Ok(0.0);
            };
            let v = match value {
                Value::Null => 0.0,
                Value::Number(v) => v
                    .as_f64()
                    .ok_or_else(|| AppError::Invalid(format!("invalid pricing for {trimmed}")))?,
                Value::String(v) if v.trim().is_empty() => 0.0,
                Value::String(v) => v
                    .parse::<f64>()
                    .map_err(|_| AppError::Invalid(format!("invalid pricing for {trimmed}")))?,
                _ => return Err(AppError::Invalid(format!("invalid pricing for {trimmed}"))),
            };
            if !v.is_finite() || v < 0.0 {
                return Err(AppError::Invalid(format!(
                    "pricing for {trimmed} must be finite and non-negative"
                )));
            }
            Ok(v)
        };
        valid.insert(
            trimmed.to_string(),
            json!({
                "input_per_million_usd":number("input_per_million_usd")?,
                "output_per_million_usd":number("output_per_million_usd")?
            }),
        );
    }
    Ok(valid)
}
async fn pricing_read(db: &Database) -> AppResult<Value> {
    let settings = settings::effective(db, PRICING_KEY).await?;
    Ok(
        json!({"models":settings.get("models").filter(|v|v.is_object()).cloned().unwrap_or(json!({}))}),
    )
}
async fn pricing_get(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(pricing_read(&state.db).await?))
}
async fn pricing_put(
    State(state): State<AppState>,
    Json(value): Json<Value>,
) -> AppResult<Json<Value>> {
    let prices = validate_pricing(&value)?;
    let updated = json!({"models":prices});
    persist(&state.db, PRICING_KEY, updated.clone()).await?;
    Ok(Json(updated))
}
fn estimate(models: &Value, provider: &str, model: &str, input: i64, output: i64) -> Option<i64> {
    let entries = models.as_object()?;
    let lookup = format!("{provider}:{model}");
    let rates = entries
        .get(&lookup)
        .or_else(|| entries.get(model))?
        .as_object()?;
    let incoming = rates.get("input_per_million_usd")?.as_f64()?;
    let outgoing = rates.get("output_per_million_usd")?.as_f64()?;
    if !incoming.is_finite() || !outgoing.is_finite() || incoming < 0.0 || outgoing < 0.0 {
        return None;
    }
    let result = input as f64 * incoming + output as f64 * outgoing;
    if !result.is_finite() {
        return None;
    }
    Some((result.max(0.0)).round_ties_even() as i64)
}
fn usd(micros: i64) -> f64 {
    micros as f64 / 1_000_000.0
}
#[derive(Debug, Deserialize)]
struct UsageQuery {
    hours: Option<i64>,
    recent_limit: Option<i64>,
}
async fn usage_get(
    State(state): State<AppState>,
    Query(q): Query<UsageQuery>,
) -> AppResult<Json<Value>> {
    let hours = q.hours.unwrap_or(24);
    let limit = q.recent_limit.unwrap_or(50);
    if !(1..=2160).contains(&hours) || !(1..=200).contains(&limit) {
        return Err(AppError::Invalid(
            "hours must be 1..2160 and recent_limit 1..200".into(),
        ));
    }
    let since = Utc::now() - Duration::hours(hours);
    let pricing = pricing_read(&state.db).await?;
    let models = pricing.get("models").unwrap_or(&Value::Null);
    let grouped=sqlx::query(
        "SELECT provider,model,COUNT(*)::BIGINT AS requests,
         COUNT(*) FILTER(WHERE NOT success)::BIGINT AS failures,
         COALESCE(SUM(input_tokens),0)::BIGINT AS input_tokens,
         COALESCE(SUM(output_tokens),0)::BIGINT AS output_tokens,
         COALESCE(SUM(total_tokens),0)::BIGINT AS total_tokens,
         COALESCE(SUM(estimated_cost_microusd),0)::BIGINT AS stored_cost,
         COUNT(estimated_cost_microusd)::BIGINT AS stored_priced,
         COUNT(*) FILTER (WHERE estimated_cost_microusd IS NULL)::BIGINT AS unpriced,
         COALESCE(SUM(input_tokens) FILTER(WHERE estimated_cost_microusd IS NULL),0)::BIGINT AS unpriced_input,
         COALESCE(SUM(output_tokens) FILTER(WHERE estimated_cost_microusd IS NULL),0)::BIGINT AS unpriced_output
         FROM ai_usage_events WHERE created_at >= $1
         GROUP BY provider,model"
    ).bind(since).fetch_all(&state.db.pool).await?;
    let mut requests = 0_i64;
    let mut failures = 0_i64;
    let mut input_tokens = 0_i64;
    let mut output_tokens = 0_i64;
    let mut total_tokens = 0_i64;
    let mut total_cost = 0_i64;
    let mut priced_requests = 0_i64;
    let mut by_model = Vec::new();
    for row in grouped {
        let provider: String = row.try_get("provider")?;
        let model: String = row.try_get("model")?;
        let count: i64 = row.try_get("requests")?;
        let failed: i64 = row.try_get("failures")?;
        let input: i64 = row.try_get("input_tokens")?;
        let output: i64 = row.try_get("output_tokens")?;
        let total: i64 = row.try_get("total_tokens")?;
        let mut cost: i64 = row.try_get("stored_cost")?;
        let mut priced: i64 = row.try_get("stored_priced")?;
        if let Some(reconstructed) = estimate(
            models,
            &provider,
            &model,
            row.try_get("unpriced_input")?,
            row.try_get("unpriced_output")?,
        ) {
            cost = cost.saturating_add(reconstructed);
            priced = priced.saturating_add(row.try_get::<i64, _>("unpriced")?);
        }
        requests += count;
        failures += failed;
        input_tokens += input;
        output_tokens += output;
        total_tokens += total;
        total_cost = total_cost.saturating_add(cost);
        priced_requests += priced;
        by_model.push(json!({
            "provider":provider,"model":model,"requests":count,"failures":failed,
            "input_tokens":input,"output_tokens":output,"total_tokens":total,
            "estimated_cost_usd": if priced>0 {Some(usd(cost))} else {None}
        }));
    }
    by_model.sort_by(|a, b| {
        b["requests"]
            .as_i64()
            .cmp(&a["requests"].as_i64())
            .then_with(|| a["provider"].as_str().cmp(&b["provider"].as_str()))
            .then_with(|| a["model"].as_str().cmp(&b["model"].as_str()))
    });
    let statuses = sqlx::query(
        "SELECT status_code,COUNT(*)::BIGINT AS count
         FROM ai_usage_events WHERE created_at>=$1 AND NOT success
         GROUP BY status_code ORDER BY count DESC,status_code ASC NULLS FIRST",
    )
    .bind(since)
    .fetch_all(&state.db.pool)
    .await?;
    let by_status = statuses
        .into_iter()
        .map(|row| -> AppResult<Value> {
            Ok(
                json!({"status_code":row.try_get::<Option<i32>,_>("status_code")?,
                  "count":row.try_get::<i64,_>("count")?}),
            )
        })
        .collect::<AppResult<Vec<_>>>()?;
    let recent_rows = sqlx::query(
        "SELECT id,task_id,provider,model,operation,success,status_code,
         input_tokens,output_tokens,total_tokens,latency_ms,estimated_cost_microusd,
         error_type,error_message,created_at FROM ai_usage_events
         WHERE created_at>=$1 ORDER BY created_at DESC,id DESC LIMIT $2",
    )
    .bind(since)
    .bind(limit)
    .fetch_all(&state.db.pool)
    .await?;
    let mut recent = Vec::with_capacity(recent_rows.len());
    for row in recent_rows {
        let provider: String = row.try_get("provider")?;
        let model: String = row.try_get("model")?;
        let stored: Option<i64> = row.try_get("estimated_cost_microusd")?;
        let input: i32 = row.try_get("input_tokens")?;
        let output: i32 = row.try_get("output_tokens")?;
        let effective =
            stored.or_else(|| estimate(models, &provider, &model, input as i64, output as i64));
        recent.push(json!({
            "id":row.try_get::<Uuid,_>("id")?,
            "task_id":row.try_get::<Option<Uuid>,_>("task_id")?,
            "provider":provider,"model":model,
            "operation":row.try_get::<String,_>("operation")?,
            "success":row.try_get::<bool,_>("success")?,
            "status_code":row.try_get::<Option<i32>,_>("status_code")?,
            "input_tokens":input,"output_tokens":output,
            "total_tokens":row.try_get::<i32,_>("total_tokens")?,
            "latency_ms":row.try_get::<i32,_>("latency_ms")?,
            "estimated_cost_usd":effective.map(usd),
            "error_type":row.try_get::<Option<String>,_>("error_type")?,
            "error_message":row.try_get::<Option<String>,_>("error_message")?,
            "created_at":row.try_get::<DateTime<Utc>,_>("created_at")?,
        }));
    }
    Ok(Json(json!({
        "hours":hours,"requests":requests,"successes":requests-failures,
        "failures":failures,"input_tokens":input_tokens,"output_tokens":output_tokens,
        "total_tokens":total_tokens,"priced_requests":priced_requests,
        "estimated_cost_usd":if priced_requests>0{Some(usd(total_cost))}else{None},
        "by_model":by_model,"by_status":by_status,"recent":recent
    })))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/storage", get(storage_get).put(storage_put))
        .route(
            "/operations/ai-usage/pricing",
            get(pricing_get).put(pricing_put),
        )
        .route("/operations/ai-usage", get(usage_get))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn storage_default_zero_disables_published_media_deletion() {
        assert_eq!(ttl_from_value(&json!({})), 0);
        assert_eq!(ttl_from_value(&json!({"asset_ttl_days":0})), 0);
        assert_eq!(ttl_from_value(&json!({"asset_ttl_days":3651})), 3650);
        assert_eq!(ttl_from_value(&json!({"asset_ttl_days":-1})), 0);
    }
    #[test]
    fn pricing_refuses_negative_nonfinite_and_bad_models() {
        assert!(
            validate_pricing(&json!({"models":{"provider:gpt":{"input_per_million_usd":-1}}}))
                .is_err()
        );
        assert!(validate_pricing(&json!({"models":{"":{}}})).is_err());
        assert!(validate_pricing(&json!({"models":{"gpt":4}})).is_err());
    }
    #[test]
    fn pricing_uses_provider_specific_then_fallback() {
        let pricing = json!({"provider:gpt":{"input_per_million_usd":1.0,"output_per_million_usd":3.0},
            "gpt":{"input_per_million_usd":10.0,"output_per_million_usd":10.0}});
        assert_eq!(estimate(&pricing, "provider", "gpt", 10, 10), Some(40));
        assert_eq!(estimate(&pricing, "else", "gpt", 10, 10), Some(200));
        assert_eq!(estimate(&pricing, "else", "missing", 10, 10), None);
    }
}
