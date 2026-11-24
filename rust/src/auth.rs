//! Interoperable v1 admin-device cookie verification, native password and login.
use crate::{AppError, AppResult, Config, Database};
use axum::http::{HeaderMap, header};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use pbkdf2::pbkdf2_hmac;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use std::net::SocketAddr;
use subtle::ConstantTimeEq;
use uuid::Uuid;

type Hmac256 = Hmac<Sha256>;
const COOKIE_NAME: &str = "videoroll_admin_device";
const MAX_AGE: i64 = 180 * 24 * 3600;
const MAX_FAILED_ATTEMPTS: i32 = 8;

fn mac(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut h = Hmac256::new_from_slice(key).expect("HMAC key");
    h.update(data);
    h.finalize().into_bytes().to_vec()
}
pub fn cookie_secret(internal_api_secret: &str) -> String {
    format!(
        "v1.{}",
        hex::encode(mac(
            internal_api_secret.as_bytes(),
            b"videoroll-admin-cookie:v1"
        ))
    )
}
fn signing_key(cookie_secret: &str, password_hash: &str) -> [u8; 32] {
    let text = format!("videoroll-admin-cookie-key:v1:{cookie_secret}:{password_hash}");
    Sha256::digest(text.as_bytes()).into()
}
pub fn verify_cookie_value(
    value: &str,
    internal_api_secret: &str,
    password_hash: &str,
    now: i64,
) -> bool {
    if password_hash.trim().is_empty() || internal_api_secret.trim().is_empty() {
        return false;
    }
    let parts: Vec<_> = value.trim().split('.').collect();
    if parts.len() != 3 || parts.iter().any(|s| s.is_empty()) {
        return false;
    }
    let Ok(expires) = parts[1].parse::<i64>() else {
        return false;
    };
    if now >= expires {
        return false;
    }
    let Ok(received) = URL_SAFE_NO_PAD.decode(parts[2]) else {
        return false;
    };
    let secret = cookie_secret(internal_api_secret);
    let key = signing_key(&secret, password_hash);
    let message = format!("{}.{}", parts[0], expires);
    let signed = mac(&key, message.as_bytes());
    bool::from(signed.ct_eq(&received))
}
pub fn mint_cookie(internal_api_secret: &str, password_hash: &str, now: i64) -> String {
    let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let expires = now + MAX_AGE;
    let message = format!("{token}.{expires}");
    let key = signing_key(&cookie_secret(internal_api_secret), password_hash);
    format!(
        "{message}.{}",
        URL_SAFE_NO_PAD.encode(mac(&key, message.as_bytes()))
    )
}
pub fn verify_password(password: &str, encoded: &str) -> bool {
    if password.is_empty() {
        return false;
    }
    let parts: Vec<_> = encoded.split('$').collect();
    if parts.len() != 4 || parts[0] != "pbkdf2_sha256" {
        return false;
    }
    let Ok(iterations) = parts[1].parse::<u32>() else {
        return false;
    };
    if !(1..=2_000_000).contains(&iterations) {
        return false;
    }
    let (Ok(salt), Ok(expected)) = (
        URL_SAFE_NO_PAD.decode(parts[2]),
        URL_SAFE_NO_PAD.decode(parts[3]),
    ) else {
        return false;
    };
    if salt.is_empty() || expected.len() != 32 {
        return false;
    }
    let mut actual = [0u8; 32];
    pbkdf2_hmac::<Sha256>(password.as_bytes(), &salt, iterations, &mut actual);
    bool::from(actual.as_slice().ct_eq(&expected))
}
pub fn encode_password(password: &str) -> AppResult<String> {
    if !(8..=128).contains(&password.chars().count()) {
        return Err(AppError::Invalid(
            "password must contain 8–128 characters".into(),
        ));
    }
    let salt = Uuid::new_v4().into_bytes();
    let mut digest = [0u8; 32];
    pbkdf2_hmac::<Sha256>(password.as_bytes(), &salt, 200_000, &mut digest);
    Ok(format!(
        "pbkdf2_sha256{d}200000{d}{}{d}{}",
        URL_SAFE_NO_PAD.encode(salt),
        URL_SAFE_NO_PAD.encode(digest),
        d = '$'
    ))
}
pub(crate) fn cookie_from_headers(headers: &HeaderMap) -> Option<String> {
    let values = headers.get_all(header::COOKIE);
    for v in values.iter() {
        if let Ok(raw) = v.to_str() {
            for pair in raw.split(';') {
                if let Some((name, value)) = pair.trim().split_once('=')
                    && name == COOKIE_NAME
                    && value.len() < 2048
                {
                    return Some(value.trim().to_string());
                }
            }
        }
    }
    None
}
pub async fn password_hash(db: &Database) -> AppResult<String> {
    let row: Option<(Option<String>,)> = sqlx::query_as(
        "SELECT value_json->>'password_hash' FROM app_settings WHERE key='admin.auth'",
    )
    .fetch_optional(&db.pool)
    .await?;
    Ok(row.and_then(|v| v.0).unwrap_or_default())
}
pub async fn request_authenticated(headers: &HeaderMap, db: &Database, config: &Config) -> bool {
    let Some(secret) = config.legacy_internal_secret.as_deref() else {
        return false;
    };
    let Some(cookie) = cookie_from_headers(headers) else {
        return false;
    };
    let Ok(hash) = password_hash(db).await else {
        return false;
    };
    verify_cookie_value(&cookie, secret, &hash, chrono::Utc::now().timestamp())
}
pub async fn auth_status(headers: &HeaderMap, db: &Database, config: &Config) -> AppResult<Value> {
    let password = password_hash(db).await?;
    let trusted = request_authenticated(headers, db, config).await;
    Ok(json!({"password_set":!password.is_empty(),"trusted":trusted}))
}
pub async fn ensure_login_allowed(db: &Database, source: &str) -> AppResult<()> {
    let key = hex::encode(Sha256::digest(source.as_bytes()));
    let row = sqlx::query(
        "SELECT failures,blocked_until,window_started FROM auth_login_limits WHERE source_key=$1",
    )
    .bind(&key)
    .fetch_optional(&db.pool)
    .await?;
    if let Some(row) = row {
        let until: Option<chrono::DateTime<chrono::Utc>> = row.try_get("blocked_until")?;
        if until.is_some_and(|date| date > chrono::Utc::now()) {
            return Err(AppError::RateLimited);
        }
    }
    Ok(())
}
pub async fn record_login_result(db: &Database, source: &str, success: bool) -> AppResult<()> {
    let key = hex::encode(Sha256::digest(source.as_bytes()));
    if success {
        sqlx::query("DELETE FROM auth_login_limits WHERE source_key=$1")
            .bind(key)
            .execute(&db.pool)
            .await?;
        return Ok(());
    }
    sqlx::query(
        r#"INSERT INTO auth_login_limits(source_key,failures,window_started,blocked_until)
           VALUES($1,1,now(),NULL) ON CONFLICT(source_key)
           DO UPDATE SET
              failures=CASE WHEN auth_login_limits.window_started<now()-interval '15 minutes'
                    THEN 1 ELSE auth_login_limits.failures+1 END,
              window_started=CASE WHEN auth_login_limits.window_started<now()-interval '15 minutes'
                    THEN now() ELSE auth_login_limits.window_started END,
              blocked_until=CASE WHEN auth_login_limits.window_started>=now()-interval '15 minutes'
                AND auth_login_limits.failures+1>=$2 THEN now()+interval '10 minutes'
                ELSE auth_login_limits.blocked_until END"#,
    )
    .bind(key)
    .bind(MAX_FAILED_ATTEMPTS)
    .execute(&db.pool)
    .await?;
    Ok(())
}
pub fn caller_identity(headers: &HeaderMap, addr: Option<SocketAddr>) -> String {
    // Only accept a proxy-supplied real IP when the immediate peer is loopback.
    match addr {
        Some(addr) if addr.ip().is_loopback() => headers
            .get("x-real-ip")
            .and_then(|v| v.to_str().ok())
            .filter(|v| v.parse::<std::net::IpAddr>().is_ok())
            .map(str::to_string)
            .unwrap_or_else(|| addr.ip().to_string()),
        Some(addr) => addr.ip().to_string(),
        None => "unknown".to_string(),
    }
}
pub fn login_cookie_header(config: &Config, value: &str) -> AppResult<axum::http::HeaderValue> {
    let flag = if config.secure_cookies {
        "Secure; "
    } else {
        ""
    };
    let text =
        format!("{COOKIE_NAME}={value}; Max-Age={MAX_AGE}; Path=/; HttpOnly; SameSite=Lax; {flag}");
    axum::http::HeaderValue::from_str(&text)
        .map_err(|_| AppError::Invalid("invalid auth cookie".into()))
}
pub fn logout_cookie_header(config: &Config) -> axum::http::HeaderValue {
    let flag = if config.secure_cookies {
        "Secure; "
    } else {
        ""
    };
    axum::http::HeaderValue::from_str(&format!(
        "{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; {flag}"
    ))
    .expect("static cookie")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip_admin_cookie_matches_v1_format() {
        let pw = "pbkdf2_sha256$200000$c2FsdA$aGFzaA";
        let s = mint_cookie("test-internal-secret", pw, 1_800_000_000);
        assert!(verify_cookie_value(
            &s,
            "test-internal-secret",
            pw,
            1_800_000_001
        ));
        assert!(!verify_cookie_value(&s, "different", pw, 1_800_000_001));
        assert!(!verify_cookie_value(
            &s,
            "test-internal-secret",
            "different",
            1_800_000_001
        ));
        assert!(!verify_cookie_value(
            &s,
            "test-internal-secret",
            pw,
            2_000_000_000
        ));
    }
    #[test]
    fn pbkdf2_matches_v1_format() {
        let hash = encode_password("example-password").unwrap();
        assert!(verify_password("example-password", &hash));
        assert!(!verify_password("wrong-password", &hash));
        assert!(!verify_password("x", "invalid"));
    }
    #[test]
    fn proxy_header_is_not_trusted_from_remote_clients() {
        let mut headers = HeaderMap::new();
        headers.insert("x-real-ip", "1.2.3.4".parse().unwrap());
        let remote: std::net::SocketAddr = "192.0.2.10:1234".parse().unwrap();
        assert_eq!(caller_identity(&headers, Some(remote)), "192.0.2.10");
        let local: std::net::SocketAddr = "127.0.0.1:1234".parse().unwrap();
        assert_eq!(caller_identity(&headers, Some(local)), "1.2.3.4");
    }
}
