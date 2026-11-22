//! Native V1 YouTube settings facade.
//! Cookies are ONLY stored as owner-readable Netscape files, never DB rows,
//! JSON responses, logs, or V2 assets. Legacy encrypted v1 cookies are NOT
//! silently imported or decrypted; users must explicitly import into Rust.
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
use uuid::Uuid;

const SETTING_KEY: &str = "youtube.settings";
const MAX_COOKIES: usize = 200_000;
const MAX_PROXY: usize = 512;
#[derive(Debug, Clone)]
pub struct YoutubeRuntime {
    pub proxy: Option<String>,
    pub cookie_file: Option<PathBuf>,
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
    let s = raw
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    if s.len() > MAX_COOKIES {
        return Err(AppError::Invalid("cookies.txt exceeds 200KB".into()));
    }
    let mut lines = Vec::new();
    let mut count = 0;
    for line in s.lines() {
        if line.trim().is_empty() {
            continue;
        }
        if line.starts_with('#') && !line.starts_with("#HttpOnly_") {
            if !line.chars().any(|c| c == '\0') {
                lines.push(line.to_owned());
            }
            continue;
        }
        let parts = line.split('\t').collect::<Vec<_>>();
        if parts.len() != 7 || parts.iter().any(|v| v.chars().any(|c| c.is_control())) {
            return Err(AppError::Invalid(
                "cookies.txt must use Netscape tab-delimited fields".into(),
            ));
        }
        let domain = parts[0]
            .trim_start_matches("#HttpOnly_")
            .trim_start_matches('.')
            .to_ascii_lowercase();
        if !trusted_domain(&domain)
            || !matches!(parts[1], "TRUE" | "FALSE")
            || !parts[2].starts_with('/')
            || !matches!(parts[3], "TRUE" | "FALSE")
            || parts[4].parse::<i64>().is_err()
            || parts[5].is_empty()
        {
            return Err(AppError::Invalid(
                "cookie domain/fields invalid or not YouTube/Google".into(),
            ));
        }
        lines.push(line.to_owned());
        count += 1;
    }
    if count == 0 {
        return Err(AppError::Invalid(
            "cookies.txt has no Netscape cookie records".into(),
        ));
    }
    Ok(format!(
        "# Netscape HTTP Cookie File\n{}\n",
        lines.join("\n")
    ))
}
pub async fn prepare_cookie_copy(
    config: &Config,
    settings: &YoutubeRuntime,
) -> AppResult<Option<tempfile::TempDir>> {
    let Some(path) = settings.cookie_file.as_deref() else {
        return Ok(None);
    };
    let dir = config.storage_root.join(".videoroll-private");
    let temp = tempfile::Builder::new()
        .prefix("yt-worker-")
        .tempdir_in(&dir)?;
    let bytes = fs::read(path).await?;
    if bytes.len() > MAX_COOKIES + 10_000 {
        return Err(AppError::Invalid("cookie source file oversized".into()));
    }
    let parsed = std::str::from_utf8(&bytes)
        .map_err(|_| AppError::Invalid("cookie source is not UTF8".into()))?;
    let validated = validate_cookies(parsed)?;
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
    Ok(Some(temp))
}

fn cookie_path(config: &Config) -> PathBuf {
    config
        .storage_root
        .join(".videoroll-private")
        .join("youtube-cookies.txt")
}
async fn read_cookie(config: &Config) -> AppResult<Option<String>> {
    let file = cookie_path(config);
    let dir = config.storage_root.join(".videoroll-private");
    match fs::symlink_metadata(&dir).await {
        Ok(m) if m.is_dir() && !m.file_type().is_symlink() => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if m.permissions().mode() & 0o077 != 0 {
                    return Err(AppError::Invalid(
                        "YouTube cookie directory permissions must be 0700".into(),
                    ));
                }
            }
        }
        Ok(_) => {
            return Err(AppError::Invalid(
                "YouTube cookie directory is not a safe directory".into(),
            ));
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    match fs::symlink_metadata(&file).await {
        Ok(metadata) => {
            if !metadata.is_file()
                || metadata.file_type().is_symlink()
                || metadata.len() > MAX_COOKIES as u64 + 10_000
            {
                return Err(AppError::Invalid(
                    "V2 cookies file is not a bounded regular file".into(),
                ));
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if metadata.permissions().mode() & 0o077 != 0 {
                    return Err(AppError::Invalid(
                        "YouTube cookie file permissions must be 0600".into(),
                    ));
                }
            }
            let bytes = fs::read(&file).await?;
            let value = String::from_utf8(bytes)
                .map_err(|_| AppError::Invalid("invalid cookie file UTF8".into()))?;
            Ok(Some(validate_cookies(&value)?))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}
#[cfg(unix)]
async fn save_cookie(config: &Config, value: Option<&str>) -> AppResult<()> {
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
                .await?;
        }
        Err(err) => return Err(err.into()),
    }
    let path = cookie_path(config);
    if let Some(raw) = value {
        let temp = dir.join(format!(".youtube-{}.tmp", Uuid::new_v4()));
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&temp)
            .await?;
        use tokio::io::AsyncWriteExt;
        let result = async {
            file.write_all(raw.as_bytes()).await?;
            file.sync_all().await?;
            Ok::<_, std::io::Error>(())
        }
        .await;
        drop(file);
        if let Err(err) = result {
            let _ = fs::remove_file(&temp).await;
            return Err(err.into());
        }
        if let Err(err) = fs::rename(&temp, &path).await {
            let _ = fs::remove_file(&temp).await;
            return Err(err.into());
        }
    } else {
        match fs::remove_file(path).await {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}
#[cfg(not(unix))]
async fn save_cookie(_config: &Config, _value: Option<&str>) -> AppResult<()> {
    Err(AppError::Unavailable(
        "secure cookie storage requires Unix file permissions".into(),
    ))
}
async fn read_settings(db: &Database) -> AppResult<Value> {
    // Only the V2 overlay is trusted as mutable configuration. Legacy
    // cookies_txt_enc cannot be decrypted safely without its own key.
    let own: Option<Value> = sqlx::query_scalar("SELECT value_json FROM vr2_settings WHERE key=$1")
        .bind(SETTING_KEY)
        .fetch_optional(&db.pool)
        .await?;
    Ok(own.unwrap_or(json!({})))
}
fn runtime_from_json(value: &Value, cookie_file: Option<PathBuf>) -> AppResult<YoutubeRuntime> {
    let proxy = validate_proxy(value.get("proxy").and_then(Value::as_str).unwrap_or(""))?;
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
        .unwrap_or(false);
    if enabled && cookie_file.is_none() {
        return Err(AppError::Invalid(
            "YouTube cookies enabled but no valid cookies.txt available".into(),
        ));
    }
    Ok(YoutubeRuntime {
        proxy,
        cookie_file: if enabled { cookie_file } else { None },
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
    let cookies = read_cookie(config).await?;
    runtime_from_json(&stored, cookies.map(|_| cookie_path(config)))
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
        domains.insert(p[0].trim_start_matches("#HttpOnly_").to_string());
        auth |= matches!(p[5], "SID" | "SAPISID" | "__Secure-3PSID" | "LOGIN_INFO");
        visitor |= p[5] == "VISITOR_INFO1_LIVE";
        captcha |= p[5] == "GOOGLE_ABUSE_EXEMPTION";
    }
    json!({"cookies_count":count,"cookies_domains_count":domains.len(),
        "cookies_has_auth":auth,"cookies_has_visitor_info":visitor,
        "cookies_has_bot_check_bypass":captcha})
}
async fn snapshot(state: &AppState) -> AppResult<Value> {
    let stored = read_settings(&state.db).await?;
    let cookies = read_cookie(&state.config).await?;
    let runtime = runtime_from_json(
        &stored,
        cookies.as_ref().map(|_| cookie_path(&state.config)),
    )?;
    let mut result = json!({
        "proxy":runtime.proxy.unwrap_or_default(),
        "cookies_set":cookies.is_some(),
        "cookies_enabled":stored.get("cookies_enabled").and_then(Value::as_bool).unwrap_or(false),
        "compatibility_mode_enabled":runtime.compatibility,
        "parallel_download_enabled":runtime.parallel,
        "parallel_download_connections":runtime.connections,
        "cookies_updated_at":stored.get("cookies_updated_at"),
        // This is a private V2-managed file, NOT the legacy YOUTUBE_COOKIE_FILE env option.
        "cookie_file_configured":false,
        "cookie_file_exists":false,
        "home_scan_enabled":false,"home_scan_interval_minutes":60,
        "home_scan_limit":10,"home_scan_long_videos_only":false,
        "home_scan_min_duration_seconds":180,"home_scan_running":false,
        "home_scan_last_log_lines":[],"home_scan_last_sample_urls":[]
    });
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
    if updates.len() > 10 {
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
    let old = read_settings(&state.db).await?;
    let mut merged = old.as_object().cloned().unwrap_or_default();
    let old_cookie = read_cookie(&state.config).await?;
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
                merged.insert(key.clone(), json!(validate_proxy(input)?));
            }
            "cookies_txt" => {
                let input = value
                    .as_str()
                    .ok_or_else(|| AppError::Invalid("cookies_txt must be a string".into()))?;
                if input.trim().is_empty() {
                    merged.insert("cookies_enabled".into(), json!(false));
                    merged.insert("cookies_updated_at".into(), Value::Null);
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
            "cookies_enabled" | "parallel_download_enabled" | "compatibility_mode_enabled" => {
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
            _ => unreachable!(),
        }
    }
    let valid_cookie = next_cookie
        .as_ref()
        .map(|v| v.is_some())
        .unwrap_or(old_cookie.is_some());
    runtime_from_json(
        &Value::Object(merged.clone()),
        if valid_cookie {
            Some(cookie_path(&state.config))
        } else {
            None
        },
    )?;
    // Persist the new private cookie before the DB points to it. Filesystem
    // failure never enables a missing cookie. A DB failure leaves an inert
    // private file, not a published asset.
    if let Some(change) = next_cookie.as_ref() {
        save_cookie(&state.config, change.as_deref()).await?;
    }
    sqlx::query(
        "INSERT INTO vr2_settings(key,value_json,version) VALUES($1,$2,1)
         ON CONFLICT(key) DO UPDATE SET value_json=$2,version=vr2_settings.version+1,updated_at=now()"
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
        assert!(validate_cookies(".evil.test\tTRUE\t/\tTRUE\t1788912012\tSID\tsecret").is_err());
        assert!(validate_cookies("Cookie: SID=secret").is_err());
        assert!(validate_cookies("not-a-cookie").is_err());
    }
    #[test]
    fn reject_proxy_credentials_and_arbitrary_schemes() {
        assert!(validate_proxy("http://127.0.0.1:7890").unwrap().is_some());
        assert!(validate_proxy("socks5://10.0.0.1:1080").unwrap().is_some());
        assert!(validate_proxy("http://a:password@127.0.0.1:7890").is_err());
        assert!(validate_proxy("file:///etc/shadow").is_err());
    }
}
