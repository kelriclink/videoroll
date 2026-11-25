//! Original V1 "delete finished video" action on the canonical storage schema.
//! This is a metadata transaction and a deferred GC request, NOT an rm call.
//! Never touches old Python-owned tasks or legacy physical media keys.
use crate::{AppError, AppResult, api::AppState, storage::AssetRegistry};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::delete,
};
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

async fn delete_final(
    State(s): State<AppState>,
    Path((task_id, asset_id)): Path<(Uuid, Uuid)>,
) -> AppResult<Json<Value>> {
    let mut tx = s.db.pool.begin().await?;
    let owner: Option<String> =
        sqlx::query_scalar("SELECT runtime_owner FROM tasks WHERE id=$1 FOR UPDATE")
            .bind(task_id)
            .fetch_optional(&mut *tx)
            .await?;
    if owner.as_deref() != Some("rust") {
        return Err(AppError::NotFound("asset not found".into()));
    }
    let asset = sqlx::query(
        "SELECT kind::text AS kind,storage_key FROM assets
         WHERE id=$1 AND task_id=$2 FOR UPDATE",
    )
    .bind(asset_id)
    .bind(task_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(|| AppError::NotFound("asset not found".into()))?;
    let kind: String = asset.try_get("kind")?;
    if kind != "video_final" {
        return Err(AppError::Invalid(
            "only video_final assets can be deleted".into(),
        ));
    }
    let key: String = asset.try_get("storage_key")?;
    AssetRegistry::checked_key(&key)?;
    if !key.starts_with("v2/") {
        return Err(AppError::Conflict(
            "legacy physical files are outside the native deletion lifecycle".into(),
        ));
    }

    // Never delete a source asset while a subtitle/render/dub/publish stage is
    // active or an external post may already have happened (unknown result).
    let protected: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_jobs j WHERE j.task_id=$1
         AND j.state IN ('pending','running','awaiting','unknown'))
         OR EXISTS(SELECT 1 FROM vr2_workflows w WHERE w.task_id=$1
           AND w.state IN ('running','paused','unknown'))
         OR EXISTS(SELECT 1 FROM vr2_render_executions e WHERE e.task_id=$1
           AND (e.state IN ('queued','running','uploading','paused') OR (e.state='canceled' AND e.lease_until>now())))
         OR EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends p WHERE p.task_id=$1
           AND p.state NOT IN ('published','failed'))
         OR EXISTS(SELECT 1 FROM vr2_social_publish_intents p WHERE p.task_id=$1
           AND p.state NOT IN ('published','failed'))
         OR EXISTS(SELECT 1 FROM dubbing_jobs d WHERE d.task_id=$1
           AND d.status IN('queued','preparing','synthesizing','aligning','mixing',
                           'rendering','pause_requested','cancel_requested'))",
    )
    .bind(task_id)
    .fetch_one(&mut *tx)
    .await?;
    if protected {
        return Err(AppError::Conflict(
            "unfinished workflow or uncertain platform operation still needs this asset".into(),
        ));
    }
    // A historic execution may reference the asset by a real FK. Fail closed
    // rather than deleting provenance or exposing an unexpected SQL 500.
    let execution_ref: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM render_executions WHERE uploaded_asset_id=$1)",
    )
    .bind(asset_id)
    .fetch_one(&mut *tx)
    .await?;
    if execution_ref {
        return Err(AppError::Conflict(
            "render execution still references this asset".into(),
        ));
    }
    let link_rows = sqlx::query("DELETE FROM playout_asset_links WHERE asset_id=$1 RETURNING id")
        .bind(asset_id)
        .fetch_all(&mut *tx)
        .await?;
    let link_ids: Vec<Uuid> = link_rows.iter().map(|r| r.get::<Uuid, _>("id")).collect();
    sqlx::query("DELETE FROM subtitles WHERE task_id=$1 AND storage_key=$2")
        .bind(task_id)
        .bind(&key)
        .execute(&mut *tx)
        .await?;
    sqlx::query("DELETE FROM assets WHERE id=$1 AND task_id=$2")
        .bind(asset_id)
        .bind(task_id)
        .execute(&mut *tx)
        .await?;
    // Only release shared object ownership when no other asset still refers
    // to exactly the same storage key. Independent owners keep their holds.
    let other: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM assets WHERE storage_key=$1)")
            .bind(&key)
            .fetch_one(&mut *tx)
            .await?;
    if !other {
        sqlx::query(
            "DELETE FROM storage_object_refs WHERE storage_key=$1 AND (
               (owner_type='task' AND owner_id=$2 AND role='video_final')
               OR (owner_type='workflow' AND owner_id IN(
                  SELECT id FROM pipeline_runs WHERE task_id=$2 AND engine='rust-v2'))
               OR (owner_type='playout' AND owner_id=ANY($3::uuid[]))
             )",
        )
        .bind(&key)
        .bind(task_id)
        .bind(&link_ids)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "UPDATE storage_objects SET retain_until=now(),updated_at=now()
             WHERE storage_key=$1 AND state='live'",
        )
        .bind(&key)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    // The ffplayout destination is intentionally retained: a real playlist
    // may still be playing it. GC removes the source only after refs expire.
    Ok(Json(json!({"deleted":true})))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/tasks/{task_id}/assets/{asset_id}", delete(delete_final))
}
