//! Original Python subtitle embedding model inventory and vector runtime diagnostics.
//! Uses the original subtitle.translate settings, not a V2-only shadow registry.
use crate::{
    AppError, AppResult, api::AppState, subtitle_models_v1, subtitle_translate_settings_v1,
};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::Row;
use std::{
    fs,
    path::{Path as FilePath, PathBuf},
};

#[derive(Deserialize)]
struct ModelDirRequest {
    model_dir: Option<String>,
}
fn default_model() -> String {
    "BAAI/bge-small-zh-v1.5".into()
}
#[derive(Deserialize)]
struct DownloadRequest {
    #[serde(default = "default_model")]
    model: String,
    name: Option<String>,
    model_dir: Option<String>,
    revision: Option<String>,
    #[serde(default)]
    force: bool,
}

fn directory(cfg: &Value, supplied: Option<&str>) -> PathBuf {
    let from_settings = cfg.get("rag_embedding_model_dir").and_then(Value::as_str);
    let selected = supplied
        .filter(|s| !s.trim().is_empty())
        .or_else(|| from_settings.filter(|s| !s.trim().is_empty()))
        .unwrap_or("/models/embeddings");
    PathBuf::from(selected)
}
async fn config(state: &AppState) -> AppResult<Value> {
    let raw: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key='subtitle.translate'")
            .fetch_optional(&state.db.pool)
            .await?;
    Ok(subtitle_translate_settings_v1::view(
        &raw.unwrap_or(Value::Null),
    ))
}
fn list_dir(root: &FilePath) -> AppResult<Vec<Value>> {
    if !root.exists() {
        return Ok(Vec::new());
    }
    let mut rows = Vec::new();
    for row in fs::read_dir(root)? {
        let row = row?;
        // Avoid following directory symlinks into arbitrary model trees.
        if !row.file_type()?.is_dir() {
            continue;
        }
        let path = row.path();
        rows.push(json!({"name":row.file_name().to_string_lossy(),
            "path":path.to_string_lossy(),"size_bytes":subtitle_models_v1::dir_bytes(&path)}));
    }
    rows.sort_by_key(|v| v["name"].as_str().unwrap_or("").to_lowercase());
    Ok(rows)
}
async fn list_models(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let root = directory(&config(&state).await?, None);
    let rows = tokio::task::spawn_blocking(move || list_dir(&root))
        .await
        .map_err(|_| AppError::Unavailable("embedding model listing interrupted".into()))??;
    Ok(Json(json!(rows)))
}
async fn list_models_for_dir(
    State(state): State<AppState>,
    Json(body): Json<ModelDirRequest>,
) -> AppResult<Json<Value>> {
    let root = directory(&config(&state).await?, body.model_dir.as_deref());
    let rows = tokio::task::spawn_blocking(move || list_dir(&root))
        .await
        .map_err(|_| AppError::Unavailable("embedding model listing interrupted".into()))??;
    Ok(Json(json!(rows)))
}
async fn download_model(
    State(state): State<AppState>,
    Json(body): Json<DownloadRequest>,
) -> AppResult<Json<Value>> {
    let root = directory(&config(&state).await?, body.model_dir.as_deref());
    crate::subtitle_model_download_v1::embedding_snapshot(
        state,
        body.model,
        body.name,
        body.revision,
        body.force,
        root,
    )
    .await
}
fn writes_enabled() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("1" | "true")
    )
}
async fn delete_model(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> AppResult<Json<Value>> {
    if !writes_enabled() {
        return Err(AppError::Conflict(
            "V1 embedding model deletion disabled".into(),
        ));
    }
    let name = name.trim();
    if !subtitle_models_v1::name_is_valid(name) {
        return Err(AppError::Invalid(
            "invalid model name (allowed: [A-Za-z0-9._-], max 64 chars)".into(),
        ));
    }
    let root = directory(&config(&state).await?, None);
    let name = name.to_string();
    tokio::task::spawn_blocking(move || -> AppResult<()> {
        let candidate = root.join(&name);
        let metadata = match fs::symlink_metadata(&candidate) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(AppError::Invalid(
                "embedding model must be a real directory".into(),
            ));
        }
        let base = fs::canonicalize(&root)?;
        let path = fs::canonicalize(&candidate)?;
        if path.parent() != Some(base.as_path()) {
            return Err(AppError::Invalid(
                "model path escapes configured directory".into(),
            ));
        }
        fs::remove_dir_all(candidate)?;
        Ok(())
    })
    .await
    .map_err(|_| AppError::Unavailable("embedding model deletion interrupted".into()))??;
    Ok(Json(json!({"deleted":true})))
}
async fn runtime(State(state): State<AppState>) -> AppResult<Json<Value>> {
    let cfg = config(&state).await?;
    let provider = cfg["rag_embedding_provider"].as_str().unwrap_or("").trim();
    let model = cfg["rag_embedding_model"].as_str().unwrap_or("").trim();
    let device = cfg["rag_embedding_device"].as_str().unwrap_or("").trim();
    let dims = cfg["rag_embedding_dimensions"].as_i64().unwrap_or(1).max(1);
    let mut response = json!({"provider":provider,"model":model,"device":device,
        "configured_dimensions":dims,"supported":false,"pgvector_version":"",
        "column_type":"","total_embeddings":0,"active_embeddings":0,"buckets":[],
        "hnsw_index_present":false,"hnsw_usable_for_current_query":false,
        "search_mode":"unavailable","detail":""});
    let result: Result<Value, sqlx::Error> = async {
        let version: String = sqlx::query_scalar(
            "SELECT COALESCE((SELECT extversion FROM pg_extension WHERE extname='vector'),'')",
        )
        .fetch_one(&state.db.pool)
        .await?;
        let column: Option<String> = sqlx::query_scalar(
            "SELECT format_type(a.atttypid,a.atttypmod) FROM pg_attribute a
             JOIN pg_class t ON t.oid=a.attrelid
             JOIN pg_namespace n ON n.oid=t.relnamespace
             WHERE n.nspname=current_schema() AND t.relname='translation_knowledge_items'
               AND a.attname='embedding' AND NOT a.attisdropped",
        )
        .fetch_optional(&state.db.pool)
        .await?;
        let column = column.unwrap_or_default();
        if version.is_empty() {
            return Ok(json!({"pgvector_version":"","column_type":column,
                "detail":"pgvector extension is not installed"}));
        }
        let rows = sqlx::query(
            "SELECT COALESCE(embedding_model,'') AS embedding_model,
             vector_dims(embedding) AS dimensions, COUNT(*)::bigint AS count
             FROM translation_knowledge_items WHERE embedding IS NOT NULL
             GROUP BY 1,2 ORDER BY count DESC,embedding_model,dimensions",
        )
        .fetch_all(&state.db.pool)
        .await?;
        let (mut buckets, mut total, mut active) = (Vec::<Value>::new(), 0i64, 0i64);
        for row in rows {
            let m: String = row.try_get("embedding_model")?;
            let d: i32 = row.try_get("dimensions")?;
            let n: i64 = row.try_get("count")?;
            total += n;
            if d as i64 == dims
                && (model.is_empty() || m == model || m.ends_with(&format!(":{model}")))
            {
                active += n;
            }
            buckets.push(json!({"embedding_model":m,"dimensions":d,"count":n}));
        }
        let hnsw: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname=current_schema()
             AND tablename='translation_knowledge_items'
             AND lower(indexdef) LIKE '% using hnsw %')",
        )
        .fetch_one(&state.db.pool)
        .await?;
        let usable = hnsw && column.to_lowercase() == format!("vector({dims})");
        let detail = if usable {
            ""
        } else if column.eq_ignore_ascii_case("vector") {
            "embedding column has variable dimensions; current raw-vector HNSW query is unavailable"
        } else if hnsw {
            "HNSW index exists but does not match the configured vector dimension/query shape"
        } else {
            "no usable HNSW index for the current embedding query"
        };
        Ok(json!({"pgvector_version":version,"column_type":column,
            "supported":true,"total_embeddings":total,"active_embeddings":active,
            "buckets":buckets,"hnsw_index_present":hnsw,
            "hnsw_usable_for_current_query":usable,
            "search_mode":if usable {"hnsw"} else {"exact_scan"},"detail":detail}))
    }
    .await;
    match result {
        Ok(extra) => {
            if let (Some(a), Some(b)) = (response.as_object_mut(), extra.as_object()) {
                a.extend(b.iter().map(|(k, v)| (k.clone(), v.clone())));
            }
        }
        Err(e) => {
            tracing::warn!(error=%e,"embedding runtime diagnostics unavailable");
            response["detail"] = json!(format!("{}: {}", std::any::type_name::<sqlx::Error>(), e));
        }
    }
    Ok(Json(response))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/embedding/models", get(list_models))
        .route("/subtitle/embedding/models/list", post(list_models_for_dir))
        .route("/subtitle/embedding/models/download", post(download_model))
        .route(
            "/subtitle/embedding/models/{name}",
            axum::routing::delete(delete_model),
        )
        .route("/subtitle/embedding/runtime", get(runtime))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inventory() {
        let t = tempfile::tempdir().unwrap();
        fs::create_dir(t.path().join("B")).unwrap();
        fs::create_dir(t.path().join("a")).unwrap();
        fs::write(t.path().join("B/weights.bin"), [0u8; 7]).unwrap();
        let rows = list_dir(t.path()).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["name"], "a");
        assert_eq!(rows[1]["size_bytes"], 7);
    }
    #[test]
    fn settings_override() {
        assert_eq!(
            directory(&json!({"rag_embedding_model_dir":"/tmp/models"}), None),
            PathBuf::from("/tmp/models")
        );
        assert_eq!(
            directory(&json!({}), None),
            PathBuf::from("/models/embeddings")
        );
    }
}
