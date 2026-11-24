//! Native original GET /bilibili/discovery for the dashboard.
//! Purely informational catalogue IDs must NEVER become creator-center
//! mission_id/topic_id or establish upload eligibility.
use crate::api::AppState;
use axum::{
    Json, Router,
    extract::{Query, State},
    routing::get,
};
use chrono::{DateTime, SecondsFormat, Utc};
use reqwest::{Client, Url};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::OnceLock,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;

const RANKING: &str = "/x/web-interface/ranking/v2";
const POPULAR: &str = "/x/web-interface/popular";
const ACTIVITIES: &str = "/x/activity/page/list";
const TTL: f64 = 300.0;
const FORCE_MIN: f64 = 60.0;
const STALE_MAX: f64 = 3600.0;
const MAX_RESPONSE: usize = 1024 * 1024;
const NOTE: &str = "公开活动列表仅供浏览，不代表账号有报名或投稿资格。";

#[derive(Clone, Debug)]
struct Entry {
    timestamp: f64,
    items: Vec<Value>,
    source: &'static str,
}
type Cache = HashMap<&'static str, Entry>;
static CACHE: OnceLock<Mutex<Cache>> = OnceLock::new();
#[derive(Deserialize, Default)]
struct QueryArgs {
    #[serde(default)]
    refresh: bool,
}
fn timestamp() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}
fn iso(epoch: f64) -> Option<String> {
    DateTime::<Utc>::from_timestamp_micros((epoch * 1_000_000.0) as i64)
        .map(|t| t.to_rfc3339_opts(SecondsFormat::Micros, false))
}
fn n(value: &Value) -> i64 {
    let parsed = match value {
        Value::Number(v) => v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    };
    parsed.unwrap_or(0).max(0)
}
fn label(v: Option<&Value>, max_chars: usize) -> String {
    v.and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(max_chars)
        .collect()
}
fn public_link(v: Option<&Value>) -> Option<String> {
    let raw = v.and_then(Value::as_str)?.trim();
    let u = Url::parse(raw).ok()?;
    if u.scheme() != "https"
        || !matches!(
            u.host_str(),
            Some("www.bilibili.com" | "member.bilibili.com")
        )
        || !u.path().starts_with("/blackboard/")
        || !u.username().is_empty()
        || u.password().is_some()
    {
        return None;
    }
    Some(raw.to_owned())
}
fn image_link(v: Option<&Value>) -> Option<String> {
    let raw = v.and_then(Value::as_str)?.trim();
    let candidate = if raw.starts_with("//") {
        format!("https:{raw}")
    } else {
        raw.to_owned()
    };
    let u = Url::parse(&candidate).ok()?;
    let host = u.host_str().unwrap_or("");
    if u.scheme() != "https"
        || !host.ends_with(".hdslb.com")
        || !u.username().is_empty()
        || u.password().is_some()
    {
        return None;
    }
    Some(candidate)
}
fn video_id(s: &str) -> bool {
    s.len() == 12
        && s.starts_with("BV")
        && s.as_bytes()[2..].iter().all(|b| b.is_ascii_alphanumeric())
}
fn ranking(payload: &Value) -> Result<Vec<Value>, ()> {
    if payload.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(());
    }
    let rows = payload
        .pointer("/data/list")
        .and_then(Value::as_array)
        .ok_or(())?;
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for row in rows {
        if !row.is_object() {
            continue;
        }
        let bvid = label(row.get("bvid"), 20);
        let title = label(row.get("title"), 120);
        if !video_id(&bvid) || title.is_empty() || !seen.insert(bvid.clone()) {
            continue;
        }
        let stats = &row["stat"];
        let owner = &row["owner"];
        out.push(json!({
            "rank":out.len()+1,
            "bvid":bvid,"title":title,
            "category":label(row.get("tname"),40),
            "uploader":label(owner.get("name"),55),
            "cover":image_link(row.get("pic")),
            "views":n(&stats["view"]),
            "likes":n(&stats["like"]),
            "danmaku":n(&stats["danmaku"]),
            "url":format!("https://www.bilibili.com/video/{bvid}")
        }));
        if out.len() >= 20 {
            break;
        }
    }
    Ok(out)
}
fn activities(payload: &Value, now: i64) -> Result<Vec<Value>, ()> {
    if payload.get("code").and_then(Value::as_i64) != Some(0) {
        return Err(());
    }
    let rows = payload
        .pointer("/data/list")
        .and_then(Value::as_array)
        .ok_or(())?;
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for row in rows {
        if !row.is_object() {
            continue;
        }
        let id = n(&row["id"]);
        let start = n(&row["stime"]);
        let end = n(&row["etime"]);
        let title = label(row.get("name"), 110);
        let link = public_link(row.get("pc_url"));
        let status = row.get("state");
        if id == 0
            || seen.contains(&id)
            || title.is_empty()
            || link.is_none()
            || !matches!(status, Some(Value::Number(v)) if v.as_i64()==Some(1))
                && !matches!(status, Some(Value::String(v)) if v=="1")
            || start == 0
            || end == 0
            || !(start <= now && now <= end)
        {
            continue;
        }
        let (Some(start_iso), Some(end_iso)) = (
            DateTime::<Utc>::from_timestamp(start, 0).map(|v| v.to_rfc3339()),
            DateTime::<Utc>::from_timestamp(end, 0).map(|v| v.to_rfc3339()),
        ) else {
            continue;
        };
        seen.insert(id);
        out.push(json!({
            "id":id,"title":title,
            "description":label(row.get("desc"),240),
            "start_at":start_iso,"end_at":end_iso,
            "cover":image_link(row.get("pc_cover")),
            "url":link,"eligibility":"unknown",
        }));
        if out.len() >= 30 {
            break;
        }
    }
    Ok(out)
}
fn upstream_base() -> Result<String, ()> {
    if let Ok(raw) = std::env::var("VIDEOROLL_V2_TEST_BILI_DISCOVERY_BASE") {
        if std::env::var("VIDEOROLL_V2_ALLOW_TEST_BILI_DISCOVERY_LOOPBACK")
            .ok()
            .as_deref()
            != Some("true")
        {
            return Err(());
        }
        let url = Url::parse(&raw).map_err(|_| ())?;
        if url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || url.port() != Some(18196)
            || url.path() != "/"
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(());
        }
        return Ok(raw.trim_end_matches('/').to_owned());
    }
    Ok("https://api.bilibili.com".into())
}
fn client() -> Result<Client, ()> {
    Client::builder()
        .timeout(Duration::from_secs(8))
        .connect_timeout(Duration::from_secs(4))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .user_agent("Mozilla/5.0 (compatible; VideoRollDashboard/1.0)")
        .build()
        .map_err(|_| ())
}
async fn upstream(
    client: &Client,
    base: &str,
    path: &str,
    params: &[(&str, &str)],
) -> Result<Value, ()> {
    let url = format!("{base}{path}");
    let response = client
        .get(&url)
        .header("Referer", "https://www.bilibili.com/")
        .header("Accept", "application/json")
        .query(params)
        .send()
        .await
        .map_err(|_| ())?;
    if !response.status().is_success()
        || response
            .content_length()
            .is_some_and(|v| v > MAX_RESPONSE as u64)
    {
        return Err(());
    }
    let bytes = response.bytes().await.map_err(|_| ())?;
    if bytes.len() > MAX_RESPONSE {
        return Err(());
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
    if !value.is_object() {
        return Err(());
    }
    Ok(value)
}
async fn fetch_ranking(client: &Client, base: &str) -> Result<(Vec<Value>, &'static str), ()> {
    let ranking_result = upstream(client, base, RANKING, &[("rid", "0"), ("type", "all")])
        .await
        .and_then(|v| ranking(&v));
    if let Ok(items) = ranking_result {
        return Ok((items, "排行榜"));
    }
    let value = upstream(client, base, POPULAR, &[("pn", "1"), ("ps", "20")]).await?;
    Ok((ranking(&value)?, "热门综合"))
}
async fn fetch_activities(
    client: &Client,
    base: &str,
    now: f64,
) -> Result<(Vec<Value>, &'static str), ()> {
    let first = upstream(
        client,
        base,
        ACTIVITIES,
        &[("plat", "1,3"), ("pn", "1"), ("ps", "30")],
    )
    .await?;
    let mut rows = first
        .pointer("/data/list")
        .and_then(Value::as_array)
        .cloned()
        .ok_or(())?;
    // Require the first page to be valid before accessing page two.
    let primary = activities(&first, now as i64)?;
    if let Ok(second) = upstream(
        client,
        base,
        ACTIVITIES,
        &[("plat", "1,3"), ("pn", "2"), ("ps", "30")],
    )
    .await
    {
        if second.get("code").and_then(Value::as_i64) == Some(0) {
            if let Some(extra) = second.pointer("/data/list").and_then(Value::as_array) {
                rows.extend(extra.iter().cloned());
                let merged = json!({"code":0,"data":{"list":rows}});
                if let Ok(all) = activities(&merged, now as i64) {
                    return Ok((all, "公开活动"));
                }
            }
        }
    }
    Ok((primary, "公开活动"))
}
fn cached_value(existing: Option<&Entry>, now: f64, refresh: bool) -> Option<Value> {
    let entry = existing?;
    let age = now - entry.timestamp;
    if age >= TTL || (refresh && age >= FORCE_MIN) {
        return None;
    }
    Some(
        json!({"items":entry.items,"fetched_at":iso(entry.timestamp),
        "source":entry.source,"stale":false,"error":null}),
    )
}
fn old_value(existing: Option<&Entry>, now: f64) -> Value {
    if let Some(entry) = existing {
        if now - entry.timestamp < STALE_MAX {
            return json!({"items":entry.items,"fetched_at":iso(entry.timestamp),
                "source":entry.source,"stale":true,"error":"上游暂时不可用，显示缓存数据"});
        }
    }
    json!({"items":[],"fetched_at":null,"source":null,
        "stale":false,"error":"B站数据暂时不可用"})
}
async fn feed(
    cache: &mut Cache,
    name: &'static str,
    refresh: bool,
    now: f64,
    client: &Client,
    base: &str,
) -> Value {
    if let Some(cached) = cached_value(cache.get(name), now, refresh) {
        return cached;
    }
    let result = match name {
        "ranking" => fetch_ranking(client, base).await,
        _ => fetch_activities(client, base, now).await,
    };
    match result {
        Ok((items, source)) => {
            cache.insert(
                name,
                Entry {
                    timestamp: now,
                    items: items.clone(),
                    source,
                },
            );
            json!({"items":items,"fetched_at":iso(now),
                "source":source,"stale":false,"error":null})
        }
        Err(_) => {
            tracing::warn!(feed = name, "Bilibili discovery upstream unavailable");
            old_value(cache.get(name), now)
        }
    }
}
async fn read(State(_state): State<AppState>, Query(params): Query<QueryArgs>) -> Json<Value> {
    let now = timestamp();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut lock = cache.lock().await;
    let configured = match (upstream_base(), client()) {
        (Ok(base), Ok(client)) => Some((base, client)),
        _ => None,
    };
    let ranking;
    let activities;
    if let Some((base, client)) = configured {
        ranking = feed(&mut lock, "ranking", params.refresh, now, &client, &base).await;
        activities = feed(&mut lock, "activities", params.refresh, now, &client, &base).await;
    } else {
        ranking = old_value(lock.get("ranking"), now);
        activities = old_value(lock.get("activities"), now);
    }
    Json(json!({"ranking":ranking,"activities":activities,"note":NOTE}))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/bilibili/discovery", get(read))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_video_ids_and_public_links_survive() {
        let payload = json!({"code":0,"data":{"list":[
            {"bvid":"BV1234567890","title":"Valid","pic":"//i0.hdslb.com/example.jpg",
             "stat":{"view":125,"like":3,"danmaku":-3},"owner":{"name":"UP"}},
            {"bvid":"BV1234567890","title":"Duplicate"},
            {"bvid":"BV123","title":"Invalid"},
            {"bvid":"BVabcdefghij","title":" ","pic":"https://evil.example/image.jpg"}
        ]}});
        let out = ranking(&payload).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["rank"], 1);
        assert_eq!(out[0]["danmaku"], 0);
        assert_eq!(out[0]["cover"], "https://i0.hdslb.com/example.jpg");
        assert!(public_link(Some(&json!("https://fake.com/blackboard/test"))).is_none());
    }
    #[test]
    fn activity_catalog_is_not_a_creator_center_mission_id() {
        let payload = json!({"code":0,"data":{"list":[
            {"id":45,"stime":1730000000,"etime":1930000000,"state":1,
             "name":"Example","pc_url":"https://www.bilibili.com/blackboard/act.html",
             "pc_cover":"https://i0.hdslb.com/cover.jpg"},
            {"id":45,"stime":1730000000,"etime":1930000000,"state":1,
             "name":"Duplicate","pc_url":"https://www.bilibili.com/blackboard/act2.html"},
            {"id":46,"stime":1730000000,"etime":1930000000,"state":0,
             "name":"Inactive","pc_url":"https://www.bilibili.com/blackboard/act3.html"},
            {"id":47,"stime":1730000000,"etime":1930000000,"state":1,
             "name":"Bad host","pc_url":"https://evil.com/blackboard/act4.html"},
            {"id":48,"stime":1730000000,"etime":1930000000,"state":"01",
             "name":"String state 01 is not active","pc_url":"https://www.bilibili.com/blackboard/test"}
        ]}});
        let out = activities(&payload, 1795500000).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["id"], 45);
        assert_eq!(out[0]["eligibility"], "unknown");
        assert!(out[0].get("topic_id").is_none());
        assert!(out[0].get("mission_id").is_none());
    }
    #[test]
    fn ttl_refresh_and_stale_age() {
        let entry = Entry {
            timestamp: 1000.,
            items: vec![json!({"id":5})],
            source: "公开活动",
        };
        assert!(cached_value(Some(&entry), 1050., true).is_some());
        assert!(cached_value(Some(&entry), 1060., true).is_none());
        assert!(cached_value(Some(&entry), 1250., false).is_some());
        assert!(cached_value(Some(&entry), 1300., false).is_none());
        assert_eq!(old_value(Some(&entry), 4500.)["stale"], true);
        assert_eq!(old_value(Some(&entry), 4600.)["stale"], false);
    }
}
