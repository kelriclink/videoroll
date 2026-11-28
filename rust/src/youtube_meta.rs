//! Native YouTube metadata facade for the original task page.
//! yt-dlp is the maintained extractor. Its JSON output is strictly bounded,
//! persisted only for V2 tasks, and never treated as a source of local paths.
use crate::{AppError, AppResult, api::AppState, domain::AssetInfo, legacy_contracts::read_task};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::get,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::Row;
use std::{process::Stdio, time::Duration};
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    time::timeout,
};
use uuid::Uuid;

// Original yt-dlp JSON includes format and caption manifests; production
// objects exceed 13 MB. Bound actual bytes read instead of rejecting them.
pub(crate) const MAX_INFO: usize = 64 * 1024 * 1024;
pub(crate) async fn read_info_file(path:&std::path::Path)->AppResult<Value>{
    let mut reader=fs::File::open(path).await?.take((MAX_INFO+1) as u64);
    let mut bytes=Vec::new();reader.read_to_end(&mut bytes).await?;
    if bytes.len()>MAX_INFO{return Err(AppError::Invalid("metadata asset exceeds read budget".into()));}
    serde_json::from_slice(&bytes).map_err(|_|AppError::Invalid("cached YouTube metadata corrupt".into()))
}
pub(crate) fn meta_view(value: &Value, fallback: &str) -> AppResult<Value> {
    let source = value
        .as_object()
        .ok_or_else(|| AppError::Invalid("YouTube metadata is not an object".into()))?;
    let title = source
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if title.is_empty() || title.chars().count() > 1000 {
        return Err(AppError::Invalid(
            "YouTube metadata title missing or too long".into(),
        ));
    }
    let webpage_url = source
        .get("webpage_url")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback);
    crate::ingest::validate_youtube_url(webpage_url)?;
    let desc = source
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok(json!({
        "title":title,
        "description":desc.chars().take(30_000).collect::<String>(),
        "webpage_url":webpage_url,
        "uploader":source.get("uploader").and_then(Value::as_str),
        "upload_date":source.get("upload_date").and_then(Value::as_str),
        "duration":source.get("duration").and_then(Value::as_f64)
          .filter(|d| d.is_finite() && *d>=0.0).map(|d|d as u64)
    }))
}
pub(crate) struct DownloadedInfo {
    key: String,
    sha: String,
    size: i64,
    value: Value,
}
/// Python pick_thumbnail_url: pixel area plus preference, first maximum wins.
fn pick_thumbnail_url(info:&Value)->Option<String>{
    let int=|v:&Value| v.as_i64().or_else(||v.as_str()?.parse().ok()).unwrap_or(0);
    let mut best=None;
    let mut score=-1i64;
    if let Some(items)=info["thumbnails"].as_array(){
        for item in items{
            let Some(url)=item["url"].as_str().filter(|s|!s.is_empty()).or_else(||item["src"].as_str()).map(str::trim).filter(|s|!s.is_empty()) else {continue};
            let candidate=int(&item["width"]).saturating_mul(int(&item["height"])).saturating_add(int(&item["preference"]).saturating_mul(10));
            if candidate>score {score=candidate;best=Some(url.to_owned());}
        }
    }
    best.or_else(||info["thumbnail"].as_str().map(str::trim).filter(|s|!s.is_empty()).map(str::to_owned))
}
fn thumbnail_url(raw:&str)->AppResult<reqwest::Url>{
    let url=reqwest::Url::parse(raw).map_err(|_|AppError::Invalid("invalid YouTube thumbnail URL".into()))?;
    let host=url.host_str().unwrap_or("").trim_end_matches('.').to_ascii_lowercase();
    if !matches!(url.scheme(),"http"|"https") || !["youtube.com","ytimg.com","googleusercontent.com","ggpht.com"].iter().any(|s|host==*s||host.ends_with(&format!(".{s}"))){
        return Err(AppError::Invalid("YouTube thumbnail URL host is not allowed".into()))
    }
    Ok(url)
}
/// A prepared cover is removed on rollback or duplicate; the committed file
/// stays under the original final/<task>/cover_youtube_<digest>_<uuid>.jpg key.
pub(crate) struct PreparedCover {key:String,path:std::path::PathBuf,sha:String,size:i64,keep:bool}
impl Drop for PreparedCover {
    fn drop(&mut self){if !self.keep {let _=std::fs::remove_file(&self.path);}}
}
impl PreparedCover {pub(crate) fn committed(&mut self){self.keep=true;}}
pub(crate) async fn prepare_downloaded_cover(db:&crate::Database,registry:&crate::storage::AssetRegistry,config:&crate::Config,task:Uuid,info:&DownloadedInfo)->AppResult<Option<PreparedCover>>{
    let Some(raw)=pick_thumbnail_url(&info.value) else {return Ok(None)};
    let mut url=thumbnail_url(&raw)?;
    let settings=crate::youtube_settings_v1::runtime(db,config).await?;
    let ua=std::env::var("YOUTUBE_USER_AGENT").unwrap_or_else(|_| "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36".into());
    let mut builder=reqwest::Client::builder().timeout(Duration::from_secs(30)).redirect(reqwest::redirect::Policy::none()).user_agent(ua);
    if let Some(proxy)=settings.proxy.as_deref(){builder=builder.proxy(reqwest::Proxy::all(proxy).map_err(|_|AppError::Invalid("invalid thumbnail proxy".into()))?);}
    let client=builder.build().map_err(|_|AppError::Unavailable("thumbnail client initialization failed".into()))?;
    let max=std::env::var("YOUTUBE_THUMBNAIL_MAX_BYTES").ok().and_then(|x|x.parse::<u64>().ok()).unwrap_or(20*1024*1024).clamp(1024*1024,50*1024*1024);
    let dir=tempfile::tempdir_in(&registry.root)?;
    let input=dir.path().join("thumbnail.img");let output=dir.path().join("thumbnail.jpg");
    let mut response=None;
    for hop in 0..=5{
        let r=client.get(url.clone()).send().await.map_err(|_|AppError::Unavailable("thumbnail download failed".into()))?;
        if matches!(r.status().as_u16(),301|302|303|307|308){
            if hop==5 {return Err(AppError::Unavailable("thumbnail exceeded redirect limit".into()));}
            let next=r.headers().get(reqwest::header::LOCATION).and_then(|v|v.to_str().ok()).ok_or_else(||AppError::Invalid("thumbnail redirect missing Location".into()))?;
            url=thumbnail_url(url.join(next).map_err(|_|AppError::Invalid("invalid thumbnail redirect".into()))?.as_str())?;
        }else{response=Some(r.error_for_status().map_err(|_|AppError::Unavailable("thumbnail HTTP failure".into()))?);break;}
    }
    let mut response=response.ok_or_else(||AppError::Unavailable("thumbnail unavailable".into()))?;
    if response.content_length().is_some_and(|n|n>max){return Err(AppError::Invalid("thumbnail exceeds size limit".into()));}
    let mut file=fs::File::create(&input).await?;let mut total=0u64;
    while let Some(chunk)=response.chunk().await.map_err(|_|AppError::Unavailable("thumbnail stream failed".into()))?{
        total=total.saturating_add(chunk.len() as u64);if total>max {return Err(AppError::Invalid("thumbnail exceeds size limit".into()));}
        file.write_all(&chunk).await?;
    }
    file.flush().await?;drop(file);
    if total==0 {return Ok(None);}
    let status=timeout(Duration::from_secs(60),Command::new(&config.ffmpeg_binary).args(["-nostdin","-y","-loglevel","error","-i"]).arg(&input)
        .args(["-vf","scale=1146:717:force_original_aspect_ratio=increase,crop=1146:717","-frames:v","1","-q:v","2"]).arg(&output)
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).kill_on_drop(true).status()).await
        .map_err(|_|AppError::Unavailable("thumbnail conversion timed out".into()))??;
    if !status.success(){return Err(AppError::Unavailable("thumbnail JPEG conversion failed".into()));}
    let bytes=fs::read(&output).await?;if bytes.is_empty(){return Ok(None);}
    let sha=hex::encode(Sha256::digest(&bytes));let nonce=Uuid::new_v4().simple().to_string();
    let key=format!("final/{task}/cover_youtube_{}_{}.jpg",&sha[..16],&nonce[..12]);
    let path=registry.resolve_existing(&key).await?;
    if let Some(parent)=path.parent(){fs::create_dir_all(parent).await?;}
    fs::hard_link(&output,&path).await?;
    Ok(Some(PreparedCover{key,path,sha,size:bytes.len() as i64,keep:false}))
}
pub(crate) async fn persist_downloaded_cover(tx:&mut sqlx::Transaction<'_,sqlx::Postgres>,task:Uuid,cover:&PreparedCover)->AppResult<bool>{
    // The original deduplicates by task + cover SHA, not by generated key.
    let existing:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE task_id=$1 AND kind='cover_image'::asset_kind AND sha256=$2)")
        .bind(task).bind(&cover.sha).fetch_one(&mut **tx).await?;
    if existing{return Ok(false)}
    sqlx::query("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days')")
        .bind(&cover.key).bind(&cover.sha).bind(cover.size).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'task',$2,'cover_image') ON CONFLICT DO NOTHING")
        .bind(&cover.key).bind(task).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES($1,$2,'cover_image'::asset_kind,$3,$4,$5)")
        .bind(Uuid::new_v4()).bind(task).bind(&cover.key).bind(&cover.sha).bind(cover.size).execute(&mut **tx).await?;
    Ok(true)
}
/// Read the info JSON produced by the same successful download. Older
/// completed staging directories may predate --write-info-json.
pub(crate) async fn read_downloaded_info(registry: &crate::storage::AssetRegistry, path: &std::path::Path, url: &str) -> AppResult<Option<DownloadedInfo>> {
    let meta = match fs::symlink_metadata(path).await {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    if !meta.is_file() || meta.len() > MAX_INFO as u64 { return Err(AppError::Invalid("downloaded YouTube metadata invalid".into())); }
    let bytes = fs::read(path).await?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| AppError::Invalid("downloaded YouTube metadata is not JSON".into()))?;
    if value.get("id").and_then(Value::as_str) != Some(crate::ingest::youtube_video_id(url)?.as_str()) {
        return Err(AppError::Invalid("downloaded YouTube metadata ID mismatch".into()));
    }
    meta_view(&value, url)?;
    let root = fs::canonicalize(&registry.root).await?;
    let canonical = fs::canonicalize(path).await?;
    let key = canonical.strip_prefix(root).map_err(|_| AppError::Invalid("metadata outside storage root".into()))?.to_string_lossy().into_owned();
    Ok(Some(DownloadedInfo { key, sha:hex::encode(Sha256::digest(&bytes)),size:bytes.len() as i64,value }))
}
/// Match Python snapshot_downloaded_youtube_info and commit alongside raw media.
pub(crate) async fn persist_downloaded_info(tx: &mut sqlx::Transaction<'_,sqlx::Postgres>, task: Uuid, info: &DownloadedInfo, url: &str) -> AppResult<()> {
    let value = &info.value;
    let view = meta_view(value,url)?;
    let uploader = value.get("uploader").and_then(Value::as_str).filter(|s| !s.trim().is_empty()).or_else(|| value.get("channel").and_then(Value::as_str)).map(|s| s.trim().chars().take(256).collect::<String>());
    let date = value.get("upload_date").and_then(Value::as_str).map(|s|s.trim().chars().take(32).collect::<String>());
    let duration = view["duration"].as_u64().and_then(|d|i32::try_from(d).ok());
    sqlx::query("INSERT INTO youtube_video_meta(task_id,source_id,title,description,webpage_url,uploader,upload_date,duration) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(task_id) DO UPDATE SET source_id=EXCLUDED.source_id,title=EXCLUDED.title,description=EXCLUDED.description,webpage_url=EXCLUDED.webpage_url,uploader=EXCLUDED.uploader,upload_date=EXCLUDED.upload_date,duration=EXCLUDED.duration,updated_at=now()")
        .bind(task).bind(value["id"].as_str()).bind(view["title"].as_str().unwrap_or_default()).bind(value["description"].as_str().unwrap_or_default()).bind(view["webpage_url"].as_str().unwrap_or(url)).bind(uploader).bind(date).bind(duration).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days') ON CONFLICT(storage_key) DO NOTHING")
        .bind(&info.key).bind(&info.sha).bind(info.size).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'task',$2,'metadata_json') ON CONFLICT DO NOTHING")
        .bind(&info.key).bind(task).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) SELECT $1,$2,'metadata_json'::asset_kind,$3,$4,$5 WHERE NOT EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND kind='metadata_json'::asset_kind AND storage_key=$3)")
        .bind(Uuid::new_v4()).bind(task).bind(&info.key).bind(&info.sha).bind(info.size).execute(&mut **tx).await?;
    Ok(())
}
pub(crate) async fn latest_info(
    state: &AppState,
    id: Uuid,
) -> AppResult<Option<(Value, AssetInfo)>> {
    let asset = sqlx::query_as::<_, AssetInfo>(
        "SELECT id,task_id,kind::text,storage_key,sha256,size_bytes,duration_ms,created_at
         FROM assets WHERE task_id=$1 AND kind='metadata_json'::asset_kind
         ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    let Some(asset) = asset else { return Ok(None) };
    let file = state.storage.resolve_existing(&asset.storage_key).await?;
    let meta = fs::metadata(&file).await?;
    if meta.len() > MAX_INFO as u64 {
        return Err(AppError::Invalid(
            "metadata asset exceeds read budget".into(),
        ));
    }
    let value = read_info_file(&file).await?;
    Ok(Some((value, asset)))
}
async fn read_metadata(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let task = crate::legacy_contracts::read_visible_task(&state.db, id).await?;
    if task.source_type != "youtube" {
        return Err(AppError::Invalid("not a YouTube task".into()));
    }
    let (value, _) = latest_info(&state, id)
        .await?
        .ok_or_else(|| AppError::NotFound("YouTube metadata not fetched".into()))?;
    Ok(Json(meta_view(
        &value,
        task.source_url.as_deref().unwrap_or(""),
    )?))
}
async fn extract_info(
    config: &crate::Config,
    settings: &crate::youtube_settings_v1::YoutubeRuntime,
    url: &str,
    expected_id: &str,
) -> AppResult<Value> {
    let cookies = crate::youtube_settings_v1::prepare_cookie_copy(config, settings).await?;
    let mut process = Command::new(&config.download_binary);
    process
        .args(crate::download_runtime::original_transport_args())
        .arg("--ignore-config")
        .arg("--no-playlist")
        .arg("--skip-download")
        .arg("--dump-single-json")
        .arg("--no-warnings");
    process.args(crate::download_runtime::original_extractor_args(settings.compatibility,None)?);
    if let Some(proxy) = settings.proxy.as_deref() {
        process.arg("--proxy").arg(proxy);
    }
    if let Some(secret) = cookies.as_ref() {
        process
            .arg("--cookies")
            .arg(secret.path());
    }
    process
        .arg("--")
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = process.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AppError::Unavailable("yt-dlp has no stdout".into()))?;
    let mut reader = stdout.take((MAX_INFO + 1) as u64);
    let mut bytes = Vec::new();
    let read = timeout(Duration::from_secs(90), reader.read_to_end(&mut bytes))
        .await
        .map_err(|_| AppError::Unavailable("yt-dlp metadata extraction timed out".into()))??;
    if read > MAX_INFO {
        let _ = child.kill().await;
        return Err(AppError::Invalid("yt-dlp metadata is too large".into()));
    }
    let result = timeout(Duration::from_secs(30), child.wait())
        .await
        .map_err(|_| AppError::Unavailable("yt-dlp metadata process did not exit".into()))??;
    if !result.success() {
        return Err(AppError::Unavailable(
            "yt-dlp failed to extract metadata".into(),
        ));
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| AppError::Unavailable("yt-dlp returned invalid JSON metadata".into()))?;
    if value.get("id").and_then(Value::as_str) != Some(expected_id) {
        return Err(AppError::Invalid("yt-dlp video ID mismatch".into()));
    }
    meta_view(&value, url)?;
    Ok(value)
}
pub(crate) async fn fetch_metadata(
    State(state): State<AppState>,
    Path(id): Path<Uuid>,
) -> AppResult<Json<Value>> {
    let task = read_task(&state.db, id).await?;
    if task.source_type != "youtube" {
        return Err(AppError::Invalid("not a YouTube task".into()));
    }
    let url = task
        .source_url
        .as_deref()
        .ok_or_else(|| AppError::Invalid("YouTube source URL missing".into()))?;
    let video_id = crate::ingest::youtube_video_id(url)?;
    if let Some((value, asset)) = latest_info(&state, id).await? {
        return Ok(Json(
            json!({"metadata":meta_view(&value,url)?,"metadata_asset":asset}),
        ));
    }
    let settings = crate::youtube_settings_v1::runtime(&state.db, &state.config).await?;
    let value = extract_info(&state.config, &settings, url, &video_id).await?;
    let bytes = serde_json::to_vec(&value)
        .map_err(|_| AppError::Invalid("cannot serialize metadata".into()))?;
    if bytes.len() > MAX_INFO {
        return Err(AppError::Invalid("YouTube metadata too large".into()));
    }
    let sha = hex::encode(Sha256::digest(&bytes));
    let asset_id = Uuid::new_v4();
    let key = format!("v2/youtube-meta/{id}/{asset_id}.json");
    let file = state.storage.resolve_existing(&key).await?;
    let parent = file
        .parent()
        .ok_or_else(|| AppError::Invalid("metadata directory invalid".into()))?;
    fs::create_dir_all(parent).await?;
    let mut written = false;
    let result: AppResult<AssetInfo> = async {
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&file)
            .await?;
        output.write_all(&bytes).await?;
        output.sync_all().await?;
        drop(output);
        let mut tx = state.db.pool.begin().await?;
        // No unauthorized import into a task from the V1 production workload.
        let owned: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1 AND runtime_owner='rust')",
        )
        .bind(id)
        .fetch_one(&mut *tx)
        .await?;
        if !owned {
            return Err(AppError::NotFound("V2 task ownership lost".into()));
        }
        sqlx::query(
            "INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until)
             VALUES($1,$2,$3,'live',now()+interval '2 days')",
        )
        .bind(&key)
        .bind(&sha)
        .bind(bytes.len() as i64)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
             VALUES($1,'task',$2,'metadata_json')",
        )
        .bind(&key)
        .bind(id)
        .execute(&mut *tx)
        .await?;
        let title = value
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        sqlx::query(
            "INSERT INTO app_settings(key,value_json,version)
             VALUES('task.title.'||$1::text,jsonb_build_object('source_title',$2::text),1)
             ON CONFLICT(key) DO UPDATE SET
               value_json=CASE
                  WHEN NULLIF(trim(app_settings.value_json->>'source_title'),'') IS NULL
                  THEN jsonb_set(app_settings.value_json,'{source_title}',to_jsonb($2::text),true)
                  ELSE app_settings.value_json END,
               version=CASE WHEN NULLIF(trim(app_settings.value_json->>'source_title'),'') IS NULL
                    THEN app_settings.version+1 ELSE app_settings.version END,
               updated_at=now()",
        )
        .bind(id)
        .bind(title)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes)
             VALUES($1,$2,'metadata_json'::asset_kind,$3,$4,$5)",
        )
        .bind(asset_id)
        .bind(id)
        .bind(&key)
        .bind(&sha)
        .bind(bytes.len() as i64)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        written = true;
        state.db.asset(asset_id).await
    }
    .await;
    match result {
        Ok(asset) => Ok(Json(
            json!({"metadata":meta_view(&value,url)?,"metadata_asset":asset}),
        )),
        Err(err) => {
            if !written {
                let _ = fs::remove_file(file).await;
            }
            Err(err)
        }
    }
}
/// Original Python durable progress fallback and operation status projection.
/// Read only: never adopt a historical operation merely because a page is opened.
async fn historical_progress(state: &AppState, id: Uuid) -> AppResult<Value> {
    let stored: Option<Value> = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
        .bind(format!("youtube.download_progress.{id}")).fetch_optional(&state.db.pool).await?;
    let mut data=json!({"task_id":id,"status":"idle","active":false,"progress":0,
        "downloaded_bytes":0,"total_bytes":null,"speed_bytes_per_second":null,
        "eta_seconds":null,"filename":null,"error":null,"updated_at":null});
    if let Some(stored)=stored.as_ref().and_then(Value::as_object) {
        for (key,value) in data.as_object_mut().unwrap() {
            if key!="task_id" { if let Some(old)=stored.get(key) { *value=old.clone(); } }
        }
    }
    let row=sqlx::query("SELECT status,lease_until,updated_at,last_error FROM operation_inbox WHERE operation_key=$1")
        .bind(format!("youtube-download:{id}")).fetch_optional(&state.db.pool).await?;
    if let Some(row)=row {
        let status:String=row.try_get("status")?;
        let lease:Option<chrono::DateTime<chrono::Utc>>=row.try_get("lease_until")?;
        let updated:chrono::DateTime<chrono::Utc>=row.try_get("updated_at")?;
        let error:Option<String>=row.try_get("last_error")?;
        match status.as_str() {
            "processing" if lease.is_none_or(|t|t<=chrono::Utc::now()) => {
                data["status"]=json!("queued"); data["active"]=json!(false);
                data["error"]=json!("Download owner lost; awaiting lease recovery");
                data["updated_at"]=json!(updated.to_rfc3339());
            }
            "queued"|"pending" => {
                data["status"]=json!("queued"); data["active"]=json!(false);
                data["error"]=json!(error); data["updated_at"]=json!(updated.to_rfc3339());
            }
            "failed" => { data["status"]=json!("failed"); data["active"]=json!(false); data["error"]=json!(error); }
            "completed" => { data["status"]=json!("completed"); data["active"]=json!(false); data["progress"]=json!(100); data["error"]=Value::Null; }
            "processing" => { data["active"]=json!(true); }
            _ => {}
        }
    }
    Ok(data)
}
async fn progress(State(state): State<AppState>, Path(id): Path<Uuid>) -> AppResult<Json<Value>> {
    // Original endpoint checks the task table, not the Rust ownership-filtered view.
    let exists:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1)")
        .bind(id).fetch_one(&state.db.pool).await?;
    if !exists { return Err(AppError::NotFound("task not found".into())); }
    let row = sqlx::query(
        "SELECT id,state,attempts,last_error,updated_at,lease_until FROM vr2_jobs
         WHERE task_id=$1 AND stage='download'
         ORDER BY generation DESC,created_at DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    if row.is_none() { return Ok(Json(historical_progress(&state,id).await?)); }
    let stored_progress = crate::download_runtime::active_progress(&state.db, id).await?;
    let (status, active, percentage, error, updated, live_metrics) = if let Some(row) = row {
        let state: String = row.try_get("state")?;
        let job_id: Uuid = row.try_get("id")?;
        let lease_until: Option<chrono::DateTime<chrono::Utc>> = row.try_get("lease_until")?;
        let active = state == "running" && lease_until.is_some_and(|l| l > chrono::Utc::now());
        let live_metrics = stored_progress.as_ref().filter(|(id, _, _)| *id == job_id);
        let status = match state.as_str() {
            "pending" => "queued",
            "running" if active && live_metrics.is_some_and(|(_,p,_)|p.percent==99) => "storing",
            "running" if active && live_metrics.is_some_and(|(_,p,_)|p.percent==95) => "processing",
            "running" if active => "downloading",
            "running" => "queued",
            "awaiting" => "processing",
            "succeeded" => "completed",
            "failed" | "canceled" | "unknown" => "failed",
            _ => "idle",
        };
        (
            status,
            active,
            if state == "succeeded" {
                100
            } else if active || state=="failed" {
                live_metrics.map(|(_, p, _)| p.percent).unwrap_or(0)
            } else {
                0
            },
            if state=="succeeded" { None } else { row.try_get::<Option<String>, _>("last_error")? },
            row.try_get::<chrono::DateTime<chrono::Utc>, _>("updated_at")?
                .to_rfc3339(),
            if active || state=="failed" {
                live_metrics.map(|(_, p, _)| p.clone())
            } else {
                None
            },
        )
    } else {
        ("idle", false, 0, None, String::new(), None)
    };
    let asset: Option<(String, Option<i64>)> = sqlx::query_as(
        "SELECT storage_key,size_bytes FROM assets
         WHERE task_id=$1 AND kind='video_raw'::asset_kind
         ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    .bind(id)
    .fetch_optional(&state.db.pool)
    .await?;
    let mut bytes = asset.as_ref().and_then(|(_, size)| *size);
    if status == "completed"
        && bytes.is_none()
        && let Some((key, _)) = asset.as_ref()
    {
        let path = state.storage.resolve_existing(key).await?;
        bytes = Some(fs::metadata(path).await?.len().min(i64::MAX as u64) as i64);
    }
    Ok(Json(json!({
        "task_id":id,"status":status,"active":active,"progress":percentage,
        "downloaded_bytes":if status=="completed"{bytes.unwrap_or(0)}else{live_metrics.as_ref().map(|p|p.downloaded_bytes).unwrap_or(0)},
        "total_bytes":if status=="completed"{bytes}else{live_metrics.as_ref().and_then(|p|p.total_bytes)},
        "speed_bytes_per_second":live_metrics.as_ref().and_then(|p|p.speed_bytes_per_second),
        "eta_seconds":if status=="completed"{Some(0)}else{live_metrics.as_ref().and_then(|p|p.eta_seconds)},
        "filename":if status=="completed"{asset.as_ref().and_then(|(key,_)|key.rsplit('/').next())}else{None},
        "error":error,"updated_at":if updated.is_empty(){None}else{Some(updated)},
    })))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/tasks/{id}/youtube_meta", get(read_metadata))
        .route(
            "/tasks/{id}/actions/youtube_meta",
            axum::routing::post(fetch_metadata),
        )
        .route("/tasks/{id}/youtube_download_progress", get(progress))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_thumbnail_selection_and_allowed_redirect_hosts() {
        let info=json!({"thumbnail":"https://i.ytimg.com/fallback.jpg","thumbnails":[{"url":"https://i.ytimg.com/small.jpg","width":100,"height":100},{"src":"https://i.ytimg.com/big.jpg","width":640,"height":360}]});
        assert_eq!(pick_thumbnail_url(&info).as_deref(),Some("https://i.ytimg.com/big.jpg"));
        assert_eq!(pick_thumbnail_url(&json!({"thumbnail":"https://i.ytimg.com/fallback.jpg"})).as_deref(),Some("https://i.ytimg.com/fallback.jpg"));
        assert!(thumbnail_url("https://i.ytimg.com/a.jpg").is_ok());
        assert!(thumbnail_url("https://ytimg.com.invalid/a.jpg").is_err());
        assert!(thumbnail_url("http://127.0.0.1/a.jpg").is_err());
    }
    #[test]
    fn normalize_yt_info_requires_a_real_video_url_and_title() {
        let info = json!({"id":"0123456789A","title":" A ",
            "webpage_url":"https://youtu.be/0123456789A","duration":19.7});
        let out = meta_view(&info, "https://youtu.be/0123456789A").unwrap();
        assert_eq!(out["title"], "A");
        assert_eq!(out["duration"], 19);
        assert!(meta_view(&json!({"title":"A","webpage_url":"file:///etc/passwd"}), "").is_err());
        assert!(meta_view(&json!({"title":""}), "https://youtu.be/0123456789A").is_err());
    }
}
