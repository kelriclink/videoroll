//! Original Bilibili account and read-only publisher contracts.
//! Reuse the Python app_settings / publish_jobs rows without a second store.
//! Only an explicitly isolated loopback test endpoint may receive test Cookies.
use crate::{AppError, AppResult, Config, Database, api::AppState, providers::decode_secret};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use chrono::{DateTime, Utc};
use reqwest::{Client, Url, header};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::Row;
use std::time::Duration;
use uuid::Uuid;

const AUTH_KEY: &str = "bilibili.auth";
const LIMIT: usize = 16 * 1024;

async fn stored(db: &Database) -> AppResult<Value> {
    Ok(
        sqlx::query_scalar::<_, Value>("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(AUTH_KEY)
            .fetch_optional(&db.pool)
            .await?
            .unwrap_or_else(|| json!({})),
    )
}
fn secret(record: &Value, field: &str) -> AppResult<Option<String>> {
    record
        .get(field)
        .and_then(Value::as_str)
        .map(decode_secret)
        .transpose()
        .map(|v| v.flatten())
}
fn cookie_parts(value: &str) -> std::collections::HashMap<String, String> {
    value
        .split(';')
        .filter_map(|part| {
            let (name, val) = part.trim().split_once('=')?;
            let name = name.trim();
            if name.is_empty() {
                None
            } else {
                Some((name.to_owned(), val.trim().to_owned()))
            }
        })
        .collect()
}
fn normalize_cookie(value: &str) -> AppResult<String> {
    let cleaned = value.replace(['\r', '\n'], " ");
    let cleaned = cleaned.trim();
    let cleaned = if cleaned
        .get(..7)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("cookie:"))
    {
        cleaned[7..].trim()
    } else {
        cleaned
    };
    if cleaned.len() > LIMIT || cleaned.chars().any(|c| c.is_control()) {
        return Err(AppError::Invalid("invalid Bilibili Cookie header".into()));
    }
    Ok(cleaned.into())
}
fn auth_view(record: &Value) -> AppResult<Value> {
    let cookie = secret(record, "cookie_enc")?.unwrap_or_default();
    let parsed = cookie_parts(&cookie);
    let sessdata = match parsed.get("SESSDATA").filter(|v| !v.is_empty()) {
        Some(v) => Some(v.clone()),
        None => secret(record, "sessdata_enc")?,
    };
    let jct = match parsed.get("bili_jct").filter(|v| !v.is_empty()) {
        Some(v) => Some(v.clone()),
        None => secret(record, "bili_jct_enc")?,
    };
    Ok(json!({"cookie_set":!cookie.is_empty(),
        "sessdata_set":sessdata.as_deref().is_some_and(|v|!v.is_empty()),
        "bili_jct_set":jct.as_deref().is_some_and(|v|!v.is_empty())}))
}
fn cookie_header(record: &Value) -> AppResult<String> {
    if let Some(cookie) = secret(record, "cookie_enc")?.filter(|v| !v.is_empty()) {
        return normalize_cookie(&cookie);
    }
    let mut parts = vec![];
    if let Some(v) = secret(record, "sessdata_enc")?.filter(|v| !v.is_empty()) {
        parts.push(format!("SESSDATA={v}"));
    }
    if let Some(v) = secret(record, "bili_jct_enc")?.filter(|v| !v.is_empty()) {
        parts.push(format!("bili_jct={v}"));
    }
    normalize_cookie(&parts.join("; "))
}
/// Use original Python Bilibili credentials as the runtime source of truth.
/// Explicit startup-only secrets are a fallback for older isolated deployments.
fn credential_values(
    record: &Value,
    fallback_cookie: Option<&str>,
    fallback_csrf: Option<&str>,
) -> AppResult<(String, String)> {
    let saved = cookie_header(record)?;
    let cookie = if saved.is_empty() {
        fallback_cookie.unwrap_or("").trim().to_owned()
    } else {
        saved
    };
    if cookie.is_empty() {
        return Err(AppError::Unavailable(
            "Bilibili Cookie is not configured".into(),
        ));
    }
    let parsed = cookie_parts(&cookie);
    let saved_csrf = match parsed.get("bili_jct").filter(|v| !v.is_empty()) {
        Some(v) => Some(v.clone()),
        None => secret(record, "bili_jct_enc")?,
    };
    let csrf = saved_csrf
        .or_else(|| fallback_csrf.map(str::to_owned))
        .unwrap_or_default();
    if !(12..=80).contains(&csrf.len()) || !csrf.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err(AppError::Invalid("Bilibili CSRF token invalid".into()));
    }
    Ok((cookie, csrf))
}
pub(crate) async fn publication_credentials(
    db: &Database,
    config: &Config,
) -> AppResult<(String, String)> {
    let row = stored(db).await?;
    credential_values(
        &row,
        config.native_bilibili_cookie.as_deref(),
        config.native_bilibili_csrf.as_deref(),
    )
}
fn safe_secret(v: &str) -> AppResult<String> {
    let v = v.trim();
    if v.len() > LIMIT || v.chars().any(|c| c.is_control()) {
        return Err(AppError::Invalid("invalid Bilibili secret".into()));
    }
    Ok(v.into())
}
fn update_record(input: Value, payload: &AuthUpdate) -> AppResult<Value> {
    let mut value = input.as_object().cloned().unwrap_or_default();
    if let Some(cookie) = payload.cookie.as_deref() {
        let cookie = normalize_cookie(cookie)?;
        if cookie.is_empty() {
            for key in ["cookie_enc", "sessdata_enc", "bili_jct_enc"] {
                value.remove(key);
            }
        } else {
            value.insert("cookie_enc".into(), json!(format!("dbplain:v1:{cookie}")));
            let parsed = cookie_parts(&cookie);
            for (key, part) in [("sessdata_enc", "SESSDATA"), ("bili_jct_enc", "bili_jct")] {
                if let Some(token) = parsed.get(part).filter(|v| !v.is_empty()) {
                    value.insert(key.into(), json!(format!("dbplain:v1:{token}")));
                }
            }
        }
    }
    for (key, input) in [
        ("sessdata_enc", payload.sessdata.as_deref()),
        ("bili_jct_enc", payload.bili_jct.as_deref()),
    ] {
        if let Some(v) = input {
            let v = safe_secret(v)?;
            if v.is_empty() {
                value.remove(key);
            } else {
                value.insert(key.into(), json!(format!("dbplain:v1:{v}")));
            }
        }
    }
    Ok(Value::Object(value))
}
#[derive(Debug, Deserialize, Default)]
struct AuthUpdate {
    cookie: Option<String>,
    sessdata: Option<String>,
    bili_jct: Option<String>,
}
async fn get_auth(State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(auth_view(&stored(&state.db).await?)?))
}
async fn put_auth(
    State(state): State<AppState>,
    Json(payload): Json<AuthUpdate>,
) -> AppResult<Json<Value>> {
    let mut tx = state.db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('app.settings:bilibili.auth',0))")
        .execute(&mut *tx)
        .await?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES($1,'{}'::jsonb,1) ON CONFLICT(key) DO NOTHING")
        .bind(AUTH_KEY).execute(&mut *tx).await?;
    let existing: Value =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1 FOR UPDATE")
            .bind(AUTH_KEY)
            .fetch_one(&mut *tx)
            .await?;
    let value = update_record(existing, &payload)?;
    // Ensure the new row can still be read before persisting it.
    let flags = auth_view(&value)?;
    sqlx::query(
        "UPDATE app_settings SET value_json=$2,version=version+1,updated_at=now() WHERE key=$1",
    )
    .bind(AUTH_KEY)
    .bind(&value)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(flags))
}
fn api_url(_config: &Config, host: &str, path: &str) -> AppResult<Url> {
    // Test-only override, deliberately disconnected from the UPOS upload base.
    if std::env::var("VIDEOROLL_V2_ALLOW_TEST_BILIBILI_LOOPBACK")
        .ok()
        .as_deref()
        == Some("true")
        && let Ok(base) = std::env::var("VIDEOROLL_V2_TEST_BILIBILI_BASE")
    {
        let root = Url::parse(&base)
            .map_err(|_| AppError::Invalid("invalid Bilibili test origin".into()))?;
        if root.scheme() == "http"
            && matches!(root.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
            && root.path() == "/"
            && root.username().is_empty()
            && root.password().is_none()
            && root.query().is_none()
            && root.fragment().is_none()
        {
            return root
                .join(path.trim_start_matches('/'))
                .map_err(|_| AppError::Invalid("invalid Bilibili test path".into()));
        }
        return Err(AppError::Invalid(
            "Bilibili test origin must be loopback HTTP".into(),
        ));
    }
    Url::parse(&format!("https://{host}{path}"))
        .map_err(|_| AppError::Invalid("invalid Bilibili official endpoint".into()))
}
async fn bili_get(state: &AppState, host: &str, path: &str) -> AppResult<Value> {
    let cookie = cookie_header(&stored(&state.db).await?)?;
    if cookie.is_empty() {
        return Err(AppError::Invalid("bilibili cookie is not set".into()));
    }
    let client = Client::builder()
        .timeout(Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()?;
    let url = api_url(&state.config, host, path)?;
    let response = client
        .get(url)
        .header(header::USER_AGENT, "videoroll/0.1")
        .header(header::COOKIE, cookie)
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(AppError::Unavailable(
            "Bilibili metadata service rejected the request".into(),
        ));
    }
    if response
        .content_length()
        .is_some_and(|v| v > 2 * 1024 * 1024)
    {
        return Err(AppError::Unavailable(
            "Bilibili metadata response too large".into(),
        ));
    }
    let bytes = response.bytes().await?;
    if bytes.len() > 2 * 1024 * 1024 {
        return Err(AppError::Unavailable(
            "Bilibili metadata response too large".into(),
        ));
    }
    let parsed: Value = serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Unavailable("Bilibili metadata returned invalid JSON".into()))?;
    Ok(parsed)
}
async fn auth_me(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let data = bili_get(&state, "api.bilibili.com", "/x/member/web/account").await?;
    if data.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(AppError::Unauthorized);
    }
    let row = data
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| AppError::Unavailable("Bilibili returned empty user information".into()))?;
    let mid = row
        .get("mid")
        .and_then(Value::as_i64)
        .or_else(|| {
            row.get("mid")
                .and_then(Value::as_str)
                .and_then(|v| v.parse().ok())
        })
        .unwrap_or(0);
    let uname = row
        .get("uname")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if mid <= 0 || uname.is_empty() {
        return Err(AppError::Unavailable(
            "Bilibili returned empty user information".into(),
        ));
    }
    fn optional(row: &Map<String, Value>, field: &str) -> Value {
        match row.get(field) {
            Some(Value::String(v)) if !v.is_empty() => json!(v),
            Some(Value::Number(n)) if n.to_string() != "0" => json!(n.to_string()),
            _ => Value::Null,
        }
    }
    Ok(Json(json!({"mid":mid,"uname":uname,
        "userid":optional(row,"userid"),"sign":optional(row,"sign"),
        "rank":optional(row,"rank")})))
}
fn node(value: &Value, depth: usize) -> AppResult<Value> {
    if depth > 8 {
        return Err(AppError::Unavailable(
            "invalid Bilibili partition depth".into(),
        ));
    }
    let mut out = value
        .as_object()
        .cloned()
        .ok_or_else(|| AppError::Unavailable("invalid Bilibili partition node".into()))?;
    if !out.get("id").is_some_and(Value::is_i64) || !out.get("name").is_some_and(Value::is_string) {
        return Err(AppError::Unavailable(
            "Bilibili partition id/name is invalid".into(),
        ));
    }
    let children = out.remove("children").unwrap_or_else(|| json!([]));
    let children = children
        .as_array()
        .ok_or_else(|| AppError::Unavailable("Bilibili partition children invalid".into()))?;
    out.insert(
        "children".into(),
        Value::Array(
            children
                .iter()
                .map(|v| node(v, depth + 1))
                .collect::<AppResult<Vec<_>>>()?,
        ),
    );
    Ok(Value::Object(out))
}
async fn archive_types(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let ts = Utc::now().timestamp_millis();
    let payload = bili_get(
        &state,
        "member.bilibili.com",
        &format!("/x/vupre/web/archive/pre?ts={ts}"),
    )
    .await?;
    if payload.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(AppError::Unavailable(
            "Bilibili partition service returned an error".into(),
        ));
    }
    let source = payload
        .get("data")
        .and_then(|x| x.get("typelist"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let items = source
        .iter()
        .map(|v| node(v, 0))
        .collect::<AppResult<Vec<_>>>()?;
    Ok(Json(json!({"typelist":items})))
}
async fn publish_job(
    State(state): State<AppState>,
    Path(job): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let found=sqlx::query("SELECT id,task_id,state::text AS state,aid,bvid,created_at,updated_at FROM publish_jobs WHERE id=$1")
        .bind(job).fetch_optional(&state.db.pool).await?
        .ok_or_else(||AppError::NotFound("job not found".into()))?;
    let created_at: DateTime<Utc> = found.try_get("created_at")?;
    let updated_at: DateTime<Utc> = found.try_get("updated_at")?;
    Ok(Json(json!({"id":found.try_get::<Uuid,_>("id")?,
        "task_id":found.try_get::<Uuid,_>("task_id")?,
        "state":found.try_get::<String,_>("state")?,
        "aid":found.try_get::<Option<String>,_>("aid")?,
        "bvid":found.try_get::<Option<String>,_>("bvid")?,
        "created_at":created_at,"updated_at":updated_at})))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/bilibili/auth/settings", get(get_auth).put(put_auth))
        .route("/bilibili/auth/me", get(auth_me))
        .route("/bilibili/archive/types", get(archive_types))
        .route("/bilibili/publish/jobs/{job_id}", get(publish_job))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn previous_auth_field_semantics_and_cookie_normalization() {
        let row = update_record(
            json!({"unknown":"kept"}),
            &AuthUpdate {
                cookie: Some("Cookie: SESSDATA=abc; bili_jct=xyz".into()),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(
            auth_view(&row).unwrap(),
            json!({"cookie_set":true,"sessdata_set":true,"bili_jct_set":true})
        );
        assert_eq!(cookie_header(&row).unwrap(), "SESSDATA=abc; bili_jct=xyz");
        assert_eq!(row["unknown"], "kept");
        let cleared = update_record(
            row,
            &AuthUpdate {
                cookie: Some("".into()),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(
            auth_view(&cleared).unwrap(),
            json!({"cookie_set":false,"sessdata_set":false,"bili_jct_set":false})
        );
        assert!(normalize_cookie("hi\nInjected: header").is_ok());
        assert!(safe_secret("abc\nheader").is_err());
    }
    #[test]
    fn publication_uses_original_database_credentials_before_environment() {
        let record = json!({"cookie_enc":"dbplain:v1:SESSDATA=original; bili_jct=1234567890ab",
            "bili_jct_enc":"dbplain:v1:old_should_not_win"});
        let (cookie, csrf) =
            credential_values(&record, Some("SESSDATA=fallback"), Some("fallbacktoken123"))
                .unwrap();
        assert_eq!(cookie, "SESSDATA=original; bili_jct=1234567890ab");
        assert_eq!(csrf, "1234567890ab");
        let (cookie, csrf) =
            credential_values(&json!({}), Some("SESSDATA=old"), Some("1234567890ab")).unwrap();
        assert_eq!(cookie, "SESSDATA=old");
        assert_eq!(csrf, "1234567890ab");
        assert!(credential_values(&json!({}), None, None).is_err());
        assert!(
            credential_values(
                &json!({"cookie_enc":"gAAAAlegacy"}),
                Some("SESSDATA=fallback"),
                Some("1234567890ab")
            )
            .is_err()
        );
    }
    #[test]
    fn partition_nodes_preserve_original_extra_fields() {
        let v = node(&json!({"id":17,"name":"科技","new":true}), 0).unwrap();
        assert_eq!(v["children"], json!([]));
        assert_eq!(v["new"], true);
        assert!(node(&json!({"name":"bad"}), 0).is_err());
    }
}
