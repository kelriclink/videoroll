//! Persistent YouTube channel/playlist discovery without touching v1 Hatchet.
//! Reuses v1 youtube_sources metadata and independent v2 task source dedup.
use crate::{AppError, AppResult, Config, Database, api::AppState, scheduler::Scheduler};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{FromRow, Row};
use std::process::Stdio;
use tokio::{process::Command, time::timeout};
use uuid::Uuid;

#[derive(Debug, Clone, FromRow, Serialize)]
pub struct Source {
    pub id: Uuid,
    pub source_type: String,
    pub source_id: String,
    pub source_url: Option<String>,
    pub display_name: Option<String>,
    pub license: String,
    pub enabled: bool,
    pub scan_interval_minutes: i32,
    pub scan_limit: i32,
    pub auto_process: bool,
    pub last_scan_discovered_count: i32,
    pub last_scan_created_count: i32,
    pub last_scan_error: Option<String>,
}
#[derive(Debug, Deserialize)]
pub struct NewSource {
    pub source_type: String,
    pub source_id: String,
    pub source_url: Option<String>,
    pub display_name: Option<String>,
    #[serde(default = "default_license")]
    pub license: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default = "default_si")]
    pub scan_interval_minutes: i32,
    #[serde(default = "default_limit")]
    pub scan_limit: i32,
    #[serde(default = "yes")]
    pub auto_process: bool,
}
fn default_license() -> String {
    "unknown".into()
}
fn yes() -> bool {
    true
}
fn default_si() -> i32 {
    60
}
fn default_limit() -> i32 {
    20
}
#[derive(Debug, Deserialize)]
pub struct SourceUpdate {
    pub enabled: Option<bool>,
    pub auto_process: Option<bool>,
    pub scan_interval_minutes: Option<i32>,
    pub scan_limit: Option<i32>,
    pub display_name: Option<String>,
}
#[derive(Debug, Deserialize)]
struct LimitQuery {
    limit: Option<i64>,
}
#[derive(Debug, Serialize)]
pub struct ScanReport {
    pub source_id: Uuid,
    pub discovered: usize,
    pub created: usize,
    pub duplicated: usize,
    pub pipelines_started: usize,
    pub created_task_ids: Vec<Uuid>,
    pub started_pipeline_job_ids: Vec<String>,
}
fn source_sql() -> &'static str {
    "SELECT id,source_type::text,source_id,source_url,display_name,license::text,enabled,scan_interval_minutes,scan_limit,auto_process,last_scan_discovered_count,last_scan_created_count,last_scan_error FROM youtube_sources"
}
fn check_source(source_type: &str, source_id: &str, url: Option<&str>) -> AppResult<String> {
    if !matches!(source_type, "channel" | "playlist") {
        return Err(AppError::Invalid(
            "YouTube source type must be channel or playlist".into(),
        ));
    }
    if source_id.len() < 8
        || source_id.len() > 128
        || !source_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'))
    {
        return Err(AppError::Invalid("YouTube source ID malformed".into()));
    }
    let derived = if source_type == "channel" {
        format!("https://www.youtube.com/channel/{source_id}/videos")
    } else {
        format!("https://www.youtube.com/playlist?list={source_id}")
    };
    if let Some(value) = url {
        let parsed = reqwest::Url::parse(value)
            .map_err(|_| AppError::Invalid("YouTube source URL invalid".into()))?;
        if parsed.scheme() != "https"
            || !matches!(
                parsed.host_str(),
                Some("youtube.com" | "www.youtube.com" | "m.youtube.com")
            )
        {
            return Err(AppError::Invalid(
                "source URL must point to the YouTube website".into(),
            ));
        }
        if parsed.username() != "" || parsed.password().is_some() {
            return Err(AppError::Invalid(
                "YouTube source URL cannot contain credentials".into(),
            ));
        }
    }
    // Source ID is canonical and used to ensure stable feed identity.
    Ok(derived)
}
async fn list(
    State(state): State<AppState>,
    Query(q): Query<LimitQuery>,
) -> AppResult<Json<Vec<Source>>> {
    let query = format!("{} ORDER BY created_at DESC LIMIT $1", source_sql());
    let rows = sqlx::query_as::<_, Source>(&query)
        .bind(q.limit.unwrap_or(100).clamp(1, 200))
        .fetch_all(&state.db.pool)
        .await?;
    Ok(Json(rows))
}
async fn get_source(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Source>> {
    Ok(Json(load(&state.db, id).await?))
}
async fn load(db: &Database, id: Uuid) -> AppResult<Source> {
    sqlx::query_as::<_, Source>(&format!("{} WHERE id=$1", source_sql()))
        .bind(id)
        .fetch_optional(&db.pool)
        .await?
        .ok_or_else(|| AppError::NotFound("YouTube source not found".into()))
}
async fn create(
    State(state): State<AppState>,
    Json(v): Json<NewSource>,
) -> AppResult<(StatusCode, Json<Source>)> {
    let url = check_source(&v.source_type, &v.source_id, v.source_url.as_deref())?;
    if !matches!(v.license.as_str(), "own" | "authorized" | "cc" | "unknown") {
        return Err(AppError::Invalid("invalid source license".into()));
    }
    let id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO youtube_sources(id,source_type,source_id,source_url,display_name,license,enabled,scan_interval_minutes,scan_limit,auto_process,last_scan_discovered_count,last_scan_created_count,last_scan_started_pipeline_count,last_scan_skipped_duplicates) VALUES($1,$2::youtube_source_type,$3,$4,$5,$6::youtube_source_license,$7,$8,$9,$10,0,0,0,0)"
    ).bind(id).bind(&v.source_type).bind(&v.source_id).bind(url).bind(v.display_name.as_ref().map(|s|s.chars().take(255).collect::<String>()))
        .bind(&v.license).bind(v.enabled).bind(v.scan_interval_minutes.clamp(1,1440))
        .bind(v.scan_limit.clamp(1,200)).bind(v.auto_process)
        .execute(&state.db.pool).await?;
    Ok((StatusCode::CREATED, Json(load(&state.db, id).await?)))
}
async fn update(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
    Json(v): Json<SourceUpdate>,
) -> AppResult<Json<Source>> {
    let mut tx = state.db.pool.begin().await?;
    let affected=sqlx::query(
        "UPDATE youtube_sources SET enabled=COALESCE($1,enabled),auto_process=COALESCE($2,auto_process),scan_interval_minutes=COALESCE($3,scan_interval_minutes),scan_limit=COALESCE($4,scan_limit),display_name=COALESCE($5,display_name),updated_at=now() WHERE id=$6"
    ).bind(v.enabled).bind(v.auto_process)
        .bind(v.scan_interval_minutes.map(|v|v.clamp(1,1440)))
        .bind(v.scan_limit.map(|v|v.clamp(1,200)))
        .bind(v.display_name.map(|s|s.chars().take(255).collect::<String>()))
        .bind(id).execute(&mut *tx).await?;
    if affected.rows_affected() == 0 {
        return Err(AppError::NotFound("YouTube source not found".into()));
    }
    tx.commit().await?;
    Ok(Json(load(&state.db, id).await?))
}
async fn remove(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<StatusCode> {
    let result = sqlx::query("DELETE FROM youtube_sources WHERE id=$1")
        .bind(id)
        .execute(&state.db.pool)
        .await?;
    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("YouTube source not found".into()));
    }
    Ok(StatusCode::NO_CONTENT)
}
#[derive(Debug, Clone, PartialEq, Eq)]
struct FeedVideo {
    id: String,
    published_at: Option<chrono::DateTime<chrono::Utc>>,
}
fn feed_published_at(value: &Value) -> Option<chrono::DateTime<chrono::Utc>> {
    for key in ["timestamp", "release_timestamp"] {
        if let Some(ts) = value.get(key).and_then(Value::as_i64)
            && let Some(date) = chrono::DateTime::<chrono::Utc>::from_timestamp(ts, 0)
        {
            return Some(date);
        }
    }
    for key in ["upload_date", "release_date"] {
        if let Some(raw) = value.get(key).and_then(Value::as_str)
            && let Ok(date) = chrono::NaiveDate::parse_from_str(raw, "%Y%m%d")
        {
            return date.and_hms_opt(0, 0, 0).map(|dt| dt.and_utc());
        }
    }
    None
}
fn normalize_flat_feed(
    raw: &[u8],
    since: Option<chrono::DateTime<chrono::Utc>>,
) -> AppResult<Vec<FeedVideo>> {
    if raw.len() > 20 * 1024 * 1024 {
        return Err(AppError::Unavailable(
            "YouTube feed exceeded size budget".into(),
        ));
    }
    let lines = std::str::from_utf8(raw)
        .map_err(|_| AppError::Unavailable("yt-dlp returned non-UTF8 metadata".into()))?;
    let mut videos = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for line in lines.lines() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let id = value.get("id").and_then(Value::as_str).unwrap_or("");
        if id.len() != 11
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'))
        {
            continue;
        }
        if matches!(
            value.get("live_status").and_then(Value::as_str),
            Some("is_live" | "is_upcoming")
        ) {
            continue;
        }
        let published_at = feed_published_at(&value);
        // Python excludes undated entries when an explicit cutoff is supplied.
        if since.is_some_and(|cutoff| published_at.is_none_or(|d| d <= cutoff)) {
            continue;
        }
        if seen.insert(id.to_string()) {
            videos.push(FeedVideo {
                id: id.to_string(),
                published_at,
            });
        }
    }
    Ok(videos)
}
fn parse_rss(raw:&str)->AppResult<Vec<FeedVideo>>{
    let doc=roxmltree::Document::parse(raw).map_err(|_|AppError::Unavailable("invalid YouTube RSS XML".into()))?;
    let root=doc.root_element();
    let atom="http://www.w3.org/2005/Atom";
    let yt="http://www.youtube.com/xml/schemas/2015";
    if !root.has_tag_name((atom,"feed")){return Err(AppError::Unavailable("YouTube RSS response is not an Atom feed".into()));}
    let mut seen=std::collections::HashSet::new();
    let mut entries=Vec::new();
    for item in root.children().filter(|n|n.has_tag_name((atom,"entry"))){
        let Some(id)=item.children().find(|n|n.has_tag_name((yt,"videoId"))).and_then(|n|n.text()).map(str::trim) else{continue;};
        if id.len()!=11 || !id.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'_'||b==b'-')||!seen.insert(id.to_owned()){continue;}
        let Some(date)=item.children().find(|n|n.has_tag_name((atom,"published"))).and_then(|n|n.text()) else{continue;};
        let date=chrono::DateTime::parse_from_rfc3339(date.trim()).map_err(|_|AppError::Unavailable("invalid YouTube RSS published date".into()))?;
        entries.push(FeedVideo{id:id.to_owned(),published_at:Some(date.with_timezone(&chrono::Utc))});
    }
    Ok(entries)
}
async fn fetch_rss(kind:&str,id:&str,proxy:Option<&str>)->AppResult<Vec<FeedVideo>>{
    let param=if kind=="channel"{"channel_id"}else{"playlist_id"};
    let mut builder=reqwest::Client::builder().timeout(std::time::Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(std::env::var("YOUTUBE_USER_AGENT").unwrap_or_else(|_|"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36".into()));
    if let Some(proxy)=proxy{builder=builder.proxy(reqwest::Proxy::all(proxy).map_err(|_|AppError::Invalid("invalid YouTube RSS proxy".into()))?);}
    let mut response=builder.build()?.get("https://www.youtube.com/feeds/videos.xml").query(&[(param,id)]).send().await.map_err(|_|AppError::Unavailable("YouTube RSS request failed".into()))?;
    if !response.status().is_success(){return Err(AppError::Unavailable(format!("YouTube RSS HTTP {}",response.status())));}
    let mut bytes=Vec::new();
    while let Some(chunk)=response.chunk().await.map_err(|_|AppError::Unavailable("YouTube RSS read failed".into()))?{
        if bytes.len()+chunk.len()>2*1024*1024{return Err(AppError::Unavailable("YouTube RSS exceeded size budget".into()));}
        bytes.extend_from_slice(&chunk);
    }
    let raw=std::str::from_utf8(&bytes).map_err(|_|AppError::Unavailable("YouTube RSS non-UTF8".into()))?;
    parse_rss(raw)
}
/// Claim the source before an HTTP 202 response. The lease survives process
/// crashes, so callers cannot accidentally schedule duplicate scans.
pub async fn claim_scan(
    db: &Database,
    source_id: Uuid,
) -> AppResult<(String, chrono::DateTime<chrono::Utc>)> {
    claim_scan_inner(db,source_id,true).await?
        .ok_or_else(||AppError::Conflict("source is disabled or already being scanned".into()))
}
async fn claim_scan_inner(db:&Database,source_id:Uuid,force:bool)->AppResult<Option<(String,chrono::DateTime<chrono::Utc>)>>{
    let token = Uuid::new_v4().to_string();
    let started = sqlx::query_scalar::<_, chrono::DateTime<chrono::Utc>>(
        "UPDATE youtube_sources SET scan_lock_owner=$1,scan_lock_until=now()+($4*interval '1 second'),last_scan_started_at=now(),last_scan_error=NULL,updated_at=now()
         WHERE id=$2 AND enabled=true AND (scan_lock_until IS NULL OR scan_lock_until<now())
         AND ($3 OR COALESCE(last_scan_finished_at,last_scan_started_at) IS NULL OR COALESCE(last_scan_finished_at,last_scan_started_at)+scan_interval_minutes*interval '1 minute'<=now())
         RETURNING last_scan_started_at"
    ).bind(&token).bind(source_id).bind(force).bind(scan_ttl()).fetch_optional(&db.pool).await?;
    Ok(started.map(|date|(token,date)))
}
fn scan_ttl()->i64{std::env::var("YOUTUBE_SOURCE_SCAN_LOCK_TTL_SECONDS").ok().and_then(|v|v.parse::<i64>().ok()).unwrap_or(900).max(60)}

pub async fn scan_claimed(
    config: &Config,
    db: &Database,
    scheduler: &Scheduler,
    source_id: Uuid,
    lease_token: &str,
    limit_override: Option<i32>,
    auto_override: Option<bool>,
    since: Option<chrono::DateTime<chrono::Utc>>,
) -> AppResult<ScanReport> {
    let lease = sqlx::query(
        "SELECT source_url,source_type::text,source_id,license::text,proof_url,scan_limit,auto_process
         FROM youtube_sources WHERE id=$1 AND scan_lock_owner=$2 AND scan_lock_until>now()",
    )
    .bind(source_id)
    .bind(lease_token)
    .fetch_optional(&db.pool)
    .await?
    .ok_or_else(|| AppError::Conflict("YouTube source scan claim expired".into()))?;
    let source_type: String = lease.try_get("source_type")?;
    let canonical_id: String = lease.try_get("source_id")?;
    let url: Option<String> = lease.try_get("source_url")?;
    let url = check_source(&source_type, &canonical_id, url.as_deref())?;
    let license: String = lease.try_get("license")?;
    let proof:Option<String>=lease.try_get("proof_url")?;
    let scan_limit: i32 = limit_override.unwrap_or(lease.try_get("scan_limit")?);
    let auto_process: bool = auto_override.unwrap_or(lease.try_get("auto_process")?);
    // Python heartbeats its manual source lease throughout long enumerations.
    let heartbeat_db = db.clone();
    let heartbeat_token = lease_token.to_string();
    let heartbeat = tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs((scan_ttl()/3).clamp(15,60) as u64)).await;
            let renewed = sqlx::query(
                "UPDATE youtube_sources SET scan_lock_until=now()+($3*interval '1 second')
                 WHERE id=$1 AND scan_lock_owner=$2 AND scan_lock_until>now()",
            )
            .bind(source_id)
            .bind(&heartbeat_token)
            .bind(scan_ttl())
            .execute(&heartbeat_db.pool)
            .await;
            match renewed {
                Ok(result) if result.rows_affected() == 1 => {}
                Ok(_) => break,
                Err(error) => {
                    tracing::warn!(%source_id,%error,"YouTube scan lease heartbeat failed");
                    break;
                }
            }
        }
    });
    let mut report = ScanReport {source_id,discovered:0,created:0,duplicated:0,pipelines_started:0,created_task_ids:Vec::new(),started_pipeline_job_ids:Vec::new()};
    let mut feed_completed=false;
    let mut enqueue_error=None;
    let result = async {
        let settings = crate::youtube_settings_v1::runtime(db, config).await?;
        let cookies = crate::youtube_settings_v1::prepare_cookie_copy(config, &settings).await?;
        let urls=if source_type=="channel"{vec![url.clone(),format!("https://www.youtube.com/channel/{canonical_id}/shorts")]}else{vec![url]};
        let mut ids=Vec::new();
        let mut successful=false;
        let mut seen=std::collections::HashSet::new();
        for feed_url in urls {
        let fetched=async {
        let mut downloader = Command::new(&config.download_binary);
        // Enumerate the entire source and use the same proxy/cookie settings
        // as the existing VideoRoll downloader. Never log credential contents.
        downloader.args([
            "--ignore-config",
            "--flat-playlist",
            "--dump-json",
            "--no-warnings",
            "--no-playlist-reverse",
        ]);
        if let Some(proxy) = settings.proxy.as_deref() {
            downloader.arg("--proxy").arg(proxy);
        }
        if let Some(cookie_file) = cookies.as_ref() {
            downloader
                .arg("--cookies")
                .arg(cookie_file.path());
        }
        downloader.args(crate::download_runtime::original_transport_args()).args(crate::download_runtime::original_extractor_args(settings.compatibility,None)?);
        let command = downloader
            .arg("--")
            .arg(feed_url)
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output();
        let output = timeout(std::time::Duration::from_secs(240), command)
            .await
            .map_err(|_| AppError::Unavailable("yt-dlp source scan exceeded 4 minutes".into()))??;
        if !output.status.success() {
            return Err(AppError::Unavailable(format!(
                "yt-dlp source scan returned {}",
                output.status
            )));
        }
        normalize_flat_feed(&output.stdout,None)
        }.await;
        match fetched {
            Ok(entries)=>{successful=true;for item in entries{if seen.insert(item.id.clone()){ids.push(item);}}},
            Err(error)=>tracing::warn!(%source_id,%error,"YouTube source tab failed"),
        }
        }
        if source_type=="channel" {ids.sort_by(|a,b|b.published_at.cmp(&a.published_at));}
        if ids.is_empty(){
            match fetch_rss(&source_type,&canonical_id,settings.proxy.as_deref()).await{
                Ok(entries)=>{ids=entries;successful=true;},
                Err(error)=>{if !successful{return Err(AppError::Unavailable(format!("all YouTube feed fetch attempts failed: {error}")));}tracing::warn!(%source_id,%error,"YouTube RSS fallback failed");}
            }
        }
        ids.retain(|item|since.is_none_or(|cutoff|item.published_at.is_some_and(|d|d>cutoff)));
        feed_completed=true;
        report.discovered=ids.len();
        // The Python scanner enumerates *all* entries, then takes the first
        // N missing videos. Checking only the first N entries repeatedly
        // starves older, still-unprocessed playlist items.
        let mut ingested = std::collections::HashSet::new();
        for batch in ids.chunks(500) {
            let source_ids: Vec<String> = batch.iter().map(|v| v.id.clone()).collect();
            let existing: Vec<String> = sqlx::query_scalar(
                "SELECT source_id FROM ingested_videos
                 WHERE platform='youtube' AND source_id = ANY($1)",
            )
            .bind(&source_ids)
            .fetch_all(&db.pool)
            .await?;
            ingested.extend(existing);
        }
        report.duplicated=ids.iter().filter(|v| ingested.contains(&v.id)).count();
        for entry in ids
            .into_iter()
            .filter(|v| !ingested.contains(&v.id))
            .take(scan_limit.clamp(1, 200) as usize)
        {
            // Never let a stale scanner create tasks after losing its lease.
            let owns_lease: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM youtube_sources
                  WHERE id=$1 AND scan_lock_owner=$2 AND scan_lock_until>now())",
            )
            .bind(source_id)
            .bind(lease_token)
            .fetch_one(&db.pool)
            .await?;
            if !owns_lease {
                return Err(AppError::Conflict("YouTube source scan lease lost".into()));
            }
            let url = format!("https://www.youtube.com/watch?v={}", entry.id);
            let (task, workflow, duplicate, start_error) = scheduler
                .create_scanned_youtube_task(&url,&license,auto_process,proof.as_deref(),entry.published_at,source_id,lease_token)
                .await?;
            if duplicate {
                // Another scanner (including V1) won the unique source race.
                report.duplicated += 1;
            } else {
                report.created += 1;
                report.created_task_ids.push(task.id);
            }
            if let Some(flow) = workflow {
                report.pipelines_started += 1;
                report.started_pipeline_job_ids.push(flow.id.to_string());
            }
            if let Some(error)=start_error{tracing::warn!(%source_id,%error,"source pipeline enqueue failed");enqueue_error=Some(error);}
        }
        Ok::<(), AppError>(())
    }.await;
    heartbeat.abort();
    let error=result.as_ref().err().map(|e|e.to_string().chars().take(1000).collect::<String>()).or(enqueue_error);
    let (discovered,created,duplicates,pipelines)=(report.discovered as i32,report.created as i32,report.duplicated as i32,report.pipelines_started as i32);
    sqlx::query(
        "UPDATE youtube_sources SET last_scan_finished_at=now(),last_scan_discovered_count=CASE WHEN $8 THEN $1 ELSE last_scan_discovered_count END,last_scan_created_count=CASE WHEN $8 THEN $2 ELSE last_scan_created_count END,last_scan_skipped_duplicates=CASE WHEN $8 THEN $3 ELSE last_scan_skipped_duplicates END,last_scan_started_pipeline_count=CASE WHEN $8 THEN $4 ELSE last_scan_started_pipeline_count END,last_scan_error=$5,last_nonempty_scan_finished_at=CASE WHEN $8 AND $1>0 THEN now() ELSE COALESCE(last_nonempty_scan_finished_at,CASE WHEN last_scan_discovered_count>0 THEN last_scan_finished_at END) END,last_nonempty_discovered_count=CASE WHEN $8 AND $1>0 THEN $1 WHEN last_nonempty_scan_finished_at IS NULL AND last_scan_discovered_count>0 THEN last_scan_discovered_count ELSE last_nonempty_discovered_count END,total_scan_count=COALESCE(total_scan_count,0)+CASE WHEN $8 AND $5 IS NULL THEN 1 ELSE 0 END,total_failed_scan_count=COALESCE(total_failed_scan_count,0)+CASE WHEN $5 IS NULL THEN 0 ELSE 1 END,total_created_count=COALESCE(total_created_count,0)+$2,total_started_pipeline_count=COALESCE(total_started_pipeline_count,0)+$4,scan_lock_owner=NULL,scan_lock_until=NULL,updated_at=now() WHERE id=$6 AND scan_lock_owner=$7"
    ).bind(discovered).bind(created).bind(duplicates).bind(pipelines).bind(error)
        .bind(source_id).bind(&lease_token).bind(feed_completed).execute(&db.pool).await?;
    result.map(|()|report)
}
pub async fn scan_source(
    config: &Config,
    db: &Database,
    scheduler: &Scheduler,
    source_id: Uuid,
) -> AppResult<ScanReport> {
    let (token, _) = claim_scan(db, source_id).await?;
    scan_claimed(config, db, scheduler, source_id, &token, None, None, None).await
}

async fn scan(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<ScanReport>> {
    Ok(Json(
        scan_source(&state.config, &state.db, &state.scheduler, id).await?,
    ))
}
pub async fn scan_due(config: &Config, db: &Database, scheduler: &Scheduler) -> AppResult<u64> {
    let ids=sqlx::query_scalar::<_,Uuid>(
        "SELECT id FROM youtube_sources WHERE enabled=true AND (COALESCE(last_scan_finished_at,last_scan_started_at) IS NULL OR COALESCE(last_scan_finished_at,last_scan_started_at)+scan_interval_minutes*interval '1 minute'<=now()) AND (scan_lock_until IS NULL OR scan_lock_until<now()) ORDER BY COALESCE(last_scan_finished_at,last_scan_started_at,created_at),id"
    ).fetch_all(&db.pool).await?;
    let mut count=0u64;
    for id in ids {
        let Some((token,_))=claim_scan_inner(db,id,false).await? else{continue;};
        match scan_claimed(config,db,scheduler,id,&token,None,None,None).await {
            Ok(_)=>count+=1,
            Err(error)=>tracing::warn!(source_id=%id,error=%error,"YouTube source scan failed"),
        }
    }
    Ok(count)
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/youtube/sources", get(list).post(create))
        .route(
            "/youtube/sources/{id}",
            get(get_source).patch(update).delete(remove),
        )
        .route("/youtube/sources/{id}/scan", post(scan))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn metadata_source_scan_is_deduplicated_and_limited() {
        let lines = concat!(
            "{\"id\":\"0123456789A\",\"title\":\"one\"}\n",
            "{\"id\":\"0123456789A\",\"title\":\"duplicate\"}\n",
            "{\"id\":\"0123456789B\",\"live_status\":\"is_live\"}\n",
            "{\"id\":\"0123456789C\"}\n"
        );
        let videos = normalize_flat_feed(lines.as_bytes(), None).unwrap();
        assert_eq!(
            videos.iter().map(|v| v.id.as_str()).collect::<Vec<_>>(),
            vec!["0123456789A", "0123456789C"]
        );
        assert_eq!(videos.len(), 2);
    }
    #[test]
    fn explicit_since_filters_undated_and_old_entries() {
        let lines = concat!(
            "{\"id\":\"0123456789A\",\"upload_date\":\"20261118\"}\n",
            "{\"id\":\"0123456789B\",\"timestamp\":1795348800}\n",
            "{\"id\":\"0123456789C\"}\n"
        );
        let cutoff = chrono::DateTime::parse_from_rfc3339("2026-11-20T00:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let rows = normalize_flat_feed(lines.as_bytes(), Some(cutoff)).unwrap();
        assert_eq!(
            rows.iter().map(|v| v.id.as_str()).collect::<Vec<_>>(),
            vec!["0123456789B"]
        );
    }
    #[test]
    fn rss_atom_parsing_preserves_original_publication_date() {
        let raw=r#"<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>0123456789A</yt:videoId><published>2026-11-22T12:00:00Z</published></entry></feed>"#;
        let rows=parse_rss(raw).unwrap();
        assert_eq!(rows.len(),1);
        assert_eq!(rows[0].id,"0123456789A");
        assert_eq!(rows[0].published_at.unwrap().to_rfc3339(),"2026-11-22T12:00:00+00:00");
        assert!(parse_rss("<html/>").is_err());
        assert!(parse_rss(r#"<feed xmlns="http://www.w3.org/2005/Atom"/>"#).unwrap().is_empty());
    }
    #[test]
    fn user_source_url_cannot_switch_to_internal_network() {
        assert!(check_source("channel", "UC1234567890000000000", None).is_ok());
        assert!(check_source("playlist", "PL1234567890000000000", None).is_ok());
        assert!(
            check_source(
                "channel",
                "UC1234567890000000000",
                Some("http://127.0.0.1:9000/")
            )
            .is_err()
        );
    }
}
