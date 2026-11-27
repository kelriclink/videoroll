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
// Cost only when the provider reports both counters and the configured
// pricing contains this provider/model. Missing data is not zero cost.
pub(crate) async fn reported_cost(db:&Database,endpoint:&Url,model:&str,usage:&Value)->Option<u64>{
    let field=|a:&str,b:&str|usage[a].as_u64().or_else(||usage[b].as_u64()).and_then(|v|i32::try_from(v).ok());
    let input=field("prompt_tokens","input_tokens")?;
    let output=field("completion_tokens","output_tokens")?;
    let pricing=settings::effective(db,"ai.usage.pricing").await.ok()?;
    estimated_cost(&pricing,&provider_name(endpoint),model,input,output).and_then(|v|u64::try_from(v).ok())
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
// Keep only final assistant content and token usage. Reasoning is never
// persisted or returned as a translation. The existing HTTP byte budget
// covers all SSE data including reasoning and incomplete events.
fn decode_chat_envelope(raw: &[u8], sse: bool) -> Result<Value, ()> {
    if !sse {
        return serde_json::from_slice(raw).map_err(|_| ());
    }
    let text = std::str::from_utf8(raw).map_err(|_| ())?;
    let mut content = String::new();
    let mut usage = Value::Null;
    let mut done = false;
    for line in text.lines() {
        let Some(data) = line.trim().strip_prefix("data:") else {
            continue;
        };
        let data = data.trim();
        if data == "[DONE]" {
            done = true;
            break;
        }
        let Ok(event) = serde_json::from_str::<Value>(data) else {
            continue;
        };
        if !event["error"].is_null() {
            return Err(());
        }
        if event["usage"].is_object() {
            usage = event["usage"].clone();
        }
        if let Some(choice) = event.pointer("/choices/0") {
            if let Some(finish) = choice["finish_reason"].as_str() {
                if finish == "stop" {
                    done = true;
                } else if matches!(finish, "length" | "content_filter" | "tool_calls") {
                    return Err(());
                }
            }
            let delta = choice.get("delta").or_else(|| choice.get("message"));
            if let Some(delta) = delta {
                match &delta["content"] {
                    Value::String(v) => content.push_str(v),
                    Value::Array(values) => {
                        for v in values {
                            if let Some(t) = v.as_str().or_else(|| v["text"].as_str()) {
                                content.push_str(t);
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    if !done || content.trim().is_empty() {
        return Err(());
    }
    Ok(serde_json::json!({"choices":[{"message":{"content":content}}],"usage":usage}))
}
fn rejected_field(raw: &[u8]) -> Option<&'static str> {
    let value: Value = serde_json::from_slice(raw).ok()?;
    let err = value.get("error").unwrap_or(&value);
    let code = err["code"].as_str().unwrap_or("").to_ascii_lowercase();
    let message = err["message"]
        .as_str()
        .or_else(|| err.as_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if code == "context_length_exceeded"
        || code == "context_window_exceeded"
        || [
            "maximum context length",
            "context length exceeded",
            "context window exceeded",
            "input tokens exceed",
        ]
        .iter()
        .any(|v| message.contains(v))
    {
        return Some("context");
    }
    if message.contains("enable_thinking")
        && [
            "unsupported",
            "unknown",
            "not permitted",
            "not allowed",
            "extra forbidden",
            "unrecognized",
        ]
        .iter()
        .any(|v| message.contains(v))
    {
        return Some("thinking");
    }
    None
}
async fn bounded_error(mut response: reqwest::Response) -> Option<&'static str> {
    let mut raw = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if chunk.len() > 16384usize.saturating_sub(raw.len()) {
            return None;
        }
        raw.extend_from_slice(&chunk);
    }
    rejected_field(&raw)
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
    chat_json_inner(
        db,
        request,
        endpoint,
        model,
        operation,
        task_id,
        max_bytes,
        error_label,
        observed,
        None,
    )
    .await
}
#[allow(clippy::too_many_arguments)]
pub(crate) async fn chat_json_traced(
    db: &Database,
    request: RequestBuilder,
    endpoint: &Url,
    model: &str,
    operation: &str,
    task_id: Option<Uuid>,
    max_bytes: usize,
    error_label: &str,
) -> AppResult<Value> {
    let mut trace = crate::media::translation_trace::Trace::start(db, model, operation).await;
    let result = chat_json_inner(
        db,
        request,
        endpoint,
        model,
        operation,
        task_id,
        max_bytes,
        error_label,
        None,
        trace.as_mut(),
    )
    .await;
    if let Some(trace) = trace {
        trace.finish(result.is_ok()).await;
    }
    result
}
#[allow(clippy::too_many_arguments)]
async fn chat_json_inner(
    db: &Database,
    request: RequestBuilder,
    endpoint: &Url,
    model: &str,
    operation: &str,
    task_id: Option<Uuid>,
    max_bytes: usize,
    error_label: &str,
    observed: Option<&mut Option<HttpFailure>>,
    mut trace: Option<&mut crate::media::translation_trace::Trace>,
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
        let reason = if status.as_u16() == 400 {
            bounded_error(response).await
        } else {
            None
        };
        return Err(AppError::Unavailable(match reason {
            Some("context") => {
                "translation prompt exceeds context budget: provider rejected context length".into()
            }
            Some("thinking") => "OpenAI provider explicitly rejected enable_thinking".into(),
            _ => format!("{error_label} HTTP {status}"),
        }));
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
    let sse = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.to_ascii_lowercase().contains("text/event-stream"));
    let mut response = response;
    let mut raw = Vec::new();
    let mut line_start = 0;
    let mut scanned = 0;
    loop {
        match response.chunk().await {
            Ok(Some(chunk)) if chunk.len() <= max_bytes.saturating_sub(raw.len()) => {
                raw.extend_from_slice(&chunk);
                if sse {
                    if let Some(trace) = trace.as_deref_mut() {
                        for end in scanned..raw.len() {
                            if raw[end] == b'\n' {
                                trace.consume(&raw[line_start..end]).await;
                                line_start = end + 1;
                            }
                        }
                        scanned = raw.len();
                    }
                }
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
    if sse && line_start < raw.len() {
        if let Some(trace) = trace.as_deref_mut() {
            trace.consume(&raw[line_start..]).await;
        }
    }
    let envelope: Value = match decode_chat_envelope(&raw, sse) {
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
    fn sse_final_content_usage_and_typed_rejections() {
        let raw = concat!(
            ": keep-alive\n",
            "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"private reasoning\"}}]}\r\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"{\\\"answer\\\":\"}}]}\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"1}\"},\"finish_reason\":\"stop\"}]}\n",
            "data: {\"usage\":{\"total_tokens\":17}}\n",
            "data: [DONE]\n"
        );
        let result = decode_chat_envelope(raw.as_bytes(), true).unwrap();
        assert_eq!(result["choices"][0]["message"]["content"], "{\"answer\":1}");
        assert_eq!(result["usage"]["total_tokens"], 17);
        assert!(!result.to_string().contains("private reasoning"));
        assert!(
            decode_chat_envelope(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n",
                true
            )
            .is_err()
        );
        assert!(
            decode_chat_envelope(
                b"data: {\"error\":{\"message\":\"fail\"}}\ndata: [DONE]",
                true
            )
            .is_err()
        );
        assert_eq!(
            rejected_field(br#"{"error":{"code":"context_length_exceeded"}}"#),
            Some("context")
        );
        assert_eq!(
            rejected_field(br#"{"error":{"message":"unknown enable_thinking"}}"#),
            Some("thinking")
        );
        assert_eq!(
            rejected_field(br#"{"error":{"message":"authentication failed"}}"#),
            None
        );
    }
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
