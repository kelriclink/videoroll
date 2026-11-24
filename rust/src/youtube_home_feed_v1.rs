//! Native YouTube authenticated Home/Browse reader.
//! The original Python home recommendation parser remains a differential
//! reference only; no Python HTTP service is involved at runtime.
use crate::{AppError, AppResult};
use reqwest::{Client, Url, header};
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::{
    collections::HashSet,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const ORIGIN: &str = "https://www.youtube.com";
const API_KEY: &str = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8";
const CLIENT_VERSION: &str = "2.20260206.01.00";
const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Video {
    pub id: String,
    pub title: String,
    pub duration: Option<u64>,
    pub short: bool,
    pub renderer: String,
}
impl Video {
    pub(crate) fn url(&self) -> String {
        format!("{ORIGIN}/watch?v={}", self.id)
    }
}
#[derive(Debug, Clone)]
pub(crate) struct Stats {
    pub candidate_count: usize,
    pub explicit_shorts_count: usize,
    pub known_duration_count: usize,
    pub unknown_duration_count: usize,
    pub below_min_duration_count: usize,
    pub kept_unknown_duration_count: usize,
    pub eligible_count: usize,
    pub min_duration_seconds: u64,
    pub log_lines: Vec<String>,
}
#[derive(Debug)]
pub(crate) struct Feed {
    pub videos: Vec<Video>,
    pub stats: Stats,
}
fn text(value: &Value) -> String {
    if let Some(value) = value.as_str() {
        return value.trim().to_string();
    }
    if let Some(s) = value.get("simpleText").and_then(Value::as_str) {
        return s.trim().into();
    }
    if let Some(s) = value.get("content").and_then(Value::as_str) {
        return s.trim().into();
    }
    if let Some(runs) = value.get("runs").and_then(Value::as_array) {
        return runs
            .iter()
            .filter_map(|v| v.get("text").and_then(Value::as_str))
            .collect::<String>()
            .trim()
            .into();
    }
    String::new()
}
fn valid_video_id(id: &str) -> bool {
    // Native youtube_video_id accepts the platform's 11-character IDs.
    id.len() == 11
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}
fn colon_seconds(raw: &str) -> Option<u64> {
    let s = raw.trim();
    let parts = s.split(':').collect::<Vec<_>>();
    if !(2..=3).contains(&parts.len())
        || parts
            .iter()
            .any(|v| v.is_empty() || !v.bytes().all(|c| c.is_ascii_digit()))
    {
        return None;
    }
    let mut n = 0u64;
    for part in parts {
        n = n.checked_mul(60)?.checked_add(part.parse::<u64>().ok()?)?;
    }
    if n == 0 || n > 7 * 24 * 3600 {
        return None;
    }
    Some(n)
}
fn seconds(node: &Value) -> Option<u64> {
    for item in [
        node.get("lengthText"),
        node.get("thumbnailText"),
        node.get("durationText"),
    ]
    .into_iter()
    .flatten()
    {
        if let Some(n) = colon_seconds(&text(item)) {
            return Some(n);
        }
    }
    if let Some(overlays) = node.get("thumbnailOverlays").and_then(Value::as_array) {
        for overlay in overlays {
            if let Some(n) = colon_seconds(&text(
                &overlay["thumbnailOverlayTimeStatusRenderer"]["text"],
            )) {
                return Some(n);
            }
        }
    }
    for path in [
        "/contentImage/thumbnailViewModel/overlays",
        "/contentImage/collectionThumbnailViewModel/primaryThumbnail/thumbnailViewModel/overlays",
    ] {
        if let Some(overlays) = node.pointer(path).and_then(Value::as_array) {
            for overlay in overlays {
                if let Some(n) = colon_seconds(&text(
                    &overlay["thumbnailOverlayTimeStatusRenderer"]["text"],
                )) {
                    return Some(n);
                }
            }
        }
    }
    None
}
fn is_short(node: &Value, renderer: &str) -> bool {
    renderer == "shortsLockupViewModel"
        || renderer == "reelItemRenderer"
        || node
            .pointer("/navigationEndpoint/reelWatchEndpoint/videoId")
            .is_some()
        || node
            .pointer("/onTap/innertubeCommand/reelWatchEndpoint/videoId")
            .is_some()
        || node
            .pointer(
                "/rendererContext/commandContext/onTap/innertubeCommand/reelWatchEndpoint/videoId",
            )
            .is_some()
        || node
            .pointer("/navigationEndpoint/commandMetadata/webCommandMetadata/url")
            .and_then(Value::as_str)
            .is_some_and(|s| s.contains("/shorts/"))
        || node
            .get("thumbnailOverlays")
            .and_then(Value::as_array)
            .is_some_and(|rows| {
                rows.iter().any(|v| {
                    v.pointer("/thumbnailOverlayTimeStatusRenderer/style")
                        .and_then(Value::as_str)
                        == Some("SHORTS")
                })
            })
}
fn parse_video(node: &Value, renderer: &str) -> Option<Video> {
    // These are original Home feed renderers, not arbitrary watch endpoints.
    if !matches!(
        renderer,
        "videoRenderer"
            | "gridVideoRenderer"
            | "richItemRenderer"
            | "compactVideoRenderer"
            | "lockupViewModel"
            | "shortsLockupViewModel"
            | "reelItemRenderer"
    ) {
        return None;
    }
    if node.get("playlistId").is_some() {
        return None;
    }
    if renderer == "lockupViewModel"
        && node
            .get("contentType")
            .and_then(Value::as_str)
            .is_some_and(|s| s != "LOCKUP_CONTENT_TYPE_VIDEO")
    {
        return None;
    }
    let id = if renderer == "lockupViewModel" {
        node.get("contentId").or_else(|| {
            node.pointer(
                "/rendererContext/commandContext/onTap/innertubeCommand/watchEndpoint/videoId",
            )
        })
    } else if renderer == "shortsLockupViewModel" {
        node.pointer("/onTap/innertubeCommand/reelWatchEndpoint/videoId")
            .or_else(|| node.pointer("/navigationEndpoint/reelWatchEndpoint/videoId"))
    } else {
        node.get("videoId")
            .or_else(|| node.pointer("/navigationEndpoint/watchEndpoint/videoId"))
            .or_else(|| node.pointer("/onTap/innertubeCommand/watchEndpoint/videoId"))
    };
    let id = id?.as_str()?;
    if !valid_video_id(id) {
        return None;
    }
    let title = [
        node.get("title"),
        node.pointer("/metadata/lockupMetadataViewModel/title"),
        node.pointer("/overlayMetadata/primaryText"),
        node.get("headline"),
    ]
    .into_iter()
    .flatten()
    .map(text)
    .find(|s| !s.is_empty())
    .unwrap_or_default();
    if title.is_empty() && node.get("thumbnail").is_none() && node.get("contentImage").is_none() {
        return None;
    }
    Some(Video {
        id: id.to_owned(),
        title,
        duration: seconds(node),
        short: is_short(node, renderer),
        renderer: renderer.to_owned(),
    })
}
fn collect(
    node: &Value,
    renderer: &str,
    out: &mut Vec<Video>,
    seen: &mut HashSet<String>,
    limit: usize,
    depth: usize,
) {
    if out.len() >= limit || depth > 75 {
        return;
    }
    if let Some(video) = parse_video(node, renderer) {
        if seen.insert(video.id.clone()) {
            out.push(video);
        }
        if out.len() >= limit {
            return;
        }
    }
    if let Some(map) = node.as_object() {
        for (key, value) in map {
            collect(value, key, out, seen, limit, depth + 1);
            if out.len() >= limit {
                return;
            }
        }
    } else if let Some(items) = node.as_array() {
        for value in items {
            collect(value, renderer, out, seen, limit, depth + 1);
            if out.len() >= limit {
                return;
            }
        }
    }
}
pub(crate) fn videos(payload: &Value, limit: usize) -> Vec<Video> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    collect(payload, "", &mut out, &mut seen, limit, 0);
    out
}
fn continuations(node: &Value, out: &mut Vec<String>, seen: &mut HashSet<String>, depth: usize) {
    if depth > 75 || out.len() >= 50 {
        return;
    }
    if let Some(map) = node.as_object() {
        for key in [
            "continuationCommand",
            "nextContinuationData",
            "reloadContinuationData",
        ] {
            if let Some(v) = map.get(key) {
                if let Some(t) = v
                    .get("token")
                    .or_else(|| v.get("continuation"))
                    .and_then(Value::as_str)
                {
                    if !t.is_empty() && t.len() <= 4096 && seen.insert(t.into()) {
                        out.push(t.into());
                    }
                }
            }
        }
        for value in map.values() {
            continuations(value, out, seen, depth + 1);
        }
    } else if let Some(items) = node.as_array() {
        for value in items {
            continuations(value, out, seen, depth + 1);
        }
    }
}
fn tokens(payload: &Value) -> Vec<String> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    continuations(payload, &mut out, &mut seen, 0);
    out
}
fn duration_repr(n: Option<u64>) -> String {
    match n {
        Some(n) if n >= 3600 => format!("{}:{:02}:{:02}", n / 3600, (n / 60) % 60, n % 60),
        Some(n) => format!("{}:{:02}", n / 60, n % 60),
        None => "-".into(),
    }
}
pub(crate) fn filter(
    videos: Vec<Video>,
    requested: usize,
    long_only: bool,
    min_duration: u64,
) -> Feed {
    let mut eligible = Vec::new();
    let mut logs = Vec::new();
    let mut shorts = 0usize;
    let mut known = 0usize;
    let mut unknown = 0usize;
    let mut below = 0usize;
    let mut kept_unknown = 0usize;
    for video in &videos {
        if video.short {
            shorts += 1;
        }
        // Python V1 long-only statistics exclude explicit Shorts from the
        // known/unknown duration denominator; non-long scans count them.
        if !long_only || !video.short {
            if video.duration.is_some() {
                known += 1
            } else {
                unknown += 1
            }
        }
        if long_only {
            let decision = if video.short {
                "short".to_owned()
            } else if video.duration.is_some_and(|n| n < min_duration) {
                below += 1;
                "below_min_duration".to_owned()
            } else {
                if video.duration.is_none() {
                    kept_unknown += 1;
                }
                eligible.push(video.clone());
                if video.duration.is_none() {
                    "keep_unknown_duration".into()
                } else {
                    "keep".into()
                }
            };
            if logs.len() < 20 {
                let title = video
                    .title
                    .replace('\n', " ")
                    .chars()
                    .take(72)
                    .collect::<String>();
                logs.push(format!(
                    "[{}] {} dur={} type={} :: {}",
                    if decision == "keep" || decision == "keep_unknown_duration" {
                        "keep"
                    } else {
                        "skip"
                    },
                    video.id,
                    duration_repr(video.duration),
                    video.renderer,
                    title
                ));
            }
        } else {
            eligible.push(video.clone());
        }
    }
    if !long_only {
        logs = vec![
            format!(
                "raw_candidates={} candidate_limit={} requested={}",
                videos.len(),
                requested,
                requested
            ),
            format!(
                "explicit_shorts={} known_duration={} unknown_duration={}",
                shorts, known, unknown
            ),
            format!(
                "eligible={} returned={}",
                eligible.len(),
                eligible.len().min(requested)
            ),
        ];
    }
    let eligible_count = eligible.len();
    eligible.truncate(requested);
    Feed {
        videos: eligible,
        stats: Stats {
            candidate_count: videos.len(),
            explicit_shorts_count: shorts,
            known_duration_count: known,
            unknown_duration_count: unknown,
            below_min_duration_count: below,
            kept_unknown_duration_count: kept_unknown,
            eligible_count,
            min_duration_seconds: min_duration,
            log_lines: logs,
        },
    }
}
pub(crate) fn cookie_header(raw: &str) -> AppResult<String> {
    let normalized = crate::youtube_settings_v1::validate_cookies(raw)?;
    let mut cookies = Vec::new();
    let mut names = HashSet::new();
    for line in normalized.lines() {
        if line.starts_with('#') && !line.starts_with("#HttpOnly_") {
            continue;
        }
        let fields = line.split('\t').collect::<Vec<_>>();
        if fields.len() != 7 {
            continue;
        }
        let domain = fields[0]
            .trim_start_matches("#HttpOnly_")
            .trim_start_matches('.');
        if domain != "youtube.com" && !domain.ends_with(".youtube.com") {
            continue;
        }
        let name = fields[5];
        let val = fields[6];
        if !name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.'))
            || val.chars().any(char::is_control)
            || val.contains(';')
        {
            continue;
        }
        if names.insert(name.to_string()) {
            cookies.push(format!("{name}={val}"));
        }
    }
    if cookies.is_empty() {
        return Err(AppError::Invalid(
            "youtube cookies are empty or do not match youtube.com".into(),
        ));
    }
    Ok(cookies.join("; "))
}
fn cookie_value<'a>(cookies: &'a str, name: &str) -> Option<&'a str> {
    cookies.split(';').map(str::trim).find_map(|pair| {
        pair.strip_prefix(name)
            .and_then(|rest| rest.strip_prefix('='))
    })
}
fn auth(cookies: &str) -> Option<String> {
    let sid = ["SAPISID", "__Secure-3PAPISID", "APISID"]
        .iter()
        .find_map(|name| cookie_value(cookies, name))?;
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let source = format!("{ts} {sid} {ORIGIN}");
    let hash = Sha1::digest(source.as_bytes());
    Some(format!("SAPISIDHASH {ts}_{hash:x}"))
}
fn home_base() -> AppResult<String> {
    if let Ok(raw) = std::env::var("VIDEOROLL_V2_TEST_YOUTUBE_HOME_BASE") {
        if std::env::var("VIDEOROLL_V2_ALLOW_TEST_YOUTUBE_HOME_LOOPBACK")
            .ok()
            .as_deref()
            != Some("true")
        {
            return Err(AppError::Conflict(
                "YouTube mock override requires explicit test guard".into(),
            ));
        }
        let url =
            Url::parse(&raw).map_err(|_| AppError::Invalid("YouTube mock base invalid".into()))?;
        if url.scheme() != "http"
            || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
            || url.username() != ""
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || url.path() != "/"
        {
            return Err(AppError::Conflict(
                "YouTube home mock must be loopback HTTP root".into(),
            ));
        }
        return Ok(raw.trim_end_matches('/').into());
    }
    Ok(ORIGIN.into())
}
fn session(data: &str, cookie: &str) -> (String, Value) {
    let parsed = data
        .strip_prefix(")]}'")
        .and_then(|s| serde_json::from_str::<Value>(s).ok());
    // Python: ytcfg=data[0][2], device_info=ytcfg[0][0],
    // API key=ytcfg[1]. Browser sessions may rotate this public key/version.
    let ytcfg = parsed.as_ref().and_then(|v| v.pointer("/0/2"));
    let device = ytcfg.and_then(|v| v.pointer("/0/0"));
    let key = ytcfg
        .and_then(|v| v.get(1))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(API_KEY)
        .to_owned();
    let pick = |n: usize, default: &str| -> String {
        device
            .and_then(|v| v.get(n))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(default)
            .into()
    };
    let visitor = pick(
        13,
        cookie_value(cookie, "VISITOR_INFO1_LIVE").unwrap_or("visitor"),
    );
    let client = json!({"hl":"en","gl":"US","clientName":"WEB",
        "clientVersion":pick(16,CLIENT_VERSION),"visitorData":visitor,
        "userAgent":USER_AGENT,"osName":pick(17,"Windows"),
        "osVersion":pick(18,"10.0"),"browserName":pick(86,"Chrome"),
        "browserVersion":pick(87,"125.0.0.0"),
        "deviceMake":pick(11,""),"deviceModel":pick(12,""),
        "timeZone":pick(79,&std::env::var("TZ").unwrap_or_else(|_|"UTC".into())),
        "remoteHost":"","screenDensityFloat":1,"screenHeightPoints":1440,
        "screenPixelDensity":1,"screenWidthPoints":2560,"platform":"DESKTOP",
        "clientFormFactor":"UNKNOWN_FORM_FACTOR",
        "userInterfaceTheme":"USER_INTERFACE_THEME_LIGHT",
        "utcOffsetMinutes":0,"memoryTotalKbytes":"8000000",
        "originalUrl":format!("{ORIGIN}/"),
        "mainAppWebInfo":{"graftUrl":format!("{ORIGIN}/"),
          "pwaInstallabilityStatus":"PWA_INSTALLABILITY_STATUS_UNKNOWN",
          "webDisplayMode":"WEB_DISPLAY_MODE_BROWSER",
          "isWebNativeShareAvailable":true}});
    (
        key,
        json!({"client":client,"user":{"enableSafetyMode":false,"lockedSafetyMode":false},"request":{"useSsl":true,"internalExperimentFlags":[]}}),
    )
}
async fn read_json_response(response: reqwest::Response) -> AppResult<Value> {
    if !response.status().is_success() {
        return Err(AppError::Unavailable(format!(
            "youtube home browse HTTP {}",
            response.status().as_u16()
        )));
    }
    if response.content_length().is_some_and(|n| n > 12_000_000) {
        return Err(AppError::Unavailable(
            "youtube home browse response too large".into(),
        ));
    }
    let bytes = response
        .bytes()
        .await
        .map_err(|_| AppError::Unavailable("YouTube browse request body failed".into()))?;
    if bytes.len() > 12_000_000 {
        return Err(AppError::Unavailable(
            "youtube home browse response too large".into(),
        ));
    }
    serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Unavailable("invalid YouTube browse JSON".into()))
}
pub(crate) async fn fetch(
    cookies_txt: &str,
    proxy: Option<&str>,
    limit: usize,
    long_only: bool,
    min_duration: u64,
) -> AppResult<Feed> {
    let cookies = cookie_header(cookies_txt)?;
    let base = home_base()?;
    let mut builder = Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(USER_AGENT);
    if let Some(proxy) = proxy.filter(|p| !p.is_empty()) {
        builder = builder.proxy(
            reqwest::Proxy::all(proxy)
                .map_err(|_| AppError::Invalid("YouTube proxy invalid".into()))?,
        );
    }
    let client = builder
        .build()
        .map_err(|_| AppError::Unavailable("YouTube HTTP client failed".into()))?;
    let tz = std::env::var("TZ")
        .unwrap_or_else(|_| "UTC".into())
        .replace('/', ".");
    let visitor = cookie_value(&cookies, "VISITOR_INFO1_LIVE").unwrap_or("visitor");
    let sw = client
        .get(format!("{base}/sw.js_data"))
        .header(header::REFERER, format!("{ORIGIN}/sw.js"))
        .header(
            header::COOKIE,
            format!("PREF=tz={tz};VISITOR_INFO1_LIVE={visitor};"),
        )
        .send()
        .await
        .ok();
    let sw = match sw {
        Some(response) if response.status().is_success() => {
            response.text().await.unwrap_or_default()
        }
        _ => String::new(),
    };
    let (key, context) = session(&sw, &cookies);
    let mut all = Vec::<Video>::new();
    let mut ids = HashSet::<String>::new();
    let mut visited = HashSet::<String>::new();
    let mut queue = Vec::<String>::new();
    let candidates = if long_only {
        (limit * 8).max(80).min(400)
    } else {
        limit
    };
    for page in 0..4usize {
        let request = if page == 0 {
            json!({"context":context,"browseId":"FEwhat_to_watch"})
        } else {
            if queue.is_empty() {
                break;
            }
            json!({"context":context,"continuation":queue.remove(0)})
        };
        let mut req = client
            .post(format!("{base}/youtubei/v1/browse"))
            .query(&[("key", key.as_str()), ("prettyPrint", "false")])
            .header(header::ORIGIN, ORIGIN)
            .header(header::REFERER, format!("{ORIGIN}/"))
            .header(header::COOKIE, &cookies)
            .header("X-YouTube-Client-Name", "1")
            .header(
                "X-YouTube-Client-Version",
                context["client"]["clientVersion"]
                    .as_str()
                    .unwrap_or(CLIENT_VERSION),
            )
            .header(
                "X-Goog-Visitor-Id",
                context["client"]["visitorData"].as_str().unwrap_or(""),
            )
            .json(&request);
        if let Some(auth) = auth(&cookies) {
            req = req
                .header(header::AUTHORIZATION, auth)
                .header("X-Goog-Authuser", "0");
        }
        let response = req
            .send()
            .await
            .map_err(|_| AppError::Unavailable("youtube home browse request failed".into()))?;
        let payload = read_json_response(response).await?;
        for video in videos(&payload, candidates) {
            if ids.insert(video.id.clone()) {
                all.push(video)
            }
            if all.len() >= candidates && !long_only {
                break;
            }
        }
        for token in tokens(&payload) {
            if visited.insert(token.clone()) {
                queue.push(token);
            }
        }
        if !long_only && all.len() >= limit {
            break;
        }
        if long_only
            && all.len() >= candidates
            && filter(all.clone(), limit, true, min_duration).videos.len() >= limit
        {
            break;
        }
    }
    Ok(filter(all, limit, long_only, min_duration))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_original_renderer_and_shorts() {
        let data = json!({"contents":[
            {"videoRenderer":{"videoId":"abcdef12345","title":{"runs":[{"text":"Normal"}]},"lengthText":{"simpleText":"12:35"}}},
            {"reelItemRenderer":{"videoId":"shorts12345","title":{"simpleText":"Short clip"}}},
            {"lockupViewModel":{"contentType":"LOCKUP_CONTENT_TYPE_VIDEO","contentId":"lockup12345","metadata":{"lockupMetadataViewModel":{"title":{"content":"New style"}}}}},
            {"videoRenderer":{"videoId":"abcdef12345","title":{"simpleText":"Duplicate"}}}
        ]});
        let v = videos(&data, 20);
        assert_eq!(v.len(), 3);
        assert_eq!(v[0].duration, Some(755));
        assert!(v[1].short);
        let f = filter(v, 10, true, 180);
        assert_eq!(f.stats.explicit_shorts_count, 1);
        assert_eq!(f.stats.eligible_count, 2);
        assert_eq!(f.stats.kept_unknown_duration_count, 1);
    }
    #[test]
    fn rejects_fake_video_id_and_non_youtube_cookies() {
        assert!(!valid_video_id("x"));
        assert!(
            cookie_header("# Netscape HTTP Cookie File\n.evil.test\tTRUE\t/\tTRUE\t0\tSID\ttoken")
                .is_err()
        );
        let cookie = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsomething";
        assert_eq!(cookie_header(cookie).unwrap(), "SID=something");
    }
    #[test]
    fn extracts_continuations() {
        let v = json!({"continuationCommand":{"token":"first"},"nextContinuationData":{"continuation":"second"}});
        assert_eq!(tokens(&v).len(), 2);
    }
}
