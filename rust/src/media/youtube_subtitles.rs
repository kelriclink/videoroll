//! Select actual YouTube caption tracks using the original Python policy.
//! yt-dlp owns the network/cookie handling; no caption URL is fetched directly.
use crate::{AppError, AppResult, Config, Database, subtitles::SubtitleCue};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{process::Stdio, time::Duration};
use tokio::{fs, io::AsyncReadExt, process::Command, time::timeout};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct Selection {
    pub language: String,
    pub source: String,
    pub reason: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Captions {
    pub selection: Selection,
    pub cues: Vec<SubtitleCue>,
}
pub(crate) fn mode(value: Option<&str>, prefer: Option<bool>) -> AppResult<String> {
    let v = value.unwrap_or("").trim().to_ascii_lowercase();
    if v.is_empty() {
        return Ok(if prefer.unwrap_or(true) {
            "target"
        } else {
            "off"
        }
        .into());
    }
    if !matches!(v.as_str(), "off" | "target" | "auto_source") {
        return Err(AppError::Invalid(
            "youtube_subtitle_mode must be off, target or auto_source".into(),
        ));
    }
    Ok(v)
}
fn normalize(s: &str) -> String {
    s.trim()
        .replace('_', "-")
        .split('-')
        .filter(|v| !v.is_empty())
        .collect::<Vec<_>>()
        .join("-")
        .to_ascii_lowercase()
}
fn tracks(info: &Value, field: &str) -> Vec<String> {
    info[field]
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(k, v)| {
            !k.is_empty()
                && k.len() <= 64
                && k.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
                && !k.eq_ignore_ascii_case("live_chat")
                && v.as_array().is_some_and(|a| {
                    a.iter()
                        .any(|t| t.as_object().is_some_and(|o| !o.is_empty()))
                })
        })
        .map(|(k, _)| k.clone())
        .collect()
}
fn score(candidate: &str, target: &str) -> Option<i32> {
    let c = normalize(candidate);
    let t = normalize(target);
    if c.is_empty() || t.is_empty() {
        return None;
    }
    if c == t {
        return Some(0);
    }
    let a: Vec<_> = c.split('-').collect();
    let b: Vec<_> = t.split('-').collect();
    if a[0] != b[0] {
        return None;
    }
    let mut s = 20;
    if a.len() == 1 {
        s += 5;
    }
    if b.len() == 1 {
        s -= 5;
    }
    if c.contains("-orig") {
        s += 5;
    }
    let overlap = a[1..]
        .iter()
        .filter(|v| b[1..].contains(v))
        .collect::<std::collections::HashSet<_>>()
        .len();
    s -= 2 * overlap.min(2) as i32;
    Some(s + a.len().abs_diff(b.len()) as i32)
}
fn best(items: &[String], target: &str) -> Option<String> {
    items
        .iter()
        .filter_map(|k| score(k, target).map(|s| (s, k.to_ascii_lowercase(), k)))
        .min()
        .map(|(_, _, k)| k.clone())
}
pub(crate) fn select(info: &Value, target: &str, mode: &str) -> Option<Selection> {
    if mode == "off" {
        return None;
    }
    let manual = tracks(info, "subtitles");
    let automatic = tracks(info, "automatic_captions");
    let target = if target.trim().is_empty() {
        "zh"
    } else {
        target
    };
    if mode == "target" {
        return best(&manual, target)
            .map(|language| Selection {
                language,
                source: "subtitles".into(),
                reason: "target".into(),
            })
            .or_else(|| {
                best(&automatic, target).map(|language| Selection {
                    language,
                    source: "automatic_captions".into(),
                    reason: "target".into(),
                })
            });
    }
    let language = [
        "original_language",
        "default_audio_language",
        "audio_language",
        "language",
    ]
    .iter()
    .find_map(|k| best(&automatic, info[*k].as_str().unwrap_or("")))
    .or_else(|| {
        automatic
            .iter()
            .filter(|v| normalize(v).contains("-orig"))
            .min()
            .cloned()
    })
    .or_else(|| {
        automatic
            .iter()
            .min_by_key(|v| {
                let n = normalize(v);
                (n.contains('-'), n.len(), v.to_ascii_lowercase())
            })
            .cloned()
    })?;
    Some(Selection {
        language,
        source: "automatic_captions".into(),
        reason: "auto_source".into(),
    })
}
async fn capture(mut cmd: Command, limit: usize, seconds: u64) -> AppResult<Vec<u8>> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = cmd.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AppError::Unavailable("caption process stdout missing".into()))?;
    let mut data = Vec::new();
    let result = timeout(Duration::from_secs(seconds), async {
        stdout
            .take((limit + 1) as u64)
            .read_to_end(&mut data)
            .await?;
        if data.len() > limit {
            return Err(AppError::Invalid(
                "caption process exceeded output budget".into(),
            ));
        }
        if !child.wait().await?.success() {
            return Err(AppError::Unavailable("caption process failed".into()));
        }
        Ok::<(), AppError>(())
    })
    .await;
    match result {
        Ok(Ok(())) => Ok(data),
        Ok(Err(e)) => {
            let _ = child.kill().await;
            Err(e)
        }
        Err(_) => {
            let _ = child.kill().await;
            Err(AppError::Unavailable("caption process timed out".into()))
        }
    }
}
pub(crate) async fn fetch(
    config: &Config,
    db: &Database,
    task: uuid::Uuid,
    target: &str,
    mode: &str,
) -> AppResult<Option<Captions>> {
    if mode == "off" {
        return Ok(None);
    }
    let task = db.task(task).await?;
    if task.source_type != "youtube" {
        return Ok(None);
    }
    let id = crate::ingest::youtube_video_id(task.source_url.as_deref().unwrap_or(""))?;
    // Canonical single-video URL prevents arbitrary host or playlist input.
    let url = format!("https://www.youtube.com/watch?v={id}");
    let runtime = crate::youtube_settings_v1::runtime(db, config).await?;
    let cookies = crate::youtube_settings_v1::prepare_cookie_copy(config, &runtime).await?;
    let temp = tempfile::Builder::new().prefix("vr-ytsub-").tempdir()?;
    let command = || {
        let mut cmd = Command::new(&config.download_binary);
        cmd.args([
            "--ignore-config",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--extractor-retries",
            "1",
            "--extractor-args",
            if runtime.compatibility {
                "youtube:player_client=default,web_embedded;skip=translated_subs"
            } else {
                "youtube:skip=translated_subs"
            },
        ]);
        if let Some(proxy) = runtime.proxy.as_deref() {
            cmd.arg("--proxy").arg(proxy);
        }
        if let Some(cookie) = cookies.as_ref() {
            cmd.arg("--cookies").arg(cookie.path().join("cookies.txt"));
        }
        cmd.current_dir(temp.path());
        cmd
    };
    let mut cmd = command();
    cmd.args(["--dump-single-json", "--", &url]);
    let data = capture(cmd, 8 * 1024 * 1024, 90).await?;
    let info: Value = serde_json::from_slice(&data)
        .map_err(|_| AppError::Invalid("caption metadata invalid".into()))?;
    if info["id"].as_str() != Some(id.as_str()) {
        return Err(AppError::Invalid(
            "caption metadata video ID mismatch".into(),
        ));
    }
    let Some(selection) = select(&info, target, mode) else {
        return Ok(None);
    };
    let mut cmd = command();
    cmd.args([
        "--sub-format",
        "vtt/srt/best",
        "--sub-langs",
        &format!("^{}$", selection.language),
        "--output",
        "track.%(ext)s",
    ]);
    if selection.source == "subtitles" {
        cmd.args(["--write-subs", "--no-write-auto-subs"]);
    } else {
        cmd.args(["--no-write-subs", "--write-auto-subs"]);
    }
    cmd.args(["--", &url]);
    capture(cmd, 128 * 1024, 90).await?;
    let mut entries = fs::read_dir(temp.path()).await?;
    let mut files = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(&format!("track.{}.", selection.language)) {
            continue;
        }
        let m = fs::symlink_metadata(entry.path()).await?;
        if !m.is_file() || m.len() == 0 || m.len() > 8 * 1024 * 1024 {
            return Err(AppError::Invalid(
                "caption file invalid or oversized".into(),
            ));
        }
        files.push(entry.path());
    }
    if files.len() != 1 {
        return Err(AppError::Invalid(
            "expected exactly one downloaded caption track".into(),
        ));
    }
    let input = &files[0];
    let srt = if input.extension().and_then(|v| v.to_str()) == Some("srt") {
        input.clone()
    } else {
        let out = temp.path().join("converted.srt");
        let mut cmd = Command::new(&config.ffmpeg_binary);
        cmd.args([
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
        ])
        .arg(input)
        .args(["-map", "0:s:0", "-c:s", "srt", "-y"])
        .arg(&out);
        capture(cmd, 4096, 60).await?;
        out
    };
    if fs::metadata(&srt).await?.len() > 8 * 1024 * 1024 {
        return Err(AppError::Invalid("converted caption exceeds budget".into()));
    }
    let mut cues = crate::subtitles::parse_srt(&fs::read_to_string(&srt).await?)?;
    cues.sort_by_key(|c| c.start_ms);
    cues.dedup();
    if cues.is_empty() || cues.len() > 100_000 {
        return Ok(None);
    }
    Ok(Some(Captions { selection, cues }))
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn original_selection_policy() {
        let info = json!({"original_language":"en","subtitles":{"zh-Hans":[{"ext":"vtt"}],"live_chat":[{"ext":"json"}]},"automatic_captions":{"zh":[{"ext":"vtt"}],"en-orig":[{"ext":"vtt"}],"en":[{"ext":"vtt"}]}});
        assert_eq!(select(&info, "zh", "target").unwrap().language, "zh-Hans");
        assert_eq!(select(&info, "zh", "auto_source").unwrap().language, "en");
        assert!(select(&info, "ja", "target").is_none());
        assert!(select(&info, "zh", "off").is_none());
        assert_eq!(mode(Some("off"), Some(true)).unwrap(), "off");
        assert_eq!(mode(None, Some(false)).unwrap(), "off");
        assert!(mode(Some("bogus"), None).is_err());
    }
}
