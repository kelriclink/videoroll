//! Python /subtitle/models GET and /subtitle/models/{name} DELETE.
//! Uses the same SUBTITLE_WHISPER_MODEL_DIR as the original subtitle service,
//! not a shadow registry. List returns actual on-disk bytes for model folders.
use crate::{AppError, AppResult, api::AppState};
use axum::{Json, Router, extract::Path, routing::get};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path as FilePath, PathBuf},
};
pub(crate) fn model_dir() -> PathBuf {
    std::env::var_os("SUBTITLE_WHISPER_MODEL_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/models/whisper"))
}
pub(crate) fn name_is_valid(name: &str) -> bool {
    let name = name.trim();
    (1..=64).contains(&name.len())
        && name
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}
pub(crate) fn dir_bytes(path: &FilePath) -> u64 {
    let Ok(entries) = fs::read_dir(path) else {
        return 0;
    };
    let mut total = 0u64;
    for row in entries.flatten() {
        let Ok(meta) = row.metadata() else {
            continue;
        };
        if meta.is_file() {
            total = total.saturating_add(meta.len());
        } else if meta.is_dir() && !row.path().is_symlink() {
            total = total.saturating_add(dir_bytes(&row.path()));
        }
    }
    total
}
async fn list_models() -> AppResult<Json<Value>> {
    let root = model_dir();
    let models = tokio::task::spawn_blocking(move || -> AppResult<Vec<Value>> {
        fs::create_dir_all(&root)?;
        let mut models = Vec::<Value>::new();
        for row in fs::read_dir(&root)? {
            let row = row?;
            let name = row.file_name().to_string_lossy().to_string();
            let metadata = row.file_type()?;
            if name.starts_with('.') || !metadata.is_dir() {
                continue;
            }
            let path = row.path();
            models.push(json!({"name":name,"path":path.to_string_lossy(),
                "size_bytes":dir_bytes(&path)}));
        }
        models.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        Ok(models)
    })
    .await
    .map_err(|_| AppError::Unavailable("model listing interrupted".into()))??;
    Ok(Json(json!(models)))
}
async fn remove_model(Path(name): Path<String>) -> AppResult<Json<Value>> {
    let name = name.trim().to_string();
    if !name_is_valid(&name) {
        return Err(AppError::Invalid("invalid model name".into()));
    }
    let root = model_dir();
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    ) {
        return Err(AppError::Conflict("V1 model deletion disabled".into()));
    }
    tokio::task::spawn_blocking(move || -> AppResult<()> {
        let root = fs::canonicalize(&root)?;
        let path = root.join(&name);
        let meta = fs::symlink_metadata(&path).map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                AppError::NotFound("model not found".into())
            } else {
                e.into()
            }
        })?;
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(AppError::Invalid("model must be a real directory".into()));
        }
        fs::remove_dir_all(&path)?;
        Ok(())
    })
    .await
    .map_err(|_| AppError::Unavailable("model deletion interrupted".into()))??;
    Ok(Json(json!({"deleted":true})))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/models", get(list_models))
        .route(
            "/subtitle/models/{name}",
            axum::routing::delete(remove_model),
        )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accepted_names() {
        assert!(name_is_valid("large-v3-turbo"));
        assert!(name_is_valid("openvino.whisper"));
        assert!(!name_is_valid("../x"));
        assert!(!name_is_valid(".."));
        assert!(!name_is_valid("/etc"));
    }
}
