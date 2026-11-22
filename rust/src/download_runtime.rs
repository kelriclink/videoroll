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
use serde_json::{Value, json};
use sqlx::Row;
use std::{
    path::Path,
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{
    process::Command,
    time::{interval, timeout},
};
use tokio_util::codec::{FramedRead, LinesCodec};

pub const PROGRESS_TEMPLATE: &str = "download:vr2|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress._percent_str)s";
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
    ];
    if settings.compatibility {
        args.extend([
            "--extractor-args".into(),
            "youtube:player_client=default,web_embedded".into(),
        ]);
    }
    if settings.parallel {
        args.extend([
            "--concurrent-fragments".into(),
            settings.connections.to_string(),
        ]);
        if opts
            && !settings
                .proxy
                .as_deref()
                .is_some_and(|p| p.starts_with("socks5"))
            && patched_ytdlp(binary).await
            && usable_aria2c().await
        {
            args.extend([
                "--downloader".into(),
                "http,https:aria2c".into(),
                "--downloader-args".into(),
                format!(
                    "aria2c:-c -x {} -s {} -k 1M",
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
        r#"INSERT INTO vr2_download_progress(task_id,job_id,downloaded_bytes,total_bytes,
            speed_bytes_per_second,eta_seconds,percent)
          SELECT j.task_id,j.id,$3,$4,$5,$6,$7 FROM vr2_jobs j
          JOIN vr2_workflows w ON w.id=j.workflow_id
          WHERE j.id=$1 AND j.lease_token=$2 AND j.lease_until>now()
            AND j.state='running' AND w.state='running'
          ON CONFLICT(task_id) DO UPDATE SET
            job_id=EXCLUDED.job_id,downloaded_bytes=EXCLUDED.downloaded_bytes,
            total_bytes=EXCLUDED.total_bytes,
            speed_bytes_per_second=EXCLUDED.speed_bytes_per_second,
            eta_seconds=EXCLUDED.eta_seconds,percent=EXCLUDED.percent,
            updated_at=now()"#,
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
pub async fn run(
    config: &Config,
    db: &Database,
    scheduler: &Scheduler,
    job: &WorkItem,
    output: &Path,
    url: &str,
) -> AppResult<Value> {
    let settings = crate::youtube_settings_v1::runtime(db, config).await?;
    let binary = &config.download_binary;
    let cookies = prepare_cookie_copy(config, &settings).await?;
    let mut args = prepare_args(binary, &settings, true).await;
    if let Some(proxy) = settings.proxy.as_deref() {
        args.push("--proxy".into());
        args.push(proxy.into());
    }
    if let Some(ref secret) = cookies {
        args.push("--cookies".into());
        args.push(
            secret
                .path()
                .join("cookies.txt")
                .to_string_lossy()
                .into_owned(),
        );
    }
    args.extend([
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
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = command.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AppError::Unavailable("yt-dlp progress stdout unavailable".into()))?;
    // Hard-cap each line; output from remote extractors is untrusted.
    let mut lines = FramedRead::new(stdout, LinesCodec::new_with_max_length(8192));
    let mut heartbeat = interval(Duration::from_secs(30));
    let mut last_record = Instant::now() - Duration::from_secs(3);
    let mut last_p = None::<Progress>;
    let mut has_closed = false;
    loop {
        tokio::select! {
            line=lines.next(),if !has_closed=>{
                match line{
                    Some(Ok(line))=>{
                        if let Some(p)=parse_progress(&line){
                            if last_record.elapsed()>=Duration::from_secs(2)
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
            res=child.wait()=>{
                let status=res?;
                if !status.success(){
                    return Err(AppError::Unavailable(format!(
                        "yt-dlp exited with status {}; inspect task worker diagnostics",status.code().unwrap_or(-1)
                    )));
                }
                if let Some(p)=last_p.as_ref(){record(db,job,p).await?;}
                return Ok(json!({"tool":"yt-dlp","resumed":true}));
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
