//! Native ASR + translation using authenticated OpenAI-compatible APIs.
//! The more specialized legacy RAG/Whisper paths remain available as external workers.
use crate::{
    AppError, AppResult, Config, Database,
    domain::WorkItem,
    media::ffmpeg::{ensure_task_asset, latest_asset_key, probe_video},
    storage::AssetRegistry,
    subtitles::{SubtitleCue, to_ass, to_srt},
};
use reqwest::{Client, Url, multipart};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{path::Path, process::Stdio, time::Duration};
use tokio::{fs, process::Command, time::sleep};

const WINDOW: f64 = 45.0;
const OVERLAP: f64 = 4.0;
const MAX_BYTES: usize = 12 * 1024 * 1024;

#[derive(Debug, Deserialize)]
struct AsrSegment {
    start: f64,
    end: f64,
    text: String,
    #[serde(default)]
    no_speech_prob: Option<f64>,
    #[serde(default)]
    avg_logprob: Option<f64>,
    #[serde(default)]
    compression_ratio: Option<f64>,
}
#[derive(Debug, Deserialize)]
struct AsrResponse {
    #[serde(default)]
    segments: Vec<AsrSegment>,
    #[serde(default)]
    text: String,
}
#[derive(Debug, Serialize, Deserialize)]
struct Checkpoint {
    revision: u32,
    fingerprint: String,
    offset_ms: u64,
    cues: Vec<SubtitleCue>,
}
#[derive(Debug, Serialize, Deserialize)]
struct TranslationCheckpoint {
    revision: u32,
    fingerprint: String,
    translated: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct SubtitleProfile {
    pub translate_enabled: bool,
    pub target_lang: String,
    pub bilingual: bool,
    pub asr_language: String,
    pub translate_style: String,
}
impl Default for SubtitleProfile {
    fn default() -> Self {
        Self {
            translate_enabled: true,
            target_lang: "zh".into(),
            bilingual: false,
            asr_language: "auto".into(),
            translate_style: "口语自然".into(),
        }
    }
}
fn fingerprint(value: &Value) -> AppResult<String> {
    let raw = serde_json::to_vec(value)
        .map_err(|_| AppError::Invalid("unable to serialize checkpoint identity".into()))?;
    Ok(hex::encode(Sha256::digest(&raw)))
}
fn endpoint(value: &str) -> AppResult<Url> {
    let url = Url::parse(value).map_err(|_| AppError::Invalid("invalid model endpoint".into()))?;
    let allowed = url.scheme() == "https"
        || (url.scheme() == "http"
            && matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1")));
    if !allowed || !url.username().is_empty() || url.password().is_some() {
        return Err(AppError::Invalid(
            "model endpoint requires HTTPS or loopback HTTP".into(),
        ));
    }
    Ok(url)
}
async fn settings(db: &Database, job: &WorkItem) -> AppResult<SubtitleProfile> {
    let runtime: Option<Value> =
        Some(crate::settings::effective(db, "subtitle.auto_profile").await?);
    let mut v = serde_json::to_value(SubtitleProfile::default())
        .map_err(|_| AppError::Invalid("subtitle profile invalid".into()))?;
    if let (Some(target), Some(saved)) = (
        v.as_object_mut(),
        runtime.as_ref().and_then(Value::as_object),
    ) {
        for (k, v) in saved {
            target.insert(k.clone(), v.clone());
        }
    }
    let workflow: Option<Value> =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
            .bind(job.workflow_id)
            .fetch_optional(&db.pool)
            .await?;
    if let (Some(target), Some(overrides)) = (
        v.as_object_mut(),
        workflow
            .as_ref()
            .and_then(|v| v.get("subtitle"))
            .and_then(Value::as_object),
    ) {
        for (k, v) in overrides {
            target.insert(k.clone(), v.clone());
        }
    }
    serde_json::from_value(v).map_err(|_| AppError::Invalid("subtitle config types invalid".into()))
}
fn normalize_asr(
    resp: AsrResponse,
    offset_ms: u64,
    keep_after_ms: u64,
    duration_ms: u64,
) -> AppResult<Vec<SubtitleCue>> {
    if resp.segments.is_empty() && !resp.text.trim().is_empty() {
        return Err(AppError::Unavailable(
            "ASR returned text without segment timestamps".into(),
        ));
    }
    let mut items = Vec::new();
    for segment in resp.segments {
        if !segment.start.is_finite()
            || !segment.end.is_finite()
            || segment.start < 0.0
            || segment.end <= segment.start
        {
            continue;
        }
        // Whisper's hallucination indicators are advisory rather than a sole
        // hard stop. Require more than one suspicious signal before dropping.
        let mostly_silence = segment
            .no_speech_prob
            .is_some_and(|v| v.is_finite() && v > 0.85);
        let low_confidence = segment
            .avg_logprob
            .is_some_and(|v| v.is_finite() && v < -1.0);
        let repetitive = segment
            .compression_ratio
            .is_some_and(|v| v.is_finite() && v > 3.5);
        if (mostly_silence && low_confidence) || (low_confidence && repetitive) {
            continue;
        }
        let start = offset_ms.saturating_add((segment.start * 1000.0).round() as u64);
        let end = offset_ms
            .saturating_add((segment.end * 1000.0).round() as u64)
            .min(duration_ms);
        if start >= end || (start + end) / 2 < keep_after_ms || segment.text.trim().is_empty() {
            continue;
        }
        if segment.text.chars().count() > 5000 {
            return Err(AppError::Unavailable(
                "ASR segment exceeds safe length".into(),
            ));
        }
        items.push(SubtitleCue {
            start_ms: start,
            end_ms: end,
            text: segment.text.trim().into(),
            secondary_text: None,
        });
    }
    Ok(items)
}
async fn run_asr(
    client: &Client,
    config: &Config,
    path: &Path,
    lang: &str,
) -> AppResult<AsrResponse> {
    let target = config
        .asr_endpoint
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("no native ASR endpoint configured".into()))?;
    endpoint(target)?;
    if fs::metadata(path).await?.len() > 24 * 1024 * 1024 {
        return Err(AppError::Invalid("ASR audio chunk exceeds 24 MiB".into()));
    }
    let audio = fs::read(path).await?;
    let part = multipart::Part::bytes(audio)
        .file_name("audio.flac")
        .mime_str("audio/flac")
        .map_err(|_| AppError::Invalid("invalid MIME".into()))?;
    let form = multipart::Form::new()
        .part("file", part)
        .text("model", config.asr_model.clone())
        .text("response_format", "verbose_json");
    let form = if lang == "auto" || lang.is_empty() {
        form
    } else {
        form.text("language", lang.to_string())
    };
    let mut req = client.post(target).multipart(form);
    if let Some(key) = config.asr_api_key.as_ref() {
        req = req.bearer_auth(key);
    }
    let response = req.send().await?;
    if !response.status().is_success() {
        return Err(AppError::Unavailable(format!(
            "ASR HTTP {}",
            response.status()
        )));
    }
    if response
        .content_length()
        .is_some_and(|n| n > MAX_BYTES as u64)
    {
        return Err(AppError::Unavailable("ASR response too large".into()));
    }
    let raw = response.bytes().await?;
    if raw.len() > MAX_BYTES {
        return Err(AppError::Unavailable("ASR response too large".into()));
    }
    serde_json::from_slice(&raw)
        .map_err(|_| AppError::Unavailable("ASR response schema invalid".into()))
}
async fn convert_audio(
    config: &Config,
    video: &Path,
    target: &Path,
    start: f64,
    seconds: f64,
) -> AppResult<()> {
    let result = Command::new(&config.ffmpeg_binary)
        .args(["-hide_banner", "-nostdin", "-loglevel", "error"])
        .arg("-ss")
        .arg(format!("{start:.3}"))
        .arg("-i")
        .arg(video)
        .arg("-t")
        .arg(format!("{seconds:.3}"))
        .args(["-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", "-y"])
        .arg(target)
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if !result.status.success() {
        return Err(AppError::Unavailable(format!(
            "FFmpeg audio extraction failed: {}",
            result.status
        )));
    }
    Ok(())
}
async fn atomic_write(path: &Path, body: &[u8]) -> AppResult<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).await?;
    }
    let temp = path.with_extension("partial");
    let outcome = async {
        use tokio::io::AsyncWriteExt;
        let mut file = fs::File::create(&temp).await?;
        file.write_all(body).await?;
        file.sync_all().await?;
        drop(file);
        fs::rename(&temp, path).await?;
        Ok::<(), AppError>(())
    }
    .await;
    if outcome.is_err() {
        let _ = fs::remove_file(&temp).await;
    }
    outcome
}
fn parse_translation(content: &str, count: usize) -> AppResult<Vec<String>> {
    let parsed: Value = serde_json::from_str(content.trim())
        .map_err(|_| AppError::Unavailable("translation is not a JSON object".into()))?;
    let entries = parsed
        .get("items")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Unavailable("translation item mapping absent".into()))?;
    if entries.len() != count {
        return Err(AppError::Unavailable(
            "translation output count mismatch".into(),
        ));
    }
    let mut output = vec![None::<String>; count];
    for item in entries {
        let index = item
            .get("i")
            .and_then(Value::as_u64)
            .ok_or_else(|| AppError::Unavailable("translation mapping index absent".into()))?
            as usize;
        let value = item
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if index >= count || value.is_empty() || output[index].is_some() {
            return Err(AppError::Unavailable(
                "translation mapping has duplicate or missing IDs".into(),
            ));
        }
        output[index] = Some(value.chars().take(6000).collect());
    }
    output
        .into_iter()
        .map(|v| v.ok_or_else(|| AppError::Unavailable("missing translation".into())))
        .collect()
}
async fn translate_batch(
    client: &Client,
    config: &Config,
    profile: &SubtitleProfile,
    batch: &[SubtitleCue],
) -> AppResult<Vec<String>> {
    let url = config
        .translate_endpoint
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("translation endpoint not configured".into()))?;
    endpoint(url)?;
    let model = config
        .translate_model
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("translation model not configured".into()))?;
    let input = batch
        .iter()
        .enumerate()
        .map(|(i, v)| json!({"i":i,"source":v.text}))
        .collect::<Vec<_>>();
    let payload = json!({
        "model":model,"temperature":0.2,
        "response_format":{"type":"json_object"},
        "messages":[
            {"role":"system","content":format!(
                "Translate subtitles into {} in style {}. Return only a JSON object containing items, one per original input: {{\"items\":[{{\"i\":0,\"text\":\"translated\"}}]}}. Do not change the source-to-index mapping.",
                profile.target_lang,profile.translate_style
            )},
            {"role":"user","content":serde_json::to_string(&input).unwrap_or_default()}
        ]
    });
    let mut req = client.post(url).json(&payload);
    if let Some(key) = config.translate_api_key.as_ref() {
        req = req.bearer_auth(key);
    }
    let resp = req.send().await?;
    if !resp.status().is_success() {
        return Err(AppError::Unavailable(format!(
            "translation HTTP {}",
            resp.status()
        )));
    }
    if resp.content_length().is_some_and(|v| v > MAX_BYTES as u64) {
        return Err(AppError::Unavailable(
            "translation response too large".into(),
        ));
    }
    let raw = resp.bytes().await?;
    if raw.len() > MAX_BYTES {
        return Err(AppError::Unavailable(
            "translation response too large".into(),
        ));
    }
    let value: Value = serde_json::from_slice(&raw)
        .map_err(|_| AppError::Unavailable("translation response invalid".into()))?;
    let content = value
        .pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Unavailable("translation content missing".into()))?;
    parse_translation(content, batch.len())
}
pub async fn process(
    config: &Config,
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
) -> AppResult<Value> {
    if let Some(srt) = latest_asset_key(db, job.task_id, "subtitle_srt").await?
        && let Ok(path) = registry.resolve_existing(&srt).await
        && fs::metadata(path).await.is_ok()
    {
        return Ok(json!({"srt_key":srt,"reused":true}));
    }
    let video_key = latest_asset_key(db, job.task_id, "video_raw")
        .await?
        .ok_or_else(|| AppError::NotFound("raw video is missing for transcription".into()))?;
    let video = registry.resolve_existing(&video_key).await?;
    let probe = probe_video(&config.ffprobe_binary, &video).await?;
    let audio_present = probe
        .get("streams")
        .and_then(Value::as_array)
        .is_some_and(|streams| {
            streams
                .iter()
                .any(|s| s.get("codec_type").and_then(Value::as_str) == Some("audio"))
        });
    if !audio_present {
        return Ok(json!({"no_speech":true,"reason":"no_audio_stream"}));
    }
    let duration = probe
        .pointer("/format/duration")
        .and_then(Value::as_str)
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(0.0);
    if !duration.is_finite() || duration <= 0.0 || duration > 48.0 * 3600.0 {
        return Err(AppError::Invalid(
            "ASR duration is invalid or exceeds 48 hours".into(),
        ));
    }
    let profile = settings(db, job).await?;
    let original_hash:Option<String>=sqlx::query_scalar(
        "SELECT sha256 FROM assets WHERE task_id=$1 AND storage_key=$2 AND kind='video_raw'::asset_kind ORDER BY created_at DESC LIMIT 1"
    ).bind(job.task_id).bind(&video_key).fetch_optional(&db.pool).await?.flatten();
    let source_digest = if let Some(source_hash) = original_hash.filter(|v| v.len() == 64) {
        source_hash
    } else {
        registry.sha256(&video_key).await?.0
    };
    let asr_identity = fingerprint(&json!({
        "checkpoint_version":2,"video_sha256":source_digest,
        "model":config.asr_model,"asr_endpoint":config.asr_endpoint,
        "language":profile.asr_language,"duration_ms":(duration*1000.0).round() as u64,
        "window_seconds":WINDOW,"overlap_seconds":OVERLAP
    }))?;
    let client = Client::builder()
        .timeout(Duration::from_secs(300))
        .build()?;
    let prefix = format!("v2/sub/{}/{}", job.task_id, job.id);
    let workspace = registry.resolve_existing(&format!("{prefix}/work")).await?;
    fs::create_dir_all(&workspace).await?;
    let stride = WINDOW - OVERLAP;
    let count = (duration / stride).ceil() as usize;
    let mut cues = Vec::new();
    for index in 0..count {
        let start = index as f64 * stride;
        if start >= duration {
            break;
        }
        let seconds = (duration - start).min(WINDOW);
        let offset_ms = (start * 1000.0).round() as u64;
        let checkpoint = workspace.join(format!("asr-{index:05}.json"));
        let cached = fs::read(&checkpoint)
            .await
            .ok()
            .and_then(|raw| serde_json::from_slice::<Checkpoint>(&raw).ok())
            .filter(|row| {
                row.revision == 2 && row.offset_ms == offset_ms && row.fingerprint == asr_identity
            });
        let rows = if let Some(cached) = cached {
            cached.cues
        } else {
            let flac = workspace.join(format!("audio-{index:05}.flac"));
            convert_audio(config, &video, &flac, start, seconds).await?;
            let mut result = None;
            for attempt in 0..3 {
                match run_asr(&client, config, &flac, &profile.asr_language).await {
                    Ok(resp) => {
                        result = Some(resp);
                        break;
                    }
                    Err(err) if attempt < 2 => {
                        tracing::warn!(chunk=index,attempt,error=%err,"ASR chunk retry");
                        sleep(Duration::from_secs(2_u64.pow(attempt))).await;
                    }
                    Err(err) => return Err(err),
                }
            }
            let _ = fs::remove_file(&flac).await;
            let keep_after = if index == 0 {
                0
            } else {
                offset_ms + (OVERLAP * 500.0) as u64
            };
            let result = normalize_asr(
                result.ok_or_else(|| AppError::Unavailable("ASR returned no result".into()))?,
                offset_ms,
                keep_after,
                (duration * 1000.0) as u64,
            )?;
            atomic_write(
                &checkpoint,
                &serde_json::to_vec(&Checkpoint {
                    revision: 2,
                    fingerprint: asr_identity.clone(),
                    offset_ms,
                    cues: result.clone(),
                })
                .map_err(|_| AppError::Invalid("ASR checkpoint encode failed".into()))?,
            )
            .await?;
            result
        };
        cues.extend(rows);
    }
    cues.sort_by_key(|cue| cue.start_ms);
    cues.dedup_by(|a, b| {
        a.text.eq_ignore_ascii_case(&b.text) && a.start_ms.abs_diff(b.start_ms) < 800
    });
    if cues.is_empty() {
        return Ok(json!({"no_speech":true,"reason":"no_credible_segments"}));
    }
    let original = cues.clone();
    if profile.translate_enabled {
        let mut translated = Vec::new();
        for (index, batch) in cues.chunks(24).enumerate() {
            let checkpoint = workspace.join(format!("translation-{index:05}.json"));
            let identity = fingerprint(&json!({
                "checkpoint_version":2,
                "source":batch.iter().map(|v|json!({
                    "start_ms":v.start_ms,"end_ms":v.end_ms,"text":v.text
                })).collect::<Vec<_>>(),
                "model":config.translate_model,
                "endpoint":config.translate_endpoint,
                "target":profile.target_lang,"style":profile.translate_style,
                "bilingual":profile.bilingual,
            }))?;
            let saved = fs::read(&checkpoint)
                .await
                .ok()
                .and_then(|raw| serde_json::from_slice::<TranslationCheckpoint>(&raw).ok())
                .filter(|v| {
                    v.revision == 2
                        && v.fingerprint == identity
                        && v.translated.len() == batch.len()
                        && v.translated.iter().all(|s| !s.trim().is_empty())
                });
            let batch_result = match saved {
                Some(cached) => cached.translated,
                None => {
                    let values = translate_batch(&client, config, &profile, batch).await?;
                    atomic_write(
                        &checkpoint,
                        &serde_json::to_vec(&TranslationCheckpoint {
                            revision: 2,
                            fingerprint: identity,
                            translated: values.clone(),
                        })
                        .map_err(|_| {
                            AppError::Invalid("translation checkpoint encode failed".into())
                        })?,
                    )
                    .await?;
                    values
                }
            };
            translated.extend(batch_result);
        }
        for (cue, text) in cues.iter_mut().zip(translated) {
            if profile.bilingual {
                cue.secondary_text = Some(cue.text.clone());
            }
            cue.text = text;
        }
    }
    let srt = to_srt(&cues)?;
    let ass = to_ass(&cues, 1920, 1080, "Arial", 48, profile.bilingual)?;
    let metadata = serde_json::to_vec(&json!({
        "source":original,"translated":cues,
        "model":config.asr_model,"translation":profile.translate_enabled
    }))
    .map_err(|_| AppError::Invalid("segment metadata encode failed".into()))?;
    let segments_key = format!("{prefix}/segments.json");
    let srt_key = format!("{prefix}/subtitle.srt");
    let ass_key = format!("{prefix}/subtitle.ass");
    for (key, body, kind) in [
        (&segments_key, metadata.as_slice(), "segments_json"),
        (&srt_key, srt.as_bytes(), "subtitle_srt"),
        (&ass_key, ass.as_bytes(), "subtitle_ass"),
    ] {
        let path = registry.resolve_existing(key).await?;
        atomic_write(&path, body).await?;
        ensure_task_asset(db, registry, job.task_id, job, kind, key).await?;
    }
    Ok(json!({"no_speech":false,"cues":cues.len(),
        "srt_key":srt_key,"ass_key":ass_key,"segments_key":segments_key}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn segment_offsets_are_not_invented() {
        assert!(
            normalize_asr(
                AsrResponse {
                    text: "hello".into(),
                    segments: vec![]
                },
                0,
                0,
                20000
            )
            .is_err()
        );
        let rows = normalize_asr(
            AsrResponse {
                text: String::new(),
                segments: vec![
                    AsrSegment {
                        start: 0.0,
                        end: 1.0,
                        text: "old".into(),
                        no_speech_prob: None,
                        avg_logprob: None,
                        compression_ratio: None,
                    },
                    AsrSegment {
                        start: 2.0,
                        end: 3.0,
                        text: "new".into(),
                        no_speech_prob: None,
                        avg_logprob: None,
                        compression_ratio: None,
                    },
                ],
            },
            41000,
            42000,
            60000,
        )
        .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].start_ms, 43000);
    }
    #[test]
    fn confidence_filter_needs_correlated_warning_signals() {
        let response = AsrResponse {
            text: "".into(),
            segments: vec![
                AsrSegment {
                    start: 0.0,
                    end: 1.0,
                    text: "hallucination".into(),
                    no_speech_prob: Some(0.98),
                    avg_logprob: Some(-1.6),
                    compression_ratio: None,
                },
                AsrSegment {
                    start: 1.0,
                    end: 2.0,
                    text: "legitimate".into(),
                    no_speech_prob: Some(0.92),
                    avg_logprob: Some(-0.4),
                    compression_ratio: None,
                },
            ],
        };
        let rows = normalize_asr(response, 0, 0, 3000).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].text, "legitimate");
    }
    #[test]
    fn different_translation_models_invalidate_checkpoints() {
        let a = fingerprint(&json!({"model":"a","prompt_version":2,"text":"hello"})).unwrap();
        let b = fingerprint(&json!({"model":"b","prompt_version":2,"text":"hello"})).unwrap();
        assert_ne!(a, b);
    }
    #[test]
    fn rejects_duplicate_or_missing_translation_indices() {
        assert_eq!(
            parse_translation("{\"items\":[{\"i\":0,\"text\":\"你好\"}]}", 1).unwrap(),
            vec!["你好"]
        );
        assert!(
            parse_translation(
                "{\"items\":[{\"i\":0,\"text\":\"A\"},{\"i\":0,\"text\":\"B\"}]}",
                2
            )
            .is_err()
        );
        assert!(parse_translation("{\"items\":[]}", 2).is_err());
    }
    #[test]
    fn provider_endpoint_is_explicit_and_secured() {
        assert!(endpoint("https://api.groq.com/openai/v1/audio/transcriptions").is_ok());
        assert!(endpoint("http://127.0.0.1:3000/v1/audio/transcriptions").is_ok());
        assert!(endpoint("http://8.8.8.8/v1/audio/transcriptions").is_err());
    }
}
