//! Recover original Python task-scoped, content-addressed subtitle snapshots.
//! No legacy worker is executed. The exact old request and media digest are
//! frozen in the native generation; unproven or changed artifacts fail closed.
use crate::{
    AppError, AppResult, Database,
    storage::AssetRegistry,
    subtitles::{SubtitleCue, validate},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::Row;
use tokio::fs;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct Artifact {
    pub key: String,
    pub sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct Plan {
    pub original_job: Uuid,
    pub video: Artifact,
    pub source: Artifact,
    pub final_segments: Option<Artifact>,
    pub original_translated: bool,
}
fn conflict(s: &str) -> AppError {
    AppError::Conflict(s.into())
}
pub(crate) async fn asset_digest(
    db: &Database,
    registry: &AssetRegistry,
    task: Uuid,
    kind: &str,
    key: &str,
) -> AppResult<Artifact> {
    AssetRegistry::checked_key(key)?;
    let row=sqlx::query("SELECT sha256,size_bytes FROM assets WHERE task_id=$1 AND kind::text=$2 AND storage_key=$3 ORDER BY created_at DESC,id DESC LIMIT 1")
      .bind(task).bind(kind).bind(key).fetch_optional(&db.pool).await?.ok_or_else(||conflict("resume asset ownership missing"))?;
    let sha: Option<String> = row.try_get("sha256")?;
    let size: Option<i64> = row.try_get("size_bytes")?;
    let path = registry.resolve_existing(key).await?;
    if !fs::metadata(&path)
        .await
        .map(|m| m.is_file())
        .unwrap_or(false)
    {
        return Err(conflict("resume asset missing"));
    }
    let (actual, n) = registry.sha256(key).await?;
    if sha.as_deref() != Some(actual.as_str()) || size != Some(n) || n <= 0 {
        return Err(conflict(
            "resume asset differs from original recorded digest or size",
        ));
    }
    Ok(Artifact {
        key: key.into(),
        sha256: actual,
    })
}
async fn snapshot(
    registry: &AssetRegistry,
    task: Uuid,
    key: &str,
    role: &str,
) -> AppResult<(Artifact, Vec<SubtitleCue>)> {
    let prefix = format!("sub/{task}/{role}_");
    let tail = key
        .strip_prefix(&prefix)
        .and_then(|v| v.strip_suffix(".json"))
        .ok_or_else(|| conflict("legacy snapshot is not in the original task namespace"))?;
    let (hash, suffix) = tail
        .split_once('_')
        .ok_or_else(|| conflict("legacy snapshot name missing content digest"))?;
    if hash.len() != 16
        || suffix.len() != 12
        || !hash
            .bytes()
            .chain(suffix.bytes())
            .all(|c| c.is_ascii_hexdigit())
    {
        return Err(conflict("invalid original snapshot name"));
    }
    let p = registry.resolve_existing(key).await?;
    let m = fs::metadata(&p)
        .await
        .map_err(|_| conflict("legacy snapshot missing"))?;
    if !m.is_file() || m.len() == 0 || m.len() > 8 * 1024 * 1024 {
        return Err(conflict("legacy snapshot empty or oversized"));
    }
    let bytes = fs::read(&p).await?;
    if bytes.len() > 8 * 1024 * 1024 {
        return Err(conflict("legacy snapshot exceeded read limit"));
    }
    let digest = hex::encode(Sha256::digest(&bytes));
    if !digest.starts_with(hash) {
        return Err(conflict("legacy snapshot content digest mismatch"));
    }
    let rows: Vec<Value> =
        serde_json::from_slice(&bytes).map_err(|_| conflict("invalid legacy subtitle JSON"))?;
    if rows.is_empty() || rows.len() > 100_000 {
        return Err(conflict("legacy snapshot cue count invalid"));
    }
    let mut cues = Vec::with_capacity(rows.len());
    for row in rows {
        let stamp = |name: &str| {
            row[name]
                .as_f64()
                .filter(|v| v.is_finite() && *v >= 0.0 && *v <= 48. * 3600.)
                .map(|v| (v * 1000.).round() as u64)
                .ok_or_else(|| conflict("invalid legacy cue time"))
        };
        let text = row["text"]
            .as_str()
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| conflict("empty legacy subtitle cue"))?;
        let secondary = row["secondary_text"]
            .as_str()
            .filter(|s| !s.trim().is_empty())
            .map(str::to_owned);
        if secondary.as_ref().is_some_and(|v| v.len() > 20_000) {
            return Err(conflict("legacy secondary cue too long"));
        }
        cues.push(SubtitleCue {
            start_ms: stamp("start")?,
            end_ms: stamp("end")?,
            text: text.into(),
            secondary_text: secondary,
            confidence: row["confidence"].as_f64().filter(|v|v.is_finite() && (0.0..=1.0).contains(v)),
        });
    }
    validate(&cues)?;
    if cues.windows(2).any(|w| w[0].start_ms > w[1].start_ms) {
        return Err(conflict("legacy subtitle timeline is not ordered"));
    }
    Ok((
        Artifact {
            key: key.into(),
            sha256: digest,
        },
        cues,
    ))
}
pub(crate) async fn prepare(
    db: &Database,
    registry: &AssetRegistry,
    task: Uuid,
    video: &str,
    source: Option<&str>,
    final_key: Option<&str>,
) -> AppResult<Option<Plan>> {
    // Only one actual historical request may provide the requested artifacts.
    let rows=sqlx::query("SELECT id,request_json FROM subtitle_jobs WHERE task_id=$1 AND status::text IN ('failed','succeeded') AND request_json#>>'{input,key}'=$2 AND NULLIF(request_json#>>'{artifacts,source_segments_key}','') IS NOT NULL AND ($3::text IS NULL OR request_json#>>'{artifacts,source_segments_key}'=$3) AND ($4::text IS NULL OR request_json#>>'{artifacts,final_subtitle_segments_key}'=$4) ORDER BY created_at DESC,id DESC LIMIT 1")
        .bind(task).bind(video).bind(source).bind(final_key).fetch_optional(&db.pool).await?;
    let Some(row) = rows else {
        if source.is_some() || final_key.is_some() {
            return Err(conflict(
                "no original subtitle request proves the selected artifacts and raw video",
            ));
        }
        return Ok(None);
    };
    let request: Value = row.try_get("request_json")?;
    let source_key = request["artifacts"]["source_segments_key"]
        .as_str()
        .ok_or_else(|| conflict("source artifact missing"))?;
    let implicit = source.is_none() && final_key.is_none();
    let video = asset_digest(db, registry, task, "video_raw", video).await?;
    let source = asset_digest(db, registry, task, "segments_json", source_key).await?;
    let (actual, _) = snapshot(registry, task, source_key, "segments").await?;
    if source != actual {
        return Err(conflict("source snapshot changed"));
    }
    // Explicit source-only requests must not silently use a final snapshot.
    let selected_final = if implicit {
        request["artifacts"]["final_subtitle_segments_key"]
            .as_str()
            .filter(|v| !v.is_empty())
    } else {
        final_key
    };
    let final_segments = match selected_final {
        Some(k) => Some(snapshot(registry, task, k, "subtitle_segments").await?.0),
        None => None,
    };
    Ok(Some(Plan {
        original_job: row.try_get("id")?,
        video,
        source,
        final_segments,
        original_translated: request["translate"]["enabled"].as_bool().unwrap_or(false),
    }))
}
pub(crate) async fn load(
    db: &Database,
    registry: &AssetRegistry,
    task: Uuid,
    plan: &Plan,
) -> AppResult<(Vec<SubtitleCue>, Option<Vec<SubtitleCue>>)> {
    let current:Option<String>=sqlx::query_scalar("SELECT storage_key FROM assets WHERE task_id=$1 AND kind='video_raw' ORDER BY created_at DESC,id DESC LIMIT 1").bind(task).fetch_optional(&db.pool).await?;
    if current.as_deref() != Some(plan.video.key.as_str()) {
        return Err(conflict("raw video changed after resume admission"));
    }
    let old:Option<Value>=sqlx::query_scalar("SELECT request_json FROM subtitle_jobs WHERE id=$1 AND task_id=$2 AND status::text IN ('failed','succeeded')").bind(plan.original_job).bind(task).fetch_optional(&db.pool).await?;
    let old = old.ok_or_else(|| conflict("original subtitle job no longer eligible"))?;
    if old["input"]["key"].as_str() != Some(plan.video.key.as_str())
        || old["artifacts"]["source_segments_key"].as_str() != Some(plan.source.key.as_str())
        || plan.final_segments.as_ref().is_some_and(|v| {
            old["artifacts"]["final_subtitle_segments_key"].as_str() != Some(v.key.as_str())
        })
    {
        return Err(conflict("original subtitle request changed"));
    }
    if asset_digest(db, registry, task, "video_raw", &plan.video.key).await? != plan.video
        || asset_digest(db, registry, task, "segments_json", &plan.source.key).await? != plan.source
    {
        return Err(conflict("original resume asset changed"));
    }
    let (a, source) = snapshot(registry, task, &plan.source.key, "segments").await?;
    if a != plan.source {
        return Err(conflict("source snapshot changed after admission"));
    }
    let final_cues = if let Some(expected) = &plan.final_segments {
        let (a, cues) = snapshot(registry, task, &expected.key, "subtitle_segments").await?;
        if &a != expected {
            return Err(conflict("final snapshot changed after admission"));
        }
        Some(cues)
    } else {
        None
    };
    Ok((source, final_cues))
}

// Use the successful subtitle stage of this generation, not whichever
// subtitle asset happens to be newest at render time.
pub(crate) async fn render_inputs(
    db: &Database,
    registry: &AssetRegistry,
    task: Uuid,
    workflow: Uuid,
) -> AppResult<Option<(String, Option<String>, String)>> {
    let plan:Option<Value>=sqlx::query_scalar("SELECT configuration->'subtitle_resume_plan' FROM vr2_workflows WHERE id=$1 AND task_id=$2").bind(workflow).bind(task).fetch_optional(&db.pool).await?.flatten();
    let Some(plan) = plan else { return Ok(None) };
    let plan: Plan =
        serde_json::from_value(plan).map_err(|_| conflict("invalid resume render plan"))?;
    let row=sqlx::query("SELECT id,result FROM vr2_jobs WHERE workflow_id=$1 AND task_id=$2 AND stage='subtitle' AND state='succeeded'").bind(workflow).bind(task).fetch_optional(&db.pool).await?.ok_or_else(||conflict("resume render has no successful subtitle stage"))?;
    let job: Uuid = row.try_get("id")?;
    let result: Value = row.try_get("result")?;
    if result["subtitle_resume_plan"]
        != serde_json::to_value(&plan).map_err(|_| conflict("resume plan encoding failed"))?
    {
        return Err(conflict("resume stage receipt differs from plan"));
    }
    let raw = asset_digest(db, registry, task, "video_raw", &plan.video.key).await?;
    if raw != plan.video {
        return Err(conflict("resume render media changed"));
    }
    let ass = format!("v2/sub/{task}/{job}/subtitle.ass");
    let srt = format!("v2/sub/{task}/{job}/subtitle.srt");
    if (!result["ass_key"].is_null() && result["ass_key"].as_str()!=Some(ass.as_str()))
        || result["srt_key"].as_str() != Some(srt.as_str())
    {
        return Err(conflict("resume render subtitle receipt mismatch"));
    }
    let ass=if result["ass_key"].is_null(){None}else{asset_digest(db,registry,task,"subtitle_ass",&ass).await?;Some(ass)};
    asset_digest(db, registry, task, "subtitle_srt", &srt).await?;
    Ok(Some((raw.key, ass, srt)))
}
