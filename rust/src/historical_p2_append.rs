//! Admit historical P1/P2 evidence without manufacturing native publication
//! receipts. All actual uploads and edits use the shared Bilibili P2 executor.
use crate::{AppError, AppResult, Database, storage::AssetRegistry};
use serde_json::{Value, json};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;
const ENGINE: &str = "rust-historical-p2-append";
fn conflict(s: &str) -> AppError {
    AppError::Conflict(s.into())
}
fn positive(v: &Value) -> Option<i64> {
    v.as_i64()
        .or_else(|| v.as_str()?.parse().ok())
        .filter(|n| *n > 0)
}
fn identity(v: &Value) -> Option<&str> {
    v.as_str().filter(|s| !s.is_empty())
}
#[derive(Debug, Clone)]
pub(crate) struct Evidence {
    pub aid: i64,
    pub cid: i64,
    pub bvid: String,
    pub key: String,
    pub sha: String,
    pub size: i64,
    pub recovery: Uuid,
    pub dub: Uuid,
    pub title: String,
}
impl Evidence {
    fn proof(&self, pub_id: Uuid) -> Value {
        json!({"publish_job_id":pub_id,"historical_dubbing_id":self.dub,"recovery_id":self.recovery,"aid":self.aid,"p1_cid":self.cid,"bvid":self.bvid,"video_key":self.key,"sha256":self.sha,"size_bytes":self.size})
    }
}
async fn evidence(
    tx: &mut Transaction<'_, Postgres>,
    task: Uuid,
    pub_id: Uuid,
    dub: Uuid,
) -> AppResult<Evidence> {
    let owner:Option<bool>=sqlx::query_scalar("SELECT runtime_owner='rust' AND status::text='published' FROM tasks WHERE id=$1 FOR UPDATE").bind(task).fetch_optional(&mut **tx).await?;
    if owner != Some(true) {
        return Err(conflict(
            "historical append requires Rust-owned published task",
        ));
    }
    let p=sqlx::query("SELECT platform::text AS platform,state::text AS state,aid::text AS aid,bvid,response_json,meta_json,account_id,bili_account_id,lease_owner,lease_until FROM publish_jobs WHERE id=$1 AND task_id=$2 FOR UPDATE").bind(pub_id).bind(task).fetch_optional(&mut **tx).await?.ok_or_else(||conflict("historical P1 missing"))?;
    if p.try_get::<String, _>("platform")? != "bilibili"
        || p.try_get::<String, _>("state")? != "published"
        || p.try_get::<Option<Uuid>, _>("account_id")?.is_some()
        || p.try_get::<Option<Uuid>, _>("bili_account_id")?.is_some()
        || p.try_get::<Option<String>, _>("lease_owner")?.is_some()
        || p.try_get::<Option<chrono::DateTime<chrono::Utc>>, _>("lease_until")?
            .is_some()
    {
        return Err(conflict(
            "historical P1 requires confirmed default-account Bilibili archive without lease",
        ));
    }
    let aid = p
        .try_get::<Option<String>, _>("aid")?
        .and_then(|s| s.parse::<i64>().ok())
        .filter(|n| *n > 0)
        .ok_or_else(|| conflict("historical P1 aid missing"))?;
    let bvid = p
        .try_get::<Option<String>, _>("bvid")?
        .filter(|s| s.starts_with("BV") && s.len() <= 32)
        .ok_or_else(|| conflict("historical P1 bvid missing"))?;
    let response = p
        .try_get::<Option<Value>, _>("response_json")?
        .unwrap_or(Value::Null);
    if response.get("mode").and_then(Value::as_str) != Some("web")
        || response.get("reused_from_job_id").is_some()
        || positive(&response["result"]["aid"]) != Some(aid)
        || identity(&response["result"]["bvid"]) != Some(bvid.as_str())
    {
        return Err(conflict("historical P1 receipt identity mismatch"));
    }
    let parts = response["video_parts"]
        .as_array()
        .filter(|a| a.len() == 1)
        .ok_or_else(|| conflict("historical P1 must have exactly one recorded part"))?;
    let cid = positive(&parts[0]["upload"]["biz_id"])
        .ok_or_else(|| conflict("historical P1 CID missing"))?;
    // A legacy append could already have edited the archive. No guessing from
    // titles or reusing an old upload receipt. Only absent or definitely
    // pre-upload failed records can be admitted, and delivery must be settled.
    if let Some(old) = response.get("auto_dub_p2") {
        if old["state"] != "failed"
            || old.get("upload").is_some_and(|v| !v.is_null())
            || old.get("cid").is_some_and(|v| !v.is_null())
            || old.get("upload_debug").is_some_and(|v| !v.is_null())
            || old.get("recovered").and_then(Value::as_bool) == Some(true)
        {
            return Err(conflict("legacy P2 outcome requires manual reconciliation"));
        }
    }
    let unsettled:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pipeline_runs WHERE task_id=$1 AND state NOT IN ('succeeded','failed','canceled','completed','launch_failed')) OR EXISTS(SELECT 1 FROM publish_jobs WHERE task_id=$1 AND (state::text IN ('draft','submitted','submitting','unknown') OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)) OR EXISTS(SELECT 1 FROM publish_jobs WHERE id<>$2 AND platform::text='bilibili' AND aid::text=$3) OR EXISTS(SELECT 1 FROM outbox_events e WHERE e.aggregate_id=$2::text AND e.task_name='bilibili_publisher.auto_dub_p2' AND NOT(e.status IN ('dispatched','native_completed') AND EXISTS(SELECT 1 FROM operation_inbox i WHERE i.operation_key=e.operation_key AND i.status='completed'))) ").bind(task).bind(pub_id).bind(aid.to_string()).fetch_one(&mut **tx).await?;
    if unsettled {
        return Err(conflict(
            "historical archive has unsettled delivery or duplicate identity",
        ));
    }
    let d=sqlx::query("SELECT output_json FROM dubbing_jobs WHERE id=$1 AND task_id=$2 AND status='succeeded' AND lease_owner IS NULL AND lease_until IS NULL FOR UPDATE").bind(dub).bind(task).fetch_optional(&mut **tx).await?.ok_or_else(||conflict("historical dubbing not succeeded"))?;
    let output: Value = d.try_get("output_json")?;
    let render = identity(&output["render_job_id"])
        .and_then(|s| Uuid::parse_str(s).ok())
        .ok_or_else(|| conflict("historical render identity missing"))?;
    let key = identity(&output["video_key"])
        .filter(|s| s.starts_with(&format!("v2/dubbing/{task}/historical-{render}-")))
        .ok_or_else(|| conflict("historical recovered P2 key missing"))?
        .to_owned();
    if output.pointer("/youdub/stage").and_then(Value::as_str) != Some("done") {
        return Err(conflict("historical dubbing stage incomplete"));
    }
    let r=sqlx::query("SELECT i.id,i.result_json FROM operation_inbox i JOIN render_jobs r ON r.id=$1 AND r.task_id=$2 AND r.status::text='succeeded' AND r.lease_owner IS NULL AND r.lease_until IS NULL WHERE i.operation_key=$3 AND i.runtime_engine='rust-historical-p2-render' AND i.task_id=$2 AND i.status='completed' AND i.request_json->>'dubbing_id'=$4 AND i.request_json->>'render_id'=$1::text AND i.result_json->>'historical_dubbing_id'=$4 AND i.result_json->>'video_key'=$5 FOR UPDATE OF i,r").bind(render).bind(task).bind(format!("rust-historical-p2-render:{render}")).bind(dub.to_string()).bind(&key).fetch_optional(&mut **tx).await?.ok_or_else(||conflict("exact historical recovery proof missing"))?;
    let recovery: Uuid = r.try_get("id")?;
    let result: Value = r.try_get("result_json")?;
    let asset = identity(&result["asset_id"])
        .and_then(|s| Uuid::parse_str(s).ok())
        .ok_or_else(|| conflict("recovery asset missing"))?;
    let sha = identity(&result["sha256"])
        .filter(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(|| conflict("recovery checksum missing"))?
        .to_owned();
    let size = positive(&result["size_bytes"]).ok_or_else(|| conflict("recovery size missing"))?;
    let live:Option<String>=sqlx::query_scalar("SELECT o.storage_key FROM storage_objects o JOIN assets a ON a.storage_key=o.storage_key WHERE a.id=$1 AND a.task_id=$2 AND a.kind::text='video_dubbed' AND o.storage_key=$3 AND a.sha256=$4 AND a.size_bytes=$5 AND o.checksum_sha256=$4 AND o.size_bytes=$5 AND o.state='live' AND EXISTS(SELECT 1 FROM storage_object_refs ref WHERE ref.storage_key=o.storage_key AND ref.owner_type='task' AND ref.owner_id=$2 AND ref.role='video_dubbed') FOR UPDATE OF o").bind(asset).bind(task).bind(&key).bind(&sha).bind(size).fetch_optional(&mut **tx).await?;
    if live.is_none() {
        return Err(conflict("historical P2 asset ledger mismatch"));
    }
    let meta: Value = p.try_get("meta_json")?;
    let title = identity(&meta["meta"]["title"])
        .or_else(|| identity(&meta["title"]))
        .unwrap_or("已发布视频")
        .to_owned();
    Ok(Evidence {
        aid,
        cid,
        bvid,
        key,
        sha,
        size,
        recovery,
        dub,
        title,
    })
}
pub(crate) async fn enqueue(
    db: &Database,
    task: Uuid,
    pub_id: Uuid,
    dub: Uuid,
    title: Option<&str>,
) -> AppResult<Value> {
    if !crate::historical_p2_render::enabled() {
        return Err(conflict(
            "historical append requires native-only mode after legacy drain",
        ));
    }
    let mut tx = db.pool.begin().await?;
    let ev = evidence(&mut tx, task, pub_id, dub).await?;
    // Same archive lock as native enqueue; no parallel independent P2 intent.
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(ev.aid)
        .execute(&mut *tx)
        .await?;
    let other: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM vr2_bilibili_p2_appends WHERE aid=$1 AND publish_job_id<>$2)",
    )
    .bind(ev.aid)
    .bind(pub_id)
    .fetch_one(&mut *tx)
    .await?;
    if other {
        return Err(conflict("another P2 intent owns the archive"));
    }
    let title = crate::bili_p2::trim_title(title, &ev.title)?;
    let proof = ev.proof(pub_id);
    let prior=sqlx::query("SELECT runtime_engine,task_id,request_json,p2_state,p2_title,p2_attempt FROM operation_inbox WHERE id=$1 FOR UPDATE").bind(pub_id).fetch_optional(&mut *tx).await?;
    if let Some(row) = prior {
        if row.try_get::<String, _>("runtime_engine")? != ENGINE
            || row.try_get::<Option<Uuid>, _>("task_id")? != Some(task)
            || row.try_get::<Value, _>("request_json")? != proof
            || row.try_get::<Option<String>, _>("p2_title")?.as_deref() != Some(title.as_str())
        {
            return Err(conflict("historical P2 intent identity collision"));
        }
        let status: Option<String> = row.try_get("p2_state")?;
        let attempt: Option<i32> = row.try_get("p2_attempt")?;
        if matches!(
            status.as_deref(),
            Some("queued" | "uploading" | "uploaded" | "submitting")
        ) {
            return Ok(
                json!({"publish_job_id":pub_id,"dubbing_job_id":dub,"aid":ev.aid,"state":status,"attempt":attempt}),
            );
        }
        // Do not automatically retry historical failure/unknown/published.
        return Err(conflict(
            "historical P2 intent already settled; explicit reconciliation required",
        ));
    }
    sqlx::query("INSERT INTO operation_inbox(id,operation_key,runtime_engine,task_id,status,request_json,completed_at,p2_dubbing_job_id,p2_aid,p2_output_video_key,p2_title,p2_state,p2_attempt,p2_created_at) VALUES($1,$2,$3,$4,'completed',$5,now(),$6,$7,$8,$9,'queued',1,now())").bind(pub_id).bind(format!("rust-historical-p2-append:{pub_id}")).bind(ENGINE).bind(task).bind(proof).bind(ev.recovery).bind(ev.aid).bind(&ev.key).bind(title).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO storage_object_refs(storage_key,owner_type,owner_id,role) VALUES($1,'native_bilibili_p2',$2,'p2_video') ON CONFLICT DO NOTHING").bind(&ev.key).bind(pub_id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(
        json!({"publish_job_id":pub_id,"dubbing_job_id":dub,"aid":ev.aid,"state":"queued","attempt":1}),
    )
}
/// Re-check the exact original receipt and asset immediately before the worker
/// uses them. Never accept a different historical dubbing or current asset.
pub(crate) async fn execution_evidence(
    db: &Database,
    store: &AssetRegistry,
    pub_id: Uuid,
    task: Uuid,
    recovery: Uuid,
) -> AppResult<Option<Evidence>> {
    let proof: Option<Value> = sqlx::query_scalar(
        "SELECT request_json FROM operation_inbox WHERE id=$1 AND task_id=$2 AND runtime_engine=$3",
    )
    .bind(pub_id)
    .bind(task)
    .bind(ENGINE)
    .fetch_optional(&db.pool)
    .await?;
    let Some(proof) = proof else { return Ok(None) };
    if !crate::historical_p2_render::enabled() {
        return Err(conflict("historical append disabled"));
    }
    let dub = identity(&proof["historical_dubbing_id"])
        .and_then(|s| Uuid::parse_str(s).ok())
        .ok_or_else(|| conflict("historical dubbing proof invalid"))?;
    let mut tx = db.pool.begin().await?;
    let ev = evidence(&mut tx, task, pub_id, dub).await?;
    if ev.recovery != recovery || ev.proof(pub_id) != proof {
        return Err(conflict("historical P2 source evidence changed"));
    }
    tx.commit().await?;
    let (sha, size) = store.sha256(&ev.key).await?;
    if sha != ev.sha || size != ev.size {
        return Err(conflict("historical P2 bytes changed"));
    }
    Ok(Some(ev))
}
