//! Audio-only YouDub handoff. Freeze media and render profile in the existing
//! render_executions row. Only the remote renderer may settle the P2 video.
use crate::{AppError, AppResult, Config, Database, domain::WorkItem, storage::AssetRegistry};
use serde_json::{Value, json};
use sqlx::Row;
use std::path::{Path, PathBuf};
use tokio::{fs, process::Command};
use uuid::Uuid;

pub(crate) const MODE: &str = "dub_burn_in";

async fn inside(root: &Path, result: &Value, field: &str) -> AppResult<PathBuf> {
    let raw = result[field]
        .as_str()
        .ok_or_else(|| AppError::Invalid(format!("YouDub {field} missing")))?;
    let path = fs::canonicalize(raw).await?;
    if !path.starts_with(root) || !fs::metadata(&path).await?.is_file() {
        return Err(AppError::Conflict(
            "YouDub handoff artifact escapes runtime".into(),
        ));
    }
    Ok(path)
}

async fn snapshot(store: &AssetRegistry, source: &Path, key: &str) -> AppResult<(String, i64)> {
    let dest = store.resolve_existing(key).await?;
    fs::create_dir_all(
        dest.parent()
            .ok_or_else(|| AppError::Invalid("artifact parent missing".into()))?,
    )
    .await?;
    let temp = dest.with_extension(format!("{}.partial", Uuid::new_v4()));
    let result = async {
        fs::copy(source, &temp).await?;
        fs::OpenOptions::new()
            .read(true)
            .open(&temp)
            .await?
            .sync_all()
            .await?;
        // Never replace an existing asset. Concurrent recovery uses the same
        // deterministic key and must prove that its bytes match.
        match fs::hard_link(&temp, &dest).await {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let canonical_root = fs::canonicalize(&store.root).await?;
                let tmp_key = temp
                    .strip_prefix(&canonical_root)
                    .map_err(|_| AppError::Conflict("artifact temporary path escaped root".into()))?
                    .to_str()
                    .ok_or_else(|| AppError::Invalid("non UTF-8 artifact path".into()))?;
                if store.sha256(tmp_key).await? != store.sha256(key).await? {
                    return Err(AppError::Conflict("YouDub handoff changed on retry".into()));
                }
            }
            Err(e) => return Err(e.into()),
        }
        let proof = store.sha256(key).await?;
        if proof.1 <= 0 {
            return Err(AppError::Invalid("empty YouDub artifact".into()));
        }
        Ok(proof)
    }
    .await;
    let _ = fs::remove_file(&temp).await;
    result
}

async fn probe_audio(config: &Config, path: &Path) -> AppResult<()> {
    let output = tokio::time::timeout(
        std::time::Duration::from_secs(20),
        Command::new(&config.ffprobe_binary)
            .args([
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
            ])
            .arg(path)
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| AppError::Unavailable("YouDub audio probe timeout".into()))??;
    let value: Value = serde_json::from_slice(&output.stdout).unwrap_or_default();
    let streams = value["streams"].as_array().cloned().unwrap_or_default();
    if !output.status.success()
        || !streams.iter().any(|s| s["codec_type"] == "audio")
        || streams.iter().any(|s| s["codec_type"] == "video")
        || !value["format"]["duration"]
            .as_str()
            .and_then(|s| s.parse::<f64>().ok())
            .is_some_and(|d| d.is_finite() && d > 0.)
    {
        return Err(AppError::Invalid("YouDub output is not valid audio".into()));
    }
    Ok(())
}

pub(crate) async fn enqueue(
    config: &Config,
    db: &Database,
    store: &AssetRegistry,
    job: &WorkItem,
    runtime: &Path,
    native_id: Uuid,
    source_key: &str,
    source_srt_key: &str,
    result: &Value,
) -> AppResult<Uuid> {
    if !config.remote_renderer_enabled {
        return Err(AppError::Conflict(
            "audio-only YouDub requires remote renderer".into(),
        ));
    }
    let root = fs::canonicalize(runtime).await?;
    let mixed = inside(&root, result, "mixed_audio").await?;
    let voice = inside(&root, result, "voice_audio").await?;
    let srt = inside(&root, result, "aligned_srt").await?;
    if fs::metadata(&srt).await?.len() > 16 * 1024 * 1024 {
        return Err(AppError::Invalid("aligned SRT too large".into()));
    }
    let raw = fs::read_to_string(&srt).await?;
    let cues = crate::subtitles::parse_srt(&raw)?;
    if cues.is_empty() {
        return Err(AppError::Invalid("aligned SRT contains no cues".into()));
    }
    probe_audio(config, &mixed).await?;
    probe_audio(config, &voice).await?;
    let prefix = format!("v2/dubbing/{}/{}", job.task_id, job.id);
    let mixed_key = format!("{prefix}/mixed.m4a");
    let voice_key = format!("{prefix}/voice.wav");
    let srt_key = format!("{prefix}/aligned.srt");
    let ass_key = format!("{prefix}/aligned.ass");
    let (mixed_proof, voice_proof, srt_proof) = tokio::try_join!(
        snapshot(store, &mixed, &mixed_key),
        snapshot(store, &voice, &voice_key),
        snapshot(store, &srt, &srt_key)
    )?;
    // The ASS and transferred SRT must come from exactly the same bytes.
    use sha2::Digest as _;
    if srt_proof.0 != hex::encode(sha2::Sha256::digest(raw.as_bytes())) {
        return Err(AppError::Conflict(
            "aligned SRT changed during handoff".into(),
        ));
    }
    let ass = crate::subtitles::to_ass(&cues, 1920, 1080, "Arial", 48, false)?;
    let tempdir = tempfile::tempdir()?;
    let ass_file = tempdir.path().join("aligned.ass");
    fs::write(&ass_file, ass).await?;
    let ass_proof = snapshot(store, &ass_file, &ass_key).await?;
    let source_proof = store.sha256(source_key).await?;
    let source_srt_proof = store.sha256(source_srt_key).await?;
    let id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("VideoRoll-v2-remote-render:{}", job.id).as_bytes(),
    );
    let mut profile = crate::media::ffmpeg::profile(db, job).await?;
    profile.burn_in = true;
    profile.soft_sub = false;
    let artifacts=[("input",source_key,&source_proof),("audio",&mixed_key,&mixed_proof),("ass",&ass_key,&ass_proof),("srt",&srt_key,&srt_proof)].into_iter().map(|(role,key,(sha,size))|json!({
        "role":role,"storage_key":key,"sha256":sha,"size_bytes":size,"download_url":format!("/render-workers/v1/executions/{id}/artifacts/{role}")
    })).collect::<Vec<_>>();
    let spec = json!({"schema_version":1,"render_job_id":job.id,"task_id":job.task_id,"mode":MODE,
        "request":{"runtime_profile":false,"burn_in":true,"soft_sub":false,"input_key":source_key,"audio_key":mixed_key,"srt_key":srt_key,"ass_key":ass_key,
            "dubbing_source_srt_key":source_srt_key,"dubbing_source_srt_sha256":source_srt_proof.0,"dubbing_model_job_id":native_id,"voice_key":voice_key,"voice_sha256":voice_proof.0,"render":profile},
        "artifacts":artifacts});
    let mut tx = db.pool.begin().await?;
    let owned:bool=sqlx::query_scalar("SELECT runtime_owner='rust' AND status::text NOT IN ('canceled','failed','published','publishing') FROM tasks WHERE id=$1 FOR UPDATE").bind(job.task_id).fetch_one(&mut *tx).await?;
    let flow:bool=sqlx::query_scalar("SELECT state='running' AND current_stage='dubbing' AND task_id=$2 FROM vr2_workflows WHERE id=$1 FOR UPDATE").bind(job.workflow_id).bind(job.task_id).fetch_one(&mut *tx).await?;
    let held:bool=sqlx::query_scalar("SELECT state='running' AND lease_token=$2 AND lease_until>now() AND stage='dubbing' AND workflow_id=$3 AND task_id=$4 FROM vr2_jobs WHERE id=$1 FOR UPDATE").bind(job.id).bind(job.lease_token).bind(job.workflow_id).bind(job.task_id).fetch_one(&mut *tx).await?;
    if !owned || !flow || !held {
        return Err(AppError::Conflict("P2 handoff lease lost".into()));
    }
    let previous=sqlx::query("SELECT id,mode,render_spec,state FROM render_executions WHERE job_id=$1 AND runtime_engine='rust-v2' FOR UPDATE").bind(job.id).fetch_optional(&mut *tx).await?;
    if let Some(previous) = previous {
        let old: Value = previous.try_get("render_spec")?;
        if previous.try_get::<Uuid, _>("id")? != id
            || previous.try_get::<String, _>("mode")? != MODE
            || previous.try_get::<String, _>("state")? != "queued"
            || old["artifacts"] != spec["artifacts"]
            || old["request"]["dubbing_model_job_id"] != spec["request"]["dubbing_model_job_id"]
            || old["request"]["voice_sha256"] != spec["request"]["voice_sha256"]
            || old["request"]["dubbing_source_srt_sha256"]
                != spec["request"]["dubbing_source_srt_sha256"]
        {
            return Err(AppError::Conflict(
                "P2 handoff immutable receipt differs".into(),
            ));
        }
        tx.commit().await?;
        return Ok(id);
    }
    for (role, key, (sha, size)) in [
        ("input", source_key, &source_proof),
        ("source_srt", source_srt_key, &source_srt_proof),
        ("dubbing_audio", &mixed_key, &mixed_proof),
        ("dubbing_audio", &voice_key, &voice_proof),
        ("dubbing_aligned_srt", &srt_key, &srt_proof),
        ("dubbing_ass", &ass_key, &ass_proof),
    ] {
        let affected=sqlx::query("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days') ON CONFLICT(storage_key) DO UPDATE SET retain_until=GREATEST(storage_objects.retain_until,now()+interval '2 days') WHERE storage_objects.state='live' AND storage_objects.checksum_sha256=$2 AND storage_objects.size_bytes=$3").bind(key).bind(sha).bind(size).execute(&mut *tx).await?.rows_affected();
        if affected != 1 {
            return Err(AppError::Conflict(
                "P2 handoff storage evidence differs".into(),
            ));
        }
        sqlx::query("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'workflow',$2,$3) ON CONFLICT DO NOTHING").bind(key).bind(job.workflow_id).bind(role).execute(&mut *tx).await?;
        // Aligned P2 subtitles must not become the newest P1 subtitle asset.
        if role == "dubbing_audio" {
            sqlx::query("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) SELECT $1,$2,'dubbing_audio'::asset_kind,$3,$4,$5 WHERE NOT EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND kind::text='dubbing_audio' AND storage_key=$3)").bind(Uuid::new_v4()).bind(job.task_id).bind(key).bind(sha).bind(size).execute(&mut *tx).await?;
        }
    }
    sqlx::query("INSERT INTO render_executions(id,job_id,task_id,workflow_id,state,attempt,runtime_engine,mode,render_spec) VALUES($1,$2,$3,$4,'queued',0,'rust-v2',$5,$6)").bind(id).bind(job.id).bind(job.task_id).bind(job.workflow_id).bind(MODE).bind(&spec).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(id)
}

#[cfg(test)]
#[path = "dubbing_handoff_test.rs"]
mod tests;
