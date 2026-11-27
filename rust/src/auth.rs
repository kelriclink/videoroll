//! Interoperable v1 admin-device cookie verification, native password and login.
use crate::{AppError, AppResult, Config, Database};
use axum::http::{HeaderMap, header};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use pbkdf2::pbkdf2_hmac;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use subtle::ConstantTimeEq;
use uuid::Uuid;

type Hmac256 = Hmac<Sha256>;
const COOKIE_NAME: &str = "videoroll_admin_device";
const MAX_AGE: i64 = 180 * 24 * 3600;
const MAX_FAILED_ATTEMPTS: i32 = 5;
const LOGIN_BURST_SECONDS: i64 = 60;

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
fn lockout_seconds(attempts:i32)->i64 {
    if attempts<MAX_FAILED_ATTEMPTS{return LOGIN_BURST_SECONDS}
    (120i64 << (attempts-MAX_FAILED_ATTEMPTS).clamp(0,16)).min(3600)
}
fn unavailable(error:sqlx::Error)->AppError {
    tracing::warn!(%error,"authentication limiter unavailable");
    AppError::AuthenticationUnavailable
}

/// Reserve an attempt before expensive password verification. PostgreSQL
/// row locking closes the check/increment race without holding a transaction
/// during PBKDF2. An interrupted verification consumes its reserved attempt.
async fn reserve_login_attempt(db:&Database,key:&str)->AppResult<(i32,u64)> {
    let mut tx=db.pool.begin().await.map_err(unavailable)?;
    sqlx::query("INSERT INTO auth_login_limits(source_key,failures) VALUES($1,0)
        ON CONFLICT(source_key) DO NOTHING").bind(&key).execute(&mut *tx).await.map_err(unavailable)?;
    let row=sqlx::query("SELECT failures,blocked_until,window_started FROM auth_login_limits
        WHERE source_key=$1 FOR UPDATE").bind(&key).fetch_one(&mut *tx).await.map_err(unavailable)?;
    let now:chrono::DateTime<chrono::Utc>=sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut *tx).await.map_err(unavailable)?;
    let until:Option<chrono::DateTime<chrono::Utc>>=row.try_get("blocked_until").map_err(unavailable)?;
    let last:chrono::DateTime<chrono::Utc>=row.try_get("window_started").map_err(unavailable)?;
    let previous:i32=row.try_get("failures").map_err(unavailable)?;
    let active=until.map_or(last+chrono::Duration::seconds(LOGIN_BURST_SECONDS)>now,|v|v>now);
    let attempts=if active {previous.max(0).saturating_add(1).min(1_000_000)}else{1};
    let seconds=lockout_seconds(attempts);
    let blocked=(attempts>=MAX_FAILED_ATTEMPTS).then_some(now+chrono::Duration::seconds(seconds));
    sqlx::query("UPDATE auth_login_limits SET failures=$2,window_started=$3,blocked_until=$4 WHERE source_key=$1")
        .bind(&key).bind(attempts).bind(now).bind(blocked).execute(&mut *tx).await.map_err(unavailable)?;
    tx.commit().await.map_err(unavailable)?;
    Ok((attempts,seconds as u64))
}

#[derive(Clone,Copy)]
pub(crate) enum AuthEndpoint { Login, Setup }
impl AuthEndpoint {
    fn name(self)->&'static str { match self { Self::Login=>"login",Self::Setup=>"setup" } }
    fn key(self,source:&str)->String {
        // Retain the deployed Rust login bucket identity across upgrades.
        let input=match self {Self::Login=>source.to_owned(),Self::Setup=>format!("setup:{source}")};
        hex::encode(Sha256::digest(input.as_bytes()))
    }
}
/// Only fixed event codes, normalized source IP and numeric counters enter
/// the original audit table. Never persist request headers or error text.
async fn audit(
    conn:&mut sqlx::PgConnection,endpoint:AuthEndpoint,source:&str,
    event:&str,outcome:&str,code:Option<&str>,payload:Value
)->AppResult<()> {
    sqlx::query("INSERT INTO security_audit_events(id,event_type,actor_type,outcome,source_ip,payload_json,error_code)
        VALUES($1,$2,'admin',$3,$4,$5,$6)")
        .bind(Uuid::new_v4()).bind(format!("admin.{}.{event}",endpoint.name()))
        .bind(outcome).bind(source).bind(payload).bind(code)
        .execute(conn).await.map_err(unavailable)?;
    Ok(())
}
pub(crate) struct AuthAttempt {
    endpoint:AuthEndpoint,
    source:String,
    key:String,
    attempts:i32,
    seconds:u64,
}
impl AuthAttempt {
    pub(crate) async fn begin(db:&Database,endpoint:AuthEndpoint,source:&str)->AppResult<Self> {
        let key=endpoint.key(source);
        let (attempts,seconds)=match reserve_login_attempt(db,&key).await {
            Ok(pair)=>pair,
            Err(error)=>{
                // If the limiter failed but the audit table is reachable,
                // retain the failure without exposing any database error.
                if let Ok(mut conn)=db.pool.acquire().await {
                    let _=audit(&mut conn,endpoint,source,"failure","failure",Some("rate_limiter_unavailable"),json!({})).await;
                }
                return Err(error)
            }
        };
        let attempt=Self{endpoint,source:source.to_owned(),key,attempts,seconds};
        if attempts>MAX_FAILED_ATTEMPTS {
            return Err(attempt.failure(db,"rate_limited",AppError::Unauthorized).await);
        }
        Ok(attempt)
    }
    pub(crate) async fn failure(&self,db:&Database,code:&'static str,error:AppError)->AppError {
        let (event,outcome,code,payload,result)=if self.attempts>=MAX_FAILED_ATTEMPTS {
            ("throttle","throttled","rate_limited",json!({"attempts":self.attempts,"retry_after":self.seconds}),AppError::AuthThrottled{retry_after:self.seconds})
        } else {
            ("failure","failure",code,json!({"attempts":self.attempts}),error)
        };
        let mut conn=match db.pool.acquire().await {Ok(c)=>c,Err(e)=>return unavailable(e)};
        match audit(&mut conn,self.endpoint,&self.source,event,outcome,Some(code),payload).await {
            Ok(())=>result,Err(e)=>e
        }
    }
    pub(crate) async fn succeed(&self,conn:&mut sqlx::PgConnection)->AppResult<()> {
        sqlx::query("DELETE FROM auth_login_limits WHERE source_key=$1")
            .bind(&self.key).execute(&mut *conn).await.map_err(unavailable)?;
        audit(conn,self.endpoint,&self.source,"success","success",None,json!({})).await
    }
}
pub(crate) fn hash_permit()->AppResult<tokio::sync::OwnedSemaphorePermit> {
    static HASH_SLOTS:std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>>=std::sync::OnceLock::new();
    HASH_SLOTS.get_or_init(||std::sync::Arc::new(tokio::sync::Semaphore::new(8)))
        .clone().try_acquire_owned().map_err(|_|AppError::AuthThrottled{retry_after:1})
}

/// Bound CPU work across callers and allow only the first five concurrent
/// attempts per source to reach password verification.
pub async fn authenticate_password(db:&Database,source:&str,password:String,stored:&str)->AppResult<()> {
    let permit=hash_permit()?;
    let attempt=AuthAttempt::begin(db,AuthEndpoint::Login,source).await?;
    if stored.is_empty() {
        return Err(attempt.failure(db,"password_not_set",AppError::Invalid("admin password is not set".into())).await)
    }
    let hash=stored.to_owned();
    let valid=tokio::task::spawn_blocking(move||{
        let _permit=permit;
        password.len()<=1024 && verify_password(&password,&hash)
    }).await.map_err(|_|AppError::AuthenticationUnavailable)?;
    if valid {
        let mut tx=db.pool.begin().await.map_err(unavailable)?;
        attempt.succeed(&mut tx).await?;
        tx.commit().await.map_err(unavailable)?;
        return Ok(());
    }
    Err(attempt.failure(db,"invalid_password",AppError::Unauthorized).await)
}

/// PostgreSQL buckets do not have Redis TTL. Keep a one-hour grace beyond
/// their expiry, and lock only a bounded page of rows that are not in use.
pub async fn collect_expired_login_limits(db:&Database,limit:i64)->AppResult<u64> {
    let result=sqlx::query("WITH due AS (
        SELECT source_key FROM auth_login_limits
        WHERE COALESCE(blocked_until,window_started+interval '60 seconds') < now()-interval '1 hour'
        ORDER BY COALESCE(blocked_until,window_started+interval '60 seconds'),source_key
        LIMIT $1 FOR UPDATE SKIP LOCKED
    ) DELETE FROM auth_login_limits l USING due WHERE l.source_key=due.source_key
        AND COALESCE(l.blocked_until,l.window_started+interval '60 seconds') < now()-interval '1 hour'")
        .bind(limit.clamp(1,1000)).execute(&db.pool).await?;
    Ok(result.rows_affected())
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
}
