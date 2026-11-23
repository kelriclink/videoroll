//! Python-compatible /subtitle/models/upload.
//! Rust owns multipart streaming, the 20 GiB wire limit, destination creation,
//! and cleanup. ZIP64/deflate/BZip2/LZMA/CRC extraction now uses native Rust
//! zip decoding with staging and size limits; no Python subprocess is needed.
use crate::{AppError, AppResult, api::AppState, subtitle_models_v1};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Multipart, Query},
    routing::post,
};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::{fs, io::AsyncWriteExt};
use uuid::Uuid;

const MAX_ZIP: u64 = 20 * 1024 * 1024 * 1024;
#[derive(Deserialize)]
struct Params {
    name: String,
}
async fn upload(Query(params): Query<Params>, mut multipart: Multipart) -> AppResult<Json<Value>> {
    let name = params.name.trim();
    if !subtitle_models_v1::name_is_valid(name) {
        return Err(AppError::Invalid("invalid model name".into()));
    }
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        return Err(AppError::Conflict("V1 model upload disabled".into()));
    }
    let mut field = multipart
        .next_field()
        .await
        .map_err(|_| AppError::Invalid("malformed model multipart".into()))?
        .ok_or_else(|| AppError::Invalid("model upload file is required".into()))?;
    if field.name() != Some("file") {
        return Err(AppError::Invalid("multipart field must be file".into()));
    }
    let filename = field.file_name().unwrap_or("");
    if !filename.to_ascii_lowercase().ends_with(".zip") {
        return Err(AppError::Invalid("only .zip is supported".into()));
    }
    let root = subtitle_models_v1::model_dir();
    fs::create_dir_all(&root).await?;
    let dest = root.join(name);
    if fs::try_exists(&dest).await? {
        return Err(AppError::Invalid(
            "model already exists; delete it first".into(),
        ));
    }
    let temporary = root.join(format!(".model-upload-{}.zip", Uuid::new_v4()));
    let result = async {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .await?;
        let mut total = 0u64;
        while let Some(chunk) = field
            .chunk()
            .await
            .map_err(|_| AppError::Invalid("malformed ZIP upload chunk".into()))?
        {
            total = total
                .checked_add(chunk.len() as u64)
                .ok_or_else(|| AppError::TooLarge("model zip too large".into()))?;
            if total > MAX_ZIP {
                return Err(AppError::TooLarge(format!(
                    "model zip too large: max {MAX_ZIP} bytes"
                )));
            }
            file.write_all(&chunk).await?;
        }
        file.flush().await?;
        file.sync_all().await?;
        drop(file);
        let data = tokio::task::spawn_blocking({
            let dest = dest.clone();
            let temporary = temporary.clone();
            let name = name.to_owned();
            move || -> AppResult<Value> {
                crate::subtitle_model_zip_v1::extract(&temporary, &dest)?;
                Ok(json!({"name":name,"path":dest.to_string_lossy(),
                          "size_bytes":subtitle_models_v1::dir_bytes(&dest)}))
            }
        })
        .await
        .map_err(|_| AppError::Unavailable("model ZIP extraction interrupted".into()))??;
        Ok(Json(data))
    }
    .await;
    let _ = fs::remove_file(&temporary).await;
    result
}
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/subtitle/models/upload",
        post(upload).layer(DefaultBodyLimit::disable()),
    )
}
