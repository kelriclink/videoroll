//! Best-effort per-attempt OpenAI-compatible AI usage capture.
//! No prompts, response bodies, API keys, URLs with credentials or cookies
//! are stored. AI telemetry failures must never break the media workflow.
use crate::{AppError, AppResult, Database, settings};
use reqwest::{RequestBuilder, Url};
use serde_json::Value;
use std::time::Instant;
use uuid::Uuid;

fn provider_name(url: &Url) -> String {
    let host = url.host_str().unwrap_or("").to_ascii_lowercase();
    if host.contains("cerebras") {
        "cerebras".into()
    } else if host.ends_with("openai.com") {
        "openai".into()
    } else if host.is_empty() {
        "openai".into()
    } else {
        host.chars().take(64).collect()
    }
}
fn counts(v: Option<&Value>) -> (i32, i32, i32) {
    let field = |keys: &[&str]| -> i32 {
        keys.iter()
            .find_map(|k| v.and_then(|o| o.get(*k)).and_then(Value::as_i64))
            .unwrap_or(0)
            .clamp(0, i32::MAX as i64) as i32
    };
    let input = field(&["prompt_tokens", "input_tokens"]);
    let output = field(&["completion_tokens", "output_tokens"]);
    let counted = field(&["total_tokens"]);
    let total = if counted > 0 {
        counted
    } else {
        input.saturating_add(output)
    };
    (input, output, total)
}
fn estimated_cost(
    value: &Value,
    provider: &str,
    model: &str,
    input: i32,
    output: i32,
) -> Option<i64> {
    let models = value.get("models")?.as_object()?;
    let rate = models
        .get(&format!("{provider}:{model}"))
        .or_else(|| models.get(model))?
        .as_object()?;
    let input_rate = rate.get("input_per_million_usd")?.as_f64()?;
    let output_rate = rate.get("output_per_million_usd")?.as_f64()?;
    if !input_rate.is_finite() || !output_rate.is_finite() || input_rate < 0.0 || output_rate < 0.0
    {
        return None;
    }
    let total = (input as f64 * input_rate + output as f64 * output_rate).round_ties_even();
    if !total.is_finite() || total < 0.0 || total > i64::MAX as f64 {
        return None;
    }
    Some(total as i64)
}
struct UsageRecord<'a> {
    url: &'a Url,
    model: &'a str,
    operation: &'a str,
    task_id: Option<Uuid>,
    success: bool,
    status: Option<u16>,
    usage: Option<&'a Value>,
    elapsed: std::time::Duration,
    error_type: Option<&'a str>,
}
async fn save(db: &Database, item: UsageRecord<'_>) {
    let provider = provider_name(item.url);
    let (input, output, total) = counts(item.usage);
    let price = match settings::effective(db, "ai.usage.pricing").await {
        Ok(v) => estimated_cost(&v, &provider, item.model, input, output),
        Err(e) => {
            tracing::warn!(error=%e,"AI usage pricing read unavailable");
            None
        }
    };
    // Fail-soft: telemetry is not part of the AI result transaction.
    let result = sqlx::query(
        "INSERT INTO ai_usage_events
          (id,task_id,provider,model,operation,success,status_code,
           input_tokens,output_tokens,total_tokens,latency_ms,estimated_cost_microusd,
           error_type,error_message)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
    )
    .bind(Uuid::new_v4())
    .bind(item.task_id)
    .bind(provider)
    .bind(item.model.chars().take(255).collect::<String>())
    .bind(item.operation.chars().take(96).collect::<String>())
    .bind(item.success)
    .bind(item.status.map(i32::from))
    .bind(input)
    .bind(output)
    .bind(total)
    .bind(item.elapsed.as_millis().min(i32::MAX as u128) as i32)
    .bind(price)
    .bind(item.error_type)
    .bind(
        item.error_type
            .map(|_| "model HTTP/transport/response failure"),
    )
    .execute(&db.pool)
    .await;
    if let Err(e) = result {
        tracing::warn!(error=%e,operation=item.operation,"AI request usage could not be recorded");
    }
}
/// Fetch and parse one request, recording even HTTP, transport and JSON errors.
/// Call it for every *attempt*, not only the last retry. A success means the
/// HTTP response is a bounded JSON envelope; model output validation follows.
#[allow(clippy::too_many_arguments)]
pub async fn chat_json(
    db: &Database,
    request: RequestBuilder,
    endpoint: &Url,
    model: &str,
    operation: &str,
    task_id: Option<Uuid>,
    max_bytes: usize,
    error_label: &str,
) -> AppResult<Value> {
    chat_json_observed(
        db,
        request,
        endpoint,
        model,
        operation,
        task_id,
        max_bytes,
        error_label,
        None,
    )
    .await
}
#[derive(Clone, Copy, Debug)]
pub(crate) struct HttpFailure {
    pub status: u16,
    pub retry_after: Option<f64>,
}
fn numeric_retry_after(value: Option<&reqwest::header::HeaderValue>) -> Option<f64> {
    value
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0)
}
#[allow(clippy::too_many_arguments)]
pub(crate) async fn chat_json_observed(
    db: &Database,
    request: RequestBuilder,
    endpoint: &Url,
    model: &str,
    operation: &str,
    task_id: Option<Uuid>,
    max_bytes: usize,
    error_label: &str,
    observed: Option<&mut Option<HttpFailure>>,
) -> AppResult<Value> {
    let started = Instant::now();
    let response = match request.send().await {
        Ok(v) => v,
        Err(e) => {
            save(
                db,
                UsageRecord {
                    url: endpoint,
                    model,
                    operation,
                    task_id,
                    success: false,
                    status: None,
                    usage: None,
                    elapsed: started.elapsed(),
                    error_type: Some("TransportError"),
                },
            )
            .await;
            return Err(AppError::Upstream(e));
        }
    };
    let status = response.status();
    if !status.is_success() {
        if let Some(out) = observed {
            *out = Some(HttpFailure {
                status: status.as_u16(),
                retry_after: numeric_retry_after(
                    response.headers().get(reqwest::header::RETRY_AFTER),
                ),
            });
        }
        save(
            db,
            UsageRecord {
                url: endpoint,
                model,
                operation,
                task_id,
                success: false,
                status: Some(status.as_u16()),
                usage: None,
                elapsed: started.elapsed(),
                error_type: Some("HttpError"),
            },
        )
        .await;
        return Err(AppError::Unavailable(format!(
            "{error_label} HTTP {status}"
        )));
    }
    if response
        .content_length()
        .is_some_and(|n| n > max_bytes as u64)
    {
        save(
            db,
            UsageRecord {
                url: endpoint,
                model,
                operation,
                task_id,
                success: false,
                status: Some(status.as_u16()),
                usage: None,
                elapsed: started.elapsed(),
                error_type: Some("OversizedResponse"),
            },
        )
        .await;
        return Err(AppError::Unavailable(format!(
            "{error_label} response too large"
        )));
    }
    let mut response = response;
    let mut raw = Vec::new();
    loop {
        match response.chunk().await {
            Ok(Some(chunk)) if chunk.len() <= max_bytes.saturating_sub(raw.len()) => {
                raw.extend_from_slice(&chunk)
            }
            Ok(Some(_)) => {
                save(
                    db,
                    UsageRecord {
                        url: endpoint,
                        model,
                        operation,
                        task_id,
                        success: false,
                        status: Some(status.as_u16()),
                        usage: None,
                        elapsed: started.elapsed(),
                        error_type: Some("OversizedResponse"),
                    },
                )
                .await;
                return Err(AppError::Unavailable(format!(
                    "{error_label} response exceeds budget"
                )));
            }
            Ok(None) => break,
            Err(e) => {
                save(
                    db,
                    UsageRecord {
                        url: endpoint,
                        model,
                        operation,
                        task_id,
                        success: false,
                        status: Some(status.as_u16()),
                        usage: None,
                        elapsed: started.elapsed(),
                        error_type: Some("ResponseReadError"),
                    },
                )
                .await;
                return Err(AppError::Upstream(e));
            }
        }
    }
    let envelope: Value = match serde_json::from_slice(&raw) {
        Ok(v) => v,
        Err(_) => {
            save(
                db,
                UsageRecord {
                    url: endpoint,
                    model,
                    operation,
                    task_id,
                    success: false,
                    status: Some(status.as_u16()),
                    usage: None,
                    elapsed: started.elapsed(),
                    error_type: Some("InvalidResponseJson"),
                },
            )
            .await;
            return Err(AppError::Unavailable(format!(
                "{error_label} response JSON invalid"
            )));
        }
    };
    save(
        db,
        UsageRecord {
            url: endpoint,
            model,
            operation,
            task_id,
            success: true,
            status: Some(status.as_u16()),
            usage: envelope.get("usage"),
            elapsed: started.elapsed(),
            error_type: None,
        },
    )
    .await;
    Ok(envelope)
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn counts_and_pricing_match_python_micro_usd() {
        assert_eq!(
            provider_name(&Url::parse("https://api.openai.com/v1/chat/completions").unwrap()),
            "openai"
        );
        let value = json!({"prompt_tokens":123,"completion_tokens":77});
        assert_eq!(counts(Some(&value)), (123, 77, 200));
        assert_eq!(
            estimated_cost(
                &json!({"models":{"openai:model":{"input_per_million_usd":2.0,
            "output_per_million_usd":4.0}}}),
                "openai",
                "model",
                1000,
                500
            ),
            Some(4000)
        );
        assert_eq!(
            estimated_cost(&json!({"models":{}}), "openai", "unknown", 100, 100),
            None
        );
    }
}
