//! Native V1 YouTube settings facade.
//! Preserve the original PostgreSQL cookie settings contract.
//! Downloads receive a disposable copy; no persistent cookie file.
//! No raw cookies in JSON responses, logs or V2 assets.
use crate::{AppError, AppResult, Config, Database, api::AppState};
use axum::{Json, Router, extract::State, routing::get};
use chrono::Utc;
use reqwest::Url;
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    path::PathBuf,
    time::{Duration, Instant},
};
use tokio::fs;

const SETTING_KEY: &str = "youtube.settings";
const MAX_COOKIES: usize = 200_000;
const MAX_PROXY: usize = 512;
#[derive(Clone)]
pub struct YoutubeRuntime {
    pub proxy: Option<String>,
    cookie_text: Option<String>,
    cookies_enabled: bool,
    pub parallel: bool,
    pub connections: u8,
    pub compatibility: bool,
}
pub fn validate_proxy(s: &str) -> AppResult<Option<String>> {
    let p = s.trim();
    if p.is_empty() {
        return Ok(None);
    }
    if p.len() > MAX_PROXY || p.chars().any(char::is_control) {
        return Err(AppError::Invalid(
            "YouTube proxy URL invalid or too long".into(),
        ));
    }
    let url = Url::parse(p)
        .map_err(|_| AppError::Invalid("YouTube proxy must be an explicit URL".into()))?;
    if !matches!(url.scheme(), "http" | "https" | "socks5" | "socks5h") {
        return Err(AppError::Invalid(
            "proxy scheme must be http,https,socks5 or socks5h".into(),
        ));
    }
    if url.host_str().is_none()
        || url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !matches!(url.path(), "" | "/")
    {
        return Err(AppError::Invalid(
            "proxy URL cannot include credentials, path, query or fragment".into(),
        ));
    }
    Ok(Some(p.into()))
}
#[allow(dead_code)]
fn trusted_domain(domain: &str) -> bool {
    const ROOTS: [&str; 6] = [
        "youtube.com",
        "youtu.be",
        "google.com",
        "googlevideo.com",
        "youtube-nocookie.com",
        "googleusercontent.com",
    ];
    ROOTS.iter().any(|suffix| {
        domain == *suffix
            || domain
                .strip_suffix(suffix)
                .is_some_and(|p| p.ends_with('.'))
    })
}
pub fn validate_cookies(raw: &str) -> AppResult<String> {
    let s = raw.trim_start_matches('\u{feff}').replace("\r\n", "\n").replace('\r', "\n");
    if s.trim().is_empty() { return Ok(String::new()); }
    let mut input = s.lines().peekable();
    while input.peek().is_some_and(|v| v.trim().is_empty()) { input.next(); }
    if input.peek().is_some_and(|v| v.starts_with("# Netscape HTTP Cookie File") || v.starts_with("# HTTP Cookie File")) {
        input.next();
        while let Some(line) = input.peek() {
            if line.trim().is_empty() { input.next(); break; }
            if line.trim_start().starts_with('#') && !line.trim_start().starts_with("#HttpOnly_") { input.next(); } else { break; }
        }
    }
    let mut lines = Vec::new();
    for line in input {
        let line = line.trim();
        if line.is_empty() || (line.starts_with('#') && !line.starts_with("#HttpOnly_")) {
            lines.push(line.to_owned());
            continue;
        }
        let (prefix, data) = match line.strip_prefix("#HttpOnly_") {
            Some(v) => ("#HttpOnly_", v), None => ("", line),
        };
        let mut parts: Vec<String> = if data.contains('\t') {
            data.split('\t').map(str::to_owned).collect()
        } else { data.split_whitespace().map(str::to_owned).collect() };
        if parts.len() != 7 { return Err(AppError::Invalid("invalid Netscape cookie fields".into())); }
        let mut domain = parts[0].trim().to_owned();
        let mut flag = parts[1].trim().to_uppercase();
        if flag != "TRUE" && flag != "FALSE" { flag = if domain.starts_with('.') {"TRUE"} else {"FALSE"}.into(); }
        if flag == "TRUE" && !domain.starts_with('.') && domain.contains('.') { domain.insert(0,'.'); }
        if domain.starts_with('.') { flag = "TRUE".into(); }
        if (flag == "TRUE") != domain.starts_with('.') || (!parts[4].is_empty() && parts[4].parse::<i64>().is_err()) {
            return Err(AppError::Invalid("invalid Netscape cookie fields".into()));
        }
        parts[0] = domain;
        parts[1] = flag;
        lines.push(format!("{prefix}{}", parts.join("\t")));
    }
    let normalized = format!("# Netscape HTTP Cookie File\n# http://curl.haxx.se/rfc/cookie_spec.html\n# This is a generated file!  Do not edit.\n\n{}\n", lines.join("\n").trim_end());
    if normalized.chars().count() > MAX_COOKIES { return Err(AppError::Invalid("cookies.txt exceeds limit".into())); }
    Ok(normalized)
}
/// The original environment cookie file takes precedence over DB cookies.
fn configured_cookie_file() -> Option<PathBuf> {
    std::env::var("YOUTUBE_COOKIE_FILE").ok().map(|s|s.trim().to_owned()).filter(|s|!s.is_empty()).map(PathBuf::from)
}
pub(crate) enum CookieInput {
    Configured(PathBuf),
    Temporary(tempfile::TempDir),
}
impl CookieInput {
    pub(crate) fn path(&self) -> PathBuf {
        match self { Self::Configured(path)=>path.clone(), Self::Temporary(dir)=>dir.path().join("cookies.txt") }
    }
}
fn select_cookie_file(configured: Option<PathBuf>, enabled: bool) -> Option<PathBuf> {
    configured.filter(|p| !enabled || p.is_file())
}
pub(crate) async fn prepare_cookie_copy(
    config: &Config,
    settings: &YoutubeRuntime,
) -> AppResult<Option<CookieInput>> {
    if let Some(path)=select_cookie_file(configured_cookie_file(), settings.cookies_enabled) {
        return Ok(Some(CookieInput::Configured(path)));
    }
    let Some(raw) = settings.cookie_text.as_deref() else {
        return Ok(None);
    };
    let dir = private_dir(config).await?;
    let temp = tempfile::Builder::new()
        .prefix("yt-worker-")
        .tempdir_in(&dir)?;
    let validated = validate_cookies(raw)?;
    #[cfg(unix)]
    {
        use tokio::io::AsyncWriteExt;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(temp.path().join("cookies.txt"))
            .await?;
        file.write_all(validated.as_bytes()).await?;
        file.sync_all().await?;
    }
    #[cfg(not(unix))]
    {
        return Err(AppError::Unavailable(
            "secure cookies require Unix permissions".into(),
        ));
    }
    Ok(Some(CookieInput::Temporary(temp)))
}

#[cfg(unix)]
async fn private_dir(config: &Config) -> AppResult<PathBuf> {
    let dir = config.storage_root.join(".videoroll-private");
    match fs::symlink_metadata(&dir).await {
        Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => {
            use std::os::unix::fs::PermissionsExt;
            if meta.permissions().mode() & 0o077 != 0 {
                return Err(AppError::Invalid(
                    "private cookie directory is not mode 0700".into(),
                ));
            }
        }
        Ok(_) => {
            return Err(AppError::Invalid(
                "cookie directory must not be a symlink".into(),
            ));
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            fs::DirBuilder::new()
                .recursive(false)
                .mode(0o700)
                .create(&dir)
                .await
                .or_else(|e| {
                    if e.kind() == std::io::ErrorKind::AlreadyExists {
                        Ok(())
                    } else {
                        Err(e)
                    }
                })?;
            let meta = fs::symlink_metadata(&dir).await?;
            use std::os::unix::fs::PermissionsExt;
            if !meta.is_dir()
                || meta.file_type().is_symlink()
                || meta.permissions().mode() & 0o077 != 0
            {
                return Err(AppError::Invalid("unsafe cookie directory".into()));
            }
        }
        Err(err) => return Err(err.into()),
    }
    Ok(dir)
}
#[cfg(not(unix))]
async fn private_dir(_config: &Config) -> AppResult<PathBuf> {
    Err(AppError::Unavailable("secure cookies require Unix".into()))
}
pub(crate) async fn read_settings(db: &Database) -> AppResult<Value> {
    // Use original settings including proxy, credentials and concurrency.
    let own: Option<Value> = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(SETTING_KEY)
        .fetch_optional(&db.pool)
        .await?;
    Ok(own.unwrap_or(json!({})))
}
pub(crate) async fn effective_cookie(_config: &Config, stored: &Value) -> AppResult<Option<String>> {
    if let Some(encoded) = stored
        .get("cookies_txt_enc")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
    {
        let text = crate::secret_compat::decode(encoded)?
            .ok_or_else(|| AppError::Invalid("empty legacy YouTube cookie".into()))?;
        return Ok(Some(validate_cookies(&text)?));
    }
    Ok(None)
}
fn runtime_from_json(value: &Value, cookie_text: Option<String>) -> AppResult<YoutubeRuntime> {
    let default_proxy = std::env::var("YOUTUBE_PROXY").unwrap_or_default();
    // An absent key uses the environment; an explicitly cleared value does not.
    let proxy = validate_proxy(match value.get("proxy") { None => &default_proxy, Some(v) => v.as_str().unwrap_or("") })?;
    let parallel = value
        .get("parallel_download_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let connections = value
        .get("parallel_download_connections")
        .and_then(Value::as_u64)
        .unwrap_or(8);
    if !(1..=16).contains(&connections) {
        return Err(AppError::Invalid(
            "parallel download connections must be 1-16".into(),
        ));
    }
    let enabled = value
        .get("cookies_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(cookie_text.is_some());
    Ok(YoutubeRuntime {
        proxy,
        cookies_enabled: enabled,
        cookie_text: if enabled { cookie_text } else { None },
        parallel,
        connections: connections as u8,
        compatibility: value
            .get("compatibility_mode_enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}
pub async fn runtime(db: &Database, config: &Config) -> AppResult<YoutubeRuntime> {
    let stored = read_settings(db).await?;
    let cookies = if stored.get("cookies_enabled").and_then(Value::as_bool) == Some(false) {
        None
    } else {
        effective_cookie(config, &stored).await?
    };
    runtime_from_json(&stored, cookies)
}
fn summary(value: &str) -> Value {
    let mut count = 0usize;
    let mut domains = HashSet::new();
    let mut auth = false;
    let mut visitor = false;
    let mut captcha = false;
    for line in value.lines() {
        if line.starts_with('#') && !line.starts_with("#HttpOnly_") {
            continue;
        }
        let p = line.split('\t').collect::<Vec<_>>();
        if p.len() != 7 {
            continue;
        }
        count += 1;
        let domain=p[0].trim_start_matches("#HttpOnly_").trim().trim_start_matches('.').to_ascii_lowercase();
        if !domain.is_empty() { domains.insert(domain); }
        auth |= matches!(p[5], "SID" | "HSID" | "SSID" | "APISID" | "SAPISID" | "__Secure-1PSID" | "__Secure-3PSID" | "__Secure-1PAPISID" | "__Secure-3PAPISID" | "LOGIN_INFO");
        visitor |= p[5] == "VISITOR_INFO1_LIVE";
        captcha |= p[5] == "GOOGLE_ABUSE_EXEMPTION";
    }
    json!({"cookies_count":count,"cookies_domains_count":domains.len(),
        "cookies_has_auth":auth,"cookies_has_visitor_info":visitor,
        "cookies_has_bot_check_bypass":captcha})
}
/// Historical Python home-scan telemetry comes from the legacy settings row.
/// V2 has no independent home-recommendation scanner yet. This routine exposes
/// persisted *past* counters, never claims a scan is active or enabled in V2.
async fn legacy_home_scan_history(db: &Database) -> AppResult<serde_json::Map<String, Value>> {
    let legacy: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(SETTING_KEY)
            .fetch_optional(&db.pool)
            .await?;
    let legacy = legacy.unwrap_or_else(|| json!({}));
    let mut values = serde_json::Map::new();
    for key in [
        "home_scan_last_started_at",
        "home_scan_last_finished_at",
        "home_scan_last_error",
    ] {
        let value = legacy
            .get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty());
        values.insert(key.into(), value.map(|s| json!(s)).unwrap_or(Value::Null));
    }
    for key in [
        "home_scan_last_discovered_count",
        "home_scan_last_started_count",
        "home_scan_last_skipped_duplicates",
        "home_scan_last_failed_count",
        "home_scan_last_candidate_count",
        "home_scan_last_explicit_shorts_count",
        "home_scan_last_known_duration_count",
        "home_scan_last_unknown_duration_count",
        "home_scan_last_below_min_duration_count",
        "home_scan_last_kept_unknown_duration_count",
        "home_scan_last_eligible_count",
    ] {
        let count = legacy.get(key).and_then(Value::as_i64).unwrap_or(0).max(0);
        values.insert(key.into(), json!(count));
    }
    for (key, max_items, max_chars) in [
        ("home_scan_last_log_lines", 40, 240),
        ("home_scan_last_sample_urls", 10, 2048),
    ] {
        let lines = legacy
            .get(key)
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::trim)
                    .filter(|v| !v.is_empty())
                    .take(max_items)
                    .map(|v| v.chars().take(max_chars).collect::<String>())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        values.insert(key.into(), json!(lines));
    }
    Ok(values)
}

async fn snapshot(state: &AppState) -> AppResult<Value> {
    let stored = read_settings(&state.db).await?;
    let cookies = effective_cookie(&state.config, &stored).await?;
    let runtime = runtime_from_json(&stored, cookies.clone())?;
    let mut result = json!({
        "proxy":runtime.proxy.unwrap_or_default(),
        "cookies_set":cookies.is_some(),
        "cookies_enabled":stored.get("cookies_enabled").and_then(Value::as_bool).unwrap_or(cookies.is_some()),
        "compatibility_mode_enabled":runtime.compatibility,
        "parallel_download_enabled":runtime.parallel,
        "parallel_download_connections":runtime.connections,
        "cookies_updated_at":stored.get("cookies_updated_at"),
        // The original environment file option is separate from DB-managed cookies.
        "cookie_file_configured":configured_cookie_file().is_some(),
        "cookie_file_exists":configured_cookie_file().is_some_and(|p|p.is_file()),
        "home_scan_enabled":stored.get("home_scan_enabled").and_then(Value::as_bool).unwrap_or(false),
        "home_scan_interval_minutes":stored.get("home_scan_interval_minutes").and_then(Value::as_i64).unwrap_or(60).clamp(1,1440),
        "home_scan_limit":stored.get("home_scan_limit").and_then(Value::as_i64).unwrap_or(10).clamp(1,100),
        "home_scan_long_videos_only":stored.get("home_scan_long_videos_only").and_then(Value::as_bool).unwrap_or(false),
        "home_scan_min_duration_seconds":stored.get("home_scan_min_duration_seconds").and_then(Value::as_i64).unwrap_or(180).clamp(0,86400),
        "home_scan_running":stored.get("home_scan_lock_until").and_then(Value::as_str)
            .and_then(|v|chrono::DateTime::parse_from_rfc3339(v).ok())
            .is_some_and(|dt|dt>Utc::now()),
        "home_scan_last_log_lines":[],"home_scan_last_sample_urls":[]
    });
    result
        .as_object_mut()
        .unwrap()
        .extend(legacy_home_scan_history(&state.db).await?);
    if let Some(raw) = cookies {
        if let (Some(to), Some(from)) = (result.as_object_mut(), summary(&raw).as_object()) {
            for (key, value) in from {
                to.insert(key.clone(), value.clone());
            }
        }
    } else {
        result
            .as_object_mut()
            .unwrap()
            .extend(summary("").as_object().unwrap().clone());
    }
    Ok(result)
}
async fn read(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(snapshot(&state).await?))
}
async fn update(State(state): State<AppState>, Json(patch): Json<Value>) -> AppResult<Json<Value>> {
    let updates = patch
        .as_object()
        .ok_or_else(|| AppError::Invalid("YouTube settings must be a JSON object".into()))?;
    if updates.len() > 12 {
        return Err(AppError::Invalid("too many YouTube setting fields".into()));
    }
    for key in updates.keys() {
        if !matches!(
            key.as_str(),
            "proxy"
                | "cookies_txt"
                | "cookies_enabled"
                | "parallel_download_enabled"
                | "parallel_download_connections"
                | "compatibility_mode_enabled"
                | "home_scan_enabled"
                | "home_scan_interval_minutes"
                | "home_scan_limit"
                | "home_scan_long_videos_only"
                | "home_scan_min_duration_seconds"
        ) {
            return Err(AppError::Invalid(format!(
                "YouTube setting {key} is not migrated yet"
            )));
        }
    }
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('vr2.youtube.settings',0))")
        .execute(&mut *tx)
        .await?;
    // Read under the same transaction that acquired the settings lock.
    // Otherwise a concurrently starting Home scan can lose its lock owner
    // when this update overwrites stale JSON from a separate pool connection.
    let old: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(SETTING_KEY)
            .fetch_optional(&mut *tx)
            .await?;
    let old = old.unwrap_or_else(|| json!({}));
    let mut merged = old.as_object().cloned().unwrap_or_default();
    let old_cookie = if updates.get("cookies_txt").is_some_and(Value::is_string) {
        None
    } else {
        effective_cookie(&state.config, &old).await?
    };
    let mut next_cookie = None;
    for (key, value) in updates {
        if value.is_null() {
            continue;
        }
        match key.as_str() {
            "proxy" => {
                let input = value
                    .as_str()
                    .ok_or_else(|| AppError::Invalid("proxy must be a string".into()))?;
                merged.insert(key.clone(), json!(validate_proxy(input)?.unwrap_or_default()));
            }
            "cookies_txt" => {
                let input = value
                    .as_str()
                    .ok_or_else(|| AppError::Invalid("cookies_txt must be a string".into()))?;
                merged.remove("cookies_txt_enc");
                if input.trim().is_empty() {
                    merged.insert("cookies_enabled".into(), json!(false));
                    merged.remove("cookies_updated_at");
                    next_cookie = Some(None);
                } else {
                    let verified = validate_cookies(input)?;
                    merged.insert("cookies_updated_at".into(), json!(Utc::now().to_rfc3339()));
                    if !updates.contains_key("cookies_enabled") {
                        merged.insert("cookies_enabled".into(), json!(true));
                    }
                    next_cookie = Some(Some(verified));
                }
            }
            "cookies_enabled"
            | "parallel_download_enabled"
            | "compatibility_mode_enabled"
            | "home_scan_enabled"
            | "home_scan_long_videos_only" => {
                let value = value
                    .as_bool()
                    .ok_or_else(|| AppError::Invalid(format!("{key} must be boolean")))?;
                merged.insert(key.clone(), json!(value));
            }
            "parallel_download_connections" => {
                let n = value.as_u64().ok_or_else(|| {
                    AppError::Invalid("parallel connections must be integer".into())
                })?;
                if !(1..=16).contains(&n) {
                    return Err(AppError::Invalid(
                        "parallel connections must be 1-16".into(),
                    ));
                }
                merged.insert(key.clone(), json!(n));
            }
            "home_scan_interval_minutes" | "home_scan_limit" | "home_scan_min_duration_seconds" => {
                let n = value
                    .as_i64()
                    .ok_or_else(|| AppError::Invalid(format!("{key} must be integer")))?;
                let (minimum, maximum) = match key.as_str() {
                    "home_scan_interval_minutes" => (1, 1440),
                    "home_scan_limit" => (1, 100),
                    "home_scan_min_duration_seconds" => (0, 86400),
                    _ => unreachable!(),
                };
                merged.insert(key.clone(), json!(n.clamp(minimum, maximum)));
            }
            _ => unreachable!(),
        }
    }
    let effective = next_cookie.as_ref().cloned().unwrap_or(old_cookie);
    // Original update validates explicit enablement, not unrelated settings reads.
    if updates.get("cookies_enabled").and_then(Value::as_bool)==Some(true)
        && effective.as_deref().is_none_or(|s|s.trim().is_empty()) {
        return Err(AppError::Invalid("cookies_txt is not set; paste cookies.txt first".into()));
    }
    runtime_from_json(&Value::Object(merged.clone()), effective)?;
    // Preserve the original settings field and Python database representation.
    // Replacement, enablement and timestamp commit in the same transaction.
    if let Some(Some(raw)) = next_cookie.as_ref() {
        merged.insert("cookies_txt_enc".into(), json!(crate::secret_compat::encode(raw)));
    } else if next_cookie.is_none() && updates.get("cookies_enabled").and_then(Value::as_bool)==Some(true) {
        if let Some(raw)=effective_cookie(&state.config, &old).await? {
            merged.insert("cookies_txt_enc".into(), json!(crate::secret_compat::encode(&raw)));
        }
    }
    sqlx::query(
        "INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=$2,version=app_settings.version+1,updated_at=now()"
    ).bind(SETTING_KEY).bind(Value::Object(merged)).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(snapshot(&state).await?))
}
async fn test_proxy(
    State(state): State<AppState>,
    Json(payload): Json<Value>,
) -> AppResult<Json<Value>> {
    let default_url = "https://www.youtube.com/robots.txt";
    let url = payload
        .get("url")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(default_url);
    let parsed = Url::parse(url).map_err(|_| AppError::Invalid("invalid proxy test URL".into()))?;
    if parsed.scheme() != "https"
        || !matches!(
            parsed.host_str(),
            Some("www.youtube.com" | "youtube.com" | "www.google.com")
        )
        || parsed.username() != ""
        || parsed.password().is_some()
        || parsed.port().is_some()
    {
        return Err(AppError::Invalid(
            "proxy test must target official YouTube/Google HTTPS host".into(),
        ));
    }
    let settings = runtime(&state.db, &state.config).await?;
    let proxy = if let Some(raw) = payload.get("proxy") {
        validate_proxy(
            raw.as_str()
                .ok_or_else(|| AppError::Invalid("proxy must be a string".into()))?,
        )?
    } else {
        settings.proxy
    };
    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(10));
    if let Some(ref server) = proxy {
        builder = builder.proxy(reqwest::Proxy::all(server)?);
    }
    let client = builder.build()?;
    let now = Instant::now();
    let outcome = client.get(parsed).send().await;
    let elapsed = now.elapsed().as_millis().min(u128::from(u32::MAX)) as u32;
    let response = match outcome {
        Ok(reply) => json!({"ok":reply.status().is_success(),"url":url,
            "used_proxy":proxy,"status_code":reply.status().as_u16(),
            "elapsed_ms":elapsed,"error":null}),
        Err(_) => json!({"ok":false,"url":url,"used_proxy":proxy,
            "status_code":null,"elapsed_ms":elapsed,
            "error":"proxy connection test failed (details withheld to avoid credentials exposure)"}),
    };
    Ok(Json(response))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/settings/youtube", get(read).put(update))
        .route("/settings/youtube/test", axum::routing::post(test_proxy))
        .route(
            "/settings/youtube/home_scan/run",
            axum::routing::post(crate::youtube_home_run_v1::run),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cookie_file_must_be_netscape_and_restricted() {
        let correct =
            "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1788912012\tSID\tsecret";
        let cleaned = validate_cookies(correct).unwrap();
        assert!(cleaned.starts_with("# Netscape"));
        assert!(cleaned.contains("SID\tsecret"));
        assert!(validate_cookies(".example.test\tTRUE\t/\tTRUE\t1788912012\tSID\tfixture").is_ok());
        assert!(validate_cookies("youtube.com true / TRUE 0 SID fixture").unwrap().contains(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture"));
        assert!(validate_cookies("Cookie: SID=secret").is_err());
        assert!(validate_cookies("not-a-cookie").is_err());
    }
    #[test]
    fn explicit_empty_proxy_and_cookie_summary_match_original() {
        assert!(runtime_from_json(&json!({"proxy":null}),None).unwrap().proxy.is_none());
        assert!(runtime_from_json(&json!({"proxy":""}),None).unwrap().proxy.is_none());
        let result=summary("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tfixture");
        assert_eq!(result["cookies_has_auth"],true);
    }
    #[test]
    fn original_cookie_file_precedence() {
        let dir=tempfile::tempdir().unwrap();
        let path=dir.path().join("fixture.txt");
        assert_eq!(select_cookie_file(Some(path.clone()),true),None);
        assert_eq!(select_cookie_file(Some(path.clone()),false),Some(path.clone()));
        std::fs::write(&path,"fixture").unwrap();
        assert_eq!(select_cookie_file(Some(path.clone()),true),Some(path));
        let s=summary(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture\nyoutube.com\tFALSE\t/\tTRUE\t0\tHSID\tfixture");
        assert_eq!(s["cookies_domains_count"],1);
    }
    #[test]
    fn reject_proxy_credentials_and_arbitrary_schemes() {
        assert!(validate_proxy("http://127.0.0.1:7890").unwrap().is_some());
        assert!(validate_proxy("socks5://10.0.0.1:1080").unwrap().is_some());
        assert!(validate_proxy("http://a:password@127.0.0.1:7890").is_err());
        assert!(validate_proxy("file:///etc/shadow").is_err());
    }
}
