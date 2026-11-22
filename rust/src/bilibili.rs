//! Native Rust Bilibili UPOS multi-part upload and single-archive submission.
//! Implements core Bilibili Web API following original BilibiliWebClient contract.
//! Never shares Cookie with the UPOS CDN. The irreversible archive POST is made
//! only once. Network uncertainty is handled by vr2_jobs state=unknown.
use crate::{
    AppError, AppResult, Config, Database,
    domain::WorkItem,
    media::ffmpeg::{latest_asset_key, probe_video},
    storage::AssetRegistry,
};
use reqwest::{
    Client, Url,
    header::{self, HeaderValue},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::HashSet, path::Path, time::Duration};
use tokio::{fs, io::AsyncReadExt, time::sleep};
use uuid::Uuid;

const UPLOAD_MAX_CHUNK: usize = 64 * 1024 * 1024;
const MAX_METADATA_BYTES: usize = 2 * 1024 * 1024;
const MAX_PARTS: u64 = 25_000;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ArchiveMeta {
    pub title: String,
    #[serde(default)]
    pub desc: String,
    pub typeid: u32,
    pub tags: Vec<String>,
    #[serde(default = "default_copyright")]
    pub copyright: u8,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub cover: String,
    #[serde(default)]
    pub topic_id: Option<i64>,
    #[serde(default)]
    pub mission_id: Option<i64>,
    // V1's additional Web publishing fields are validated explicitly.
    // Unrecognized keys must never be silently dropped during an actual post.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, Value>,
}
fn default_copyright() -> u8 {
    2
}
impl ArchiveMeta {
    pub(crate) fn validate(&self) -> AppResult<()> {
        if self.title.trim().is_empty() || self.title.chars().count() > 80 {
            return Err(AppError::Invalid(
                "Bilibili title must contain 1-80 characters".into(),
            ));
        }
        if self.typeid == 0 {
            return Err(AppError::Invalid(
                "Bilibili partition typeid is required".into(),
            ));
        }
        if self.tags.is_empty()
            || self.tags.len() > 10
            || self
                .tags
                .iter()
                .any(|tag| tag.trim().is_empty() || tag.chars().count() > 30)
        {
            return Err(AppError::Invalid(
                "Bilibili requires 1-10 nonempty valid tags".into(),
            ));
        }
        if self.copyright != 1 && self.copyright != 2 {
            return Err(AppError::Invalid(
                "Bilibili copyright type must be 1 or 2".into(),
            ));
        }
        if self.copyright == 2 && self.source.trim().is_empty() {
            return Err(AppError::Invalid(
                "reposted Bilibili video requires original source".into(),
            ));
        }
        if self.topic_id.is_some_and(|id| id <= 0) || self.mission_id.is_some_and(|id| id <= 0) {
            return Err(AppError::Invalid(
                "Bilibili topic/mission IDs must be positive (platform eligibility must be checked separately)".into(),
            ));
        }
        for (key, value) in &self.extra {
            let valid = match key.as_str() {
                "activity_name" => value.as_str().is_some_and(|v| v.chars().count() <= 120),
                "dynamic" | "neutral_mark" => {
                    value.as_str().is_some_and(|v| v.chars().count() <= 2_000)
                }
                "dtime" => value.is_null() || value.as_i64().is_some_and(|v| v > 0),
                "desc_v2" => value.is_null() || value.is_array() || value.is_object(),
                "subtitle" => {
                    value
                        .get("open")
                        .and_then(Value::as_i64)
                        .is_some_and(|v| v == 0 || v == 1)
                        && value
                            .get("lan")
                            .and_then(Value::as_str)
                            .is_some_and(|v| v.len() <= 32)
                        && value.as_object().is_some_and(|v| v.len() == 2)
                }
                "up_selection_reply" | "up_close_reply" | "up_close_danmu" | "up_close_danmaku" => {
                    value.is_boolean()
                }
                "desc_format_id" => value.as_i64() == Some(9999),
                "recreate" => value.as_i64().is_some_and(|v| v == -1 || v == 1),
                "interactive" | "act_reserve_create" | "no_disturbance" | "no_reprint"
                | "dolby" | "lossless_music" | "is_only_self" => {
                    value.is_null() || value.as_i64().is_some_and(|v| v == 0 || v == 1)
                }
                "web_os" => value.as_i64().is_some_and(|v| (1..=3).contains(&v)),
                "is_360" => value.is_null() || value.as_i64().is_some_and(|v| v == -1 || v == 1),
                "human_type2" => value.is_null() || value.as_i64().is_some_and(|v| v >= 0),
                _ => false,
            };
            if !valid {
                return Err(AppError::Invalid(format!(
                    "Bilibili metadata field {key:?} is unsupported or invalid in native publisher"
                )));
            }
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Deserialize)]
struct Preupload {
    auth: String,
    endpoint: String,
    upos_uri: String,
    biz_id: i64,
    chunk_size: u64,
}
#[derive(Debug, Clone)]
struct UploadedVideo {
    filename: String,
    cid: i64,
}
#[derive(Debug, Serialize)]
struct PutPart {
    #[serde(rename = "partNumber")]
    number: u64,
    #[serde(rename = "eTag")]
    etag: String,
}
#[derive(Debug, Clone)]
struct UploadTarget {
    url: Url,
    auth: String,
    cid: i64,
    chunk_size: u64,
    filename: String,
}
fn valid_upos_host(host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    [
        "bilivideo.com",
        "bilivideo.cn",
        "bilibili.com",
        "biliimg.com",
    ]
    .iter()
    .any(|suffix| host == *suffix || host.ends_with(&format!(".{suffix}")))
}
fn upos_target(pre: &Preupload) -> AppResult<UploadTarget> {
    if !pre.endpoint.starts_with("//")
        || !pre.upos_uri.starts_with("upos://")
        || pre.auth.is_empty()
        || pre.biz_id <= 0
        || pre.chunk_size == 0
        || pre.chunk_size > UPLOAD_MAX_CHUNK as u64
    {
        return Err(AppError::Unavailable(
            "Bilibili UPOS returned malformed upload metadata".into(),
        ));
    }
    let full = format!(
        "https:{}{}",
        pre.endpoint,
        pre.upos_uri.replacen("upos:/", "", 1)
    );
    let url =
        Url::parse(&full).map_err(|_| AppError::Unavailable("invalid Bilibili CDN URL".into()))?;
    let host = url.host_str().unwrap_or("");
    if url.scheme() != "https"
        || !valid_upos_host(host)
        || url.port_or_known_default() != Some(443)
        || url.username() != ""
        || url.password().is_some()
        || url.path().contains("..")
    {
        return Err(AppError::Unavailable(
            "Bilibili UPOS CDN hostname/port rejected".into(),
        ));
    }
    let filename = pre
        .upos_uri
        .rsplit('/')
        .next()
        .unwrap_or("")
        .rsplit_once('.')
        .map(|(name, _)| name)
        .unwrap_or("");
    if filename.is_empty() || filename.len() > 200 {
        return Err(AppError::Unavailable(
            "Bilibili UPOS filename malformed".into(),
        ));
    }
    Ok(UploadTarget {
        url,
        auth: pre.auth.clone(),
        cid: pre.biz_id,
        chunk_size: pre.chunk_size,
        filename: filename.into(),
    })
}
fn expected_platforms(conf: &Value) -> AppResult<Vec<String>> {
    let platforms = conf
        .get("auto_publish_platforms")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            AppError::Unavailable(
                "native Bilibili posting requires explicit platform selection".into(),
            )
        })?;
    let names = platforms
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>();
    if names != ["bilibili"] {
        return Err(AppError::Unavailable("native Bilibili posting supports only a single Bilibili target; use multi-platform dispatcher for others".into()));
    }
    Ok(names.into_iter().map(str::to_string).collect())
}
fn validate_assets(parts: &[(String, String)]) -> AppResult<()> {
    if !(1..=2).contains(&parts.len()) {
        return Err(AppError::Invalid("Bilibili archive needs 1-2 parts".into()));
    }
    let mut seen = HashSet::new();
    for (key, title) in parts {
        AssetRegistry::checked_key(key)?;
        if title.trim().is_empty() || title.chars().count() > 80 || !seen.insert(key) {
            return Err(AppError::Invalid(
                "Bilibili video part missing title or duplicated".into(),
            ));
        }
    }
    Ok(())
}
pub async fn archive_spec(
    db: &Database,
    job: &WorkItem,
) -> AppResult<(ArchiveMeta, Vec<(String, String)>)> {
    let profile: Option<Value> =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
            .bind(job.workflow_id)
            .fetch_optional(&db.pool)
            .await?;
    let profile = profile.unwrap_or(json!({}));
    let auto: Option<Value> = Some(crate::settings::effective(db, "subtitle.auto_profile").await?);
    let source_platforms = profile
        .get("auto_publish_platforms")
        .or_else(|| auto.as_ref().and_then(|v| v.get("auto_publish_platforms")));
    expected_platforms(&json!({"auto_publish_platforms":source_platforms}))?;

    let saved = crate::publish_meta::saved(db, job.task_id).await?;
    let meta:ArchiveMeta=serde_json::from_value(
        saved.or_else(||profile.pointer("/publish/bilibili").cloned())
            .ok_or_else(||AppError::Unavailable(
            "native Bilibili requires saved metadata or explicit publish.bilibili configuration".into()
        ))?
    ).map_err(|_|AppError::Invalid("Bilibili metadata schema invalid".into()))?;
    meta.validate()?;
    let p1 = latest_asset_key(db, job.task_id, "video_final")
        .await?
        .or(latest_asset_key(db, job.task_id, "video_raw").await?)
        .ok_or_else(|| AppError::NotFound("P1 finished video missing".into()))?;
    let p2 = latest_asset_key(db, job.task_id, "video_dubbed").await?;
    let mut parts = vec![(p1, meta.title.clone())];
    if let Some(p2) = p2 {
        let bound:Option<Uuid>=sqlx::query_scalar(
            "SELECT job_id FROM vr2_dubbing_runs WHERE task_id=$1 AND workflow_id=$2 AND output_video_key=$3 AND model_status='succeeded'"
        ).bind(job.task_id).bind(job.workflow_id).bind(&p2).fetch_optional(&db.pool).await?;
        if bound.is_none() {
            return Err(AppError::Conflict(
                "P2 cannot be submitted without matching completed YouDub run".into(),
            ));
        }
        let p2_title = profile
            .pointer("/publish/bilibili_p2_title")
            .and_then(Value::as_str)
            .unwrap_or("中文配音版")
            .to_string();
        parts.push((p2, p2_title));
    }
    validate_assets(&parts)?;
    Ok((meta, parts))
}
struct BilibiliClient {
    member: Client,
    upos: Client,
}
// Pure, testable Bilibili Web POST mapping. Only validated fields are copied.
fn archive_payload(csrf: &str, meta: &ArchiveMeta, parts: &[(UploadedVideo, String)]) -> Value {
    let mut payload = json!({
        "videos":parts.iter().map(|(part,title)|json!({
            "filename":part.filename,"title":title,"desc":"","cid":part.cid
        })).collect::<Vec<_>>(),
        "cover":if meta.cover.is_empty(){Value::Null}else{json!(meta.cover)},
        "cover43":"","title":meta.title,"copyright":meta.copyright,
        "source":if meta.copyright==2{json!(meta.source)}else{Value::Null},
        "tid":meta.typeid,"tag":meta.tags.join(","),"desc":meta.desc,
        "desc_format_id":9999,"recreate":-1,"dynamic":"",
        "interactive":0,"act_reserve_create":0,"no_disturbance":0,"no_reprint":1,
        "subtitle":{"open":0,"lan":""},
        "dolby":0,"lossless_music":0,"web_os":3,
        "topic_id":meta.topic_id,"mission_id":meta.mission_id,
        "csrf":csrf,
    });
    if let Some(obj) = payload.as_object_mut() {
        for (field, value) in &meta.extra {
            if field == "activity_name" {
                continue;
            }
            // V1 used both spellings; do not let aliases diverge.
            let name = if field == "up_close_danmaku" {
                "up_close_danmu"
            } else {
                field.as_str()
            };
            if !value.is_null() {
                obj.insert(name.to_owned(), value.clone());
            }
        }
        obj.retain(|_, v| !v.is_null());
    }
    payload
}

impl BilibiliClient {
    fn new(cookie: &str) -> AppResult<Self> {
        if cookie.trim().is_empty() || cookie.contains('\r') || cookie.contains('\n') {
            return Err(AppError::Invalid(
                "Bilibili Cookie is missing or invalid".into(),
            ));
        }
        let agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36";
        let mut headers = header::HeaderMap::new();
        headers.insert(
            header::COOKIE,
            HeaderValue::from_str(cookie)
                .map_err(|_| AppError::Invalid("Bilibili Cookie header invalid".into()))?,
        );
        headers.insert(header::USER_AGENT, HeaderValue::from_static(agent));
        headers.insert(
            header::REFERER,
            HeaderValue::from_static("https://member.bilibili.com/"),
        );
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("https://member.bilibili.com"),
        );
        let member = Client::builder()
            .default_headers(headers)
            .timeout(Duration::from_secs(45))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        let upos = Client::builder()
            .user_agent(agent)
            .timeout(Duration::from_secs(180))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self { member, upos })
    }
    async fn read_json(response: reqwest::Response) -> AppResult<Value> {
        if !response.status().is_success() {
            return Err(AppError::Unavailable(format!(
                "Bilibili server returned HTTP {}",
                response.status()
            )));
        }
        if response
            .content_length()
            .is_some_and(|n| n > MAX_METADATA_BYTES as u64)
        {
            return Err(AppError::Unavailable("Bilibili response oversized".into()));
        }
        let raw = response.bytes().await?;
        if raw.len() > MAX_METADATA_BYTES {
            return Err(AppError::Unavailable("Bilibili response oversized".into()));
        }
        serde_json::from_slice(&raw)
            .map_err(|_| AppError::Unavailable("Bilibili returned invalid JSON".into()))
    }
    fn ensure_ok(value: &Value, upload: bool) -> AppResult<()> {
        let ok = if upload {
            value.get("OK").and_then(Value::as_i64) == Some(1)
        } else {
            value.get("code").and_then(Value::as_i64) == Some(0)
        };
        if ok {
            return Ok(());
        }
        // Never send raw upstream error fields to users: they can contain credentials.
        Err(AppError::Unavailable(
            "Bilibili rejected upload/submission; inspect redacted publisher logs".into(),
        ))
    }
    async fn preupload(&self, path: &Path) -> AppResult<Preupload> {
        let size = fs::metadata(path).await?.len();
        let name = path
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or_else(|| AppError::Invalid("video filename not Unicode".into()))?;
        let response = self
            .member
            .get("https://member.bilibili.com/preupload")
            .query(&[
                ("name", name.to_string()),
                ("r", "upos".into()),
                ("profile", "ugcupos/bup".into()),
                ("ssl", "0".into()),
                ("version", "2.14.0".into()),
                ("build", "2140000".into()),
                ("size", size.to_string()),
            ])
            .send()
            .await?;
        let json = Self::read_json(response).await?;
        Self::ensure_ok(&json, true)?;
        serde_json::from_value::<Preupload>(json)
            .map_err(|_| AppError::Unavailable("invalid UPOS preupload schema".into()))
    }
    async fn init_upos(&self, target: &UploadTarget, size: u64) -> AppResult<String> {
        let response = self
            .upos
            .post(target.url.clone())
            .query(&[
                ("uploads", ""),
                ("output", "json"),
                ("profile", "ugcupos/bup"),
                ("filesize", &size.to_string()),
                ("partsize", &target.chunk_size.to_string()),
                ("biz_id", &target.cid.to_string()),
            ])
            .header("x-upos-auth", &target.auth)
            .send()
            .await?;
        let json = Self::read_json(response).await?;
        Self::ensure_ok(&json, true)?;
        let id = json.get("upload_id").and_then(Value::as_str).unwrap_or("");
        if id.is_empty() || id.len() > 1024 {
            return Err(AppError::Unavailable(
                "UPOS upload_id missing or oversized".into(),
            ));
        }
        Ok(id.into())
    }
    async fn upload(&self, path: &Path) -> AppResult<UploadedVideo> {
        let size = fs::metadata(path).await?.len();
        if size == 0 || size > 80 * 1024 * 1024 * 1024 {
            return Err(AppError::Invalid(
                "Bilibili video size must be within 80 GiB".into(),
            ));
        }
        let pre = self.preupload(path).await?;
        let target = upos_target(&pre)?;
        let upload_id = self.init_upos(&target, size).await?;
        let chunks = size.div_ceil(target.chunk_size);
        if chunks == 0 || chunks > MAX_PARTS {
            return Err(AppError::Invalid(
                "Bilibili upload needs too many parts".into(),
            ));
        }
        let mut parts = Vec::with_capacity(chunks as usize);
        let mut file = fs::File::open(path).await?;
        for idx in 0..chunks {
            let start = idx * target.chunk_size;
            let length = std::cmp::min(target.chunk_size, size - start) as usize;
            let mut data = vec![0u8; length];
            file.read_exact(&mut data).await?;
            let response = loop {
                let mut last = None;
                for attempt in 0..4 {
                    let result = self
                        .upos
                        .put(target.url.clone())
                        .query(&[
                            ("partNumber", (idx + 1).to_string()),
                            ("uploadId", upload_id.clone()),
                            ("chunk", idx.to_string()),
                            ("chunks", chunks.to_string()),
                            ("size", length.to_string()),
                            ("start", start.to_string()),
                            ("end", (start + length as u64).to_string()),
                            ("total", size.to_string()),
                        ])
                        .header("x-upos-auth", &target.auth)
                        .header(header::CONTENT_TYPE, "application/octet-stream")
                        .body(data.clone())
                        .send()
                        .await;
                    match result {
                        Ok(response) if response.status().is_success() => {
                            last = Some(response);
                            break;
                        }
                        _ if attempt < 3 => {
                            sleep(Duration::from_millis(250 * (1_u64 << attempt))).await;
                        }
                        _ => {
                            return Err(AppError::Unavailable(
                                "Bilibili UPOS chunk upload exceeded retry budget".into(),
                            ));
                        }
                    }
                }
                if let Some(response) = last {
                    break response;
                }
            };
            let etag = response
                .headers()
                .get(header::ETAG)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .trim_matches('"')
                .to_string();
            // UPOS may omit ETag; use file chunk MD5 as current v1 does.
            let etag = if etag.is_empty() {
                format!("{:x}", md5::compute(&data))
            } else {
                etag
            };
            parts.push(PutPart {
                number: idx + 1,
                etag,
            });
        }
        let response = self
            .upos
            .post(target.url.clone())
            .query(&[
                ("output", "json"),
                (
                    "name",
                    path.file_name()
                        .and_then(|v| v.to_str())
                        .unwrap_or("video.mp4"),
                ),
                ("profile", "ugcupos/bup"),
                ("uploadId", &upload_id),
                ("biz_id", &target.cid.to_string()),
            ])
            .header("x-upos-auth", &target.auth)
            .json(&json!({"parts":parts}))
            .send()
            .await?;
        let body = Self::read_json(response).await?;
        Self::ensure_ok(&body, true)?;
        Ok(UploadedVideo {
            filename: target.filename,
            cid: target.cid,
        })
    }
    async fn submit_once(
        &self,
        csrf: &str,
        meta: &ArchiveMeta,
        parts: &[(UploadedVideo, String)],
    ) -> AppResult<Value> {
        let payload = archive_payload(csrf, meta, parts);
        let response = self
            .member
            .post("https://member.bilibili.com/x/vu/web/add/v3")
            .query(&[
                ("csrf", csrf),
                ("ts", &chrono::Utc::now().timestamp_millis().to_string()),
            ])
            .json(&payload)
            .send()
            .await?;
        let value = Self::read_json(response).await?;
        Self::ensure_ok(&value, false)?;
        let bvid = value
            .pointer("/data/bvid")
            .and_then(Value::as_str)
            .filter(|v| v.starts_with("BV") && v.len() <= 32)
            .ok_or_else(|| {
                AppError::Unavailable(
                    "Bilibili archive result lacks a valid BV identifier; outcome uncertain".into(),
                )
            })?;
        Ok(json!({
            "confirmed":true,"native_bilibili":true,
            "external_id":bvid,"bvid":bvid,
            "aid":value.pointer("/data/aid"),"parts":parts.len(),
        }))
    }
}
pub async fn publish(
    config: &Config,
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
) -> AppResult<Value> {
    if !config.native_bilibili_allowed {
        return Err(AppError::Unavailable(
            "native Bilibili publisher is not enabled".into(),
        ));
    }
    let cookie = config
        .native_bilibili_cookie
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("Bilibili cookie not configured".into()))?;
    let csrf = config
        .native_bilibili_csrf
        .as_deref()
        .ok_or_else(|| AppError::Unavailable("Bilibili CSRF token not configured".into()))?;
    if csrf.len() < 12 || csrf.len() > 80 || !csrf.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err(AppError::Invalid("Bilibili CSRF token invalid".into()));
    }
    let (meta, parts) = archive_spec(db, job).await?;
    let reviewed_meta = crate::publish_meta::saved(db, job.task_id)
        .await?
        .unwrap_or(
            serde_json::to_value(&meta)
                .map_err(|_| AppError::Invalid("Bilibili metadata serialization failed".into()))?,
        );
    crate::publish_review::ensure_approved(db, registry, config, job.task_id, &reviewed_meta)
        .await?;
    let client = BilibiliClient::new(cookie)?;
    // Validate creator-center ID pairing and rules before any UPOS upload.
    crate::bili_activity::verify_remote(&client.member, &meta).await?;
    let mut uploaded = Vec::new();
    for (key, title) in parts {
        let path = registry.resolve_existing(&key).await?;
        probe_video(&config.ffprobe_binary, &path).await?;
        let upload = client.upload(&path).await?;
        uploaded.push((upload, title));
    }
    // Single archive creation: UPOS uploads are preparatory side-effects;
    // archive POST is the irreversible boundary and is NEVER retried.
    let receipt = client.submit_once(csrf, &meta, &uploaded).await?;
    tracing::info!(job_id=%job.id,parts=uploaded.len(),"Bilibili native archive confirmed");
    Ok(receipt)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn upos_hostname_and_path_are_strict() {
        let pre = Preupload {
            auth: "test".into(),
            endpoint: "//upos-sz-mirrorcos.bilivideo.com".into(),
            upos_uri: "upos://bucket/file.mp4".into(),
            biz_id: 100,
            chunk_size: 1024 * 1024,
        };
        let target = upos_target(&pre).unwrap();
        assert_eq!(target.filename, "file");
        assert_eq!(
            target.url.as_str(),
            "https://upos-sz-mirrorcos.bilivideo.com/bucket/file.mp4"
        );
        let evil = Preupload {
            endpoint: "//upos.bilivideo.com.evil.example".into(),
            ..pre.clone()
        };
        assert!(upos_target(&evil).is_err());
        let evil = Preupload {
            endpoint: "//127.0.0.1:8080".into(),
            ..pre.clone()
        };
        assert!(upos_target(&evil).is_err());
    }
    #[test]
    fn saved_extended_metadata_is_transmitted_not_silently_ignored() {
        let meta: ArchiveMeta = serde_json::from_value(json!({
            "title":"稿件","typeid":21,"tags":["视频分享"],"copyright":2,
            "source":"https://youtu.be/0123456789A","dynamic":"动态文案",
            "no_disturbance":1,"up_close_reply":true,"up_close_danmaku":true,
            "mission_id":12345,"topic_id":98765,"subtitle":{"open":0,"lan":"zh-CN"}
        }))
        .unwrap();
        meta.validate().unwrap();
        let payload = archive_payload(
            "csrf-test",
            &meta,
            &[(
                UploadedVideo {
                    filename: "sample.mp4".into(),
                    cid: 998,
                },
                "P1".into(),
            )],
        );
        assert_eq!(payload["dynamic"], "动态文案");
        assert_eq!(payload["no_disturbance"], 1);
        assert_eq!(payload["up_close_reply"], true);
        assert_eq!(payload["up_close_danmu"], true);
        assert_eq!(payload["mission_id"], 12345);
        assert_eq!(payload["topic_id"], 98765);
        assert_eq!(payload["csrf"], "csrf-test");
        assert_eq!(payload["videos"][0]["cid"], 998);
        let rejected: ArchiveMeta = serde_json::from_value(json!({
            "title":"稿件","typeid":21,"tags":["视频分享"],
            "csrf":"user-controlled","videos":[]
        }))
        .unwrap();
        assert!(rejected.validate().is_err());
    }
    #[test]
    fn native_bili_requires_valid_metadata_and_p1_p2() {
        let mut meta = ArchiveMeta {
            title: "测试标题".into(),
            desc: "source".into(),
            typeid: 21,
            tags: vec!["视频分享".into()],
            copyright: 2,
            source: "https://youtube.com/watch".into(),
            cover: String::new(),
            topic_id: None,
            mission_id: None,
            extra: serde_json::Map::new(),
        };
        assert!(meta.validate().is_ok());
        meta.source = String::new();
        assert!(meta.validate().is_err());
        assert!(
            validate_assets(&[
                ("v2/raw/test.mp4".into(), "P1".into()),
                ("v2/raw/test.mp4".into(), "P2".into())
            ])
            .is_err()
        );
    }
    #[test]
    fn native_bili_refuses_other_platforms() {
        assert!(expected_platforms(&json!({"auto_publish_platforms":["bilibili"]})).is_ok());
        assert!(
            expected_platforms(&json!({"auto_publish_platforms":["bilibili","douyin"]})).is_err()
        );
    }
}
