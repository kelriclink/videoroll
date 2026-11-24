//! Original Bilibili account and read-only publisher contracts.
//! Reuse the Python app_settings / publish_jobs rows without a second store.
//! Only an explicitly isolated loopback test endpoint may receive test Cookies.
use crate::{AppError, AppResult, Config, Database, api::AppState, providers::decode_secret};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::{get, post},
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
/// Original Account row is the source of truth for non-default Bilibili
/// identities. No default-cookie fallback is permitted for a selected account.
/// A fingerprint of the secret is recorded at enqueue, not the secret itself,
/// so rotation or revocation before dispatch cannot publish with a new user.
pub(crate) async fn selected_publication_credentials(
    db: &Database,
    config: &Config,
    account: Option<Uuid>,
) -> AppResult<(String, String, Option<String>)> {
    let Some(account) = account else {
        let (cookie, csrf) = publication_credentials(db, config).await?;
        return Ok((cookie, csrf, None));
    };
    let row = sqlx::query(
        "SELECT platform::text AS platform,is_active,secrets_encrypted,rotated_at
        FROM accounts WHERE id=$1",
    )
    .bind(account)
    .fetch_optional(&db.pool)
    .await?
    .ok_or_else(|| AppError::Invalid("selected Bilibili account does not exist".into()))?;
    let platform: String = row.try_get("platform")?;
    let active: bool = row.try_get("is_active")?;
    if platform != "bilibili" || !active {
        return Err(AppError::Conflict(
            "selected account is inactive or not a Bilibili account".into(),
        ));
    }
    let encoded: String = row.try_get("secrets_encrypted")?;
    if encoded.is_empty() || encoded.len() > LIMIT {
        return Err(AppError::Conflict(
            "selected Bilibili account has no valid credentials".into(),
        ));
    }
    let decoded = decode_secret(&encoded)?
        .ok_or_else(|| AppError::Conflict("selected account credentials missing".into()))?;
    let value = if decoded.trim_start().starts_with('{') {
        let record: Value = serde_json::from_str(&decoded)
            .map_err(|_| AppError::Invalid("invalid Bilibili account JSON".into()))?;
        let map = record.as_object().ok_or_else(|| {
            AppError::Invalid("Bilibili account credential must be an object".into())
        })?;
        // No browser sidecar storageState JSON, API key, or ambiguous hidden
        // fallback to global credentials: only explicit creator-center Cookie.
        let cookie = map
            .get("cookie_enc")
            .or_else(|| map.get("cookie"))
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::Invalid("Bilibili account Cookie is missing".into()))?;
        let cookie = decode_secret(cookie)?
            .ok_or_else(|| AppError::Invalid("empty Bilibili account Cookie".into()))?;
        let csrf = map
            .get("bili_jct_enc")
            .or_else(|| map.get("bili_jct"))
            .and_then(Value::as_str)
            .map(decode_secret)
            .transpose()?
            .flatten();
        if let Some(csrf) = csrf {
            let embedded = cookie_parts(&cookie).get("bili_jct").cloned();
            if embedded.as_deref().is_some_and(|v| v != csrf) {
                return Err(AppError::Conflict(
                    "Bilibili account CSRF differs from its Cookie".into(),
                ));
            }
            // Explicitly scoped account credentials; never use global fallback.
            json!({"cookie_enc":cookie,"bili_jct_enc":csrf})
        } else {
            json!({"cookie_enc":cookie})
        }
    } else {
        json!({"cookie_enc":decoded})
    };
    let (cookie, csrf) = credential_values(&value, None, None)?;
    if cookie_parts(&cookie)
        .get("SESSDATA")
        .is_none_or(|v| v.is_empty())
        || cookie_parts(&cookie)
            .get("bili_jct")
            .is_none_or(|v| v != &csrf)
    {
        return Err(AppError::Invalid(
            "selected Bilibili account must contain matching SESSDATA and bili_jct Cookies".into(),
        ));
    }
    use sha2::Digest as _;
    let rotated: Option<DateTime<Utc>> = row.try_get("rotated_at")?;
    let mut digest = sha2::Sha256::new();
    digest.update(cookie.as_bytes());
    digest.update(b"\0");
    if let Some(at) = rotated {
        digest.update(at.timestamp_micros().to_be_bytes());
    }
    let fingerprint = format!("{:x}", digest.finalize());
    Ok((cookie, csrf, Some(fingerprint)))
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
    bili_get_from(&state.db, &state.config, host, path).await
}
async fn bili_get_from(db: &Database, config: &Config, host: &str, path: &str) -> AppResult<Value> {
    let cookie = cookie_header(&stored(db).await?)?;
    bili_get_cookie(config, host, path, &cookie).await
}
async fn bili_get_cookie(
    config: &Config,
    host: &str,
    path: &str,
    cookie: &str,
) -> AppResult<Value> {
    if cookie.is_empty() {
        return Err(AppError::Invalid("Bilibili Cookie is missing".into()));
    }
    let client = Client::builder()
        .timeout(Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()?;
    let url = api_url(config, host, path)?;
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
/// Original Bilibili `/archive/type/recommend`: bounded, read-only AI choice
/// over live official partition IDs. The selected ID is NEVER trusted unless
/// it exists in the same fetched partition snapshot.
#[derive(Debug, Deserialize)]
struct TypeRecommendRequest {
    task_id: Option<Uuid>,
    text: Option<String>,
}

fn type_options(nodes: &[Value]) -> Vec<(i64, String)> {
    fn walk(node: &Value, parents: &[String], output: &mut Vec<(i64, String)>, depth: usize) {
        if depth > 12 || output.len() >= 1000 {
            return;
        }
        let Some(map) = node.as_object() else {
            return;
        };
        let name = map.get("name").and_then(Value::as_str).unwrap_or("").trim();
        let mut path = parents.to_vec();
        if !name.is_empty() {
            path.push(name.chars().take(100).collect());
        }
        if let Some(children) = map.get("children").and_then(Value::as_array)
            && !children.is_empty()
        {
            for child in children {
                walk(child, &path, output, depth + 1);
            }
            return;
        }
        let id = map
            .get("id")
            .and_then(Value::as_i64)
            .or_else(|| {
                map.get("id")
                    .and_then(Value::as_str)
                    .and_then(|v| v.parse::<i64>().ok())
            })
            .unwrap_or(0);
        if id > 0 && !name.is_empty() && !output.iter().any(|(seen, _)| *seen == id) {
            output.push((id, path.join("/")));
        }
    }
    let mut output = Vec::new();
    for node in nodes {
        walk(node, &[], &mut output, 0);
    }
    output
}

fn type_choice(model: &Value, options: &[(i64, String)], text: &str) -> Value {
    let id = model
        .get("typeid")
        .and_then(Value::as_i64)
        .or_else(|| {
            model
                .get("typeid")
                .and_then(Value::as_str)
                .and_then(|v| v.parse().ok())
        })
        .unwrap_or(0);
    let reason = model
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    match options.iter().find(|(available, _)| *available == id) {
        Some((_, path)) if id > 0 => json!({
            "ok":true,"typeid":id,"path":path,"reason":reason,"used_text":text
        }),
        _ => json!({"ok":false,"typeid":null,"path":null,
            "reason":"typeid not in candidate list","used_text":text}),
    }
}

async fn recommend_archive_type(
    State(state): State<AppState>,
    Json(payload): Json<TypeRecommendRequest>,
) -> AppResult<Json<Value>> {
    recommend_type_impl(&state.db, &state.config, payload).await
}

// Direct publisher reuses exactly the same source priority, provider selection,
// official partition leaf whitelist, LLM prompt and usage ledger as the V1 API.
pub(crate) async fn recommend_for_publish(
    db: &Database,
    config: &Config,
    task_id: Uuid,
    fallback_text: &str,
) -> AppResult<Value> {
    Ok(recommend_type_impl(
        db,
        config,
        TypeRecommendRequest {
            task_id: Some(task_id),
            text: Some(fallback_text.to_owned()),
        },
    )
    .await?
    .0)
}

async fn recommend_type_impl(
    db: &Database,
    config: &Config,
    payload: TypeRecommendRequest,
) -> AppResult<Json<Value>> {
    // The old service requires a stored Cookie, even though the metadata API
    // might return some information publicly. `bili_get` owns this check.
    if cookie_header(&stored(db).await?)?.is_empty() {
        return Err(AppError::Invalid("bilibili cookie is not set".into()));
    }
    let profile = crate::settings::effective(db, "subtitle.auto_profile").await?;
    let translation = crate::settings::effective(db, "subtitle.translate").await?;
    let provider_name = profile
        .get("translate_provider")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .or_else(|| translation.get("default_provider").and_then(Value::as_str))
        .unwrap_or("openai");
    if provider_name != "openai" {
        return Err(AppError::Invalid(
            "当前自动处理翻译方式不支持 AI 分区推荐".into(),
        ));
    }
    let provider_id = profile
        .get("translate_model_provider_id")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .unwrap_or("legacy");
    let model_id = profile
        .get("translate_model_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let model = crate::providers::resolve(db, config, provider_id, model_id).await?;
    let from_task: Option<String> = match payload.task_id {
        Some(task) => sqlx::query_scalar(
            "SELECT NULLIF(btrim(value_json->>'summary'),'') FROM app_settings WHERE key=$1",
        )
        .bind(format!("bilibili.tags.{task}"))
        .fetch_optional(&db.pool)
        .await?
        .flatten(),
        None => None,
    };
    let text = from_task
        .as_deref()
        .filter(|v| !v.is_empty())
        .or(payload.text.as_deref())
        .unwrap_or("")
        .trim();
    if text.is_empty() {
        return Err(AppError::Invalid(
            "missing text (and no task summary found)".into(),
        ));
    }
    let text = if text.chars().count() > 2000 {
        format!("{}…", text.chars().take(1999).collect::<String>())
    } else {
        text.to_owned()
    };
    let ts = Utc::now().timestamp_millis();
    let bili_payload = bili_get_from(
        db,
        config,
        "member.bilibili.com",
        &format!("/x/vupre/web/archive/pre?ts={ts}"),
    )
    .await?;
    if bili_payload.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(AppError::Unavailable(
            "Bilibili partition service returned an error".into(),
        ));
    }
    let source = bili_payload
        .pointer("/data/typelist")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Unavailable("bilibili typelist is empty".into()))?;
    let options = type_options(source);
    if options.is_empty() {
        return Err(AppError::Unavailable("bilibili typelist is empty".into()));
    }
    let choices = options
        .iter()
        .map(|(id, path)| format!("{id}\t{path}"))
        .collect::<Vec<_>>()
        .join("\n");
    let prompt = format!(
        "你是 B 站投稿分区（tid/typeid）助手。请根据输入文本，选择最合适的一个分区。\n要求：\n- 必须从提供的候选列表中选择，输出的 typeid 必须在候选列表里；\n- 只输出 JSON 对象，字段为：typeid（数字）与 reason（字符串，<=80字）。\n\n输入文本：\n{text}\n\n候选分区（每行：typeid<TAB>path）：\n{choices}\n\n输出 JSON：\n{{ \"typeid\": 0, \"reason\": \"\" }}"
    );
    let mut body = json!({
        "model":model.model,"temperature":0.0,"response_format":{"type":"json_object"},
        "messages":[{"role":"system","content":"Return ONLY valid JSON (no markdown, no extra text)."},
            {"role":"user","content":prompt}]
    });
    if let Some(cap) = model.max_output_tokens {
        body["max_tokens"] = json!(cap.min(1024));
    }
    let client = Client::builder()
        .timeout(Duration::from_secs(model.timeout_seconds.min(120)))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let mut request = client.post(model.endpoint.clone()).json(&body);
    if let Some(key) = model.api_key.as_deref() {
        request = request.bearer_auth(key);
    }
    let result = crate::ai_usage::chat_json(
        db,
        request,
        &model.endpoint,
        &model.model,
        "bilibili_type_recommend",
        payload.task_id,
        128 * 1024,
        "Bilibili type AI",
    )
    .await;
    let content = match result {
        Ok(value) => value
            .pointer("/choices/0/message/content")
            .and_then(Value::as_str)
            .and_then(|text| serde_json::from_str::<Value>(text).ok()),
        Err(error) => {
            tracing::warn!(error=%error, "Bilibili type classification unavailable");
            return Ok(Json(json!({"ok":false,"typeid":null,"path":null,
                "reason":"AI type recommendation unavailable","used_text":text})));
        }
    };
    let Some(object) = content else {
        return Ok(Json(json!({"ok":false,"typeid":null,"path":null,
            "reason":"AI type recommendation invalid JSON","used_text":text})));
    };
    Ok(Json(type_choice(&object, &options, &text)))
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
/// Account-management extension using the original Python `accounts` table.
/// Only owner-authenticated V2 API clients can call these routes. Never return
/// plaintext Cookies, including in errors, responses or account list views.
#[derive(Debug, Deserialize)]
struct NamedAccountInput {
    name: String,
    cookie: String,
}
fn validate_account_name(name: &str) -> AppResult<&str> {
    if name.is_empty()
        || name.len() > 64
        || !name
            .bytes()
            .enumerate()
            .all(|(i, b)| b.is_ascii_alphanumeric() || (i > 0 && matches!(b, b'.' | b'_' | b'-')))
    {
        return Err(AppError::Invalid(
            "Bilibili account name must contain 1-64 letters, digits, dot, underscore or hyphen"
                .into(),
        ));
    }
    Ok(name)
}
async fn list_named_accounts(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let rows = sqlx::query(
        "SELECT id,name,is_active,check_state,last_checked_at,rotated_at,
       length(secrets_encrypted)>0 AS cookie_set FROM accounts
       WHERE platform='bilibili'::platform ORDER BY name",
    )
    .fetch_all(&state.db.pool)
    .await?;
    let accounts = rows
        .iter()
        .map(|row| {
            Ok(json!({
                "id":row.try_get::<Uuid,_>("id")?,"name":row.try_get::<String,_>("name")?,
                "is_active":row.try_get::<bool,_>("is_active")?,
                "check_state":row.try_get::<String,_>("check_state")?,
                "cookie_set":row.try_get::<bool,_>("cookie_set")?,
                "rotated_at":row.try_get::<Option<DateTime<Utc>>,_>("rotated_at")?,
                "last_checked_at":row.try_get::<Option<DateTime<Utc>>,_>("last_checked_at")?
            }))
        })
        .collect::<AppResult<Vec<Value>>>()?;
    Ok(Json(json!({"accounts":accounts})))
}
async fn save_named_account(
    State(state): State<AppState>,
    Json(body): Json<NamedAccountInput>,
) -> AppResult<Json<Value>> {
    let name = validate_account_name(&body.name)?;
    let cookie = normalize_cookie(&body.cookie)?;
    let parts = cookie_parts(&cookie);
    if parts.get("SESSDATA").is_none_or(|v| v.is_empty())
        || parts.get("bili_jct").is_none_or(|v| v.is_empty())
    {
        return Err(AppError::Invalid(
            "Bilibili named account requires SESSDATA and bili_jct Cookies".into(),
        ));
    }
    // This validates CSRF and rejects inconsistent or malformed cookies.
    credential_values(&json!({"cookie_enc":cookie}), None, None)?;
    let encrypted = format!("dbplain:v1:{cookie}");
    let row = sqlx::query(
        "INSERT INTO accounts(id,platform,name,secrets_encrypted,rotated_at,
        is_active,check_state,last_checked_at,last_check_message)
        VALUES($1,'bilibili'::platform,$2,$3,now(),true,'unchecked',NULL,NULL)
        ON CONFLICT (platform,name) DO UPDATE SET
          secrets_encrypted=EXCLUDED.secrets_encrypted,rotated_at=now(),
          is_active=true,check_state='unchecked',last_checked_at=NULL,last_check_message=NULL
        RETURNING id,name",
    )
    .bind(Uuid::new_v4())
    .bind(name)
    .bind(encrypted)
    .fetch_one(&state.db.pool)
    .await?;
    Ok(Json(json!({"id":row.try_get::<Uuid,_>("id")?,
        "name":row.try_get::<String,_>("name")?,"is_active":true,
        "check_state":"unchecked","cookie_set":true})))
}
async fn disable_named_account(
    State(state): State<AppState>,
    Path(account_id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let mut tx = state.db.pool.begin().await?;
    let row = sqlx::query(
        "SELECT id,name FROM accounts WHERE id=$1 AND platform='bilibili'::platform FOR UPDATE",
    )
    .bind(account_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("Bilibili account not found".into()))?;
    let in_flight: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM publish_jobs
       WHERE account_id=$1 AND platform='bilibili' AND state='submitting')",
    )
    .bind(account_id)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "UPDATE accounts SET secrets_encrypted='',is_active=false,
      check_state='unchecked',last_checked_at=NULL,last_check_message=NULL,
      rotated_at=now() WHERE id=$1",
    )
    .bind(account_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"id":account_id,"name":row.try_get::<String,_>("name")?,
        "is_active":false,"cookie_set":false,"in_flight":in_flight,
        "warning":if in_flight {Some("already running platform publication may still finish; verify external result")} else {None}}),
    ))
}
/// Verify a named account via Bilibili's authenticated member profile.
async fn check_named_account(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let (cookie, _, fingerprint) =
        selected_publication_credentials(&state.db, &state.config, Some(id)).await?;
    let profile = bili_get_cookie(
        &state.config,
        "api.bilibili.com",
        "/x/member/web/account",
        &cookie,
    )
    .await?;
    if profile.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(AppError::Unauthorized);
    }
    let data = profile
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| AppError::Unavailable("Bilibili profile is incomplete".into()))?;
    let mid = data
        .get("mid")
        .and_then(Value::as_i64)
        .or_else(|| {
            data.get("mid")
                .and_then(Value::as_str)
                .and_then(|v| v.parse().ok())
        })
        .filter(|v| *v > 0)
        .ok_or_else(|| AppError::Unavailable("Bilibili account mid missing".into()))?;
    let uname = data
        .get("uname")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if uname.is_empty() {
        return Err(AppError::Unavailable(
            "Bilibili account name missing".into(),
        ));
    }
    let (_, _, current) =
        selected_publication_credentials(&state.db, &state.config, Some(id)).await?;
    if current != fingerprint {
        return Err(AppError::Conflict(
            "Bilibili account changed during verification".into(),
        ));
    }
    let label = format!(
        "Bilibili mid={mid}, uname={}",
        uname.chars().take(80).collect::<String>()
    );
    let n = sqlx::query(
        "UPDATE accounts SET check_state='valid',last_check_message=$2,
        last_checked_at=now() WHERE id=$1 AND platform='bilibili'::platform AND is_active=true",
    )
    .bind(id)
    .bind(label)
    .execute(&state.db.pool)
    .await?;
    if n.rows_affected() != 1 {
        return Err(AppError::Conflict(
            "Bilibili account no longer active".into(),
        ));
    }
    Ok(Json(
        json!({"id":id,"check_state":"valid","mid":mid,"uname":uname}),
    ))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/bilibili/auth/settings", get(get_auth).put(put_auth))
        .route(
            "/bilibili/accounts",
            get(list_named_accounts).post(save_named_account),
        )
        .route(
            "/bilibili/accounts/{account_id}",
            axum::routing::delete(disable_named_account),
        )
        .route(
            "/bilibili/accounts/{account_id}/check",
            post(check_named_account),
        )
        .route("/bilibili/auth/me", get(auth_me))
        .route("/bilibili/archive/types", get(archive_types))
        .route(
            "/bilibili/archive/type/recommend",
            post(recommend_archive_type),
        )
        .route("/bilibili/publish/jobs/{job_id}", get(publish_job))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn typeid_candidates_are_leaf_only_and_model_choice_is_fenced() {
        let rows = vec![json!({"id":17,"name":"科技","children":[
            {"id":"180","name":"知识"},{"id":181,"name":"数码"}]})];
        let choices = type_options(&rows);
        assert_eq!(
            choices,
            vec![(180, "科技/知识".into()), (181, "科技/数码".into())]
        );
        assert_eq!(
            type_choice(
                &json!({"typeid":"181","reason":"电子相关"}),
                &choices,
                "PCB"
            )["path"],
            "科技/数码"
        );
        let forged = type_choice(
            &json!({"typeid":999999,"reason":"model picked nonexisting category"}),
            &choices,
            "PCB",
        );
        assert_eq!(forged["ok"], false);
        assert_eq!(forged["typeid"], Value::Null);
    }
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
