//! Python youtube_ingest source CRUD and manual scanning V1 contract.
//! All database writes stay in the isolated V2 database until cutover.
use crate::{AppError, AppResult, api::AppState, sources};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::FromRow;
use std::{process::Stdio, time::Duration};
use tokio::{process::Command, time::timeout};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, FromRow)]
pub struct SourceRead {
    pub id: Uuid,
    pub source_type: String,
    pub source_id: String,
    pub source_url: String,
    pub display_name: Option<String>,
    pub license: String,
    pub proof_url: Option<String>,
    pub enabled: bool,
    pub scan_interval_minutes: i32,
    pub scan_limit: i32,
    pub auto_process: bool,
    pub scan_running: bool,
    pub last_scan_started_at: Option<DateTime<Utc>>,
    pub last_scan_finished_at: Option<DateTime<Utc>>,
    pub last_scan_discovered_count: i32,
    pub last_scan_created_count: i32,
    pub last_scan_started_pipeline_count: i32,
    pub last_scan_skipped_duplicates: i32,
    pub last_scan_error: Option<String>,
    pub last_nonempty_scan_finished_at: Option<DateTime<Utc>>,
    pub last_nonempty_discovered_count: i32,
    pub total_scan_count: i32,
    pub total_failed_scan_count: i32,
    pub total_created_count: i32,
    pub total_started_pipeline_count: i32,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

const SOURCE_READ: &str = r#"
SELECT id, source_type::text, source_id,
 COALESCE(NULLIF(trim(source_url),''), CASE WHEN source_type='channel'::youtube_source_type
 THEN 'https://www.youtube.com/channel/'||source_id
 ELSE 'https://www.youtube.com/playlist?list='||source_id END) AS source_url,
 display_name, license::text, proof_url, enabled,
 scan_interval_minutes, scan_limit, auto_process,
 (scan_lock_owner IS NOT NULL AND scan_lock_until>now()) AS scan_running,
 last_scan_started_at, last_scan_finished_at,
 last_scan_discovered_count,last_scan_created_count,
 last_scan_started_pipeline_count,last_scan_skipped_duplicates,
 last_scan_error,last_nonempty_scan_finished_at,last_nonempty_discovered_count,
 total_scan_count,total_failed_scan_count,total_created_count,
 total_started_pipeline_count,created_at,updated_at
FROM youtube_sources
"#;

#[derive(Debug, Deserialize)]
struct SourceCreate {
    source_type: Option<String>,
    source_id: Option<String>,
    source_url: Option<String>,
    #[serde(default = "default_license")]
    license: String,
    proof_url: Option<String>,
    #[serde(default = "yes")]
    enabled: bool,
    #[serde(default = "default_interval")]
    scan_interval_minutes: i32,
    #[serde(default = "default_limit")]
    scan_limit: i32,
    #[serde(default = "yes")]
    auto_process: bool,
}
#[derive(Debug, Deserialize)]
struct SourcePatch {
    license: Option<String>,
    proof_url: Option<Option<String>>,
    enabled: Option<bool>,
    scan_interval_minutes: Option<i32>,
    scan_limit: Option<i32>,
    auto_process: Option<bool>,
    display_name: Option<Option<String>>,
}
#[derive(Debug, Default, Deserialize)]
struct ScanOptions {
    limit: Option<i32>,
    auto_process: Option<bool>,
}
#[derive(Debug, Deserialize)]
struct ScanByIdentity {
    source_type: String,
    source_id: String,
    since: Option<DateTime<Utc>>,
    #[serde(default = "default_limit")]
    limit: i32,
    #[serde(default = "yes")]
    auto_process: bool,
}
fn default_license() -> String {
    "own".into()
}
fn default_interval() -> i32 {
    60
}
fn default_limit() -> i32 {
    20
}
fn yes() -> bool {
    true
}
fn valid_license(v: &str) -> bool {
    matches!(v, "own" | "authorized" | "cc" | "unknown")
}
fn valid_interval(v: i32) -> AppResult<()> {
    if (1..=1440).contains(&v) {
        Ok(())
    } else {
        Err(AppError::Invalid(
            "scan_interval_minutes must be 1..1440".into(),
        ))
    }
}
fn valid_limit(v: i32) -> AppResult<()> {
    if (1..=200).contains(&v) {
        Ok(())
    } else {
        Err(AppError::Invalid("scan_limit must be 1..200".into()))
    }
}
fn normal_opt(v: Option<String>) -> Option<String> {
    v.and_then(|s| {
        let s = s.trim();
        if s.is_empty() {
            None
        } else {
            Some(s.to_string())
        }
    })
}
fn display_name(v: Option<String>) -> Option<String> {
    normal_opt(v).map(|s| s.chars().take(255).collect())
}
fn source_id_valid(kind: &str, id: &str) -> bool {
    let ascii = id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'));
    if !ascii || id.len() > 128 {
        return false;
    }
    match kind {
        "channel" => id.starts_with("UC") && id.len() >= 16,
        "playlist" => {
            ["PL", "UU", "LL", "FL", "RD", "OLAK5uy_"]
                .iter()
                .any(|p| id.starts_with(p))
                && id.len() >= 12
        }
        _ => false,
    }
}
fn canonical(kind: &str, id: &str) -> String {
    if kind == "channel" {
        format!("https://www.youtube.com/channel/{id}")
    } else {
        format!("https://www.youtube.com/playlist?list={id}")
    }
}
fn direct_source(raw: &str) -> AppResult<Option<(String, String)>> {
    let raw = raw.trim();
    if source_id_valid("channel", raw) {
        return Ok(Some(("channel".into(), raw.into())));
    }
    if source_id_valid("playlist", raw) {
        return Ok(Some(("playlist".into(), raw.into())));
    }
    let normalized = if raw.starts_with('@') {
        format!("https://www.youtube.com/{raw}")
    } else if raw.starts_with("youtube.com/")
        || raw.starts_with("www.youtube.com/")
        || raw.starts_with("m.youtube.com/")
    {
        format!("https://{raw}")
    } else {
        raw.to_string()
    };
    let Ok(url) = reqwest::Url::parse(&normalized) else {
        return Ok(None);
    };
    if url.scheme() != "https"
        || !matches!(
            url.host_str(),
            Some("youtube.com" | "www.youtube.com" | "m.youtube.com")
        )
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(AppError::Invalid(
            "source_url must use the YouTube HTTPS website".into(),
        ));
    }
    // Python favors playlists when both a list ID and channel ID are present.
    if let Some(id) = url
        .query_pairs()
        .find(|(k, _)| k == "list")
        .map(|(_, v)| v.to_string())
        && source_id_valid("playlist", &id)
    {
        return Ok(Some(("playlist".into(), id)));
    }
    if let Some(id) = url
        .query_pairs()
        .find(|(k, _)| k == "channel_id")
        .map(|(_, v)| v.to_string())
        && source_id_valid("channel", &id)
    {
        return Ok(Some(("channel".into(), id)));
    }
    let parts: Vec<&str> = url.path_segments().map(|s| s.collect()).unwrap_or_default();
    if parts.len() >= 2 && parts[0] == "channel" && source_id_valid("channel", parts[1]) {
        return Ok(Some(("channel".into(), parts[1].into())));
    }
    Ok(None)
}
async fn resolve_source(
    state: &AppState,
    ty: Option<&str>,
    source_id: Option<&str>,
    source_url: Option<&str>,
) -> AppResult<(String, String, String, Option<String>)> {
    let ty = ty.map(str::trim).filter(|s| !s.is_empty());
    if let Some(ty) = ty
        && !matches!(ty, "channel" | "playlist")
    {
        return Err(AppError::Invalid(
            "source_type must be channel or playlist".into(),
        ));
    }
    let raw = source_url.map(str::trim).filter(|s| !s.is_empty());
    let resolved = match raw {
        Some(raw) => match direct_source(raw)? {
            Some((kind, id)) => (kind, id, None),
            None => {
                let normalized = if raw.starts_with('@') {
                    format!("https://www.youtube.com/{raw}")
                } else if raw.starts_with("youtube.com/") {
                    format!("https://{raw}")
                } else {
                    raw.to_string()
                };
                // Python resolves aliases/handles through yt-dlp; keep that integration
                // rather than storing an unverified URL as if it were a canonical ID.
                let u = reqwest::Url::parse(&normalized).map_err(|_| {
                    AppError::Invalid(
                        "source_url must be a YouTube channel, playlist or @handle".into(),
                    )
                })?;
                if u.scheme() != "https"
                    || !matches!(
                        u.host_str(),
                        Some("youtube.com" | "www.youtube.com" | "m.youtube.com")
                    )
                    || !u.username().is_empty()
                    || u.password().is_some()
                {
                    return Err(AppError::Invalid(
                        "source_url must point to YouTube HTTPS".into(),
                    ));
                }
                let settings =
                    crate::youtube_settings_v1::runtime(&state.db, &state.config).await?;
                let cookies =
                    crate::youtube_settings_v1::prepare_cookie_copy(&state.config, &settings)
                        .await?;
                let mut downloader = Command::new(&state.config.download_binary);
                downloader.args([
                    "--ignore-config",
                    "--skip-download",
                    "--flat-playlist",
                    "--dump-single-json",
                    "--playlist-end",
                    "1",
                    "--no-warnings",
                ]);
                if let Some(proxy) = settings.proxy.as_deref() {
                    downloader.arg("--proxy").arg(proxy);
                }
                if let Some(cookie_file) = cookies.as_ref() {
                    downloader
                        .arg("--cookies")
                        .arg(cookie_file.path().join("cookies.txt"));
                }
                if settings.compatibility {
                    downloader
                        .arg("--extractor-args")
                        .arg("youtube:player_client=default,web_embedded");
                }
                let output = timeout(
                    Duration::from_secs(45),
                    downloader
                        .arg("--")
                        .arg(&normalized)
                        .stdin(Stdio::null())
                        .kill_on_drop(true)
                        .output(),
                )
                .await
                .map_err(|_| {
                    AppError::Unavailable("YouTube source resolution timed out".into())
                })??;
                if !output.status.success() || output.stdout.len() > 1024 * 1024 {
                    return Err(AppError::Invalid(
                        "failed to resolve YouTube channel/playlist".into(),
                    ));
                }
                let data: Value = serde_json::from_slice(&output.stdout)
                    .map_err(|_| AppError::Invalid("invalid yt-dlp source metadata".into()))?;
                let playlist = data.get("playlist_id").and_then(Value::as_str).or_else(|| {
                    if data.get("_type").and_then(Value::as_str) == Some("playlist") {
                        data.get("id").and_then(Value::as_str)
                    } else {
                        None
                    }
                });
                let channel = data
                    .get("channel_id")
                    .and_then(Value::as_str)
                    .or_else(|| data.get("id").and_then(Value::as_str));
                let found = if let Some(id) = playlist.filter(|i| source_id_valid("playlist", i)) {
                    Some(("playlist".to_string(), id.to_string()))
                } else {
                    channel
                        .filter(|i| source_id_valid("channel", i))
                        .map(|id| ("channel".to_string(), id.to_string()))
                };
                let (kind, id) = found.ok_or_else(|| {
                    AppError::Invalid("cannot resolve YouTube channel/playlist ID".into())
                })?;
                let title = display_name(
                    data.get("channel")
                        .or_else(|| data.get("uploader"))
                        .or_else(|| data.get("title"))
                        .and_then(Value::as_str)
                        .map(str::to_string),
                );
                (kind, id, title)
            }
        },
        None => {
            let kind = ty.ok_or_else(|| {
                AppError::Invalid("source_type/source_id or source_url is required".into())
            })?;
            let id = source_id.map(str::trim).unwrap_or("");
            if !source_id_valid(kind, id) {
                return Err(AppError::Invalid("YouTube source ID is malformed".into()));
            }
            (kind.into(), id.into(), None)
        }
    };
    if let Some(requested) = ty
        && requested != resolved.0
    {
        return Err(AppError::Invalid(format!(
            "source_url resolved as {}, but source_type is {requested}",
            resolved.0
        )));
    }
    let url = canonical(&resolved.0, &resolved.1);
    Ok((resolved.0, resolved.1, url, resolved.2))
}
async fn load(state: &AppState, id: Uuid) -> AppResult<SourceRead> {
    sqlx::query_as::<_, SourceRead>(&format!("{SOURCE_READ} WHERE id=$1"))
        .bind(id)
        .fetch_optional(&state.db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("source not found".into()))
}
async fn list(State(state): State<AppState>) -> AppResult<Json<Vec<SourceRead>>> {
    let sql = format!("{SOURCE_READ} ORDER BY created_at DESC");
    let records = sqlx::query_as::<_, SourceRead>(&sql)
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(records))
}
async fn get_one(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<SourceRead>> {
    Ok(Json(load(&state, id).await?))
}
async fn create(
    State(state): State<AppState>,
    Json(body): Json<SourceCreate>,
) -> AppResult<Json<SourceRead>> {
    valid_interval(body.scan_interval_minutes)?;
    valid_limit(body.scan_limit)?;
    if !valid_license(&body.license) {
        return Err(AppError::Invalid("unsupported source license".into()));
    }
    let (kind, id, url, title) = resolve_source(
        &state,
        body.source_type.as_deref(),
        body.source_id.as_deref(),
        body.source_url.as_deref(),
    )
    .await?;
    let row=sqlx::query_scalar::<_,Uuid>(
        "INSERT INTO youtube_sources (id,source_type,source_id,source_url,display_name,license,proof_url,
                 enabled,scan_interval_minutes,scan_limit,auto_process)
         VALUES($1,$2::youtube_source_type,$3,$4,$5,$6::youtube_source_license,$7,$8,$9,$10,$11)
         ON CONFLICT (source_type,source_id) DO UPDATE SET
           source_url=EXCLUDED.source_url,
           display_name=COALESCE(EXCLUDED.display_name,youtube_sources.display_name),
           license=EXCLUDED.license,proof_url=EXCLUDED.proof_url,
           enabled=EXCLUDED.enabled,scan_interval_minutes=EXCLUDED.scan_interval_minutes,
           scan_limit=EXCLUDED.scan_limit,auto_process=EXCLUDED.auto_process,updated_at=now()
         RETURNING id")
      .bind(Uuid::new_v4()).bind(&kind).bind(&id).bind(&url).bind(title)
      .bind(&body.license).bind(normal_opt(body.proof_url)).bind(body.enabled)
      .bind(body.scan_interval_minutes).bind(body.scan_limit).bind(body.auto_process)
      .fetch_one(&state.db.pool).await?;
    Ok(Json(load(&state, row).await?))
}
async fn patch(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(body): Json<SourcePatch>,
) -> AppResult<Json<SourceRead>> {
    if let Some(v) = body.scan_interval_minutes {
        valid_interval(v)?;
    }
    if let Some(v) = body.scan_limit {
        valid_limit(v)?;
    }
    if let Some(v) = body.license.as_deref()
        && !valid_license(v)
    {
        return Err(AppError::Invalid("unsupported source license".into()));
    }
    let mut tx = state.db.pool.begin().await?;
    let result = sqlx::query(
        "UPDATE youtube_sources SET license=COALESCE($1::youtube_source_license,license),
         proof_url=CASE WHEN $2 THEN $3 ELSE proof_url END, enabled=COALESCE($4,enabled),
         scan_interval_minutes=COALESCE($5,scan_interval_minutes),
         scan_limit=COALESCE($6,scan_limit),auto_process=COALESCE($7,auto_process),
         display_name=CASE WHEN $8 THEN $9 ELSE display_name END,updated_at=now()
         WHERE id=$10",
    )
    .bind(body.license)
    .bind(body.proof_url.is_some())
    .bind(body.proof_url.flatten().and_then(|s| normal_opt(Some(s))))
    .bind(body.enabled)
    .bind(body.scan_interval_minutes)
    .bind(body.scan_limit)
    .bind(body.auto_process)
    .bind(body.display_name.is_some())
    .bind(
        body.display_name
            .flatten()
            .and_then(|s| display_name(Some(s))),
    )
    .bind(id)
    .execute(&mut *tx)
    .await?;
    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("source not found".into()));
    }
    tx.commit().await?;
    Ok(Json(load(&state, id).await?))
}
async fn remove(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    let result = sqlx::query("DELETE FROM youtube_sources WHERE id=$1")
        .bind(id)
        .execute(&state.db.pool)
        .await?;
    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("source not found".into()));
    }
    Ok(Json(json!({"ok":true})))
}
async fn scan_by_id(
    state: &AppState,
    id: Uuid,
    opts: ScanOptions,
    since: Option<DateTime<Utc>>,
) -> AppResult<Json<Value>> {
    if let Some(n) = opts.limit {
        valid_limit(n)?;
    }
    let source = load(state, id).await?;
    if !source.enabled {
        return Err(AppError::Invalid("source disabled".into()));
    }
    let (token, _) = sources::claim_scan(&state.db, id).await?;
    let result = sources::scan_claimed(
        &state.config,
        &state.db,
        &state.scheduler,
        id,
        &token,
        opts.limit,
        opts.auto_process,
        since,
    )
    .await?;
    Ok(Json(json!({"discovered_count":result.discovered,
        "created_task_ids":result.created_task_ids,
        "skipped_duplicates":result.duplicated,
        "started_pipeline_job_ids":result.started_pipeline_job_ids})))
}
async fn scan(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    body: Option<Json<ScanOptions>>,
) -> AppResult<Json<Value>> {
    scan_by_id(&state, id, body.map(|Json(v)| v).unwrap_or_default(), None).await
}
async fn scan_by_source(
    State(state): State<AppState>,
    Json(request): Json<ScanByIdentity>,
) -> AppResult<Json<Value>> {
    valid_limit(request.limit)?;
    let id = sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM youtube_sources WHERE source_type::text=$1 AND source_id=$2",
    )
    .bind(request.source_type)
    .bind(request.source_id)
    .fetch_optional(&state.db.pool)
    .await?
    .ok_or_else(|| AppError::Invalid("source not found or disabled".into()))?;
    scan_by_id(
        &state,
        id,
        ScanOptions {
            limit: Some(request.limit),
            auto_process: Some(request.auto_process),
        },
        request.since,
    )
    .await
}
async fn scan_start(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    body: Option<Json<ScanOptions>>,
) -> AppResult<(StatusCode, Json<Value>)> {
    let opts = body.map(|Json(v)| v).unwrap_or_default();
    if let Some(n) = opts.limit {
        valid_limit(n)?;
    }
    let source = load(&state, id).await?;
    if !source.enabled {
        return Err(AppError::Invalid("source disabled".into()));
    }
    let (token, started) = sources::claim_scan(&state.db, id).await?;
    // Claim is durable before 202. A restart can recover after lease expiry;
    // the worker always checks its claim before writing any scan result.
    tokio::spawn(async move {
        if let Err(error) = sources::scan_claimed(
            &state.config,
            &state.db,
            &state.scheduler,
            id,
            &token,
            opts.limit,
            opts.auto_process,
            None,
        )
        .await
        {
            tracing::warn!(source_id=%id,error=%error,"V1 YouTube source background scan failed");
        }
    });
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({"accepted":true,"scan_started_at":started})),
    ))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/youtube/sources", get(list).post(create))
        .route(
            "/youtube/sources/{id}",
            get(get_one).patch(patch).delete(remove),
        )
        .route("/youtube/sources/{id}/scan", post(scan))
        .route("/youtube/sources/{id}/scan/start", post(scan_start))
        .route("/youtube/scan", post(scan_by_source))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_identifiers_follow_python_url_contract() {
        let c = "UC12345678901234567890";
        let p = "PL12345678901234567890";
        assert_eq!(direct_source(c).unwrap().unwrap().0, "channel");
        assert_eq!(direct_source(p).unwrap().unwrap().0, "playlist");
        assert_eq!(
            direct_source(&format!("https://www.youtube.com/channel/{c}/videos"))
                .unwrap()
                .unwrap()
                .1,
            c
        );
        assert_eq!(
            direct_source(&format!("https://youtube.com/playlist?list={p}"))
                .unwrap()
                .unwrap()
                .1,
            p
        );
        assert!(direct_source("http://127.0.0.1:8000").is_err());
        assert!(direct_source("https://evil.youtube.com/channel/UC12345678901234567890").is_err());
    }
    #[test]
    fn invalid_source_ids_and_limits_fail_closed() {
        assert!(!source_id_valid("channel", "UCx"));
        assert!(!source_id_valid("playlist", "PL123"));
        assert!(valid_limit(0).is_err());
        assert!(valid_interval(1441).is_err());
        assert_eq!(display_name(Some(" ".into())), None);
    }
}
