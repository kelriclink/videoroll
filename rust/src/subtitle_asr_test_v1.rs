//! Native v1 ASR provider test endpoints. Generates 1s mono PCM-16 WAV,
//! sends real provider HTTP requests, decodes provider segments and exposes
//! Python's test response DTO. No fake success or production task created.
use crate::{AppError, AppResult, api::AppState, providers};
use axum::{Json, Router, extract::State, routing::post};
use base64::{Engine, engine::general_purpose::STANDARD};
use reqwest::{Client, Url, multipart};
use serde_json::{Value, json};
use std::{
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{io::AsyncWriteExt, process::Command, time::timeout};

fn input_text<'a>(v: &'a Value, key: &str, default: &'a str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or(default).trim()
}
fn valid_input(v: &Value, key: &str, max: usize, required: bool) -> AppResult<()> {
    if let Some(s) = v.get(key) {
        let s = s
            .as_str()
            .ok_or_else(|| AppError::Invalid(format!("{key} must be a string")))?;
        if s.chars().count() > max || (required && s.is_empty()) {
            return Err(AppError::Invalid(format!("{key} length is out of range")));
        }
    } else if required {
        return Err(AppError::Invalid(format!("{key} is required")));
    }
    Ok(())
}
fn valid_numeric(v: &Value, key: &str, min: f64, max: f64) -> AppResult<()> {
    if let Some(x) = v.get(key).filter(|v| !v.is_null()) {
        let n = x
            .as_f64()
            .ok_or_else(|| AppError::Invalid(format!("{key} must be numeric")))?;
        if !n.is_finite() || n < min || n > max {
            return Err(AppError::Invalid(format!("{key} out of range")));
        }
    }
    Ok(())
}
fn valid_bool(v: &Value, key: &str) -> AppResult<()> {
    if let Some(x) = v.get(key).filter(|v| !v.is_null()) {
        if !x.is_boolean() {
            return Err(AppError::Invalid(format!("{key} must be boolean")));
        }
    }
    Ok(())
}
async fn asr_stored(state: &AppState) -> AppResult<Value> {
    let raw: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.asr'")
            .fetch_optional(&state.db.pool)
            .await?;
    Ok(raw.unwrap_or(Value::Null))
}
fn api_key(request: &Value, settings: &Value, section: &str, env: &str) -> AppResult<String> {
    let provided = input_text(request, "api_key", "");
    if !provided.is_empty() {
        return Ok(provided.to_string());
    }
    let encrypted = settings
        .get(section)
        .and_then(|x| x.get("api_key_enc"))
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok(providers::decode_secret(encrypted)?
        .or_else(|| std::env::var(env).ok().filter(|s| !s.is_empty()))
        .unwrap_or_default())
}
fn wav() -> Vec<u8> {
    let mut out = Vec::with_capacity(32044);
    out.extend(b"RIFF");
    out.extend((32036_u32).to_le_bytes());
    out.extend(b"WAVEfmt ");
    out.extend(16_u32.to_le_bytes());
    out.extend(1_u16.to_le_bytes()); // PCM
    out.extend(1_u16.to_le_bytes()); // mono
    out.extend(16000_u32.to_le_bytes());
    out.extend(32000_u32.to_le_bytes());
    out.extend(2_u16.to_le_bytes());
    out.extend(16_u16.to_le_bytes());
    out.extend(b"data");
    out.extend(32000_u32.to_le_bytes());
    out.resize(32044, 0);
    out
}
fn base_url(input: &str) -> AppResult<Url> {
    let u = Url::parse(input).map_err(|_| AppError::Invalid("ASR API URL invalid".into()))?;
    if !matches!(u.scheme(), "http" | "https")
        || u.host().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
    {
        return Err(AppError::Invalid("ASR API URL invalid".into()));
    }
    Ok(u)
}
fn audio_url(base: &str) -> AppResult<Url> {
    let mut url = base_url(base)?;
    let mut path = url.path().trim_end_matches('/').to_owned();
    if path.ends_with("/audio/transcriptions") {
        return Ok(url);
    }
    if path.ends_with("/chat/completions") {
        path.truncate(path.len() - "/chat/completions".len());
    }
    if !path.ends_with("/v1") {
        path.push_str("/v1");
    }
    path.push_str("/audio/transcriptions");
    url.set_path(&path);
    Ok(url)
}
fn test_override(var: &str, production: &str) -> AppResult<String> {
    match std::env::var(var) {
        Ok(raw) => {
            let url = base_url(&raw)?;
            let loopback = matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
            if !loopback
                || !matches!(
                    std::env::var("VIDEOROLL_V2_ALLOW_TEST_ASR_LOOPBACK")
                        .ok()
                        .as_deref(),
                    Some("true" | "1")
                )
            {
                return Err(AppError::Invalid(
                    "test ASR override requires loopback".into(),
                ));
            }
            Ok(raw)
        }
        Err(_) => Ok(production.into()),
    }
}
fn normalized_text(v: &str) -> String {
    v.split_whitespace().collect::<Vec<_>>().join(" ")
}
fn parse_segments(data: &Value) -> Vec<(f64, f64, String)> {
    let mut result = Vec::new();
    if let Some(segments) = data.get("segments").and_then(Value::as_array) {
        for segment in segments {
            let s = segment
                .get("start")
                .and_then(Value::as_f64)
                .unwrap_or(0.0)
                .max(0.0);
            let e = segment
                .get("end")
                .and_then(Value::as_f64)
                .unwrap_or(s)
                .max(s);
            let t = normalized_text(segment.get("text").and_then(Value::as_str).unwrap_or(""));
            if !t.is_empty() {
                result.push((s, e, t));
            }
        }
    }
    if !result.is_empty() {
        return result;
    }
    if let Some(words) = data.get("words").and_then(Value::as_array) {
        let mut kept = Vec::new();
        for word in words {
            let t = normalized_text(word.get("word").and_then(Value::as_str).unwrap_or(""));
            if !t.is_empty() {
                let s = word
                    .get("start")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0)
                    .max(0.0);
                let e = word.get("end").and_then(Value::as_f64).unwrap_or(s).max(s);
                kept.push((s, e, t));
            }
        }
        if let (Some(a), Some(b)) = (kept.first(), kept.last()) {
            result.push((
                a.0,
                b.1,
                kept.iter()
                    .map(|x| x.2.as_str())
                    .collect::<Vec<_>>()
                    .join(" "),
            ));
        }
    }
    if !result.is_empty() {
        return result;
    }
    let text = normalized_text(data.get("text").and_then(Value::as_str).unwrap_or(""));
    if !text.is_empty() {
        result.push((0.0, 1.0, text));
    }
    result
}
fn success(segments: &[(f64, f64, String)], elapsed: u128, external: bool) -> Value {
    let words = segments
        .iter()
        .map(|v| v.2.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let mut result = json!({
        "ok":true,"status_code":null,"elapsed_ms":elapsed as u64,
        "text":words,"segments":segments.len(),"error":null
    });
    if external {
        result["longest_segment_seconds"] =
            json!(segments.iter().map(|(s, e, _)| e - s).fold(0_f64, f64::max));
        result["longest_segment_chars"] = json!(
            segments
                .iter()
                .map(|(_, _, t)| t.chars().count())
                .max()
                .unwrap_or(0)
        );
    }
    result
}
fn failure(msg: &str, elapsed: u128, external: bool) -> Value {
    let mut result = json!({
        "ok":false,"status_code":null,"elapsed_ms":elapsed as u64,
        "text":"","segments":0,"error":msg
    });
    if external {
        result["longest_segment_seconds"] = json!(0.0);
        result["longest_segment_chars"] = json!(0);
    }
    result
}
fn client() -> AppResult<Client> {
    Ok(Client::builder().timeout(Duration::from_secs(30)).build()?)
}
fn asr_option(
    body: &Value,
    settings: &Value,
    request_key: &str,
    stored_key: &str,
    default: &str,
) -> String {
    let value = body
        .get(request_key)
        .filter(|v| !v.is_null())
        .or_else(|| settings.get(stored_key).filter(|v| !v.is_null()));
    match value {
        Some(Value::Bool(v)) => v.to_string(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::String(s)) => s.to_string(),
        _ => default.into(),
    }
}
async fn post_audio(
    url: Url,
    api_key: &str,
    model: &str,
    audio: Vec<u8>,
    codec: &str,
    external_config: Option<(&Value, &Value)>,
) -> AppResult<Value> {
    let content_type = if codec == "flac" {
        "audio/flac"
    } else {
        "audio/wav"
    };
    let name = if codec == "flac" {
        "asr-test-part-1.flac"
    } else {
        "asr-test.wav"
    };
    let file = multipart::Part::bytes(audio)
        .file_name(name)
        .mime_str(content_type)
        .map_err(|_| AppError::Invalid("invalid media MIME".into()))?;
    let mut form = multipart::Form::new()
        .text("model", model.to_string())
        .text("response_format", "verbose_json")
        .text("temperature", "0")
        .part("file", file);
    if let Some((body, settings)) = external_config {
        form = form
            .text("beam_size", "5")
            .text(
                "batch_size",
                asr_option(
                    body,
                    settings,
                    "batch_size",
                    "external_whisper_batch_size",
                    "1",
                ),
            )
            .text("word_timestamps", "true")
            .text(
                "vad_filter",
                asr_option(
                    body,
                    settings,
                    "vad_enabled",
                    "external_whisper_vad_enabled",
                    "true",
                ),
            )
            .text(
                "vad_threshold",
                asr_option(
                    body,
                    settings,
                    "vad_threshold",
                    "external_whisper_vad_threshold",
                    "0.5",
                ),
            )
            .text(
                "min_silence_duration_ms",
                asr_option(
                    body,
                    settings,
                    "min_silence_ms",
                    "external_whisper_min_silence_ms",
                    "2000",
                ),
            )
            .text(
                "speech_pad_ms",
                asr_option(
                    body,
                    settings,
                    "speech_pad_ms",
                    "external_whisper_speech_pad_ms",
                    "400",
                ),
            )
            .text(
                "condition_on_previous_text",
                asr_option(
                    body,
                    settings,
                    "condition_on_previous_text",
                    "external_whisper_condition_on_previous_text",
                    "true",
                ),
            )
            .text("no_speech_threshold", "0.6")
            .text("log_prob_threshold", "-1.0")
            .text("compression_ratio_threshold", "2.4");
    }
    let mut request = client()?.post(url.clone()).multipart(form);
    if !api_key.is_empty() {
        request = request.bearer_auth(api_key);
    }
    let mut response = request.send().await?;
    if external_config.is_some() && matches!(response.status().as_u16(), 400 | 422) {
        let file = multipart::Part::bytes(wav())
            .file_name("asr-test.wav")
            .mime_str("audio/wav")
            .map_err(|_| AppError::Invalid("invalid media MIME".into()))?;
        let minimal = multipart::Form::new()
            .text("model", model.to_string())
            .text("response_format", "verbose_json")
            .part("file", file);
        let mut request = client()?.post(url).multipart(minimal);
        if !api_key.is_empty() {
            request = request.bearer_auth(api_key);
        }
        response = request.send().await?;
    }
    let status = response.status();
    if !status.is_success() {
        let content = response.text().await.unwrap_or_default();
        return Err(AppError::Unavailable(format!(
            "Whisper API failed (status={}): {}",
            status.as_u16(),
            content.chars().take(500).collect::<String>()
        )));
    }
    let data: Value = response
        .json()
        .await
        .map_err(|_| AppError::Invalid("Whisper API returned invalid JSON".into()))?;
    if !data.is_object() {
        return Err(AppError::Invalid(
            "Whisper API response must be an object".into(),
        ));
    }
    Ok(data)
}
async fn flac(wav_bytes: Vec<u8>) -> AppResult<Vec<u8>> {
    let mut child =
        Command::new(std::env::var("VIDEOROLL_V2_FFMPEG").unwrap_or_else(|_| "ffmpeg".into()))
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "wav",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "flac",
                "pipe:1",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(&wav_bytes).await?;
    }
    let output = timeout(Duration::from_secs(20), child.wait_with_output())
        .await
        .map_err(|_| AppError::Unavailable("Groq test FLAC conversion timed out".into()))??;
    if !output.status.success() || output.stdout.is_empty() {
        return Err(AppError::Unavailable(
            "Groq test FLAC conversion failed".into(),
        ));
    }
    Ok(output.stdout)
}
async fn external_test(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    valid_input(&body, "base_url", 2048, true)?;
    valid_input(&body, "api_key", 4096, false)?;
    valid_input(&body, "model", 256, false)?;
    for (k, a, b) in [
        ("batch_size", 1.0, 32.0),
        ("vad_threshold", 0.1, 0.95),
        ("min_silence_ms", 50.0, 5000.0),
        ("speech_pad_ms", 0.0, 2000.0),
        ("max_segment_seconds", 1.0, 30.0),
        ("max_segment_chars", 10.0, 500.0),
    ] {
        valid_numeric(&body, k, a, b)?;
    }
    for k in ["vad_enabled", "condition_on_previous_text"] {
        valid_bool(&body, k)?;
    }
    let started = Instant::now();
    let outcome = async {
        let url = audio_url(input_text(&body, "base_url", ""))?;
        let settings = asr_stored(&state).await?;
        let secret = api_key(
            &body,
            &settings,
            "external_whisper",
            "SUBTITLE_EXTERNAL_WHISPER_API_KEY",
        )?;
        let model = input_text(&body, "model", "whisper-1");
        post_audio(
            url,
            &secret,
            model,
            wav(),
            "wav",
            Some((&body, &crate::subtitle_asr_settings_v1::view(&settings))),
        )
        .await
    }
    .await;
    Ok(Json(match outcome {
        Ok(response) => success(
            &parse_segments(&response),
            started.elapsed().as_millis(),
            true,
        ),
        Err(e) => failure(&e.to_string(), started.elapsed().as_millis(), true),
    }))
}
async fn groq_test(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    valid_input(&body, "api_key", 4096, false)?;
    valid_input(&body, "model", 256, false)?;
    let started = Instant::now();
    let outcome = async {
        let model = input_text(&body, "model", "whisper-large-v3-turbo");
        if !matches!(model, "whisper-large-v3" | "whisper-large-v3-turbo") {
            return Err(AppError::Invalid(
                "Groq Whisper model must be whisper-large-v3 or whisper-large-v3-turbo".into(),
            ));
        }
        let settings = asr_stored(&state).await?;
        let secret = api_key(
            &body,
            &settings,
            "groq_whisper",
            "SUBTITLE_GROQ_WHISPER_API_KEY",
        )?;
        if secret.is_empty() {
            return Err(AppError::Invalid("Groq Whisper API key is not set".into()));
        }
        let base = test_override(
            "VIDEOROLL_V2_TEST_GROQ_BASE",
            "https://api.groq.com/openai/v1",
        )?;
        let url = audio_url(&base)?;
        let audio = flac(wav()).await?;
        post_audio(url, &secret, model, audio, "flac", None).await
    }
    .await;
    Ok(Json(match outcome {
        Ok(response) => success(
            &parse_segments(&response),
            started.elapsed().as_millis(),
            false,
        ),
        Err(e) => failure(&e.to_string(), started.elapsed().as_millis(), false),
    }))
}
async fn cloudflare_test(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    valid_input(&body, "account_id", 128, true)?;
    valid_input(&body, "model", 256, true)?;
    valid_input(&body, "api_key", 4096, false)?;
    let started = Instant::now();
    let outcome = async {
        let account = input_text(&body, "account_id", "");
        let model = input_text(&body, "model", "");
        if !account
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
        {
            return Err(AppError::Invalid(
                "Cloudflare Workers AI account ID contains invalid characters".into(),
            ));
        }
        if !model.starts_with("@cf/")
            || !model
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'@' | b'/' | b'_' | b'.' | b'-'))
        {
            return Err(AppError::Invalid(
                "Cloudflare Workers AI model must be a valid @cf/ model ID".into(),
            ));
        }
        let settings = asr_stored(&state).await?;
        let secret = api_key(
            &body,
            &settings,
            "cloudflare_workers_ai",
            "SUBTITLE_CLOUDFLARE_API_KEY",
        )?;
        if secret.is_empty() {
            return Err(AppError::Invalid(
                "Cloudflare Workers AI API key is not set".into(),
            ));
        }
        let base = test_override(
            "VIDEOROLL_V2_TEST_CLOUDFLARE_BASE",
            "https://api.cloudflare.com",
        )?;
        let mut url = base_url(&base)?;
        url.set_path(&format!("/client/v4/accounts/{account}/ai/run/{model}"));
        let response = client()?
            .post(url)
            .bearer_auth(secret)
            .json(&json!({"audio":STANDARD.encode(wav()),"task":"transcribe"}))
            .send()
            .await?;
        if !response.status().is_success() {
            return Err(AppError::Unavailable(format!(
                "Cloudflare Workers AI request failed (status={})",
                response.status().as_u16()
            )));
        }
        let data: Value = response
            .json()
            .await
            .map_err(|_| AppError::Invalid("Cloudflare Workers AI returned invalid JSON".into()))?;
        if data.get("success") == Some(&Value::Bool(false)) {
            return Err(AppError::Invalid(format!(
                "Cloudflare Workers AI request failed: {}",
                data.get("errors").unwrap_or(&Value::Null)
            )));
        }
        Ok(data
            .get("result")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or(data))
    }
    .await;
    Ok(Json(match outcome {
        Ok(response) => success(
            &parse_segments(&response),
            started.elapsed().as_millis(),
            false,
        ),
        Err(e) => failure(&e.to_string(), started.elapsed().as_millis(), false),
    }))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/asr/external/test", post(external_test))
        .route("/subtitle/asr/groq/test", post(groq_test))
        .route("/subtitle/asr/cloudflare/test", post(cloudflare_test))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wave_and_url() {
        assert_eq!(wav().len(), 32044);
        assert_eq!(&wav()[0..4], b"RIFF");
        assert_eq!(
            audio_url("http://127.0.0.1:18091/v1").unwrap().path(),
            "/v1/audio/transcriptions"
        );
        assert!(base_url("file:///etc/passwd").is_err());
        assert!(valid_input(&json!({"account_id":"demo"}), "account_id", 128, true).is_ok());
        let s =
            parse_segments(&json!({"segments":[{"start":0.1,"end":0.9,"text":" Test  demo "}]}));
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].2, "Test demo");
    }
}
