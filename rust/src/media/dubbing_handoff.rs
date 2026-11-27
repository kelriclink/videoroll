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

/// Explicit manual retry of the video stage only. If a P2 intent exists but
/// its evidence is incomplete, reject instead of falling back to synthesis.
pub(crate) async fn retry_render_only(
    db: &Database,
    store: &AssetRegistry,
    task: Uuid,
    job: Uuid,
) -> AppResult<Option<Uuid>> {
    let row=sqlx::query("SELECT id,workflow_id,render_spec FROM render_executions WHERE runtime_engine='rust-v2' AND mode='dub_burn_in' AND job_id=$1 AND task_id=$2")
        .bind(job).bind(task).fetch_optional(&db.pool).await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let execution: Uuid = row.try_get("id")?;
    let flow: Uuid = row.try_get("workflow_id")?;
    let spec: Value = row.try_get("render_spec")?;
    let request = &spec["request"];
    let invalid =
        || AppError::Conflict("P2 retry requires intact original audio handoff evidence".into());
    if spec["mode"] != MODE
        || spec["task_id"] != json!(task)
        || spec["render_job_id"] != json!(job)
        || request["dubbing_model_job_id"]
            .as_str()
            .and_then(|s| Uuid::parse_str(s).ok())
            .is_none()
    {
        return Err(invalid());
    }
    let mut proofs = Vec::new();
    let artifacts = spec["artifacts"].as_array().ok_or_else(invalid)?;
    if artifacts.len() != 4 {
        return Err(invalid());
    }
    let prefix = format!("v2/dubbing/{task}/{job}/");
    for (role, field, ref_role) in [
        ("input", "input_key", "input"),
        ("audio", "audio_key", "dubbing_audio"),
        ("ass", "ass_key", "dubbing_ass"),
        ("srt", "srt_key", "dubbing_aligned_srt"),
    ] {
        let key = request[field].as_str().ok_or_else(invalid)?;
        if role != "input" && !key.starts_with(&prefix) {
            return Err(invalid());
        }
        let a = artifacts
            .iter()
            .find(|a| a["role"] == role && a["storage_key"] == key)
            .ok_or_else(invalid)?;
        let sha = a["sha256"].as_str().ok_or_else(invalid)?;
        let size = a["size_bytes"]
            .as_i64()
            .filter(|s| *s > 0)
            .ok_or_else(invalid)?;
        let actual = store.sha256(key).await?;
        if actual != (sha.to_owned(), size) {
            return Err(invalid());
        }
        proofs.push((key.to_owned(), actual, ref_role));
    }
    for (key_field, sha_field, role) in [
        ("voice_key", "voice_sha256", "dubbing_audio"),
        (
            "dubbing_source_srt_key",
            "dubbing_source_srt_sha256",
            "source_srt",
        ),
    ] {
        let key = request[key_field].as_str().ok_or_else(invalid)?;
        let sha = request[sha_field].as_str().ok_or_else(invalid)?;
        if key_field == "voice_key" && !key.starts_with(&prefix) {
            return Err(invalid());
        }
        let actual = store.sha256(key).await?;
        if actual.0 != sha || actual.1 <= 0 {
            return Err(invalid());
        }
        proofs.push((key.to_owned(), actual, role));
    }
    // Consistent lock order with settlement, cleanup and claim controls.
    let mut tx = db.pool.begin().await?;
    let parent = sqlx::query(
        "SELECT runtime_owner,status::text AS status,error_code FROM tasks WHERE id=$1 FOR UPDATE",
    )
    .bind(task)
    .fetch_one(&mut *tx)
    .await?;
    let latest=sqlx::query("SELECT id,state,current_stage FROM vr2_workflows WHERE task_id=$1 ORDER BY generation DESC LIMIT 1 FOR UPDATE").bind(task).fetch_one(&mut *tx).await?;
    let j=sqlx::query("SELECT state,external_ticket FROM vr2_jobs WHERE id=$1 AND workflow_id=$2 AND task_id=$3 AND stage='dubbing' FOR UPDATE").bind(job).bind(flow).bind(task).fetch_optional(&mut *tx).await?.ok_or_else(invalid)?;
    let e=sqlx::query("SELECT state,render_spec,uploaded_asset_id,COALESCE(lease_until>now(),false) AS live FROM render_executions WHERE id=$1 AND job_id=$2 AND task_id=$3 AND workflow_id=$4 AND runtime_engine='rust-v2' AND mode='dub_burn_in' FOR UPDATE").bind(execution).bind(job).bind(task).bind(flow).fetch_optional(&mut *tx).await?.ok_or_else(invalid)?;
    if parent.try_get::<String, _>("runtime_owner")? != "rust"
        || latest.try_get::<Uuid, _>("id")? != flow
        || latest.try_get::<String, _>("current_stage")? != "dubbing"
        || j.try_get::<Option<String>, _>("external_ticket")?
            .as_deref()
            != Some(execution.to_string().as_str())
        || e.try_get::<Value, _>("render_spec")? != spec
    {
        return Err(invalid());
    }
    let state: String = e.try_get("state")?;
    let flow_state: String = latest.try_get("state")?;
    let job_state: String = j.try_get("state")?;
    if flow_state == "running"
        && job_state == "awaiting"
        && matches!(state.as_str(), "queued" | "running" | "uploading")
    {
        tx.commit().await?;
        return Ok(Some(flow));
    }
    if parent.try_get::<String, _>("status")? != "failed"
        || parent
            .try_get::<Option<String>, _>("error_code")?
            .as_deref()
            != Some("V2_REMOTE_RENDER_FAILED")
        || flow_state != "failed"
        || job_state != "failed"
        || state != "failed"
        || e.try_get::<bool, _>("live")?
        || e.try_get::<Option<Uuid>, _>("uploaded_asset_id")?.is_some()
    {
        return Err(AppError::Conflict(
            "P2 retry requires failed render without committed output or live lease".into(),
        ));
    }
    let publication:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND (state::text IN ('submitting','submitted','published','unknown') OR external_id IS NOT NULL OR external_url IS NOT NULL)) OR EXISTS(SELECT 1 FROM vr2_jobs WHERE task_id=$1 AND stage='publish' AND (state IN ('pending','running','awaiting','unknown') OR (state='succeeded' AND COALESCE(result->>'skipped','')='')))").bind(task).fetch_one(&mut *tx).await?;
    if publication {
        return Err(AppError::Conflict(
            "publication evidence blocks P2 retry".into(),
        ));
    }
    let raw:Option<String>=sqlx::query_scalar("SELECT storage_key FROM assets WHERE task_id=$1 AND kind::text='video_raw' ORDER BY created_at DESC,id DESC LIMIT 1").bind(task).fetch_optional(&mut *tx).await?;
    let srt:Option<String>=sqlx::query_scalar("SELECT storage_key FROM assets WHERE task_id=$1 AND kind::text='subtitle_srt' ORDER BY created_at DESC,id DESC LIMIT 1").bind(task).fetch_optional(&mut *tx).await?;
    if raw.as_deref() != request["input_key"].as_str()
        || srt.as_deref() != request["dubbing_source_srt_key"].as_str()
    {
        return Err(invalid());
    }
    for (key, (sha, size), role) in proofs {
        let valid:Option<String>=sqlx::query_scalar("SELECT o.storage_key FROM storage_objects o WHERE o.storage_key=$1 AND o.state='live' AND o.checksum_sha256=$2 AND o.size_bytes=$3 AND EXISTS(SELECT 1 FROM storage_object_refs r WHERE r.storage_key=o.storage_key AND r.owner_type='workflow' AND r.owner_id=$4 AND r.role=$5) FOR UPDATE OF o").bind(&key).bind(&sha).bind(size).bind(flow).bind(role).fetch_optional(&mut *tx).await?;
        if valid.is_none() {
            return Err(invalid());
        }
        sqlx::query("UPDATE storage_objects SET retain_until=GREATEST(retain_until,now()+interval '2 days') WHERE storage_key=$1").bind(key).execute(&mut *tx).await?;
    }
    // Original manual retry does not silently grant permission to publish.
    sqlx::query("UPDATE vr2_workflows SET state='running',last_error=NULL,configuration=(configuration-'after_render_publish'-'after_render_input')||jsonb_build_object('auto_publish',false,'auto_publish_platforms','[]'::jsonb),updated_at=now() WHERE id=$1").bind(flow).execute(&mut *tx).await?;
    sqlx::query("UPDATE vr2_jobs SET state='awaiting',last_error=NULL,lease_token=NULL,lease_until=NULL,finished_at=NULL,updated_at=now() WHERE id=$1").bind(job).execute(&mut *tx).await?;
    sqlx::query("UPDATE render_executions SET state='queued',attempt=0,worker_id=NULL,fence_token=NULL,lease_until=NULL,progress=0,metrics='{}',output_json='{}',error_message=NULL,log_tail=NULL,started_at=now(),finished_at=NULL,heartbeat_at=NULL,updated_at=now() WHERE id=$1").bind(execution).execute(&mut *tx).await?;
    sqlx::query("UPDATE tasks SET status='rendered'::task_status,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=$1").bind(task).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO workflow_events(id,workflow_id,job_id,kind,details) VALUES($1,$2,$3,'render.p2_requeued',$4)").bind(Uuid::new_v4()).bind(flow).bind(job).bind(json!({"execution_id":execution,"reuse_audio":true})).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Some(flow))
}
