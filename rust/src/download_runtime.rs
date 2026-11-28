//! Hardened yt-dlp/aria2c adapter and bounded, durable transfer telemetry.
//!
//! - Native yt-dlp handles HLS/DASH; aria2c (if enabled and patched) handles
//!   only plain HTTP(S) downloads, never manifests.
//! - Progress is an explicitly prefixed, size-limited stdout protocol.
//! - Database updates are tied to the current SQL lease token, so an old
//!   worker cannot overwrite newer progress after restart/redispatch.
use crate::{
    AppError, AppResult, Config, Database,
    domain::WorkItem,
    scheduler::Scheduler,
    youtube_settings_v1::{YoutubeRuntime, prepare_cookie_copy},
};
use futures_util::StreamExt;
use sqlx::Row;
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{
    io::AsyncReadExt,
    process::Command,
    time::{interval, timeout},
};
use tokio_util::codec::{FramedRead, LinesCodec};

pub const PROGRESS_TEMPLATE: &str = "download:vr2p\t%(progress.{downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta,status,filename,tmpfilename})j\t%(info.format_id)j\t%(info.requested_downloads.:.{filesize,filesize_approx})j";
#[derive(Debug, Clone, PartialEq)]
pub struct Progress {
    pub downloaded_bytes: i64,
    pub total_bytes: Option<i64>,
    pub speed_bytes_per_second: Option<f64>,
    pub eta_seconds: Option<i32>,
    pub percent: i16,
}
fn positive_i64(value: &str) -> Option<i64> {
    let num = value.trim().parse::<i64>().ok()?;
    (num >= 0).then_some(num)
}
fn positive_f64(value: &str) -> Option<f64> {
    let num = value.trim().parse::<f64>().ok()?;
    (num.is_finite() && num >= 0.0).then_some(num)
}
pub fn parse_progress(line: &str) -> Option<Progress> {
    let fields = line
        .trim_start_matches("[download] ")
        .strip_prefix("vr2|")?
        .trim()
        .split('|')
        .collect::<Vec<_>>();
    if fields.len() != 6 {
        return None;
    }
    let downloaded = positive_i64(fields[0])?;
    let total = positive_i64(fields[1]).or_else(|| positive_i64(fields[2]));
    let speed = positive_f64(fields[3]);
    let eta = positive_i64(fields[4]).and_then(|s| i32::try_from(s).ok());
    let reported = fields[5]
        .trim()
        .trim_end_matches('%')
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite() && *v >= 0.0 && *v <= 100.0);
    let computed = total
        .filter(|t| *t > 0)
        .map(|t| (downloaded as f64 * 100.0 / t as f64).clamp(0.0, 99.0));
    let percent = reported
        .or(computed)
        .unwrap_or(0.0)
        .floor()
        .clamp(0.0, 99.0) as i16;
    Some(Progress {
        downloaded_bytes: downloaded,
        total_bytes: total,
        speed_bytes_per_second: speed,
        eta_seconds: eta,
        percent,
    })
}
/// Port Python _YouTubeDownloadProgressReporter.hook: per-format completion,
/// requested total, monotonic 0..95% progress; a new attempt gets a new reporter.
#[derive(Default)]
struct DownloadProgressReporter {
    completed: std::collections::HashMap<String, i64>,
    last_percent: i16,
    selected_sizes: std::collections::HashMap<String, Option<i64>>,
}
fn event_number(v: &serde_json::Value) -> Option<i64> {
    let n=v.as_f64().or_else(||v.as_str()?.parse().ok())?;
    n.is_finite().then(||n.max(0.0) as i64)
}
fn requested_total(v: &serde_json::Value)->Option<i64>{
    let sum=v.as_array()?.iter().filter_map(|x|event_number(&x["filesize"]).filter(|n|*n>0)
        .or_else(||event_number(&x["filesize_approx"])).filter(|n|*n>0))
        .fold(0i64,|a,b|a.saturating_add(b));
    (sum>0).then_some(sum)
}
impl DownloadProgressReporter {
    fn hook(&mut self,line:&str)->Option<(Progress,bool)> {
        let line=line.trim_start_matches("[download] ");
        // yt-dlp's per-format callback may omit requested_downloads (CLI
        // creates a fresh info_dict per stream). Capture the selected format
        // sizes before downloading, equivalent to Python requested totals.
        if let Some(raw)=line.strip_prefix("vr2total\t"){
            let (parts,duration)=raw.split_once('\t').unwrap_or((raw,"null"));
            let duration=serde_json::from_str::<serde_json::Value>(duration).ok()
                .and_then(|v|v.as_f64()).filter(|n|n.is_finite()&&*n>0.0);
            if let Ok(parts)=serde_json::from_str::<serde_json::Value>(parts){
                self.selected_sizes.clear();
                if let Some(parts)=parts.as_array(){
                    for part in parts{
                        let Some(id)=part["format_id"].as_str() else{continue};
                        // yt-dlp may omit HLS filesize entirely. Estimate
                        // that part from selected bitrate and video duration,
                        // then replace with actual transfer total as it arrives.
                        let size=event_number(&part["filesize"]).filter(|n|*n>0)
                            .or_else(||event_number(&part["filesize_approx"]).filter(|n|*n>0))
                            .or_else(||{
                                let bytes=part["tbr"].as_f64()?*1000.0/8.0*duration?;
                                (bytes.is_finite()&&bytes>0.0).then_some(bytes as i64)
                            });
                        self.selected_sizes.insert(id.to_owned(),size);
                    }
                }
            }
            return None;
        }
        // Keep the old protocol for historical fixture processes.
        if !line.starts_with("vr2p\t") {
            let mut p=parse_progress(line)?;
            p.percent=p.percent.min(95).max(self.last_percent);
            self.last_percent=p.percent;
            return Some((p,false))
        }
        let mut fields=line.strip_prefix("vr2p\t")?.splitn(3,'\t');
        let event:serde_json::Value=serde_json::from_str(fields.next()?).ok()?;
        let format:serde_json::Value=serde_json::from_str(fields.next()?).unwrap_or_default();
        let requested:serde_json::Value=serde_json::from_str(fields.next()?).unwrap_or_default();
        let status=event["status"].as_str()?;
        if status=="retrying" { *self=Self::default(); return Some((Progress{downloaded_bytes:0,total_bytes:None,speed_bytes_per_second:None,eta_seconds:None,percent:0},true)); }
        if !matches!(status,"downloading"|"finished"){return None}
        let current=event_number(&event["total_bytes"]).filter(|n|*n>0)
            .or_else(||event_number(&event["total_bytes_estimate"]).filter(|n|*n>0));
        let finished=status=="finished";
        let downloaded=event_number(&event["downloaded_bytes"]).or_else(||if finished{current}else{None}).unwrap_or(0);
        let key=format.as_str().filter(|s|!s.is_empty())
            .or_else(||event["filename"].as_str().filter(|s|!s.is_empty()))
            .or_else(||event["tmpfilename"].as_str().filter(|s|!s.is_empty())).unwrap_or("download");
        if finished {self.completed.insert(key.to_owned(),current.unwrap_or(downloaded));}
        let aggregate=self.completed.iter().filter(|(k,_)|k.as_str()!=key)
            .fold(downloaded,|a,(_,b)|a.saturating_add(*b));
        let selected_split=self.selected_sizes.len()>1 && self.selected_sizes.contains_key(key);
        // Never mistake the sum of known sizes for a complete total when
        // another selected stream is missing its estimate.
        if selected_split {
            if let Some(size)=current.or_else(||finished.then_some(downloaded)){
                self.selected_sizes.insert(key.to_owned(),Some(size.max(downloaded)));
            }
        }
        let total=if selected_split {
            self.selected_sizes.values().try_fold(0i64,|sum,n|n.map(|n|sum.saturating_add(n))).filter(|n|*n>0)
        }else{requested_total(&requested)};
        let split=selected_split||total.is_some();
        let numerator=if split{aggregate}else{downloaded};
        let effective_total=if selected_split {total.map(|n|n.max(numerator))}else{total.or(current)};
        let all_finished=selected_split && self.selected_sizes.keys().all(|k|self.completed.contains_key(k));
        let ratio=if let Some(n)=effective_total.filter(|n|*n>0){
            numerator as f64/n as f64
        }else if selected_split{
            let completed=self.selected_sizes.keys().filter(|k|k.as_str()!=key&&self.completed.contains_key(*k)).count();
            let part=if finished{1.0}else{current.filter(|n|*n>0).map(|n|(downloaded as f64/n as f64).clamp(0.0,1.0)).unwrap_or(0.0)};
            (completed as f64+part)/self.selected_sizes.len() as f64
        }else{0.0};
        let mut percent=((ratio.clamp(0.0,1.0)*95.0) as i16).max(self.last_percent).min(95);
        if selected_split && !all_finished {percent=percent.min(94);}
        if all_finished || (finished && !selected_split && total.is_none()){percent=95}
        self.last_percent=percent;
        Some((Progress{downloaded_bytes:numerator,total_bytes:effective_total,
            speed_bytes_per_second:event["speed"].as_f64().filter(|n|n.is_finite()).map(|n|n.max(0.0)),
            eta_seconds:event_number(&event["eta"]).and_then(|n|i32::try_from(n).ok()),percent},finished))
    }
}
pub fn is_patched_version(version: &str) -> bool {
    let parts: Vec<_> = version.trim().split('.').collect();
    if parts.len() < 3 {
        return false;
    }
    let parsed = parts[..3]
        .iter()
        .map(|s| s.parse::<u16>())
        .collect::<Result<Vec<_>, _>>();
    parsed.is_ok_and(|numbers| (numbers[0], numbers[1], numbers[2]) >= (2026, 6, 9))
}
async fn patched_ytdlp(binary: &Path) -> bool {
    let output = timeout(
        Duration::from_secs(6),
        Command::new(binary)
            .arg("--version")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await;
    matches!(output,Ok(Ok(ref o)) if o.status.success() &&
        std::str::from_utf8(&o.stdout).is_ok_and(is_patched_version))
}
async fn usable_aria2c() -> bool {
    matches!(timeout(Duration::from_secs(4),
        Command::new("aria2c").arg("--version").stdout(Stdio::null())
        .stderr(Stdio::null()).stdin(Stdio::null()).kill_on_drop(true).status()).await,
        Ok(Ok(status)) if status.success())
}
/// Shared transport defaults from Python build_ydl_opts, also used for metadata.
pub(crate) fn original_transport_args() -> Vec<String> {
    let user_agent = std::env::var("YOUTUBE_USER_AGENT").unwrap_or_else(|_| "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36".into());
    let mut args = vec!["--retries".into(), "3".into(), "--fragment-retries".into(), "3".into(), "--user-agent".into(), user_agent];
    let timeout = std::env::var("YTDLP_SOCKET_TIMEOUT_SECONDS").ok()
        .and_then(|s| s.trim().parse::<f64>().ok()).unwrap_or(20.0);
    if timeout.is_finite() && timeout > 0.0 {
        args.extend(["--socket-timeout".into(), timeout.to_string()]);
    }
    args
}
/// Preserve Python _merged_extractor_args: compatibility replaces player_client,
/// while per-operation overrides append unique list elements.
fn extractor_args_from(raw: &str, compatibility: bool, extra: Option<(&str, &[&str])>) -> AppResult<Vec<String>> {
    let mut value: serde_json::Value = if raw.trim().is_empty() { serde_json::json!({}) } else {
        serde_json::from_str(raw).map_err(|_| AppError::Invalid("invalid YOUTUBE_EXTRACTOR_ARGS_JSON".into()))?
    };
    let root = value.as_object_mut().ok_or_else(|| AppError::Invalid("YOUTUBE_EXTRACTOR_ARGS_JSON must be a JSON object".into()))?;
    if compatibility || extra.is_some() {
        let youtube = root.entry("youtube").or_insert_with(|| serde_json::json!({}));
        if !youtube.is_object() { *youtube = serde_json::json!({}); }
        let youtube = youtube.as_object_mut().unwrap();
        if compatibility { youtube.insert("player_client".into(), serde_json::json!(["default","web_embedded"])); }
        if let Some((key, additions)) = extra {
            let mut list = youtube.get(key).and_then(|v|v.as_array()).cloned().unwrap_or_default();
            for item in additions { let item = serde_json::json!(item); if !list.contains(&item) { list.push(item); } }
            youtube.insert(key.into(), serde_json::Value::Array(list));
        }
    }
    let mut args = Vec::new();
    for (extractor, options) in root {
        let options = options.as_object().ok_or_else(||AppError::Invalid("extractor arguments must be objects".into()))?;
        let mut fields = Vec::new();
        for (key, values) in options {
            let values = values.as_array().ok_or_else(||AppError::Invalid("extractor argument values must be lists".into()))?;
            let values = values.iter().map(|v|v.as_str().ok_or_else(||AppError::Invalid("extractor argument must be a string".into()))).collect::<AppResult<Vec<_>>>()?;
            fields.push(format!("{key}={}",values.join(",")));
        }
        if !fields.is_empty() { args.extend(["--extractor-args".into(),format!("{extractor}:{}",fields.join(";"))]); }
    }
    Ok(args)
}
pub(crate) fn original_extractor_args(compatibility: bool, extra: Option<(&str, &[&str])>) -> AppResult<Vec<String>> {
    extractor_args_from(&std::env::var("YOUTUBE_EXTRACTOR_ARGS_JSON").unwrap_or_default(),compatibility,extra)
}
pub async fn prepare_args(binary: &Path, settings: &YoutubeRuntime, opts: bool) -> Vec<String> {
    let mut args = vec![
        "--ignore-config".into(),
        "--no-playlist".into(),
        "--continue".into(),
        "--no-overwrites".into(),
        "--newline".into(),
        "--progress".into(),
        "--progress-delta".into(),
        "1".into(),
        "--progress-template".into(),
        PROGRESS_TEMPLATE.into(),
        "--no-simulate".into(),
        "--print".into(),
        "before_dl:vr2total\t%(requested_formats.:.{format_id,filesize,filesize_approx,tbr})j\t%(duration)j".into(),
    ];
    args.extend(original_transport_args());
    if settings.parallel {
        args.extend([
            "--concurrent-fragments".into(),
            settings.connections.to_string(),
        ]);
        if opts
            && !settings
                .proxy
                .as_deref()
                .is_some_and(|p| { let p=p.to_ascii_lowercase(); p.starts_with("socks5:") || p.starts_with("socks5h:") || p.starts_with("socks4:") || p.starts_with("socks4a:") })
            && patched_ytdlp(binary).await
            && usable_aria2c().await
        {
            args.extend([
                "--downloader".into(),
                "http,https:aria2c".into(),
                "--downloader-args".into(),
                format!(
                    "aria2c:-c -x {} -s {} -k 8M",
                    settings.connections, settings.connections
                ),
            ]);
        } else if opts {
            tracing::warn!(
                "aria2c not installed or yt-dlp version cannot be verified >=2026.06.09; using native fragment concurrency only"
            );
        }
    }
    args
}
pub async fn record(db: &Database, job: &WorkItem, p: &Progress) -> AppResult<()> {
    sqlx::query(
        r#"UPDATE tasks t SET download_stage_job_id=j.id,
             download_stage_bytes=$3,download_stage_total_bytes=$4,
             download_stage_speed=$5,download_stage_eta_seconds=$6,
             download_stage_percent=$7,download_stage_updated_at=now()
          FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
          WHERE t.id=j.task_id AND t.runtime_owner='rust'
            AND j.id=$1 AND j.lease_token=$2 AND j.lease_until>now()
            AND j.stage='download' AND j.state='running' AND w.state='running'"#,
    )
    .bind(job.id)
    .bind(job.lease_token)
    .bind(p.downloaded_bytes)
    .bind(p.total_bytes)
    .bind(p.speed_bytes_per_second)
    .bind(p.eta_seconds)
    .bind(p.percent)
    .execute(&db.pool)
    .await?;
    Ok(())
}
pub async fn active_progress(
    db: &Database,
    id: uuid::Uuid,
) -> AppResult<Option<(uuid::Uuid, Progress, chrono::DateTime<chrono::Utc>)>> {
    let row = sqlx::query(
        "SELECT p.job_id,p.downloaded_bytes,p.total_bytes,p.speed_bytes_per_second,
         p.eta_seconds,p.percent,p.updated_at FROM vr2_download_progress p
         WHERE p.task_id=$1",
    )
    .bind(id)
    .fetch_optional(&db.pool)
    .await?;
    row.map(|row| {
        Ok((
            row.try_get("job_id")?,
            Progress {
                downloaded_bytes: row.try_get("downloaded_bytes")?,
                total_bytes: row.try_get("total_bytes")?,
                speed_bytes_per_second: row.try_get("speed_bytes_per_second")?,
                eta_seconds: row.try_get("eta_seconds")?,
                percent: row.try_get("percent")?,
            },
            row.try_get("updated_at")?,
        ))
    })
    .transpose()
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FailureKind {
    FormatUnavailable,
    MembersOnly,
    Verification,
    RateLimited,
    Network,
    Disk,
    Other,
}
impl FailureKind {
    fn label(self) -> &'static str {
        match self {
            Self::FormatUnavailable => "requested format is not available",
            Self::MembersOnly => "YouTube members-only content requires channel membership",
            Self::Verification => "authentication or bot verification required",
            Self::RateLimited => "upstream rate limited",
            Self::Network => "network or incomplete transfer",
            Self::Disk => "local storage write failed",
            Self::Other => "unclassified extractor failure",
        }
    }
}
// Keep only the last 16 KiB in RAM to match a pattern across read boundaries.
// Persist *only* fixed classifications; raw upstream error text can contain
// signed URLs, proxy credentials and arbitrary cookie-like values.
#[derive(Default)]
struct DiagnosticTail {
    tail: Vec<u8>,
    format: bool,
    members_only: bool,
    verification: bool,
    rate: bool,
    network: bool,
    disk: bool,
}
impl DiagnosticTail {
    fn push(&mut self, bytes: &[u8]) {
        self.tail.extend_from_slice(bytes);
        if self.tail.len() > 16384 {
            let drop = self.tail.len() - 16384;
            self.tail.drain(..drop);
        }
        let t = String::from_utf8_lossy(&self.tail).to_ascii_lowercase();
        self.format |= t.contains("requested format is not available");
        self.members_only |= t.contains("members-only content")
            || t.contains("members-only video")
            || t.contains("join this channel to get access");
        self.verification |= [
            "sign in",
            "confirm you",
            "login required",
            "cookies are required",
            "http error 401",
        ]
        .iter()
        .any(|p| t.contains(p));
        self.rate |= t.contains("http error 429") || t.contains("too many requests");
        self.network |= [
            "timed out",
            "connection reset",
            "incomplete read",
            "bytes read",
            "network is unreachable",
            "unable to download",
            "temporary failure in name resolution",
        ]
        .iter()
        .any(|p| t.contains(p));
        self.disk |= [
            "no space left",
            "read-only file system",
            "disk quota exceeded",
            "permission denied",
        ]
        .iter()
        .any(|p| t.contains(p));
    }
    fn classify(&self) -> FailureKind {
        if self.disk {
            FailureKind::Disk
        } else if self.members_only {
            FailureKind::MembersOnly
        } else if self.verification {
            FailureKind::Verification
        } else if self.rate {
            FailureKind::RateLimited
        } else if self.format {
            FailureKind::FormatUnavailable
        } else if self.network {
            FailureKind::Network
        } else {
            FailureKind::Other
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Backend {
    Parallel,
    Native,
    TvAndroidWeb,
    AndroidWeb,
}
impl Backend {
    fn dir(self) -> &'static str {
        match self {
            Self::Parallel => "parallel",
            Self::Native => "native",
            Self::TvAndroidWeb => "native-tv-android-web",
            Self::AndroidWeb => "native-android-web",
        }
    }
    fn fallback(self, error: FailureKind, compat: bool) -> Option<Self> {
        match self {
            Self::Parallel
                if !matches!(
                    error,
                    FailureKind::Disk | FailureKind::MembersOnly | FailureKind::Verification | FailureKind::RateLimited
                ) =>
            {
                Some(Self::Native)
            }
            Self::Native if !compat && error == FailureKind::FormatUnavailable => {
                Some(Self::TvAndroidWeb)
            }
            Self::TvAndroidWeb if !compat && error == FailureKind::FormatUnavailable => {
                Some(Self::AndroidWeb)
            }
            _ => None,
        }
    }
}
pub async fn run(
    config: &Config,
    db: &Database,
    scheduler: &Scheduler,
    job: &WorkItem,
    root: &Path,
    url: &str,
) -> AppResult<PathBuf> {
    let settings = crate::youtube_settings_v1::runtime(db, config).await?;
    let cookies = prepare_cookie_copy(config, &settings).await?;
    let parallel_args = prepare_args(&config.download_binary, &settings, true).await;
    let mut backend = if parallel_args.iter().any(|v| v == "http,https:aria2c") {
        Backend::Parallel
    } else {
        Backend::Native
    };
    let native_args = prepare_args(&config.download_binary, &settings, false).await;
    // A retry may already have completed the fallback directory. Check
    // all owned backend paths before starting another upstream transfer.
    let mut recovered = Vec::new();
    for candidate in [
        Backend::Parallel,
        Backend::Native,
        Backend::TvAndroidWeb,
        Backend::AndroidWeb,
    ] {
        let dir = root.join(candidate.dir());
        match tokio::fs::symlink_metadata(&dir).await {
            Ok(meta) if !meta.file_type().is_dir() => {
                return Err(AppError::Conflict(
                    "download backend path is not a real directory".into(),
                ));
            }
            Ok(_) => {
                if let Some(path) = crate::ingest::find_finished_video(config, &dir).await? {
                    recovered.push(path);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    if recovered.len() > 1 {
        return Err(AppError::Conflict(
            "multiple completed backend outputs require reconciliation".into(),
        ));
    }
    if let Some(path) = recovered.pop() {
        return Ok(path);
    }
    loop {
        if !scheduler.heartbeat(job).await? {
            return Err(AppError::Conflict("download lease lost".into()));
        }
        // Each downloader/client has independent partials. Never delete
        // another attempt's bytes or reuse aria2 fragments with native yt-dlp.
        let dir = root.join(backend.dir());
        tokio::fs::create_dir_all(&dir).await?;
        if !tokio::fs::symlink_metadata(&dir)
            .await?
            .file_type()
            .is_dir()
        {
            return Err(AppError::Conflict(
                "download backend directory is unsafe".into(),
            ));
        }
        if let Some(path) = crate::ingest::find_finished_video(config, &dir).await? {
            return Ok(path);
        }
        let mut args = if backend == Backend::Parallel {
            parallel_args.clone()
        } else {
            native_args.clone()
        };
        let fallback_clients: Option<&[&str]> = match backend {
            Backend::TvAndroidWeb => Some(&["tv","android_sdkless","web"]),
            Backend::AndroidWeb => Some(&["android","web"]),
            _ => None,
        };
        args.extend(original_extractor_args(settings.compatibility, fallback_clients.map(|v|("player_client",v)))?);
        tracing::info!(job_id=%job.id,backend=backend.dir(),"starting isolated download attempt");
        match run_attempt(
            config,
            db,
            scheduler,
            job,
            &dir.join("source.%(ext)s"),
            url,
            args,
            &settings,
            cookies.as_ref(),
        )
        .await?
        {
            None => {
                return crate::ingest::find_finished_video(config, &dir)
                    .await?
                    .ok_or_else(|| {
                        AppError::Unavailable("yt-dlp returned no valid completed media".into())
                    });
            }
            Some(error) => {
                if let Some(next) = backend.fallback(error, settings.compatibility) {
                    record(
                        db,
                        job,
                        &Progress {
                            downloaded_bytes: 0,
                            total_bytes: None,
                            speed_bytes_per_second: None,
                            eta_seconds: None,
                            percent: 0,
                        },
                    )
                    .await?;
                    backend = next;
                } else {
                    return Err(AppError::Unavailable(format!(
                        "yt-dlp {}: {}",
                        backend.dir(),
                        error.label()
                    )));
                }
            }
        }
    }
}
async fn run_attempt(
    config: &Config,
    db: &Database,
    scheduler: &Scheduler,
    job: &WorkItem,
    output: &Path,
    url: &str,
    mut args: Vec<String>,
    settings: &YoutubeRuntime,
    cookies: Option<&crate::youtube_settings_v1::CookieInput>,
) -> AppResult<Option<FailureKind>> {
    let binary = &config.download_binary;
    if let Some(proxy) = settings.proxy.as_deref() {
        args.push("--proxy".into());
        args.push(proxy.into());
    }
    if let Some(secret) = cookies {
        args.push("--cookies".into());
        args.push(
            secret.path()
                .to_string_lossy()
                .into_owned(),
        );
    }
    args.extend([
        "--write-info-json".into(),
        "--merge-output-format".into(),
        "mp4".into(),
        "-o".into(),
        output.to_string_lossy().into_owned(),
        "--".into(),
        url.into(),
    ]);
    let mut command = Command::new(binary);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AppError::Unavailable("yt-dlp progress stdout unavailable".into()))?;
    let mut stderr = child
        .stderr
        .take()
        .ok_or_else(|| AppError::Unavailable("yt-dlp diagnostics unavailable".into()))?;
    let mut diagnostics = DiagnosticTail::default();
    let mut errbuf = [0u8; 4096];
    let mut stderr_closed = false;
    // Hard-cap each line; output from remote extractors is untrusted.
    let mut lines = FramedRead::new(stdout, LinesCodec::new_with_max_length(8192));
    let mut heartbeat = interval(Duration::from_secs(30));
    let mut last_record = Instant::now() - Duration::from_secs(3);
    let mut last_p = None::<Progress>;
    let mut reporter = DownloadProgressReporter::default();
    let mut has_closed = false;
    loop {
        tokio::select! {
            line=lines.next(),if !has_closed=>{
                match line{
                    Some(Ok(line))=>{
                        if let Some((p,finished))=reporter.hook(&line){
                            if finished || last_record.elapsed()>=Duration::from_millis(500)
                                || last_p.as_ref().is_none_or(|old|p.percent!=old.percent && (p.percent-old.percent).abs()>=5)
                            {
                                record(db,job,&p).await?;
                                last_record=Instant::now();
                            }
                            last_p=Some(p);
                        }
                    },
                    Some(Err(_))=>return Err(AppError::Unavailable("yt-dlp exceeded bounded progress output line".into())),
                    None=>has_closed=true,
                }
            },
            read=stderr.read(&mut errbuf),if !stderr_closed=>{
                match read{Ok(0)=>stderr_closed=true,Ok(n)=>diagnostics.push(&errbuf[..n]),Err(_)=>stderr_closed=true}
            },
            res=child.wait()=>{
                let status=res?;
                if !status.success(){
                    // Child exited: drain its remaining stderr, never store raw
                    // URLs, cookies, proxy credentials or remote text in logs.
                    let _=timeout(Duration::from_secs(2),async{
                        while !stderr_closed{match stderr.read(&mut errbuf).await{Ok(0)|Err(_)=>break,Ok(n)=>diagnostics.push(&errbuf[..n])}}
                    }).await;
                    let kind=diagnostics.classify();
                    tracing::warn!(job_id=%job.id,exit_code=status.code().unwrap_or(-1),category=kind.label(),"yt-dlp attempt failed");
                    return Ok(Some(kind));
                }
                // Drain remaining stdout: yt-dlp may exit before the final
                // per-format finished event has been read by select!.
                let _=timeout(Duration::from_secs(2),async{
                    while let Some(Ok(line))=lines.next().await{
                        if let Some((p,_))=reporter.hook(&line){last_p=Some(p);}
                    }
                }).await;
                if let Some(p)=last_p.as_ref(){record(db,job,p).await?;}
                return Ok(None);
            },
            _=heartbeat.tick()=>{
                if !scheduler.heartbeat(job).await?{
                    return Err(AppError::Conflict("download lease lost".into()));
                }
            },
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn download_fallback_real_postgres_and_process() {
        let Ok(url) = std::env::var("VIDEOROLL_TEST_DOWNLOAD_DB_URL") else {
            return;
        };
        let parsed = reqwest::Url::parse(&url).unwrap();
        assert_eq!(parsed.host_str(), Some("127.0.0.1"));
        assert_eq!(parsed.path(), "/videoroll_test");
        let db = Database::connect(&url).await.unwrap();
        sqlx::raw_sql("CREATE TABLE vr2_workflows(id uuid PRIMARY KEY,state text);
            CREATE TABLE vr2_jobs(id uuid PRIMARY KEY,workflow_id uuid,task_id uuid,lease_token uuid,state text,stage text,lease_until timestamptz,updated_at timestamptz);
            CREATE TABLE app_settings(key text PRIMARY KEY,value_json jsonb);
            CREATE TABLE tasks(id uuid PRIMARY KEY,runtime_owner text,download_stage_job_id uuid,download_stage_bytes bigint,download_stage_total_bytes bigint,download_stage_speed float8,download_stage_eta_seconds int,download_stage_percent smallint,download_stage_updated_at timestamptz);")
            .execute(&db.pool).await.unwrap();
        sqlx::query("INSERT INTO app_settings VALUES('youtube.settings',$1)")
            .bind(serde_json::json!({"parallel_download_enabled":false,"cookies_enabled":false,"compatibility_mode_enabled":false}))
            .execute(&db.pool).await.unwrap();
        let job = WorkItem {
            id: uuid::Uuid::new_v4(),
            workflow_id: uuid::Uuid::new_v4(),
            task_id: uuid::Uuid::new_v4(),
            stage: crate::domain::Stage::Download,
            generation: 1,
            operation_key: "test-download".into(),
            lease_token: uuid::Uuid::new_v4(),
            attempts: 1,
            max_attempts: 3,
            payload: serde_json::json!({}),
        };
        sqlx::query("INSERT INTO tasks(id,runtime_owner) VALUES($1,'rust')")
            .bind(job.task_id)
            .execute(&db.pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO vr2_workflows VALUES($1,'running')")
            .bind(job.workflow_id)
            .execute(&db.pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO vr2_jobs VALUES($1,$2,$3,$4,'running','download',now()+interval '10 minutes',now())")
            .bind(job.id).bind(job.workflow_id).bind(job.task_id).bind(job.lease_token).execute(&db.pool).await.unwrap();
        let dir = tempfile::tempdir().unwrap();
        let sample = dir.path().join("fixture.mp4");
        let status = Command::new("/usr/bin/ffmpeg")
            .args([
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x120:r=10",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
            ])
            .arg(&sample)
            .status()
            .await
            .unwrap();
        assert!(status.success());
        let mock = dir.path().join("fake-yt-dlp");
        let script = r#"#!/usr/bin/python3
import pathlib,sys,shutil
args=sys.argv[1:]
root=pathlib.Path(__file__).parent
out=pathlib.Path(args[args.index('-o')+1]).parent
with (root/'calls').open('a') as f:f.write(out.name+'\n')
if '--extractor-args' not in args:
    (out/'source.mp4.part').write_bytes(b'resumable-native-partial')
    print('ERROR: Requested format is not available. secret credential',file=sys.stderr)
    sys.exit(1)
assert args[args.index('--extractor-args')+1]=='youtube:player_client=tv,android_sdkless,web'
shutil.copyfile(root/'fixture.mp4',out/'source.mp4')
print('[download] vr2|100|100|NA|1000|0|100.0%')
"#;
        tokio::fs::write(&mock, script).await.unwrap();
        use std::os::unix::fs::PermissionsExt;
        tokio::fs::set_permissions(&mock, std::fs::Permissions::from_mode(0o700))
            .await
            .unwrap();
        let mut config = Config::from_env().unwrap();
        config.download_binary = mock;
        config.ffprobe_binary = "/usr/bin/ffprobe".into();
        let scheduler = Scheduler::new(db.clone(), Duration::from_secs(180));
        let root = dir.path().join("transfer");
        tokio::fs::create_dir(&root).await.unwrap();
        let result = run(
            &config,
            &db,
            &scheduler,
            &job,
            &root,
            "https://youtu.be/0123456789A",
        )
        .await
        .unwrap();
        assert_eq!(result, root.join("native-tv-android-web/source.mp4"));
        assert_eq!(
            tokio::fs::read(root.join("native/source.mp4.part"))
                .await
                .unwrap(),
            b"resumable-native-partial"
        );
        assert_eq!(
            tokio::fs::read(&result).await.unwrap(),
            tokio::fs::read(&sample).await.unwrap()
        );
        let recovered = run(
            &config,
            &db,
            &scheduler,
            &job,
            &root,
            "https://youtu.be/0123456789A",
        )
        .await
        .unwrap();
        assert_eq!(recovered, result);
        assert_eq!(
            tokio::fs::read_to_string(dir.path().join("calls"))
                .await
                .unwrap(),
            "native\nnative-tv-android-web\n"
        );
        println!(
            "PASS real PostgreSQL lease, subprocess format fallback, independent partial, FFprobe validation, no replay of finished fallback"
        );
    }
    #[test]
    fn original_python_extractor_merge_preserves_other_options() {
        let raw = r#"{"youtube":{"skip":["translated_subs"],"player_client":["tv"]}}"#;
        let compatible = extractor_args_from(raw,true,None).unwrap();
        assert!(compatible[1].contains("player_client=default,web_embedded"));
        assert!(compatible[1].contains("skip=translated_subs"));
        let merged = extractor_args_from(raw,false,Some(("player_client",&["tv","android_sdkless","web"]))).unwrap();
        assert!(merged[1].contains("player_client=tv,android_sdkless,web"));
        assert!(extractor_args_from("[]",false,None).is_err());
    }
    #[test]
    fn downloader_fallback_only_on_safe_categories() {
        assert_eq!(
            Backend::Parallel.fallback(FailureKind::Network, false),
            Some(Backend::Native)
        );
        assert_eq!(
            Backend::Native.fallback(FailureKind::FormatUnavailable, false),
            Some(Backend::TvAndroidWeb)
        );
        assert_eq!(
            Backend::TvAndroidWeb.fallback(FailureKind::FormatUnavailable, false),
            Some(Backend::AndroidWeb)
        );
        assert_eq!(
            Backend::Native.fallback(FailureKind::FormatUnavailable, true),
            None
        );
        assert_eq!(Backend::Native.fallback(FailureKind::Network, false), None);
        assert_eq!(
            Backend::Parallel.fallback(FailureKind::RateLimited, false),
            None
        );
        let mut tail = DiagnosticTail::default();
        tail.push(b"secret proxy user:password requested format is not ");
        tail.push(b"available");
        tail.push(&vec![b'x'; 20000]);
        assert!(tail.tail.len() <= 16384);
        assert_eq!(tail.classify(), FailureKind::FormatUnavailable);
        tail.push(b"HTTP Error 429");
        assert_eq!(tail.classify(), FailureKind::RateLimited);
        assert!(!tail.classify().label().contains("password"));
    }
    #[test]
    fn members_only_error_is_classified_without_backend_fallback() {
        let mut tail = DiagnosticTail::default();
        tail.push(b"ERROR: [youtube] SWcY2Q58zGA: Join this channel to get access to members-");
        tail.push(b"only content like this video, and other exclusive perks.");
        assert_eq!(tail.classify(), FailureKind::MembersOnly);
        assert_eq!(Backend::Parallel.fallback(tail.classify(), false), None);
        assert_eq!(Backend::Native.fallback(tail.classify(), false), None);
        assert!(!tail.classify().label().contains("SWcY2Q58zGA"));
    }
    #[test]
    fn original_python_split_format_progress_aggregation() {
        let mut r=DownloadProgressReporter::default();
        let evt=|key:&str,status:&str,bytes:i64,total:i64|format!("vr2p\t{}\t{}\t{}",serde_json::json!({"status":status,"downloaded_bytes":bytes,"total_bytes":total}),serde_json::json!(key),serde_json::json!([{"filesize":100},{"filesize_approx":50}]));
        let (p,_)=r.hook(&evt("video","downloading",50,100)).unwrap(); assert_eq!((p.downloaded_bytes,p.total_bytes,p.percent),(50,Some(150),31));
        let (p,done)=r.hook(&evt("video","finished",100,100)).unwrap(); assert!(done);assert_eq!((p.downloaded_bytes,p.percent),(100,63));
        let (p,_)=r.hook(&evt("audio","downloading",5,50)).unwrap();assert_eq!((p.downloaded_bytes,p.percent),(105,66));
        let (p,_)=r.hook(&evt("audio","finished",50,50)).unwrap();assert_eq!((p.downloaded_bytes,p.percent),(150,95));
        let (p,_)=r.hook("vr2p\t{\"status\":\"retrying\"}\tNA\t[]").unwrap();assert_eq!((p.downloaded_bytes,p.percent),(0,0));
        let (p,_)=r.hook("vr2p\t{\"status\":\"finished\",\"total_bytes\":100,\"downloaded_bytes\":100}\tNA\t[]").unwrap();assert_eq!(p.percent,95);
    }
    #[test]
    fn selected_hls_without_filesize_does_not_count_audio_as_entire_video() {
        let mut r=DownloadProgressReporter::default();
        assert!(r.hook("vr2total\t[{\"format_id\":\"audio\",\"filesize\":2500000},{\"format_id\":\"video\",\"tbr\":3760.0}]\t161").is_none());
        let evt=|key:&str,status:&str,bytes:i64,total:i64|format!("vr2p\t{}\t{}\t[]",serde_json::json!({"status":status,"downloaded_bytes":bytes,"total_bytes":total}),serde_json::json!(key));
        let (p,_)=r.hook(&evt("audio","finished",2500000,2500000)).unwrap();
        assert!(p.total_bytes.unwrap()>75_000_000);assert!(p.percent<10);
        let (q,_)=r.hook(&evt("video","downloading",10000000,50000000)).unwrap();
        assert_eq!(q.downloaded_bytes,12500000);assert_eq!(q.total_bytes,Some(52500000));assert!(q.percent>=p.percent&&q.percent<95);
        let (q,_)=r.hook(&evt("video","finished",50000000,50000000)).unwrap();
        assert_eq!((q.downloaded_bytes,q.percent),(52500000,95));
    }
    #[test]
    fn bounded_progress_parser_is_strict() {
        let p = parse_progress("vr2|1200|6000|NA|2500.0|2| 20.0%").unwrap();
        assert_eq!(p.downloaded_bytes, 1200);
        assert_eq!(p.percent, 20);
        assert_eq!(p.speed_bytes_per_second, Some(2500.0));
        assert_eq!(
            parse_progress("[download] vr2|1200|6000|NA|2500.0|2| 20.0%")
                .unwrap()
                .percent,
            20
        );
        assert!(parse_progress("debug logging nothing").is_none());
        assert!(parse_progress("vr2|nan|100|NA|NaN|10|10%").is_none());
        assert_eq!(
            parse_progress("vr2|6000|6000|NA|NA|NA|100.0%")
                .unwrap()
                .percent,
            99
        );
        assert!(parse_progress("vr2|10|20|NA|NA|10").is_none());
    }
    #[test]
    fn aria2c_requires_patched_ytdlp() {
        assert!(!is_patched_version("2026.06.08"));
        assert!(is_patched_version("2026.06.09"));
        assert!(is_patched_version("2026.11.15"));
        assert!(!is_patched_version("unknown"));
        assert!(!is_patched_version("2025.12.01"));
    }
}
