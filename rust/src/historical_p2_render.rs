//! Explicit recovery of historical audio-only YouDub handoffs. The original
//! render/dubbing rows remain authoritative; no fabricated native workflow or
//! publication proof. All external publication is out of scope.
use crate::{AppError, AppResult, Config, Database, media::ffmpeg, storage::AssetRegistry};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use std::{path::Path, process::Stdio, time::Duration};
use tokio::{fs, process::Command};
use uuid::Uuid;
const ENGINE: &str = "rust-historical-p2-render";
const LEASE: i64 = 180;
// Legacy workers must be drained before the native-only mode is enabled.
pub(crate) fn enabled() -> bool {
    crate::outbox_dispatcher::native_only()
        && crate::render_management_read_v1::native_management()
        && !matches!(
            std::env::var("VIDEOROLL_V2_ALLOW_V1_RENDER_CONTROL")
                .ok()
                .as_deref(),
            Some("true" | "1")
        )
}

fn conflict(message: &str) -> AppError {
    AppError::Conflict(message.into())
}
fn string<'a>(v: &'a Value, k: &str) -> AppResult<&'a str> {
    v.get(k)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| conflict("incomplete historical P2 handoff"))
}
fn uuid(v: &Value, k: &str) -> AppResult<Uuid> {
    Uuid::parse_str(string(v, k)?).map_err(|_| conflict("invalid historical P2 identity"))
}
async fn lock_task(tx: &mut Transaction<'_, Postgres>, task: Uuid) -> AppResult<()> {
    let ok: Option<bool> = sqlx::query_scalar("SELECT runtime_owner='rust' AND status::text NOT IN ('canceled','publishing') FROM tasks WHERE id=$1 FOR UPDATE").bind(task).fetch_optional(&mut **tx).await?;
    if ok != Some(true) {
        return Err(conflict(
            "historical P2 requires a Rust-owned, non-stopped task",
        ));
    }
    let blocked:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND state NOT IN ('succeeded','failed','canceled','completed','launch_failed')) OR EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND state::text IN ('draft','submitting','unknown'))").bind(task).fetch_one(&mut **tx).await?;
    if blocked {
        return Err(conflict(
            "active or uncertain workflow/publication blocks historical P2 recovery",
        ));
    }
    Ok(())
}
/// Validate the original two-way association and exact source/audio/SRT keys.
/// The aligned SRT was not historically entered in assets; its content-addressed
/// filename is evidence only when paired with both original job records.
async fn evidence(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    request: &Value,
    output: &Value,
    render: Uuid,
    dub: Uuid,
) -> AppResult<Value> {
    if output
        .get("video_key")
        .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
        || uuid(request, "dubbing_job_id")? != dub
        || uuid(output, "render_job_id")? != render
        || output.pointer("/youdub/stage").and_then(Value::as_str) != Some("rendering_p2")
        || output.pointer("/youdub/task_id").and_then(Value::as_str)
            != Some(dub.to_string().as_str())
    {
        return Err(conflict("historical P2 handoff identity mismatch"));
    }
    let video = string(request, "input_key")?;
    let audio = string(request, "audio_key")?;
    let srt = string(request, "srt_key")?;
    if string(output, "source_video_key")? != video
        || string(output, "audio_key")? != audio
        || string(output, "aligned_srt_key")? != srt
        || request.get("burn_in").and_then(Value::as_bool) != Some(true)
        || request.get("runtime_profile").and_then(Value::as_bool) != Some(false)
    {
        return Err(conflict("historical P2 media association mismatch"));
    }
    for k in [video, audio, srt] {
        AssetRegistry::checked_key(k)?;
    }
    let srt_hash = srt
        .strip_prefix(&format!("sub/{task}/dubbed-"))
        .and_then(|s| s.strip_suffix(".srt"))
        .filter(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(|| conflict("aligned SRT lacks historical content identity"))?;
    let mut files = Vec::new();
    for (key, kind) in [(video, "video_raw"), (audio, "dubbing_audio")] {
        let row=sqlx::query("SELECT sha256,size_bytes FROM assets WHERE task_id=$1 AND storage_key=$2 AND kind::text=$3 ORDER BY created_at DESC,id DESC LIMIT 1").bind(task).bind(key).bind(kind).fetch_optional(&mut **tx).await?.ok_or_else(||conflict("historical P2 source asset missing"))?;
        let sha: Option<String> = row.try_get("sha256")?;
        let size: Option<i64> = row.try_get("size_bytes")?;
        if !sha
            .as_ref()
            .is_some_and(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
            || !size.is_some_and(|n| n > 0)
        {
            return Err(conflict("historical P2 asset lacks checksum or size"));
        }
        files.push(json!({"key":key,"sha256":sha,"size":size}));
    }
    files.push(json!({"key":srt,"sha256":srt_hash}));
    let profile: ffmpeg::RenderProfile =
        serde_json::from_value(request.get("render").cloned().unwrap_or(json!({})))
            .map_err(|_| conflict("invalid historical render profile"))?;
    if !matches!(
        profile.video_codec.as_str(),
        "h264" | "avc" | "hevc" | "h265" | "av1"
    ) || profile.ass_style != "clean_white"
        || request
            .get("ass_key")
            .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
    {
        return Err(conflict(
            "historical P2 profile requires unsupported ASS or encoder options",
        ));
    }
    for k in ["primary_font_scale_percent", "secondary_font_scale_percent"] {
        if request
            .pointer(&format!("/render/{k}"))
            .is_some_and(|v| v.as_i64() != Some(100))
        {
            return Err(conflict("custom subtitle scaling requires manual recovery"));
        }
    }
    Ok(json!({"render_id":render,"dubbing_id":dub,"files":files,"profile":profile}))
}
/// Explicit admin requeue. The old execution stays immutable; the original
/// render_job is queued for the Rust-only local recovery worker.
pub async fn enqueue(db: &Database, id: Uuid, reason: &str) -> AppResult<()> {
    if !enabled() {
        return Err(conflict(
            "historical P2 recovery requires native-only management after legacy drain",
        ));
    }
    let mut tx = db.pool.begin().await?;
    let row=sqlx::query("SELECT e.render_job_id,j.task_id FROM render_executions e JOIN render_jobs j ON j.id=e.render_job_id WHERE e.id=$1 AND e.runtime_engine='python'").bind(id).fetch_optional(&mut *tx).await?.ok_or_else(||conflict("not a historical P2 execution"))?;
    let task: Uuid = row.try_get("task_id")?;
    let render: Uuid = row.try_get("render_job_id")?;
    lock_task(&mut tx, task).await?;
    let r=sqlx::query("SELECT status::text AS status,request_json,lease_owner,lease_until,operation_key FROM render_jobs WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(render).bind(task).fetch_one(&mut *tx).await?;
    let req: Value = r.try_get("request_json")?;
    let dub = uuid(&req, "dubbing_job_id")?;
    let d=sqlx::query("SELECT status,output_json,lease_owner,lease_until FROM dubbing_jobs WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(dub).bind(task).fetch_optional(&mut *tx).await?.ok_or_else(||conflict("historical dubbing job not found"))?;
    let e=sqlx::query("SELECT state,lease_until,output_json FROM render_executions WHERE id=$1 AND render_job_id=$2 AND runtime_engine='python' FOR UPDATE").bind(id).bind(render).fetch_one(&mut *tx).await?;
    let key = format!("rust-historical-p2-render:{render}");
    if let Some(old)=sqlx::query("SELECT runtime_engine,task_id,status,request_json FROM operation_inbox WHERE operation_key=$1 FOR UPDATE").bind(&key).fetch_optional(&mut *tx).await? {
        if old.try_get::<String,_>("runtime_engine")?!=ENGINE || old.try_get::<Option<Uuid>,_>("task_id")?!=Some(task) || uuid(&old.try_get::<Value,_>("request_json")?,"execution_id")?!=id {return Err(conflict("historical P2 recovery operation collision"));}
        if matches!(old.try_get::<String,_>("status")?.as_str(),"pending"|"running"|"completed") {tx.commit().await?;return Ok(());}
        return Err(conflict("prior historical P2 recovery failed; inspect its receipt before another attempt"));
    }
    if !matches!(
        r.try_get::<String, _>("status")?.as_str(),
        "failed" | "canceled"
    ) || !matches!(
        d.try_get::<String, _>("status")?.as_str(),
        "failed" | "paused"
    ) || !matches!(
        e.try_get::<String, _>("state")?.as_str(),
        "failed" | "canceled" | "lost"
    ) || e
        .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("lease_until")?
        .is_some_and(|t| t > chrono::Utc::now())
        || e.try_get::<Value, _>("output_json")?
            .get("output_asset_id")
            .is_some_and(|v| !v.is_null())
    {
        return Err(conflict("historical P2 execution is not safely retryable"));
    }
    for row in [&r, &d] {
        if row.try_get::<Option<String>, _>("lease_owner")?.is_some()
            || row
                .try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("lease_until")?
                .is_some()
        {
            return Err(conflict("historical P2 job retains a lease"));
        }
    }
    if r.try_get::<Option<String>, _>("operation_key")?.as_deref()
        != Some(format!("youdub:p2-render:{dub}").as_str())
    {
        return Err(conflict("historical P2 render operation key mismatch"));
    }
    let blocked:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM render_executions WHERE render_job_id=$1 AND (state NOT IN ('failed','canceled','lost') OR lease_until>now())) OR EXISTS(SELECT 1 FROM assets WHERE task_id=$2 AND kind::text='video_dubbed') OR EXISTS(SELECT 1 FROM operation_inbox WHERE task_id=$2 AND runtime_engine<>'rust-task-cutover' AND status NOT IN ('completed','succeeded','failed','canceled')) OR EXISTS(SELECT 1 FROM outbox_events x WHERE x.aggregate_id IN ($1::text,$3::text) AND NOT (x.status IN ('native_completed','dispatched') AND EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key=x.operation_key AND i.status='completed'))) OR EXISTS(SELECT 1 FROM render_jobs j WHERE j.task_id=$2 AND j.id<>$1 AND j.status::text IN ('queued','running')) OR EXISTS(SELECT 1 FROM dubbing_jobs d WHERE d.task_id=$2 AND d.id<>$3 AND d.status IN ('queued','preparing','synthesizing','aligning','mixing','rendering','pause_requested','cancel_requested'))").bind(render).bind(task).bind(dub).fetch_one(&mut *tx).await?;
    if blocked {
        return Err(conflict(
            "historical P2 has other execution, output or unsettled work",
        ));
    }
    let output: Value = d.try_get("output_json")?;
    let mut proof = evidence(&mut tx, task, &req, &output, render, dub).await?;
    proof["execution_id"] = json!(id);
    proof["reason"] = json!(reason);
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,status,request_json,max_attempts) VALUES($1,$2,$3,$4,'pending',$5,1)").bind(Uuid::new_v4()).bind(key).bind(ENGINE).bind(task).bind(proof).execute(&mut *tx).await?;
    sqlx::query("UPDATE render_jobs SET status='queued',progress=0,retry_count=retry_count+1,started_at=NULL,finished_at=NULL,error_message=NULL,updated_at=now() WHERE id=$1").bind(render).execute(&mut *tx).await?;
    sqlx::query("UPDATE dubbing_jobs SET status='rendering',error_message=NULL,updated_at=now() WHERE id=$1").bind(dub).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}

struct Claim {
    id: Uuid,
    task: Uuid,
    render: Uuid,
    dub: Uuid,
    token: Uuid,
    proof: Value,
}
impl Claim {
    fn owner(&self) -> String {
        format!("rust-p2:{}", self.token)
    }
}
async fn claim(db: &Database) -> AppResult<Option<Claim>> {
    let mut tx = db.pool.begin().await?;
    // Serialize claims for this bounded local recovery lane; never hold a
    // transaction while FFmpeg runs. Lease renewal is independent of the codec.
    let gate: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(841019327::bigint)")
        .fetch_one(&mut *tx)
        .await?;
    if !gate {
        return Ok(None);
    }
    let row=sqlx::query("SELECT id,task_id,request_json,status FROM operation_inbox WHERE runtime_engine=$1 AND (status='pending' OR (status='running' AND lease_until<=now())) ORDER BY updated_at,id LIMIT 1").bind(ENGINE).fetch_optional(&mut *tx).await?;
    let Some(row) = row else { return Ok(None) };
    let proof: Value = row.try_get("request_json")?;
    let c = Claim {
        id: row.try_get("id")?,
        task: row.try_get("task_id")?,
        render: uuid(&proof, "render_id")?,
        dub: uuid(&proof, "dubbing_id")?,
        token: Uuid::new_v4(),
        proof,
    };
    // Task is the common first lock for every write path.
    sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
        .bind(c.task)
        .fetch_one(&mut *tx)
        .await?;
    let r=sqlx::query("SELECT status::text AS status,lease_owner,request_json FROM render_jobs WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(c.render).bind(c.task).fetch_one(&mut *tx).await?;
    let d=sqlx::query("SELECT status,lease_owner,output_json FROM dubbing_jobs WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(c.dub).bind(c.task).fetch_one(&mut *tx).await?;
    let op=sqlx::query("SELECT status,lease_owner,COALESCE(lease_until<=now(),false) AS expired FROM operation_inbox WHERE id=$1 AND runtime_engine=$2 FOR UPDATE").bind(c.id).bind(ENGINE).fetch_one(&mut *tx).await?;
    let status: String = op.try_get("status")?;
    if status == "running" && op.try_get::<bool, _>("expired")? {
        // An interrupted local encode has no committed asset. Preserve its
        // failure evidence; a new explicit recovery policy is required.
        let old: Option<String> = op.try_get("lease_owner")?;
        fail_locked(
            &mut tx,
            &c,
            old.as_deref(),
            "historical P2 worker lease expired",
        )
        .await?;
        tx.commit().await?;
        return Ok(None);
    }
    if status != "pending" {
        return Ok(None);
    }
    let eligible = lock_task(&mut tx, c.task).await.is_ok()
        && r.try_get::<String, _>("status")? == "queued"
        && d.try_get::<String, _>("status")? == "rendering"
        && r.try_get::<Option<String>, _>("lease_owner")?.is_none()
        && d.try_get::<Option<String>, _>("lease_owner")?.is_none();
    let current = evidence(
        &mut tx,
        c.task,
        &r.try_get("request_json")?,
        &d.try_get("output_json")?,
        c.render,
        c.dub,
    )
    .await;
    if !eligible
        || !current
            .is_ok_and(|v| v["files"] == c.proof["files"] && v["profile"] == c.proof["profile"])
    {
        fail_locked(
            &mut tx,
            &c,
            None,
            "historical P2 eligibility or media evidence changed",
        )
        .await?;
        tx.commit().await?;
        return Ok(None);
    }
    let busy:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM operation_inbox WHERE runtime_engine=$1 AND status='running' AND lease_until>now())").bind(ENGINE).fetch_one(&mut *tx).await?;
    if busy {
        return Ok(None);
    }
    let owner = c.owner();
    sqlx::query("UPDATE operation_inbox SET status='running',lease_owner=$2,lease_token=$3,lease_until=now()+$4*interval '1 second',heartbeat_at=now(),attempt_count=attempt_count+1,started_at=now(),updated_at=now() WHERE id=$1").bind(c.id).bind(&owner).bind(c.token).bind(LEASE).execute(&mut *tx).await?;
    sqlx::query("UPDATE render_jobs SET status='running',lease_owner=$2,lease_until=now()+$3*interval '1 second',heartbeat_at=now(),started_at=now(),updated_at=now() WHERE id=$1").bind(c.render).bind(&owner).bind(LEASE).execute(&mut *tx).await?;
    sqlx::query("UPDATE dubbing_jobs SET lease_owner=$2,lease_until=now()+$3*interval '1 second',heartbeat_at=now(),updated_at=now() WHERE id=$1").bind(c.dub).bind(&owner).bind(LEASE).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Some(c))
}
async fn fail_locked(
    tx: &mut Transaction<'_, Postgres>,
    c: &Claim,
    owner: Option<&str>,
    message: &str,
) -> AppResult<()> {
    sqlx::query("UPDATE render_jobs SET status='failed',error_message=$3,lease_owner=NULL,lease_until=NULL,finished_at=now(),updated_at=now() WHERE id=$1 AND task_id=$4 AND lease_owner IS NOT DISTINCT FROM $2 AND status::text IN ('queued','running')").bind(c.render).bind(owner).bind(message).bind(c.task).execute(&mut **tx).await?;
    sqlx::query("UPDATE dubbing_jobs SET status='failed',error_message=$3,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE id=$1 AND task_id=$4 AND lease_owner IS NOT DISTINCT FROM $2 AND status='rendering'").bind(c.dub).bind(owner).bind(message).bind(c.task).execute(&mut **tx).await?;
    sqlx::query("UPDATE operation_inbox SET status='failed',last_error=$2,lease_owner=NULL,lease_token=NULL,lease_until=NULL,finished_at=now(),updated_at=now() WHERE id=$1 AND runtime_engine=$3 AND status IN ('pending','running') AND lease_owner IS NOT DISTINCT FROM $4").bind(c.id).bind(message).bind(ENGINE).bind(owner).execute(&mut **tx).await?;
    Ok(())
}
async fn lock_claim(tx: &mut Transaction<'_, Postgres>, c: &Claim) -> AppResult<bool> {
    sqlx::query("SELECT id FROM tasks WHERE id=$1 FOR UPDATE")
        .bind(c.task)
        .fetch_one(&mut **tx)
        .await?;
    let owner = c.owner();
    let r:Option<bool>=sqlx::query_scalar("SELECT status::text='running' AND lease_owner=$2 AND lease_until>now() FROM render_jobs WHERE id=$1 AND task_id=$3 FOR UPDATE").bind(c.render).bind(&owner).bind(c.task).fetch_optional(&mut **tx).await?;
    let d:Option<bool>=sqlx::query_scalar("SELECT status='rendering' AND lease_owner=$2 AND lease_until>now() FROM dubbing_jobs WHERE id=$1 AND task_id=$3 FOR UPDATE").bind(c.dub).bind(&owner).bind(c.task).fetch_optional(&mut **tx).await?;
    let op:Option<bool>=sqlx::query_scalar("SELECT status='running' AND lease_owner=$2 AND lease_token=$3 AND lease_until>now() FROM operation_inbox WHERE id=$1 AND runtime_engine=$4 FOR UPDATE").bind(c.id).bind(&owner).bind(c.token).bind(ENGINE).fetch_optional(&mut **tx).await?;
    Ok(r == Some(true) && d == Some(true) && op == Some(true))
}
async fn renew(db: &Database, c: &Claim) -> AppResult<()> {
    let mut tx = db.pool.begin().await?;
    if !lock_claim(&mut tx, c).await? {
        return Err(conflict("historical P2 lease lost"));
    }
    lock_task(&mut tx, c.task).await?;
    for (table, id) in [
        ("render_jobs", c.render),
        ("dubbing_jobs", c.dub),
        ("operation_inbox", c.id),
    ] {
        let sql = format!(
            "UPDATE {table} SET lease_until=now()+$2*interval '1 second',heartbeat_at=now(),updated_at=now() WHERE id=$1"
        );
        sqlx::query(&sql)
            .bind(id)
            .bind(LEASE)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(())
}
async fn encode(
    cfg: &Config,
    store: &AssetRegistry,
    c: &Claim,
    temp: &Path,
) -> AppResult<(String, i64)> {
    let files = c.proof["files"]
        .as_array()
        .ok_or_else(|| conflict("missing pinned P2 files"))?;
    if files.len() != 3 {
        return Err(conflict("invalid pinned P2 files"));
    }
    let mut paths = Vec::new();
    for file in files {
        let key = string(file, "key")?;
        let path = store.resolve_existing(key).await?;
        if !fs::metadata(&path).await?.is_file() {
            return Err(conflict("historical P2 input is not a regular file"));
        }
        let (sha, size) = store.sha256(key).await?;
        if sha != string(file, "sha256")?
            || size <= 0
            || file
                .get("size")
                .and_then(Value::as_i64)
                .is_some_and(|n| n != size)
        {
            return Err(conflict("historical P2 input checksum mismatch"));
        }
        paths.push(path);
    }
    ffmpeg::probe_video(&cfg.ffprobe_binary, &paths[0]).await?;
    let mut profile: ffmpeg::RenderProfile = serde_json::from_value(c.proof["profile"].clone())
        .map_err(|_| conflict("invalid pinned profile"))?;
    // This narrow recovery lane is local CPU encoding, not a new GPU driver.
    profile.use_intel_gpu = false;
    let mut args = ffmpeg::encode_args(&paths[0], temp, Some(&paths[2]), &profile, None)?;
    let first_input = args
        .iter()
        .position(|s| s == "-i")
        .ok_or_else(|| conflict("missing render input"))?;
    args.splice(
        first_input + 2..first_input + 2,
        ["-i".into(), paths[1].to_string_lossy().into_owned()],
    );
    let audio = args
        .iter_mut()
        .find(|s| s.as_str() == "0:a?")
        .ok_or_else(|| conflict("missing audio mapping"))?;
    *audio = "1:a:0".into();
    let result = Command::new(&cfg.ffmpeg_binary)
        .args(args)
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if !result.status.success() {
        return Err(AppError::Unavailable(
            "historical P2 FFmpeg encoding failed".into(),
        ));
    }
    let info = ffmpeg::probe_video(&cfg.ffprobe_binary, temp).await?;
    if !info["streams"]
        .as_array()
        .is_some_and(|v| v.iter().any(|s| s["codec_type"] == "audio"))
    {
        return Err(conflict("historical P2 output has no audio"));
    }
    let f = fs::OpenOptions::new().read(true).open(temp).await?;
    f.sync_all().await?;
    drop(f);
    let canonical_root = fs::canonicalize(&store.root).await?;
    let key = temp
        .strip_prefix(&canonical_root)
        .map_err(|_| conflict("output path escaped storage"))?
        .to_str()
        .ok_or_else(|| conflict("invalid output path"))?;
    store.sha256(key).await
}
async fn settle(
    db: &Database,
    store: &AssetRegistry,
    c: &Claim,
    temp: &Path,
    result: AppResult<(String, i64)>,
) -> AppResult<()> {
    let mut tx = db.pool.begin().await?;
    if !lock_claim(&mut tx, c).await? {
        return Err(conflict("historical P2 lease lost before settlement"));
    }
    let owner = c.owner();
    let result = match result {
        Ok(r) => match lock_task(&mut tx, c.task).await {
            Ok(()) => Ok(r),
            Err(e) => Err(e),
        },
        Err(e) => Err(e),
    };
    let (sha, size) = match result {
        Ok(r) => r,
        Err(e) => {
            let message = e.to_string().chars().take(1000).collect::<String>();
            fail_locked(&mut tx, c, Some(&owner), &message).await?;
            tx.commit().await?;
            return Ok(());
        }
    };
    let key = format!(
        "v2/dubbing/{}/historical-{}-{}.mp4",
        c.task, c.render, c.token
    );
    let dest = store.resolve_existing(&key).await?;
    fs::rename(temp, &dest).await?;
    let asset = Uuid::new_v4();
    sqlx::query("INSERT INTO storage_objects(storage_key,checksum_sha256,size_bytes,state,retain_until) VALUES($1,$2,$3,'live',now()+interval '2 days')").bind(&key).bind(&sha).bind(size).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'task',$2,'video_dubbed')").bind(&key).bind(c.task).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO assets(id,task_id,kind,storage_key,sha256,size_bytes) VALUES($1,$2,'video_dubbed',$3,$4,$5)").bind(asset).bind(c.task).bind(&key).bind(&sha).bind(size).execute(&mut *tx).await?;
    sqlx::query("UPDATE render_jobs SET status='succeeded',progress=100,error_message=NULL,lease_owner=NULL,lease_until=NULL,finished_at=now(),updated_at=now() WHERE id=$1").bind(c.render).execute(&mut *tx).await?;
    sqlx::query("UPDATE dubbing_jobs SET status='succeeded',progress=100,error_message=NULL,lease_owner=NULL,lease_until=NULL,output_json=jsonb_set(output_json||jsonb_build_object('video_key',$2::text),'{youdub,stage}','\"done\"'::jsonb),updated_at=now() WHERE id=$1").bind(c.dub).bind(&key).execute(&mut *tx).await?;
    sqlx::query("UPDATE operation_inbox SET status='completed',result_json=$2,lease_owner=NULL,lease_token=NULL,lease_until=NULL,completed_at=now(),finished_at=now(),updated_at=now() WHERE id=$1").bind(c.id).bind(json!({"asset_id":asset,"video_key":key,"sha256":sha,"size_bytes":size,"historical_dubbing_id":c.dub})).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}
/// Run at most one local recovery, independent of the outbox and publishing loops.
pub async fn tick(db: &Database, cfg: &Config) -> AppResult<bool> {
    if !enabled() {
        return Ok(false);
    }
    let Some(c) = claim(db).await? else {
        return Ok(false);
    };
    let store = AssetRegistry::new(db.clone(), cfg.storage_root.clone());
    let key = format!(
        "v2/dubbing/{}/historical-{}-{}.partial.mp4",
        c.task, c.render, c.token
    );
    let temp = store.resolve_existing(&key).await?;
    if let Some(parent) = temp.parent() {
        fs::create_dir_all(parent).await?;
    }
    // Drop the encode future (and kill its child) before settling a lost lease.
    let result = {
        let encode = encode(cfg, &store, &c, &temp);
        tokio::pin!(encode);
        let mut heartbeat = tokio::time::interval(Duration::from_secs(30));
        heartbeat.tick().await;
        loop {
            tokio::select! { r=&mut encode=>break r, _=heartbeat.tick()=>{if let Err(e)=renew(db,&c).await {break Err(e);}} }
        }
    };
    let settled = settle(db, &store, &c, &temp, result).await;
    let _ = fs::remove_file(&temp).await;
    settled?;
    Ok(true)
}
