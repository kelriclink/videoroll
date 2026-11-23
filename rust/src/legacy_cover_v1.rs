//! Original /tasks/{task_id}/upload/cover, implemented without PIL or
//! forwarding writes to Python. Uploaded bytes are bounded and verified with
//! ffprobe, then canonicalized in a supervised FFmpeg subprocess before any
//! durable V1 asset or V2 ownership row is committed.
use crate::{AppError, AppResult, api::AppState, domain::AssetInfo};
use axum::{
    Json,
    extract::{Multipart, Path, State},
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{path::Path as FilePath, process::Stdio, time::Duration};
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    time::timeout,
};
use uuid::Uuid;

const MAX_BYTES: u64 = 50 * 1024 * 1024;
fn detect(bytes: &[u8]) -> Option<(&'static str, &'static str)> {
    if bytes.starts_with(&[0xFF, 0xD8, 0xFF]) {
        return Some(("jpg", "mjpeg"));
    }
    if bytes.starts_with(&[137, 80, 78, 71, 13, 10, 26, 10]) {
        return Some(("png", "png"));
    }
    if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        return Some(("webp", "libwebp"));
    }
    None
}
async fn canonicalize(
    ffprobe: &FilePath,
    ffmpeg: &FilePath,
    input: &FilePath,
    output: &FilePath,
    codec: &str,
) -> AppResult<()> {
    let probe = timeout(
        Duration::from_secs(15),
        Command::new(ffprobe)
            .args([
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,codec_name",
                "-of",
                "json",
            ])
            .arg(input)
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| AppError::Unavailable("cover image probe timed out".into()))??;
    if !probe.status.success() || probe.stdout.len() > 64 * 1024 {
        return Err(AppError::Invalid("invalid cover image".into()));
    }
    let probe: Value = serde_json::from_slice(&probe.stdout)
        .map_err(|_| AppError::Invalid("invalid cover image metadata".into()))?;
    let stream = probe["streams"]
        .as_array()
        .and_then(|s| s.first())
        .ok_or_else(|| AppError::Invalid("cover image stream missing".into()))?;
    let width = stream["width"].as_u64().unwrap_or(0);
    let height = stream["height"].as_u64().unwrap_or(0);
    if width == 0
        || height == 0
        || width > 8192
        || height > 8192
        || width.saturating_mul(height) > 16_000_000
    {
        return Err(AppError::TooLarge(
            "cover image dimensions are too large".into(),
        ));
    }
    let expected = match codec {
        "mjpeg" => "mjpeg",
        "png" => "png",
        "libwebp" => "webp",
        _ => return Err(AppError::Invalid("unsupported cover codec".into())),
    };
    if stream["codec_name"].as_str() != Some(expected) {
        return Err(AppError::Invalid(
            "cover must decode as JPEG, PNG, or WebP".into(),
        ));
    }
    let mut cmd = Command::new(ffmpeg);
    cmd.args([
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-i",
    ])
    .arg(input)
    .args(["-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn"]);
    match codec {
        "mjpeg" => {
            cmd.args(["-pix_fmt", "yuvj420p", "-q:v", "2"]);
        }
        "png" => {
            cmd.args(["-pix_fmt", "rgba"]);
        }
        "libwebp" => {
            cmd.args(["-lossless", "1"]);
        }
        _ => {}
    }
    let render = timeout(
        Duration::from_secs(65),
        cmd.args(["-c:v", codec, "-f", "image2", "-update", "1"])
            .arg(output)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| AppError::Unavailable("cover image canonicalization timed out".into()))??;
    if !render.status.success() {
        return Err(AppError::Invalid(
            "invalid cover image or unavailable encoder".into(),
        ));
    }
    let meta = fs::metadata(output).await?;
    if meta.len() == 0 || meta.len() > MAX_BYTES {
        return Err(AppError::TooLarge(
            "canonical cover exceeds size limit".into(),
        ));
    }
    Ok(())
}
pub(crate) async fn upload(
    State(state): State<AppState>,
    Path(task_id): Path<Uuid>,
    mut multipart: Multipart,
) -> AppResult<Json<AssetInfo>> {
    crate::legacy_contracts::read_task(&state.db, task_id).await?;
    let field = multipart
        .next_field()
        .await
        .map_err(|_| AppError::Invalid("malformed cover multipart".into()))?
        .ok_or_else(|| AppError::Invalid("upload requires a file field".into()))?;
    if field.name() != Some("file") {
        return Err(AppError::Invalid(
            "expected multipart field named file".into(),
        ));
    }
    let directory = format!("v2/artifacts/{task_id}");
    let root = state.storage.resolve_existing(&directory).await?;
    fs::create_dir_all(&root).await?;
    let id = Uuid::new_v4();
    let input = root.join(format!("{id}.cover-uploading"));
    let mut uploaded = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&input)
        .await?;
    let result = async {
        let mut stream = field;
        let mut size = 0_u64;
        while let Some(chunk) = stream
            .chunk()
            .await
            .map_err(|_| AppError::Invalid("malformed cover chunk".into()))?
        {
            size = size
                .checked_add(chunk.len() as u64)
                .ok_or_else(|| AppError::TooLarge("cover image size overflow".into()))?;
            if size > MAX_BYTES {
                return Err(AppError::TooLarge("cover image upload is too large".into()));
            }
            uploaded.write_all(&chunk).await?;
        }
        if size == 0 {
            return Err(AppError::Invalid("cover image is empty".into()));
        }
        uploaded.flush().await?;
        uploaded.sync_all().await?;
        drop(uploaded);
        let mut header = [0u8; 12];
        let mut raw = fs::File::open(&input).await?;
        let len = raw.read(&mut header).await?;
        let (ext, codec) = detect(&header[..len])
            .ok_or_else(|| AppError::Invalid("cover must decode as JPEG, PNG, or WebP".into()))?;
        let key = format!("{directory}/{id}.{ext}");
        let output = state.storage.resolve_existing(&key).await?;
        let staging = root.join(format!("{id}.canonical.{ext}"));
        let mut committed = false;
        let result =
            async {
                canonicalize(
                    &state.config.ffprobe_binary,
                    &state.config.ffmpeg_binary,
                    &input,
                    &staging,
                    codec,
                )
                .await?;
                fs::rename(&staging, &output).await?;
                let mut file = fs::File::open(&output).await?;
                let mut digest = Sha256::new();
                let mut bytes = 0i64;
                let mut buf = vec![0u8; 64 * 1024];
                loop {
                    let n = file.read(&mut buf).await?;
                    if n == 0 {
                        break;
                    }
                    bytes += n as i64;
                    digest.update(&buf[..n]);
                }
                let sha = hex::encode(digest.finalize());
                let mut tx = state.db.pool.begin().await?;
                let status: Option<String> =
                    sqlx::query_scalar("SELECT status::text FROM tasks WHERE id=$1 FOR UPDATE")
                        .bind(task_id)
                        .fetch_optional(&mut *tx)
                        .await?;
                if status.is_none() {
                    return Err(AppError::NotFound(
                        "task was removed during cover upload".into(),
                    ));
                }
                let owned: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1 AND runtime_owner='rust')",
                )
                .bind(task_id)
                .fetch_one(&mut *tx)
                .await?;
                if !owned {
                    return Err(AppError::NotFound("V2 task ownership was removed".into()));
                }
                sqlx::query(
                "INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until)
                 VALUES($1,$2,$3,'live',now()+interval '2 days')")
                .bind(&key).bind(&sha).bind(bytes).execute(&mut *tx).await?;
                sqlx::query(
                    "INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role)
                 VALUES($1,'task',$2,'cover_image')",
                )
                .bind(&key)
                .bind(task_id)
                .execute(&mut *tx)
                .await?;
                sqlx::query(
                    "INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes)
                 VALUES($1,$2,'cover_image'::asset_kind,$3,$4,$5)",
                )
                .bind(id)
                .bind(task_id)
                .bind(&key)
                .bind(&sha)
                .bind(bytes)
                .execute(&mut *tx)
                .await?;
                tx.commit().await?;
                committed = true;
                state.db.asset(id).await
            }
            .await;
        let _ = fs::remove_file(staging).await;
        if result.is_err() && !committed {
            let _ = fs::remove_file(output).await;
        }
        result
    }
    .await;
    let _ = fs::remove_file(&input).await;
    result.map(Json)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn magic_does_not_trust_extension() {
        assert_eq!(detect(&[0xff, 0xd8, 0xff, 0x10]), Some(("jpg", "mjpeg")));
        assert_eq!(
            detect(&[137, 80, 78, 71, 13, 10, 26, 10]),
            Some(("png", "png"))
        );
        assert_eq!(detect(b"RIFFabcdWEBP"), Some(("webp", "libwebp")));
        assert!(detect(b"hello.exe").is_none());
    }
}
