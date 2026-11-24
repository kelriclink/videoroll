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
    upload_id: String,
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
    // Never trust the preupload response to redirect storage bytes to a
    // caller-controlled host. HTTP loopback is allowed *only* for the isolated
    // creator-center fixture and must match that fixture's exact authority.
    let origin = std::env::var("VIDEOROLL_V2_BILIBILI_API_BASE")
        .ok()
        .and_then(|v| Url::parse(&v).ok());
    let test_allowed = std::env::var("VIDEOROLL_V2_ALLOW_TEST_BILIBILI_LOOPBACK")
        .ok()
        .as_deref()
        == Some("true")
        && origin.as_ref().is_some_and(|v| {
            v.scheme() == "http"
                && matches!(v.host_str(), Some("localhost" | "127.0.0.1" | "::1"))
                && v.port().is_some()
        });
    let full = format!(
        "{}:{}{}",
        if test_allowed { "http" } else { "https" },
        pre.endpoint,
        pre.upos_uri.replacen("upos:/", "", 1)
    );
    let url =
        Url::parse(&full).map_err(|_| AppError::Unavailable("invalid Bilibili CDN URL".into()))?;
    let host = url.host_str().unwrap_or("");
    let loopback = test_allowed
        && origin
            .as_ref()
            .is_some_and(|v| v.host_str() == url.host_str() && v.port() == url.port());
    let official = url.scheme() == "https"
        && valid_upos_host(host)
        && url.port_or_known_default() == Some(443);
    if !(official || loopback)
        || url.query().is_some()
        || url.fragment().is_some()
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
    if let Some(direct) = profile.get("bilibili_direct_v1") {
        let meta: ArchiveMeta =
            serde_json::from_value(direct.get("meta").cloned().unwrap_or(Value::Null))
                .map_err(|_| AppError::Invalid("Bilibili direct metadata invalid".into()))?;
        meta.validate()?;
        let items = direct
            .get("parts")
            .and_then(Value::as_array)
            .ok_or_else(|| AppError::Invalid("Bilibili direct part list missing".into()))?;
        if !(1..=2).contains(&items.len()) {
            return Err(AppError::Invalid(
                "Bilibili direct archive requires one or two parts".into(),
            ));
        }
        let mut parts = Vec::new();
        for (index, item) in items.iter().enumerate() {
            let key = item
                .get("key")
                .and_then(Value::as_str)
                .ok_or_else(|| AppError::Invalid("Bilibili part key missing".into()))?;
            let kind = if index == 0 {
                "video_final"
            } else {
                "video_dubbed"
            };
            if item.get("kind").and_then(Value::as_str) != Some(kind) {
                return Err(AppError::Conflict(
                    "Bilibili part kind no longer matches request".into(),
                ));
            }
            let actual:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND kind::text=$2 AND storage_key=$3)")
                .bind(job.task_id).bind(kind).bind(key).fetch_one(&db.pool).await?;
            if !actual {
                return Err(AppError::Conflict(
                    "Bilibili requested media is no longer task-owned".into(),
                ));
            }
            let title = item
                .get("title")
                .and_then(Value::as_str)
                .filter(|v| !v.trim().is_empty())
                .unwrap_or(&meta.title);
            parts.push((key.to_owned(), title.to_owned()));
        }
        validate_assets(&parts)?;
        if parts.len() == 2 {
            let original = latest_asset_key(db, job.task_id, "video_raw")
                .await?
                .ok_or_else(|| {
                    AppError::Conflict("original video missing for Bilibili P2".into())
                })?;
            let srt = latest_asset_key(db, job.task_id, "subtitle_srt")
                .await?
                .ok_or_else(|| AppError::Conflict("subtitle missing for Bilibili P2".into()))?;
            let valid: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM vr2_dubbing_runs r JOIN vr2_jobs j ON j.id=r.job_id
                WHERE r.task_id=$1 AND r.output_video_key=$2 AND r.source_video_key=$3
                 AND r.source_srt_key=$4 AND r.model_status='succeeded' AND j.state='succeeded')",
            )
            .bind(job.task_id)
            .bind(&parts[1].0)
            .bind(original)
            .bind(srt)
            .fetch_one(&db.pool)
            .await?;
            if !valid {
                return Err(AppError::Conflict(
                    "Bilibili direct P2 no longer matches completed YouDub source and subtitles"
                        .into(),
                ));
            }
        }
        return Ok((meta, parts));
    }

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
        // Manual YouDub and manual publishing are intentionally different V2
        // workflow generations. Proof of same P1/P2 source is the immutable
        // succeeded DubbingRun's original source+SRT keys, not a shared
        // workflow ID (which would break valid manual P2 submissions).
        let original = latest_asset_key(db, job.task_id, "video_raw")
            .await?
            .ok_or_else(|| AppError::NotFound("P2 original source video missing".into()))?;
        let srt = latest_asset_key(db, job.task_id, "subtitle_srt")
            .await?
            .ok_or_else(|| AppError::NotFound("P2 associated subtitle missing".into()))?;
        let matched: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM vr2_dubbing_runs r
             JOIN vr2_jobs j ON j.id=r.job_id
             WHERE r.task_id=$1 AND r.output_video_key=$2 AND
               r.source_video_key=$3 AND r.source_srt_key=$4
               AND r.model_status='succeeded' AND j.state='succeeded'
               AND j.stage='dubbing' AND j.task_id=$1)",
        )
        .bind(job.task_id)
        .bind(&p2)
        .bind(&original)
        .bind(&srt)
        .fetch_one(&db.pool)
        .await?;
        if !matched {
            return Err(AppError::Conflict(
                "P2 does not match the current original video and SRT from a completed YouDub run"
                    .into(),
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
    origin: Url,
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
    fn new(cookie: &str, origin: Url) -> AppResult<Self> {
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
        Ok(Self {
            member,
            upos,
            origin,
        })
    }
    fn url(&self, path: &str) -> AppResult<Url> {
        self.origin
            .join(path)
            .map_err(|_| AppError::Invalid("invalid Bilibili creator-center path".into()))
    }
    async fn predict_type(
        &self,
        csrf: &str,
        video: &UploadedVideo,
        title: &str,
    ) -> AppResult<Option<u32>> {
        let body = reqwest::multipart::Form::new()
            .text("filename", video.filename.clone())
            .text("title", title.to_owned())
            .text("upload_id", video.upload_id.clone());
        let response = self
            .member
            .post(self.url("x/vupre/web/archive/types/predict")?)
            .query(&[
                ("csrf", csrf),
                ("ts", &chrono::Utc::now().timestamp_millis().to_string()),
            ])
            .multipart(body)
            .send()
            .await?;
        let data = Self::read_json(response).await?;
        Self::ensure_ok(&data, false)?;
        Ok(data
            .pointer("/data/0/id")
            .and_then(Value::as_u64)
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| *n > 0))
    }
    async fn upload_cover(&self, csrf: &str, path: &Path) -> AppResult<String> {
        use base64::Engine as _;
        let image = fs::read(path).await?;
        if image.is_empty() || image.len() > 8 * 1024 * 1024 {
            return Err(AppError::Invalid(
                "Bilibili cover must be 1 byte to 8 MiB".into(),
            ));
        }
        let mime = match image.as_slice() {
            [0xff, 0xd8, 0xff, ..] => "image/jpeg",
            [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, ..] => "image/png",
            [
                b'R',
                b'I',
                b'F',
                b'F',
                _,
                _,
                _,
                _,
                b'W',
                b'E',
                b'B',
                b'P',
                ..,
            ] => "image/webp",
            _ => return Err(AppError::Invalid("unsupported Bilibili cover type".into())),
        };
        let cover = format!(
            "data:{mime};base64,{}",
            base64::engine::general_purpose::STANDARD.encode(&image)
        );
        let response = self
            .member
            .post(self.url("x/vu/web/cover/up")?)
            .query(&[("ts", chrono::Utc::now().timestamp_millis().to_string())])
            .form(&[("csrf", csrf), ("cover", &cover)])
            .send()
            .await?;
        let data = Self::read_json(response).await?;
        Self::ensure_ok(&data, false)?;
        let url = data
            .pointer("/data/url")
            .and_then(Value::as_str)
            .filter(|s| s.starts_with("https://") && s.len() < 2048)
            .ok_or_else(|| {
                AppError::Unavailable("Bilibili cover URL missing or insecure".into())
            })?;
        Ok(url.to_owned())
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
            .get(self.url("preupload")?)
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
            upload_id,
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
            .post(self.url("x/vu/web/add/v3")?)
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

/// Build an owner-preserving, single-P2 edit payload. Never reconstruct an
/// existing archive from our (possibly outdated) original submission draft.
fn append_p2_payload(
    owner: &Value,
    aid: i64,
    filename: &str,
    cid: i64,
    title: &str,
    csrf: &str,
) -> AppResult<Value> {
    let archive = owner
        .get("archive")
        .and_then(Value::as_object)
        .ok_or_else(|| AppError::Conflict("Bilibili owner archive is missing".into()))?;
    let parts = owner
        .get("videos")
        .and_then(Value::as_array)
        .filter(|v| v.len() == 1)
        .ok_or_else(|| AppError::Conflict("P2 append requires exactly one P1 part".into()))?;
    if aid <= 0 || archive.get("aid").and_then(Value::as_i64) != Some(aid) {
        return Err(AppError::Conflict("owner archive aid mismatch".into()));
    }
    let part = parts[0]
        .as_object()
        .ok_or_else(|| AppError::Conflict("malformed original P1 part".into()))?;
    let p1_cid = part.get("cid").and_then(Value::as_i64).unwrap_or(0);
    let p1_filename = part.get("filename").and_then(Value::as_str).unwrap_or("");
    if p1_cid <= 0 || p1_cid == cid || p1_filename.is_empty() {
        return Err(AppError::Conflict(
            "P1 cid/filename missing or duplicate".into(),
        ));
    }
    if cid <= 0
        || filename.trim().is_empty()
        || filename.len() > 256
        || title.trim().is_empty()
        || title.chars().count() > 80
        || csrf.trim().len() < 12
        || csrf.chars().any(char::is_control)
    {
        return Err(AppError::Invalid("invalid native P2 edit input".into()));
    }
    let keep: &[&str] = &[
        "title",
        "cover",
        "cover43",
        "copyright",
        "source",
        "tid",
        "tag",
        "desc",
        "desc_v2",
        "desc_format_id",
        "dynamic",
        "recreate",
        "interactive",
        "act_reserve_create",
        "no_disturbance",
        "no_reprint",
        "subtitle",
        "dolby",
        "lossless_music",
        "up_selection_reply",
        "up_close_reply",
        "up_close_danmu",
        "web_os",
        "is_only_self",
        "topic_id",
        "mission_id",
        "is_360",
        "neutral_mark",
        "human_type2",
    ];
    let mut body = serde_json::Map::new();
    for key in keep {
        if let Some(value) = archive.get(*key) {
            body.insert((*key).to_owned(), value.clone());
        }
    }
    for key in ["title", "copyright", "tid", "tag", "desc"] {
        if !body.contains_key(key) {
            return Err(AppError::Conflict(format!("owner archive missing {key}")));
        }
    }
    if body.get("tid").and_then(Value::as_i64).unwrap_or(0) <= 0 {
        return Err(AppError::Conflict("owner archive has invalid tid".into()));
    }
    let p1_title = part
        .get("title")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .or_else(|| archive.get("title").and_then(Value::as_str))
        .unwrap_or("");
    if p1_title.is_empty() {
        return Err(AppError::Conflict("owner P1 title missing".into()));
    }
    body.insert("aid".into(), json!(aid));
    body.insert("csrf".into(), json!(csrf));
    body.insert(
        "videos".into(),
        json!([
            {"filename":p1_filename,"title":p1_title,"desc":part.get("desc").and_then(Value::as_str).unwrap_or(""),"cid":p1_cid},
            {"filename":filename,"title":title,"desc":"","cid":cid}
        ]),
    );
    Ok(Value::Object(body))
}

fn member_origin(config: &Config) -> AppResult<Url> {
    let raw = config
        .native_bilibili_api_base
        .as_deref()
        .unwrap_or("https://member.bilibili.com");
    let url =
        Url::parse(raw).map_err(|_| AppError::Invalid("invalid creator-center URL".into()))?;
    let official = url.scheme() == "https"
        && url.host_str() == Some("member.bilibili.com")
        && url.port().is_none();
    let testing = url.scheme() == "http"
        && matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "::1"))
        && url.port().is_some();
    if !(official || testing)
        || url.username() != ""
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(AppError::Invalid(
            "P2 creator-center endpoint must be member.bilibili.com HTTPS or isolated loopback"
                .into(),
        ));
    }
    Ok(url)
}

pub(crate) struct NativeP2Client {
    inner: BilibiliClient,
    origin: Url,
    csrf: String,
}
impl NativeP2Client {
    pub(crate) async fn new(config: &Config, db: &Database) -> AppResult<Self> {
        if !config.native_bilibili_allowed {
            return Err(AppError::Unavailable(
                "native Bilibili publisher disabled".into(),
            ));
        }
        let (cookie, csrf) =
            crate::bilibili_account_v1::publication_credentials(db, config).await?;
        Ok(Self {
            inner: BilibiliClient::new(&cookie, member_origin(config)?)?,
            origin: member_origin(config)?,
            csrf,
        })
    }
    fn url(&self, path: &str) -> AppResult<Url> {
        self.origin
            .join(path)
            .map_err(|_| AppError::Invalid("invalid creator-center path".into()))
    }
    pub(crate) async fn owner(&self, aid: i64) -> AppResult<Value> {
        if aid <= 0 {
            return Err(AppError::Invalid("invalid Bilibili aid".into()));
        }
        let response = self
            .inner
            .member
            .get(self.url("x/vupre/web/archive/view")?)
            .query(&[("aid", aid)])
            .send()
            .await?;
        let value = BilibiliClient::read_json(response).await?;
        BilibiliClient::ensure_ok(&value, false)?;
        let archive = value
            .pointer("/data/archive")
            .ok_or_else(|| AppError::Unavailable("creator-center archive missing".into()))?;
        let videos = value
            .pointer("/data/videos")
            .ok_or_else(|| AppError::Unavailable("creator-center videos missing".into()))?;
        if archive.get("aid").and_then(Value::as_i64) != Some(aid) || !videos.is_array() {
            return Err(AppError::Conflict("owner archive identity mismatch".into()));
        }
        Ok(json!({"archive":archive,"videos":videos}))
    }
    pub(crate) async fn upload(&self, path: &Path) -> AppResult<(String, i64)> {
        // A local mock creator center must never accidentally upload via
        // the real hardcoded UPOS hosts. Integration tests inject a durable
        // pre-uploaded receipt when exercising the irreversible edit.
        if self.origin.scheme() == "http" {
            return Err(AppError::Unavailable(
                "UPOS upload disabled in loopback test mode".into(),
            ));
        }
        let uploaded = self.inner.upload(path).await?;
        Ok((uploaded.filename, uploaded.cid))
    }
    pub(crate) async fn edit(
        &self,
        owner: &Value,
        aid: i64,
        filename: &str,
        cid: i64,
        title: &str,
    ) -> AppResult<()> {
        let payload = append_p2_payload(owner, aid, filename, cid, title, &self.csrf)?;
        let response = self
            .inner
            .member
            .post(self.url("x/vu/web/edit")?)
            .query(&[
                ("csrf", self.csrf.as_str()),
                ("t", &chrono::Utc::now().timestamp_millis().to_string()),
            ])
            .json(&payload)
            .send()
            .await?;
        let value = BilibiliClient::read_json(response).await?;
        BilibiliClient::ensure_ok(&value, false)?;
        Ok(())
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
    let (cookie, csrf) = crate::bilibili_account_v1::publication_credentials(db, config).await?;
    if !crate::publish_platforms::enabled(db, "bilibili").await? {
        return Err(AppError::Conflict(
            "Bilibili publishing is disabled in V2 platform settings".into(),
        ));
    }
    let (mut meta, parts) = archive_spec(db, job).await?;
    let direct: Option<Value> = sqlx::query_scalar(
        "SELECT configuration->'bilibili_direct_v1' FROM vr2_workflows WHERE id=$1",
    )
    .bind(job.workflow_id)
    .fetch_optional(&db.pool)
    .await?
    .flatten();
    let reviewed_meta = if direct.is_some() {
        Some(
            serde_json::to_value(&meta)
                .map_err(|_| AppError::Invalid("metadata encoding failed".into()))?,
        )
    } else {
        crate::publish_meta::saved(db, job.task_id).await?
    };
    let reviewed_meta = reviewed_meta.unwrap_or(
        serde_json::to_value(&meta)
            .map_err(|_| AppError::Invalid("Bilibili metadata serialization failed".into()))?,
    );
    crate::publish_review::ensure_approved(db, registry, config, job.task_id, &reviewed_meta)
        .await?;
    let client = BilibiliClient::new(&cookie, member_origin(config)?)?;
    // Validate creator-center ID pairing and rules before any UPOS upload.
    crate::bili_activity::verify_remote(&client.member, &meta).await?;
    if direct.is_some() {
        crate::bilibili_publish_v1::mark_started(db, job).await?;
        if let Some(cover) = direct
            .as_ref()
            .and_then(|d| d.get("cover_key"))
            .and_then(Value::as_str)
        {
            let path = registry.resolve_existing(cover).await?;
            meta.cover = client.upload_cover(&csrf, &path).await?;
        }
    }
    let mut uploaded = Vec::new();
    for (key, title) in parts {
        let path = registry.resolve_existing(&key).await?;
        probe_video(&config.ffprobe_binary, &path).await?;
        let upload = client.upload(&path).await?;
        uploaded.push((upload, title));
    }
    // Python V1 predicts only when requested; a missing prediction preserves
    // the explicitly supplied metadata typeid rather than inventing one.
    if direct
        .as_ref()
        .and_then(|v| v.get("typeid_mode"))
        .and_then(Value::as_str)
        == Some("bilibili_predict")
    {
        if let Some((p1, _)) = uploaded.first() {
            if let Ok(Some(predicted)) = client.predict_type(&csrf, p1, &meta.title).await {
                meta.typeid = predicted;
            }
        }
    }
    // Single archive creation: UPOS uploads are preparatory side-effects;
    // archive POST is the irreversible boundary and is NEVER retried.
    let receipt = client.submit_once(&csrf, &meta, &uploaded).await?;
    tracing::info!(job_id=%job.id,parts=uploaded.len(),"Bilibili native archive confirmed");
    Ok(receipt)
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn p2_owner_edit_preserves_archive_and_never_overwrites_existing_p2() {
        let input = json!({"archive":{"aid":10,"title":"Original","copyright":2,
          "tid":171,"tag":"test","desc":"source","topic_id":2323,"mission_id":45},
          "videos":[{"cid":11,"filename":"original_video","title":"P1","desc":"first"}]});
        let payload =
            append_p2_payload(&input, 10, "dubbed_video", 20, "P2", "123456789012").unwrap();
        assert_eq!(payload["videos"].as_array().unwrap().len(), 2);
        assert_eq!(payload["videos"][0]["cid"], 11);
        assert_eq!(payload["videos"][1]["cid"], 20);
        assert_eq!(payload["topic_id"], 2323);
        assert_eq!(payload["mission_id"], 45);
        assert_eq!(payload["desc"], "source");
        assert!(append_p2_payload(&input, 11, "other", 20, "P2", "123456789012").is_err());
        assert!(append_p2_payload(&input, 10, "other", 11, "P2", "123456789012").is_err());
        let mut already = input.clone();
        already["videos"]
            .as_array_mut()
            .unwrap()
            .push(json!({"cid":40,"filename":"other"}));
        assert!(append_p2_payload(&already, 10, "other", 50, "P2", "123456789012").is_err());
    }
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
                    upload_id: String::new(),
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
