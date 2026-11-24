//! Native official Index-Translate adapter: bounded source context, exact
//! numbered output, missing-line repair, and finite split/retry budgets.
use crate::{AppError, AppResult, Database, subtitles::SubtitleCue};
use reqwest::{Client, Url};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    time::Duration,
};
use tokio::time::sleep;
use uuid::Uuid;

pub const MODEL: &str = "Index-Translate-35B-A3B";
pub const ENDPOINT: &str = "https://index-translate.bilibili.com/v1/chat/completions";
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Options {
    pub batch_size: usize,
    pub context_chars: usize,
    pub domain: String,
    pub glossary: String,
    pub instruction: String,
    pub max_tokens: u32,
    pub temperature: f64,
    pub timeout_seconds: u64,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            batch_size: 12,
            context_chars: 6000,
            domain: "视频字幕".into(),
            glossary: String::new(),
            instruction: String::new(),
            max_tokens: 4096,
            temperature: 0.,
            timeout_seconds: 120,
        }
    }
}
impl Options {
    pub fn bounded(mut self) -> Self {
        self.batch_size = self.batch_size.clamp(1, 50);
        self.context_chars = self.context_chars.clamp(1000, 6000);
        self.domain = self.domain.chars().take(90).collect();
        self.glossary = self.glossary.chars().take(4000).collect();
        self.instruction = self.instruction.chars().take(2000).collect();
        self.max_tokens = self.max_tokens.clamp(128, 8192);
        self.temperature = self.temperature.clamp(0., 2.);
        self.timeout_seconds = self.timeout_seconds.clamp(15, 300);
        self
    }
}
pub async fn options(db: &Database) -> AppResult<Options> {
    let saved = crate::settings::effective(db, "subtitle.translate").await?;
    let mut opts = Options::default();
    if let Some(v) = saved.get("index_batch_size").and_then(Value::as_u64) {
        opts.batch_size = v.min(usize::MAX as u64) as usize;
    }
    if let Some(v) = saved.get("index_context_chars").and_then(Value::as_u64) {
        opts.context_chars = v.min(usize::MAX as u64) as usize;
    }
    if let Some(v) = saved.get("index_domain").and_then(Value::as_str) {
        opts.domain = v.into();
    }
    if let Some(v) = saved.get("index_glossary").and_then(Value::as_str) {
        opts.glossary = v.into();
    }
    if let Some(v) = saved.get("index_instruction").and_then(Value::as_str) {
        opts.instruction = v.into();
    }
    if let Some(v) = saved.get("index_max_tokens").and_then(Value::as_u64) {
        opts.max_tokens = v.min(u32::MAX as u64) as u32;
    }
    if let Some(v) = saved.get("index_temperature").and_then(Value::as_f64) {
        opts.temperature = v;
    }
    if let Some(v) = saved.get("index_timeout_seconds").and_then(Value::as_u64) {
        opts.timeout_seconds = v;
    }
    Ok(opts.bounded())
}
pub(crate) fn url() -> AppResult<Url> {
    let chosen = std::env::var("VIDEOROLL_V2_INDEX_ENDPOINT").unwrap_or_else(|_| ENDPOINT.into());
    let parsed = Url::parse(&chosen).map_err(|_| AppError::Invalid("Index URL invalid".into()))?;
    let official =
        parsed.scheme() == "https" && parsed.host_str() == Some("index-translate.bilibili.com");
    let local = parsed.scheme() == "http"
        && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1"));
    if (!official && !local) || parsed.username() != "" || parsed.password().is_some() {
        return Err(AppError::Invalid(
            "Index endpoint must be official HTTPS or local loopback".into(),
        ));
    }
    Ok(parsed)
}
fn take(input: &str, n: usize) -> String {
    input.chars().take(n).collect()
}
pub fn context(cues: &[SubtitleCue], start: usize, limit: usize) -> String {
    let lines = cues
        .iter()
        .enumerate()
        .map(|(i, cue)| format!("[{}] {}", i + 1, cue.text.trim()))
        .collect::<Vec<_>>()
        .join("\n");
    if lines.chars().count() <= limit {
        return lines;
    }
    let left = start.saturating_sub(30);
    let right = (start + 90).min(cues.len());
    let nearby = cues[left..right]
        .iter()
        .enumerate()
        .map(|(i, cue)| format!("[{}] {}", left + i + 1, cue.text.trim()))
        .collect::<Vec<_>>()
        .join("\n");
    let side = (limit.saturating_sub(110 + nearby.chars().count().min(limit / 2))) / 2;
    let head = take(&lines, side);
    let tail = lines
        .chars()
        .rev()
        .take(side)
        .collect::<String>()
        .chars()
        .rev()
        .collect::<String>();
    let local_len = limit.saturating_sub(side * 2 + 110).max(100);
    format!(
        "【开头】\n{head}\n【当前段落】\n{}\n【结尾】\n{tail}",
        take(&nearby, local_len)
    )
}
fn messages(
    cues: &[SubtitleCue],
    start: usize,
    end: usize,
    target: &str,
    style: &str,
    opts: &Options,
    ctx_limit: usize,
) -> Value {
    let mut system = format!(
        "请将视频原始语言的{}字幕翻译成{}。仅翻译用户提供的字幕数组，保持 idx 与顺序，绝不漏条。\
        仅输出 JSON 对象：{{\"translations\":[{{\"idx\":数字,\"text\":\"译文\"}}]}}。\
        不解释，不添加原文没有的事实，保留技术单位、数字、代码和专有名词。\
        风格：{}。下列视频上下文仅作参考，禁止翻译上下文或把背景加入字幕。",
        opts.domain, target, style
    );
    if !opts.glossary.is_empty() {
        system.push_str("\n【术语表】");
        system.push_str(&opts.glossary);
    }
    if !opts.instruction.is_empty() {
        system.push_str("\n【附加要求】");
        system.push_str(&opts.instruction);
    }
    system.push_str("\n【视频参考上下文】\n");
    system.push_str(&context(cues, start, ctx_limit));
    let batch = (start..end)
        .map(|i| json!({"idx":i+1,"text":cues[i].text}))
        .collect::<Vec<_>>();
    json!([
        {"role":"system","content":system},
        {"role":"user","content":format!("只翻译下面 {} 行字幕并保留相同数量的 idx：\n{}",
            batch.len(),serde_json::to_string(&batch).unwrap_or_default())}
    ])
}
pub fn mapping(value: &Value, start: usize, end: usize) -> AppResult<BTreeMap<usize, String>> {
    let obj = value
        .as_object()
        .ok_or_else(|| AppError::Unavailable("Index result is not a JSON object".into()))?;
    let mut rows = Vec::new();
    if let Some(array) = obj.get("translations").and_then(Value::as_array) {
        rows.extend(array.iter().cloned());
    } else if let Some(map) = obj.get("translations").and_then(Value::as_object) {
        for (idx, v) in map {
            rows.push(json!({"idx":idx,"text":v.get("text").unwrap_or(v)}));
        }
    } else {
        for (idx, v) in obj {
            if idx.bytes().all(|c| c.is_ascii_digit()) {
                rows.push(json!({"idx":idx,"text":v.get("text").unwrap_or(v)}));
            }
        }
        if rows.is_empty() && obj.contains_key("idx") && obj.contains_key("text") {
            rows.push(value.clone());
        } else if rows.is_empty()
            && end - start == 1
            && let Some(s) = obj.get("translation").and_then(Value::as_str)
        {
            rows.push(json!({"idx":start+1,"text":s}));
        }
    }
    let mut results = BTreeMap::new();
    for row in rows {
        let idx = row
            .get("idx")
            .and_then(Value::as_u64)
            .map(|v| v as usize)
            .or_else(|| {
                row.get("idx")
                    .and_then(Value::as_str)
                    .and_then(|s| s.parse::<usize>().ok())
            });
        let Some(idx) = idx else {
            continue;
        };
        if idx <= start || idx > end {
            continue;
        }
        let text = row.get("text").and_then(Value::as_str).unwrap_or("").trim();
        if text.is_empty() {
            continue;
        }
        if results.insert(idx, take(text, 8000)).is_some() {
            return Err(AppError::Unavailable(
                "duplicate idx from Index provider".into(),
            ));
        }
    }
    Ok(results)
}
fn decode_envelope(value: Value, start: usize, end: usize) -> AppResult<BTreeMap<usize, String>> {
    let content = value
        .pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Unavailable("Index response content missing".into()))?;
    let fence = std::iter::repeat_n(char::from(96), 3).collect::<String>();
    let raw = content.trim();
    let raw = raw
        .strip_prefix(&(fence.clone() + "json"))
        .or_else(|| raw.strip_prefix(&fence))
        .unwrap_or(raw);
    let raw = raw.strip_suffix(&fence).unwrap_or(raw);
    let decoded: Value = serde_json::from_str(raw.trim())
        .map_err(|_| AppError::Unavailable("Index response mapping not valid JSON".into()))?;
    mapping(&decoded, start, end)
}
async fn call(
    db: &Database,
    task_id: Uuid,
    client: &Client,
    url: &Url,
    opts: &Options,
    messages: Value,
) -> AppResult<Value> {
    let body = json!({"model":MODEL,"messages":messages,"temperature":opts.temperature,
        "max_tokens":opts.max_tokens,"stream":false,
        "chat_template_kwargs":{"enable_thinking":false},
        "response_format":{"type":"json_object"}});
    for attempt in 0..3 {
        let request = client
            .post(url.clone())
            .header("User-Agent", "Index-Translate-Client/1.0")
            .json(&body);
        match crate::ai_usage::chat_json(
            db,
            request,
            url,
            MODEL,
            "index_translate",
            Some(task_id),
            8 * 1024 * 1024,
            "Index gateway",
        )
        .await
        {
            Ok(value) => return Ok(value),
            Err(AppError::Unavailable(message))
                if attempt < 2
                    && [" 429", " 500", " 502", " 503", " 504"]
                        .iter()
                        .any(|code| message.contains(code)) => {}
            Err(AppError::Upstream(err)) if attempt < 2 => {
                tracing::warn!(error=%err.without_url(),attempt,"Index request retry");
            }
            Err(err) => return Err(err),
        }
        sleep(Duration::from_secs(1 << attempt)).await;
    }
    Err(AppError::Unavailable(
        "Index request retry limit exceeded".into(),
    ))
}
#[derive(Clone, Copy, Debug)]
struct Window {
    start: usize,
    end: usize,
    context_chars: usize,
    tries: usize,
}
pub async fn translate(
    db: &Database,
    task_id: Uuid,
    client: &Client,
    source: &[SubtitleCue],
    start: usize,
    end: usize,
    options: &Options,
    target: &str,
    style: &str,
) -> AppResult<Vec<String>> {
    if start >= end || end > source.len() || end - start > 50 {
        return Err(AppError::Invalid("Index window out of bounds".into()));
    }
    let opts = options.clone().bounded();
    let url = url()?;
    let mut pending = VecDeque::from([Window {
        start,
        end,
        context_chars: opts.context_chars,
        tries: 0,
    }]);
    let mut translated = BTreeMap::<usize, String>::new();
    let mut request_count = 0;
    while let Some(window) = pending.pop_front() {
        if (window.start..window.end).all(|i| translated.contains_key(&(i + 1))) {
            continue;
        }
        request_count += 1;
        if request_count > (end - start) * 8 {
            return Err(AppError::Unavailable(
                "Index repair budget exhausted".into(),
            ));
        }
        let msgs = messages(
            source,
            window.start,
            window.end,
            target,
            style,
            &opts,
            window.context_chars,
        );
        let result = call(db, task_id, client, &url, &opts, msgs)
            .await
            .and_then(|v| decode_envelope(v, window.start, window.end));
        match result {
            Ok(mapping) => {
                for (idx, text) in mapping {
                    translated.entry(idx).or_insert(text);
                }
                let missing = (window.start..window.end)
                    .filter(|i| !translated.contains_key(&(i + 1)))
                    .collect::<Vec<_>>();
                for i in missing {
                    if window.end - window.start == 1 {
                        if window.tries >= 2 {
                            return Err(AppError::Unavailable(format!(
                                "Index failed to translate subtitle {}",
                                i + 1
                            )));
                        }
                        pending.push_back(Window {
                            tries: window.tries + 1,
                            context_chars: window.context_chars.min(3000),
                            ..window
                        });
                    } else {
                        pending.push_back(Window {
                            start: i,
                            end: i + 1,
                            tries: 0,
                            context_chars: window.context_chars.min(3000),
                        });
                    }
                }
            }
            Err(err) if window.end - window.start > 1 => {
                let mid = (window.start + window.end) / 2;
                for (a, b) in [(window.start, mid), (mid, window.end)] {
                    pending.push_back(Window {
                        start: a,
                        end: b,
                        context_chars: window.context_chars.min(3000),
                        tries: window.tries + 1,
                    });
                }
                tracing::debug!(error=%err,start=window.start,end=window.end,
                    "Index split malformed batch");
            }
            Err(err) if window.tries < 2 => {
                pending.push_back(Window {
                    tries: window.tries + 1,
                    context_chars: (window.context_chars / 2).max(1000),
                    ..window
                });
                tracing::debug!(error=%err,"Index retry single line with smaller context");
            }
            Err(err) => return Err(err),
        }
    }
    (start..end)
        .map(|i| {
            translated.remove(&(i + 1)).ok_or_else(|| {
                AppError::Unavailable(format!("missing Index translation at {}", i + 1))
            })
        })
        .collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parse_official_index_variants() {
        let a = json!({"translations":[{"idx":2,"text":"二"},{"idx":3,"text":"三"}]});
        let b = json!({"translations":{"2":"二","3":"三"}});
        let c = json!({"2":"二","3":"三"});
        for v in [&a, &b, &c] {
            assert_eq!(mapping(v, 1, 3).unwrap().len(), 2);
        }
        assert!(
            mapping(
                &json!({"translations":[
            {"idx":2,"text":"A"},{"idx":2,"text":"B"}]}),
                1,
                3
            )
            .is_err()
        );
    }
    #[test]
    fn context_is_bounded_for_long_source() {
        let cues = (0..1000)
            .map(|i| SubtitleCue {
                start_ms: i * 1000,
                end_ms: i * 1000 + 500,
                text: "words ".repeat(90),
                secondary_text: None,
            })
            .collect::<Vec<_>>();
        let text = context(&cues, 500, 6000);
        assert!(text.chars().count() <= 6200);
        assert!(text.contains("【当前段落】"));
    }
    #[test]
    fn caps_public_gateway_request() {
        let opts = Options {
            batch_size: 999,
            context_chars: 100_000,
            max_tokens: 100_000,
            ..Options::default()
        }
        .bounded();
        assert_eq!(opts.batch_size, 50);
        assert_eq!(opts.context_chars, 6000);
        assert_eq!(opts.max_tokens, 8192);
    }
}
