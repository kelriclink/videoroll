//! Isolated subprocess adapter for the third-party YouDub VoxCPM2/Demucs pipeline.
//! The original model runtime is not embedded inside the Tokio API.
use crate::{
    AppError, AppResult, Config, Database,
    domain::WorkItem,
    media::ffmpeg::{ensure_task_asset, latest_asset_key, probe_video},
    storage::AssetRegistry,
};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{fs, process::Command};
use uuid::Uuid;

pub async fn auto_enabled(db: &Database, job: &WorkItem) -> AppResult<bool> {
    let setting: Option<Value> =
        Some(crate::settings::effective(db, "subtitle.auto_profile").await?);
    let mut enabled = setting
        .as_ref()
        .and_then(|v| v.get("auto_dub_p2"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let settings: Option<Value> =
        sqlx::query_scalar("SELECT configuration FROM vr2_workflows WHERE id=$1")
            .bind(job.workflow_id)
            .fetch_optional(&db.pool)
            .await?;
    if let Some(value) = settings
        .as_ref()
        .and_then(|v| v.get("auto_dub_p2"))
        .and_then(Value::as_bool)
    {
        enabled = value;
    }
    if let Some(value) = settings
        .as_ref()
        .and_then(|v| v.pointer("/dubbing/enabled"))
        .and_then(Value::as_bool)
    {
        enabled = value;
    }
    Ok(enabled)
}
fn ensure_isolated_runtime(path: &Path) -> AppResult<()> {
    if !path.is_absolute() {
        return Err(AppError::Invalid(
            "YouDub runtime must be an absolute isolated path".into(),
        ));
    }
    if path == Path::new("/DATA/youdub-runtime") || path == Path::new("/") {
        return Err(AppError::Invalid(
            "v2 cannot use original production YouDub runtime".into(),
        ));
    }
    Ok(())
}
fn bridge_args<'a>(mode: &'a str, task_id: &'a str) -> Vec<&'a str> {
    vec![mode, "--task-id", task_id]
}
fn parse_json_stdout(output: &[u8]) -> AppResult<Value> {
    let value = std::str::from_utf8(output)
        .map_err(|_| AppError::Unavailable("YouDub emitted invalid UTF-8".into()))?;
    // Some third-party ML dependencies print progress before the JSON line.
    for row in value.lines().rev() {
        if let Ok(json) = serde_json::from_str::<Value>(row.trim())
            && json.is_object()
        {
            return Ok(json);
        }
    }
    Err(AppError::Unavailable(
        "YouDub adapter did not return JSON".into(),
    ))
}
async fn active_lease(db: &Database, job: &WorkItem) -> AppResult<()> {
    let active:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id WHERE j.id=$1 AND j.lease_token=$2 AND j.state='running' AND j.lease_until>now() AND w.state='running')")
        .bind(job.id).bind(job.lease_token).fetch_one(&db.pool).await?;
    if !active {
        return Err(AppError::Conflict("YouDub lease lost".into()));
    }
    Ok(())
}
async fn gpu_slot(runtime: &Path, db: &Database, job: &WorkItem) -> AppResult<std::fs::File> {
    fs::create_dir_all(runtime).await?;
    // This is the same per-runtime flock used by the old Python adapter.
    // Never hold a SQL transaction during GPU admission or inference.
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(runtime.join(".videoroll-native-gpu.lock"))?;
    use std::os::fd::AsRawFd;
    loop {
        active_lease(db, job).await?;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(file);
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::WouldBlock {
            return Err(error.into());
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}
fn missing_task(error: &AppError) -> bool {
    // Only an explicit missing task response authorizes creation. Do not
    // treat SQLite busy, transport, permission or malformed JSON as absent.
    matches!(error,AppError::NotFound(message) if message=="YouDub task absent")
}
async fn command(config: &Config, runtime: &Path, args: &[&str]) -> AppResult<Value> {
    let python = config
        .youdub_python
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("YouDub Python runtime is not configured".into()))?;
    let bridge = config
        .youdub_bridge
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("YouDub bridge path is not configured".into()))?;
    let result = Command::new(python)
        .arg(bridge)
        .args(args)
        .env("VIDEOROLL_YOUDUB_RUNTIME", runtime)
        .env("VIDEOROLL_YOUDUB_STATUS_PROTOCOL", "2")
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await?;
    if !result.status.success() {
        let error = String::from_utf8_lossy(&result.stderr);
        if args.first() == Some(&"--status")
            && result.status.code() == Some(44)
            && error.trim() == "VIDEOROLL_YOUDUB_TASK_NOT_FOUND"
        {
            return Err(AppError::NotFound("YouDub task absent".into()));
        }
        return Err(AppError::Unavailable(format!(
            "YouDub bridge exited with {}: {}",
            result.status,
            error
                .chars()
                .rev()
                .take(500)
                .collect::<String>()
                .chars()
                .rev()
                .collect::<String>()
        )));
    }
    parse_json_stdout(&result.stdout)
}
fn native_progress(value: &Value) -> (String, u64) {
    let stage = value["current_stage"].as_str().unwrap_or("");
    let rows = value["stages"].as_array();
    let count = rows.map_or(0, |r| r.len()).clamp(1, 64);
    let completed = rows
        .into_iter()
        .flatten()
        .filter(|v| matches!(v["status"].as_str(), Some("succeeded" | "skipped")))
        .count()
        .min(count);
    let partial = rows
        .into_iter()
        .flatten()
        .find(|v| {
            v["name"].as_str() == Some(stage)
                && !matches!(v["status"].as_str(), Some("succeeded" | "skipped"))
        })
        .and_then(|v| v["progress"].as_f64())
        .filter(|v| v.is_finite())
        .unwrap_or(0.)
        .clamp(0., 100.);
    let progress = ((completed as f64 + partial / 100.) * 96. / count as f64)
        .floor()
        .clamp(1., 96.) as u64;
    let status = match stage {
        "tts" => "synthesizing",
        "merge_audio" | "split_audio" => "aligning",
        "merge_video" => "mixing",
        _ => "preparing",
    };
    (status.into(), progress)
}
async fn save_progress(db: &Database, job: &WorkItem, value: &Value) -> AppResult<()> {
    let (phase, progress) = native_progress(value);
    let stage = value["current_stage"]
        .as_str()
        .unwrap_or("")
        .chars()
        .take(64)
        .collect::<String>();
    let rows=sqlx::query("UPDATE operation_inbox j SET result_json=jsonb_set(COALESCE(j.result_json,'{}'::jsonb),'{dubbing_progress}',jsonb_build_object('phase',$3::text,'progress',GREATEST($4::int,CASE WHEN jsonb_typeof(j.result_json#>'{dubbing_progress,progress}')='number' THEN (j.result_json#>>'{dubbing_progress,progress}')::int ELSE 0 END),'stage',$5::text),true),updated_at=now() WHERE j.id=$1 AND j.lease_token=$2 AND j.runtime_engine='rust-v2' AND j.status='running' AND j.lease_until>now() AND EXISTS(SELECT 1 FROM pipeline_runs w WHERE w.id=j.workflow_id AND w.state='running')")
        .bind(job.id).bind(job.lease_token).bind(phase).bind(progress as i32).bind(stage).execute(&db.pool).await?.rows_affected();
    if rows != 1 {
        return Err(AppError::Conflict(
            "YouDub lease lost while reporting progress".into(),
        ));
    }
    Ok(())
}
struct OwnedGroup(Option<u32>);
impl Drop for OwnedGroup {
    fn drop(&mut self) {
        if let Some(pid) = self.0.take() {
            if pid > 1 && pid <= i32::MAX as u32 {
                unsafe {
                    libc::kill(-(pid as i32), libc::SIGKILL);
                }
            }
        }
    }
}
// Capture an exact resident process via a Linux pidfd; never signal a
// reused PID, a process from another runtime, or an arbitrary PID file.
// The control API starts residents with setsid and a dedicated runtime.
struct ResidentGuard {
    fd: std::os::fd::OwnedFd,
    armed: bool,
}
impl ResidentGuard {
    fn capture(runtime: &Path, bridge: &Path) -> AppResult<Self> {
        use std::os::fd::{AsRawFd, FromRawFd};
        let pid = std::fs::read_to_string(runtime.join("voxcpm-resident.pid"))?
            .trim()
            .parse::<i32>()
            .map_err(|_| AppError::Conflict("invalid resident identity".into()))?;
        if pid <= 1 {
            return Err(AppError::Conflict("unsafe resident identity".into()));
        }
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
        if fd < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let fd = unsafe { std::os::fd::OwnedFd::from_raw_fd(fd as i32) };
        let cmd = std::fs::read(format!("/proc/{pid}/cmdline"))?;
        let expected = std::fs::canonicalize(bridge)?;
        use std::os::unix::ffi::OsStrExt;
        let args = cmd.split(|c| *c == 0).collect::<Vec<_>>();
        if !args.contains(&expected.as_os_str().as_bytes())
            || !args.contains(&b"--serve".as_slice())
        {
            return Err(AppError::Conflict(
                "resident process does not match bridge".into(),
            ));
        }
        let environment = std::fs::read(format!("/proc/{pid}/environ"))?;
        let root = std::fs::canonicalize(runtime)?;
        let valid = environment
            .split(|c| *c == 0)
            .filter_map(|v| v.strip_prefix(b"VIDEOROLL_YOUDUB_RUNTIME="))
            .any(|v| {
                std::fs::canonicalize(std::ffi::OsStr::from_bytes(v)).is_ok_and(|p| p == root)
            });
        if !valid {
            return Err(AppError::Conflict(
                "resident runtime identity mismatch".into(),
            ));
        }
        let mut poll = libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        if unsafe { libc::poll(&mut poll, 1, 0) } != 0 {
            return Err(AppError::Conflict("resident process exited".into()));
        }
        Ok(Self { fd, armed: false })
    }
    fn stop(&mut self) {
        use std::os::fd::AsRawFd;
        if self.armed {
            unsafe {
                libc::syscall(
                    libc::SYS_pidfd_send_signal,
                    self.fd.as_raw_fd(),
                    libc::SIGKILL,
                    std::ptr::null::<libc::siginfo_t>(),
                    0,
                );
            };
            self.armed = false;
        }
    }
    async fn wait_stopped(&self) -> AppResult<()> {
        use std::os::fd::AsRawFd;
        for _ in 0..100 {
            let mut poll = libc::pollfd {
                fd: self.fd.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            if unsafe { libc::poll(&mut poll, 1, 0) } > 0 {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        Err(AppError::Conflict(
            "resident process stop not confirmed".into(),
        ))
    }
}
impl Drop for ResidentGuard {
    fn drop(&mut self) {
        self.stop();
    }
}
async fn run_monitored(
    config: &Config,
    db: &Database,
    job: &WorkItem,
    runtime: &Path,
    id: &str,
    registry: &AssetRegistry,
) -> AppResult<Value> {
    let python = config
        .youdub_python
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("YouDub Python missing".into()))?;
    let bridge = config
        .youdub_bridge
        .as_ref()
        .ok_or_else(|| AppError::Unavailable("YouDub bridge missing".into()))?;
    let control: Value = match fs::read(runtime.join("videoroll-control.json")).await {
        Ok(raw) => serde_json::from_slice(&raw)
            .map_err(|_| AppError::Conflict("YouDub control invalid".into()))?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({}),
        Err(e) => return Err(e.into()),
    };
    if control["enabled"].as_bool() == Some(false) {
        return Err(AppError::Unavailable("YouDub disabled".into()));
    }
    let resident = control["resident"]
        .as_bool()
        .unwrap_or_else(|| std::env::var("VIDEOROLL_YOUDUB_RESIDENT").as_deref() == Ok("1"));
    let mut resident_guard = if resident {
        let state = crate::api::AppState {
            config: std::sync::Arc::new(config.clone()),
            db: db.clone(),
            scheduler: crate::scheduler::Scheduler::new(db.clone(), config.job_lease),
            storage: registry.clone(),
        };
        crate::youdub_control_v1::start_resident(&state, runtime).await?;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(180);
        loop {
            active_lease(db, job).await?;
            if crate::youdub_control_v1::resident_loaded(runtime).await {
                break;
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(AppError::Unavailable(
                    "YouDub resident startup timeout".into(),
                ));
            }
            save_progress(db, job, &json!({"current_stage":"loading_model"})).await?;
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
        Some(ResidentGuard::capture(runtime, bridge)?)
    } else {
        if fs::symlink_metadata(runtime.join("voxcpm-resident.sock"))
            .await
            .is_ok()
        {
            return Err(AppError::Conflict(
                "resident YouDub socket exists; refuse competing on-demand model".into(),
            ));
        }
        None
    };
    let log = tempfile::tempfile_in(runtime)?;
    let mut command = Command::new(python);
    command
        .arg(bridge)
        .args(bridge_args("--run", id))
        .env("VIDEOROLL_YOUDUB_RUNTIME", runtime)
        .env("VIDEOROLL_YOUDUB_STATUS_PROTOCOL", "2")
        .env(
            "VIDEOROLL_YOUDUB_RESIDENT",
            if resident { "1" } else { "0" },
        )
        .stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log.try_clone()?))
        .kill_on_drop(true)
        .process_group(0);
    active_lease(db, job).await?;
    let mut child = command.spawn()?;
    let mut group = OwnedGroup(child.id());
    if let Some(guard) = resident_guard.as_mut() {
        guard.armed = true;
    }
    let mut tick = tokio::time::interval(Duration::from_secs(5));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let outcome = loop {
        tokio::select! {
            exited=child.wait()=>{let result=exited?;group.0=None;break Ok(result);}
            _=tick.tick()=>{
                if let Err(e)=active_lease(db,job).await{break Err(e);}
                match tokio::time::timeout(Duration::from_secs(3),self::command(config,runtime,&bridge_args("--status",id))).await{
                    Ok(Ok(value))=>{if let Err(e)=save_progress(db,job,&value).await{break Err(e);}},
                    _=>{tracing::debug!(job_id=%job.id,"YouDub status sample temporarily unavailable");}
                }
            }
        }
    };
    let exit = match outcome {
        Ok(v) => v,
        Err(error) => {
            // Kill only the group we created, then wait before releasing the
            // GPU lock. Lease loss never authorizes an external replay.
            drop(group);
            let _ = child.wait().await;
            if let Some(guard) = resident_guard.as_mut() {
                guard.stop();
                guard.wait_stopped().await?;
            }
            // The owned runner has exited. Reconcile only its exact native
            // task; a later worker can resume the completed upstream stages.
            let _ = tokio::time::timeout(
                Duration::from_secs(10),
                self::command(config, runtime, &bridge_args("--interrupt", id)),
            )
            .await;
            return Err(error);
        }
    };
    if !exit.success() {
        return Err(AppError::Unavailable(format!(
            "YouDub runner exited with {exit}"
        )));
    }
    if let Some(guard) = resident_guard.as_mut() {
        guard.armed = false;
    }
    active_lease(db, job).await?;
    self::command(config, runtime, &bridge_args("--status", id)).await
}
pub async fn process(
    config: &Config,
    db: &Database,
    registry: &AssetRegistry,
    job: &WorkItem,
) -> AppResult<Value> {
    if !auto_enabled(db, job).await? {
        return Ok(json!({"skipped":"automatic P2 dubbing is disabled"}));
    }
    let no_speech:Option<Value>=sqlx::query_scalar(
        "SELECT result FROM vr2_jobs WHERE workflow_id=$1 AND stage='subtitle' AND state='succeeded'"
    ).bind(job.workflow_id).fetch_optional(&db.pool).await?.flatten();
    if no_speech
        .as_ref()
        .and_then(|v| v.get("no_speech"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        return Ok(json!({"skipped":"no credible speech: P2 was not generated"}));
    }
    let runtime = config.youdub_runtime.as_deref().ok_or_else(|| {
        AppError::Unavailable("separate YouDub v2 runtime directory must be configured".into())
    })?;
    ensure_isolated_runtime(runtime)?;
    let srt_key = latest_asset_key(db, job.task_id, "subtitle_srt")
        .await?
        .ok_or_else(|| AppError::NotFound("P2 requires translated subtitle_srt".into()))?;
    let video_key = latest_asset_key(db, job.task_id, "video_raw")
        .await?
        .ok_or_else(|| AppError::NotFound("P2 requires original video_raw".into()))?;
    let srt = registry.resolve_existing(&srt_key).await?;
    let video = registry.resolve_existing(&video_key).await?;
    let video_info = probe_video(&config.ffprobe_binary, &video).await?;
    if video_info.get("streams").is_none() || fs::metadata(&srt).await?.len() < 10 {
        return Err(AppError::Unavailable(
            "P2 source media or subtitles are invalid".into(),
        ));
    }
    let native_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("VideoRoll-v2-YouDub:{}", job.workflow_id).as_bytes(),
    );
    let id = native_id.to_string();
    let dest_key = format!("v2/dubbing/{}/{}.mp4", job.task_id, job.id);
    let dest = registry.resolve_existing(&dest_key).await?;
    let existing=sqlx::query("SELECT source_video_key,source_srt_key,output_video_key,model_job_id FROM vr2_dubbing_runs WHERE job_id=$1")
        .bind(job.id).fetch_optional(&db.pool).await?;
    use sqlx::Row;
    let source_pin = json!({
        "schema":1,"native_id":native_id,"video_key":video_key,"srt_key":srt_key,
        "video_sha256":registry.sha256(&video_key).await?.0,
        "srt_sha256":registry.sha256(&srt_key).await?.0
    });
    let pin_dir = runtime.join(".videoroll-source-pins");
    let pin = pin_dir.join(format!("{id}.json"));
    let pinned = match fs::read(&pin).await {
        Ok(bytes) => {
            let previous: Value = serde_json::from_slice(&bytes)
                .map_err(|_| AppError::Conflict("YouDub source pin is invalid".into()))?;
            if previous != source_pin {
                return Err(AppError::Conflict(
                    "YouDub input changed after native task creation".into(),
                ));
            }
            true
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
        Err(e) => return Err(e.into()),
    };
    if let Some(proof) = existing.as_ref() {
        if !pinned {
            return Err(AppError::Conflict(
                "confirmed YouDub receipt has no input fingerprint".into(),
            ));
        }
        if proof.try_get::<String, _>("source_video_key")? != video_key
            || proof.try_get::<String, _>("source_srt_key")? != srt_key
            || proof.try_get::<String, _>("output_video_key")? != dest_key
            || proof.try_get::<Uuid, _>("model_job_id")? != native_id
        {
            return Err(AppError::Conflict(
                "YouDub source or receipt changed; cannot reuse output".into(),
            ));
        }
        if fs::metadata(&dest).await.is_ok()
            && probe_video(&config.ffprobe_binary, &dest).await.is_ok()
        {
            let expected:Option<String>=sqlx::query_scalar("SELECT sha256 FROM assets WHERE task_id=$1 AND kind::text='video_dubbed' AND storage_key=$2 ORDER BY created_at DESC LIMIT 1")
                .bind(job.task_id).bind(&dest_key).fetch_optional(&db.pool).await?.flatten();
            let actual = registry.sha256(&dest_key).await?.0;
            if expected
                .as_ref()
                .is_some_and(|v| v.eq_ignore_ascii_case(&actual))
            {
                active_lease(db, job).await?;
                let outcome =
                    ensure_task_asset(db, registry, job.task_id, job, "video_dubbed", &dest_key)
                        .await?;
                return Ok(json!({"native_id":native_id,"video_key":dest_key,"result":outcome}));
            }
            return Err(AppError::Conflict(
                "YouDub output checksum is absent or mismatched".into(),
            ));
        }
    }
    let _gpu = gpu_slot(runtime, db, job).await?;
    active_lease(db, job).await?;
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).await?;
    }
    // The dedicated v2 runtime is persistent, and can resume YouDub stages.
    command(config, runtime, &["--prepare"]).await?;
    // Keep the input identity durable across creation, interrupted copies,
    // retries and native status recovery. Never attach an old model result
    // to a changed subtitle/source just because the workflow UUID matches.
    fs::create_dir_all(&pin_dir).await?;
    let pinned = match fs::read(&pin).await {
        Ok(bytes) => {
            let previous: Value = serde_json::from_slice(&bytes)
                .map_err(|_| AppError::Conflict("invalid YouDub source pin".into()))?;
            if previous != source_pin {
                return Err(AppError::Conflict(
                    "YouDub source changed while waiting for GPU".into(),
                ));
            }
            true
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
        Err(e) => return Err(e.into()),
    };
    let state = match command(config, runtime, &bridge_args("--status", &id)).await {
        Ok(value) if pinned || existing.is_some() => value,
        Ok(_) => {
            return Err(AppError::Conflict(
                "unproven historical YouDub task; reconcile source receipt before reuse".into(),
            ));
        }
        Err(error) if missing_task(&error) => {
            if existing.is_some() {
                return Err(AppError::Conflict(
                    "previously confirmed YouDub task is absent".into(),
                ));
            }
            if !pinned {
                let temporary = pin.with_extension(format!("{}.partial", Uuid::new_v4()));
                let bytes = serde_json::to_vec(&source_pin)
                    .map_err(|_| AppError::Invalid("YouDub source pin encode failed".into()))?;
                {
                    use tokio::io::AsyncWriteExt;
                    let mut file = fs::OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(&temporary)
                        .await?;
                    file.write_all(&bytes).await?;
                    file.sync_all().await?;
                }
                fs::rename(&temporary, &pin).await?;
            }
            let video = video
                .to_str()
                .ok_or_else(|| AppError::Invalid("YouDub video path is not UTF-8".into()))?;
            let srt = srt
                .to_str()
                .ok_or_else(|| AppError::Invalid("YouDub SRT path is not UTF-8".into()))?;
            let mut args = vec![
                "--create",
                "--task-id",
                &id,
                "--video",
                video,
                "--srt",
                srt,
                "--direction",
                "en-zh",
            ];
            if config.remote_renderer_enabled
                && std::env::var("VIDEOROLL_V2_YOUDUB_AUDIO_ONLY")
                    .ok()
                    .as_deref()
                    == Some("1")
            {
                args.push("--audio-only");
            }
            command(config, runtime, &args).await?
        }
        Err(error) => return Err(error),
    };
    let status = state.get("status").and_then(Value::as_str).unwrap_or("");
    let final_result = match status {
        "succeeded" => state,
        "queued" | "paused" => run_monitored(config, db, job, runtime, &id, registry).await?,
        "failed" => {
            command(config, runtime, &bridge_args("--resume", &id)).await?;
            run_monitored(config, db, job, runtime, &id, registry).await?
        }
        other => {
            return Err(AppError::Unavailable(format!(
                "YouDub has non-resumable state {other}; manual reconciliation required"
            )));
        }
    };
    if final_result.get("status").and_then(Value::as_str) != Some("succeeded") {
        return Err(AppError::Unavailable(
            "YouDub did not report completed status".into(),
        ));
    }
    // A source may be replaced while the model is running. Do not attach
    // audio or video generated from the old snapshot to a new source.
    if registry.sha256(&video_key).await?.0 != source_pin["video_sha256"].as_str().unwrap_or("")
        || registry.sha256(&srt_key).await?.0 != source_pin["srt_sha256"].as_str().unwrap_or("")
    {
        return Err(AppError::Conflict(
            "YouDub inputs changed during inference".into(),
        ));
    }
    if final_result["audio_only"] == true {
        let ticket = super::dubbing_handoff::enqueue(
            config,
            db,
            registry,
            job,
            runtime,
            native_id,
            &video_key,
            &srt_key,
            &final_result,
        )
        .await?;
        return Ok(json!({"remote_render_ticket":ticket}));
    }
    let output = final_result
        .get("output_video")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .ok_or_else(|| AppError::Unavailable("YouDub output path absent".into()))?;
    // The adapter must never import arbitrary paths returned by a third-party script.
    let root = fs::canonicalize(runtime).await?;
    let source = fs::canonicalize(&output).await?;
    if !source.starts_with(&root) {
        return Err(AppError::Invalid(
            "YouDub produced an output outside v2 isolated runtime".into(),
        ));
    }
    probe_video(&config.ffprobe_binary, &source).await?;
    let temp = dest.with_extension("partial");
    fs::copy(&source, &temp).await?;
    let file = fs::OpenOptions::new().read(true).open(&temp).await?;
    file.sync_all().await?;
    drop(file);
    active_lease(db, job).await?;
    if let Ok(meta) = fs::metadata(&dest).await {
        // A prior worker may have copied the exact native result and died
        // before writing its proof. Compare actual bytes, not existence.
        // Use registry relative path with the actual extension replacement.
        let partial_key = Path::new(&dest_key)
            .with_extension("partial")
            .to_string_lossy()
            .into_owned();
        let same = meta.len() == fs::metadata(&temp).await?.len()
            && registry.sha256(&dest_key).await?.0 == registry.sha256(&partial_key).await?.0;
        if !same {
            let _ = fs::remove_file(&temp).await;
            return Err(AppError::Conflict(
                "existing YouDub output differs from recovered native receipt".into(),
            ));
        }
        fs::remove_file(&temp).await?;
    } else {
        fs::rename(temp, &dest).await?;
    }
    let outcome =
        ensure_task_asset(db, registry, job.task_id, job, "video_dubbed", &dest_key).await?;
    sqlx::query(
        "UPDATE operation_inbox SET
            dubbing_source_video_key=$4,dubbing_source_srt_key=$5,
            dubbing_output_video_key=$6,dubbing_model_job_id=$7,
            dubbing_model_status='succeeded',dubbing_run_created_at=now(),updated_at=now()
          WHERE id=$1 AND workflow_id=$2 AND task_id=$3
            AND runtime_engine='rust-v2' AND stage='dubbing'
            AND dubbing_model_status IS NULL
            AND EXISTS(SELECT 1 FROM vr2_jobs j JOIN vr2_workflows w ON w.id=j.workflow_id
                WHERE j.id=$1 AND j.lease_token=$8 AND j.state='running' AND j.lease_until>now() AND w.state='running')",
    )
    .bind(job.id)
    .bind(job.workflow_id)
    .bind(job.task_id)
    .bind(&video_key)
    .bind(&srt_key)
    .bind(&dest_key)
    .bind(native_id)
    .bind(job.lease_token)
    .execute(&db.pool)
    .await?;
    // Completed dubbing is an immutable proof for the selected subtitle and
    // source media. Never silently replace it when a replay differs.
    let proof = sqlx::query(
        "SELECT source_video_key,source_srt_key,output_video_key,model_job_id
         FROM vr2_dubbing_runs WHERE job_id=$1",
    )
    .bind(job.id)
    .fetch_optional(&db.pool)
    .await?
    .ok_or_else(|| {
        AppError::Conflict("native YouDub operation no longer owns dubbing evidence".into())
    })?;
    if proof.try_get::<String, _>("source_video_key")? != video_key
        || proof.try_get::<String, _>("source_srt_key")? != srt_key
        || proof.try_get::<String, _>("output_video_key")? != dest_key
        || proof.try_get::<Uuid, _>("model_job_id")? != native_id
    {
        return Err(AppError::Conflict(
            "native YouDub output differs from already persisted proof".into(),
        ));
    }
    Ok(json!({"native_id":native_id,"video_key":dest_key,"result":outcome}))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn progress_matches_native_stage_and_is_bounded() {
        let (phase, progress) = native_progress(
            &json!({"current_stage":"tts","stages":[{"name":"download","status":"succeeded","progress":100},{"name":"tts","status":"running","progress":50},{"name":"merge_video","status":"queued"}]}),
        );
        assert_eq!(phase, "synthesizing");
        assert_eq!(progress, 48);
        assert_eq!(
            native_progress(&json!({"current_stage":"waiting_gpu","stages":[]})),
            ("preparing".into(), 1)
        );
    }
    #[test]
    fn only_explicit_absence_allows_native_create() {
        assert!(missing_task(&AppError::NotFound(
            "YouDub task absent".into()
        )));
        assert!(!missing_task(&AppError::Unavailable(
            "SQLite database locked".into()
        )));
        assert!(!missing_task(&AppError::Unavailable(
            "Unknown YouDub task".into()
        )));
    }
    #[test]
    fn requires_dedicated_external_runtime() {
        assert!(ensure_isolated_runtime(Path::new("/DATA/youdub-runtime")).is_err());
        assert!(ensure_isolated_runtime(Path::new("/var/lib/videoroll-v2/youdub")).is_ok());
        assert!(ensure_isolated_runtime(Path::new("relative/work")).is_err());
    }
    #[test]
    fn bridge_output_requires_result_object() {
        assert!(parse_json_stdout(b"warning\n{\"status\":\"succeeded\"}\n").is_ok());
        assert!(parse_json_stdout(b"something failed\n").is_err());
    }
}
