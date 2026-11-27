//! Optional post-publication uploader collection synchronization. The archive
//! receipt is immutable. A write intent is committed before each season POST;
//! uncertain writes are reconciled by reads, never automatically replayed.
use super::*;
use sha2::{Digest, Sha256};
use sqlx::{Connection, PgConnection};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
struct Collection {
    state: String,
    title: String,
    account_id: Option<Uuid>,
    credential_sha256: String,
    attempts: u32,
    due_at: i64,
    season_id: Option<i64>,
    section_id: Option<i64>,
    reason: Option<String>,
}
fn normalize_title(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .filter(|c| !c.is_control())
        .take(80)
        .collect::<String>()
        .trim_end()
        .to_owned()
}
fn author(info: &Value) -> Option<String> {
    ["uploader", "channel", "uploader_id", "channel_id"]
        .iter()
        .filter_map(|k| info[*k].as_str())
        .map(normalize_title)
        .find(|v| !v.is_empty())
}
fn credential_hash(cookie: &str, csrf: &str) -> String {
    let mut h = Sha256::new();
    h.update((cookie.len() as u64).to_be_bytes());
    h.update(cookie.as_bytes());
    h.update(csrf.as_bytes());
    hex::encode(h.finalize())
}
pub(super) async fn intent(
    db: &Database,
    store: &AssetRegistry,
    task: Uuid,
    account: Option<Uuid>,
    cookie: &str,
    csrf: &str,
) -> AppResult<Option<Value>> {
    let source: Option<String> =
        sqlx::query_scalar("SELECT source_type::text FROM tasks WHERE id=$1")
            .bind(task)
            .fetch_optional(&db.pool)
            .await?;
    if source.as_deref() != Some("youtube") {
        return Ok(None);
    }
    let mut title = None;
    if let Some(key) = latest_asset_key(db, task, "metadata_json").await? {
        if let Ok(path) = store.resolve_existing(&key).await {
            if fs::metadata(&path)
                .await
                .is_ok_and(|m| m.len() <= 2_000_000)
            {
                if let Ok(bytes) = fs::read(path).await {
                    if bytes.len() <= 2_000_000 {
                        if let Ok(info) = serde_json::from_slice::<Value>(&bytes) {
                            title = author(&info);
                        }
                    }
                }
            }
        }
    }
    if title.is_none() {
        let saved: Option<String> =
            sqlx::query_scalar("SELECT uploader FROM youtube_video_meta WHERE task_id=$1")
                .bind(task)
                .fetch_optional(&db.pool)
                .await?
                .flatten();
        title = saved.map(|v| normalize_title(&v)).filter(|v| !v.is_empty());
    }
    Ok(title.map(|title| {
        json!(Collection {
            state: "pending".into(),
            title,
            account_id: account,
            credential_sha256: credential_hash(cookie, csrf),
            attempts: 0,
            due_at: chrono::Utc::now().timestamp() + 300,
            season_id: None,
            section_id: None,
            reason: None
        })
    }))
}
fn public_state(c: &Collection) -> Value {
    json!({"ok":c.state=="succeeded","skipped":c.state=="skipped","pending":matches!(c.state.as_str(),"pending"|"creating"|"attaching"),"state":c.state,"title":c.title,"season_id":c.season_id,"section_id":c.section_id,"attempts":c.attempts,"reason":c.reason})
}
#[derive(Clone, Debug, PartialEq)]
struct Season {
    id: i64,
    section: i64,
    title: String,
}
fn positive(v: &Value) -> Option<i64> {
    v.as_i64()
        .or_else(|| v.as_str()?.parse().ok())
        .filter(|v| *v > 0)
}
fn assigned(value: &Value) -> AppResult<Option<(i64, String)>> {
    if value["code"].as_i64() == Some(-404) {
        return Ok(None);
    }
    BilibiliClient::ensure_ok(value, false)?;
    let data = value
        .get("data")
        .ok_or_else(|| AppError::Unavailable("archive season data missing".into()))?;
    if data.is_null() || data.as_object().is_some_and(|v| v.is_empty()) {
        return Ok(None);
    }
    let season = data.get("season").unwrap_or(data);
    if season.is_null() {
        return Ok(None);
    }
    let id = positive(&season["id"])
        .ok_or_else(|| AppError::Unavailable("invalid assigned season identity".into()))?;
    let title = normalize_title(season["title"].as_str().unwrap_or(""));
    if title.is_empty() {
        return Err(AppError::Unavailable(
            "assigned season title missing".into(),
        ));
    }
    Ok(Some((id, title)))
}
fn season_ref(item: &Value) -> Option<Season> {
    let id = positive(&item["season"]["id"])?;
    let title = normalize_title(item["season"]["title"].as_str()?);
    let section = item
        .pointer("/sections/sections")?
        .as_array()?
        .iter()
        .find_map(|s| positive(&s["id"]))?;
    (!title.is_empty()).then_some(Season { id, section, title })
}
async fn lookup(
    client: &BilibiliClient,
    title: &str,
    known: Option<i64>,
) -> AppResult<Option<Season>> {
    let mut matches = Vec::new();
    for page in 1..=100 {
        let value = BilibiliClient::read_json(
            client
                .member
                .get(client.url("x2/creative/web/seasons")?)
                .query(&[
                    ("pn", page.to_string()),
                    ("ps", "30".into()),
                    ("order", "mtime".into()),
                    ("sort", "desc".into()),
                    ("draft", "1".into()),
                ])
                .send()
                .await?,
        )
        .await?;
        BilibiliClient::ensure_ok(&value, false)?;
        let data = &value["data"];
        let rows = data["seasons"]
            .as_array()
            .ok_or_else(|| AppError::Unavailable("invalid season list".into()))?;
        for row in rows {
            if row["season"]["title"].as_str().is_none() || positive(&row["season"]["id"]).is_none()
            {
                return Err(AppError::Unavailable("malformed season list entry".into()));
            }
            let same_title =
                normalize_title(row["season"]["title"].as_str().unwrap_or("")) == title;
            let same_id = known.is_some_and(|id| positive(&row["season"]["id"]) == Some(id));
            if same_title || same_id {
                let season = season_ref(row)
                    .ok_or_else(|| AppError::Unavailable("matching season not ready".into()))?;
                if season.title != title || known.is_some_and(|id| id != season.id) {
                    continue;
                }
                if !matches.iter().any(|v: &Season| v.id == season.id) {
                    matches.push(season);
                }
            }
        }
        if rows.len() < 30 || data["total"].as_i64().is_some_and(|n| n <= page * 30) {
            return if matches.len() > 1 {
                Err(AppError::Conflict(
                    "multiple matching uploader collections".into(),
                ))
            } else {
                Ok(matches.pop())
            };
        }
    }
    Err(AppError::Unavailable(
        "collection list exceeded page budget".into(),
    ))
}
fn safe_cover(raw: &str) -> Option<String> {
    let raw = if raw.starts_with("//") {
        format!("https:{raw}")
    } else {
        raw.to_owned()
    };
    let url = Url::parse(&raw).ok()?;
    let host = url.host_str()?;
    if url.scheme() != "https"
        || !(host == "hdslb.com" || host.ends_with(".hdslb.com"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || raw.len() > 2048
    {
        return None;
    }
    Some(raw)
}
async fn owner_media(
    client: &BilibiliClient,
    aid: i64,
    bvid: &str,
) -> AppResult<(i64, String, Option<String>)> {
    let value = BilibiliClient::read_json(
        client
            .member
            .get(client.url("x/vupre/web/archive/view")?)
            .query(&[("aid", aid)])
            .send()
            .await?,
    )
    .await?;
    BilibiliClient::ensure_ok(&value, false)?;
    let archive = &value["data"]["archive"];
    if positive(&archive["aid"]) != Some(aid) || archive["bvid"].as_str().is_some_and(|v| v != bvid)
    {
        return Err(AppError::Conflict(
            "collection owner archive identity mismatch".into(),
        ));
    }
    let cid = value
        .pointer("/data/videos/0/cid")
        .and_then(positive)
        .ok_or_else(|| AppError::Unavailable("published video not ready".into()))?;
    let title = normalize_title(archive["title"].as_str().unwrap_or(""));
    if title.is_empty() {
        return Err(AppError::Unavailable(
            "published archive title absent".into(),
        ));
    }
    let cover = archive["cover"]
        .as_str()
        .or_else(|| archive["pic"].as_str())
        .and_then(safe_cover);
    Ok((cid, title, cover))
}
#[derive(sqlx::FromRow)]
struct Record {
    id: Uuid,
    task_id: Uuid,
    workflow_id: Uuid,
    result: Value,
}
async fn persist(db: &Database, r: &mut Record, c: &Collection) -> AppResult<()> {
    let next = serde_json::to_value(c)
        .map_err(|_| AppError::Invalid("collection state invalid".into()))?;
    let mut tx = db.pool.begin().await?;
    if c.state == "creating" && r.result["collection_sync"]["state"] == "pending" {
        let key = format!(
            "videoroll:bili:collection-intent:{}:{}",
            c.credential_sha256, c.title
        );
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))")
            .bind(key)
            .execute(&mut *tx)
            .await?;
        let uncertain:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE id<>$1 AND stage='publish' AND state='succeeded' AND result->'collection_sync'->>'title'=$2 AND result->'collection_sync'->>'credential_sha256'=$3 AND result->'collection_sync'->>'state' IN ('creating','unknown'))")
            .bind(r.id).bind(&c.title).bind(&c.credential_sha256).fetch_one(&mut *tx).await?;
        if uncertain {
            return Err(AppError::Conflict(
                "another collection creation intent is unresolved".into(),
            ));
        }
    }
    let rows=sqlx::query("UPDATE vr2_jobs SET result=jsonb_set(jsonb_set(result,'{collection_sync}',$2::jsonb,true),'{collection}',$3::jsonb,true),updated_at=now() WHERE id=$1 AND stage='publish' AND state='succeeded' AND result->'collection_sync'=$4::jsonb AND result->>'bvid'=$5")
        .bind(r.id).bind(&next).bind(public_state(c)).bind(&r.result["collection_sync"]).bind(r.result["bvid"].as_str().unwrap_or("")).execute(&mut *tx).await?.rows_affected();
    if rows != 1 {
        return Err(AppError::Conflict(
            "collection state changed concurrently".into(),
        ));
    }
    // Mirror the optional result to the original publish_jobs contract. Do
    // not change its published state, receipt, metadata or upload progress.
    sqlx::query("UPDATE publish_jobs SET response_json=jsonb_set(COALESCE(response_json,'{}'::jsonb),'{collection}',$1::jsonb,true),updated_at=now() WHERE id=(SELECT (configuration->'bilibili_direct_v1'->>'publish_job_id')::uuid FROM vr2_workflows WHERE id=$2) AND task_id=$3 AND state::text='published' AND bvid=$4")
        .bind(public_state(c)).bind(r.workflow_id).bind(r.task_id).bind(r.result["bvid"].as_str().unwrap_or("")).execute(&mut *tx).await?;
    tx.commit().await?;
    r.result["collection_sync"] = next;
    r.result["collection"] = public_state(c);
    Ok(())
}
fn defer(c: &mut Collection, reason: &str) {
    c.reason = Some(reason.into());
    c.attempts = c.attempts.saturating_add(1);
    if c.attempts >= 97 {
        c.state = if c.state == "pending" {
            "failed"
        } else {
            "unknown"
        }
        .into();
    }
    c.due_at = chrono::Utc::now().timestamp() + 3600;
}
async fn step(
    db: &Database,
    r: &mut Record,
    c: &mut Collection,
    client: &BilibiliClient,
    csrf: &str,
) -> AppResult<()> {
    let aid = positive(&r.result["aid"])
        .ok_or_else(|| AppError::Invalid("collection receipt aid missing".into()))?;
    let bvid = r.result["bvid"]
        .as_str()
        .filter(|v| v.starts_with("BV") && v.len() <= 32)
        .ok_or_else(|| AppError::Invalid("collection receipt bvid missing".into()))?
        .to_owned();
    // Verify account ownership before using either read or write results.
    let (cid, video_title, cover) = owner_media(client, aid, &bvid).await?;
    let existing = BilibiliClient::read_json(
        client
            .member
            .get(client.url("x2/creative/web/season/aid")?)
            .query(&[("id", aid)])
            .send()
            .await?,
    )
    .await?;
    if let Some((id, title)) = assigned(&existing)? {
        let matches_identity = c.season_id.is_none_or(|v| v == id);
        c.season_id = Some(id);
        if title == c.title && matches_identity {
            c.state = "succeeded".into();
            c.reason = None;
        } else {
            c.state = "skipped".into();
            c.reason = Some("different_collection_already_assigned".into());
        }
        return persist(db, r, c).await;
    }
    if c.state == "attaching" {
        defer(c, "attachment not yet confirmed; read-only reconciliation");
        return persist(db, r, c).await;
    }
    if let Some(season) = lookup(client, &c.title, c.season_id).await? {
        c.season_id = Some(season.id);
        c.section_id = Some(season.section);
        c.state = "attaching".into();
        c.due_at = chrono::Utc::now().timestamp() + 300;
        c.reason = None;
        persist(db, r, c).await?; // durable write intent before irreversible POST
        let response=client.member.post(client.url("x2/creative/web/season/section/episodes/add")?).query(&[("csrf",csrf)]).json(&json!({"sectionId":season.section,"episodes":[{"aid":aid,"cid":cid,"title":video_title,"charging_pay":0}],"csrf":csrf})).send().await?;
        let value = BilibiliClient::read_json(response).await?;
        BilibiliClient::ensure_ok(&value, false)?;
        // The next read verifies actual assignment. No replay after timeout.
        c.due_at = chrono::Utc::now().timestamp() + 45;
        return persist(db, r, c).await;
    }
    if c.state == "creating" {
        defer(
            c,
            "collection creation not yet visible; read-only reconciliation",
        );
        return persist(db, r, c).await;
    }
    // If a different archive for this same author has an unresolved
    // creation intent, never create a second season merely because the
    // first one is not visible yet. Credential changes fail closed.
    let uncertain:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM vr2_jobs WHERE id<>$1 AND stage='publish' AND state='succeeded' AND result->'collection_sync'->>'title'=$2 AND result->'collection_sync'->>'credential_sha256'=$3 AND result->'collection_sync'->>'state' IN ('creating','unknown'))")
        .bind(r.id).bind(&c.title).bind(&c.credential_sha256).fetch_one(&db.pool).await?;
    if uncertain {
        defer(c, "another archive has an unresolved collection creation");
        return persist(db, r, c).await;
    }
    let cover = cover.or_else(|| {
        r.result
            .pointer("/submitted_meta/cover")
            .and_then(Value::as_str)
            .and_then(safe_cover)
    });
    let Some(cover) = cover else {
        c.state = "skipped".into();
        c.reason = Some("collection cover absent".into());
        return persist(db, r, c).await;
    };
    c.state = "creating".into();
    c.due_at = chrono::Utc::now().timestamp() + 300;
    c.reason = None;
    persist(db, r, c).await?;
    let response = client
        .member
        .post(client.url("x2/creative/web/season/add")?)
        .form(&[
            ("title", c.title.as_str()),
            ("cover", cover.as_str()),
            ("csrf", csrf),
        ])
        .send()
        .await?;
    let value = BilibiliClient::read_json(response).await?;
    BilibiliClient::ensure_ok(&value, false)?;
    c.season_id = positive(&value["data"]);
    if c.season_id.is_none() {
        return Err(AppError::Unavailable(
            "season creation receipt absent".into(),
        ));
    }
    c.due_at = chrono::Utc::now().timestamp() + 45;
    persist(db, r, c).await
}
pub(super) async fn tick(config: &Config, db: &Database) -> AppResult<bool> {
    if !config.native_bilibili_allowed || !crate::publish_platforms::enabled(db, "bilibili").await?
    {
        return Ok(false);
    }
    // Serialize all collection writers, including different accounts. Keep
    // this session separate from the pool; no transaction across HTTP calls.
    let mut guard = tokio::time::timeout(
        Duration::from_secs(5),
        PgConnection::connect_with(&db.pool.connect_options()),
    )
    .await
    .map_err(|_| AppError::Unavailable("collection lock connection timeout".into()))??;
    sqlx::query("SET idle_session_timeout=0")
        .execute(&mut guard)
        .await?;
    let locked: bool = sqlx::query_scalar(
        "SELECT pg_try_advisory_lock(hashtextextended('videoroll:bili:collection:v1',0))",
    )
    .fetch_one(&mut guard)
    .await?;
    if !locked {
        return Ok(false);
    }
    let Some(mut r)=sqlx::query_as::<_,Record>("SELECT id,task_id,workflow_id,result FROM vr2_jobs WHERE stage='publish' AND state='succeeded' AND result->>'native_bilibili'='true' AND result->>'confirmed'='true' AND result->'collection_sync'->>'state' IN('pending','creating','attaching') AND CASE WHEN jsonb_typeof(result->'collection_sync'->'due_at')='number' THEN (result->'collection_sync'->>'due_at')::double precision<=EXTRACT(EPOCH FROM clock_timestamp()) ELSE false END ORDER BY finished_at,id LIMIT 1").fetch_optional(&db.pool).await? else{return Ok(false);};
    let mut c: Collection = serde_json::from_value(r.result["collection_sync"].clone())
        .map_err(|_| AppError::Invalid("malformed collection intent".into()))?;
    let credentials =
        crate::bilibili_account_v1::selected_publication_credentials(db, config, c.account_id)
            .await;
    let (cookie, csrf, _) = match credentials {
        Ok(v) => v,
        Err(_) => {
            defer(&mut c, "collection credentials unavailable");
            persist(db, &mut r, &c).await?;
            return Ok(true);
        }
    };
    if credential_hash(&cookie, &csrf) != c.credential_sha256 {
        c.state = "unknown".into();
        c.reason =
            Some("publication account credentials changed; manual reconciliation required".into());
        persist(db, &mut r, &c).await?;
        return Ok(true);
    }
    let client = BilibiliClient::new(&cookie, member_origin(config)?)?;
    // Bound the entire attempt. Durable intent survives any timeout or lock
    // disconnect. A subsequent worker only reads before another distinct action.
    let result = {
        let future = step(db, &mut r, &mut c, &client, &csrf);
        tokio::pin!(future);
        let mut heartbeat = tokio::time::interval(Duration::from_secs(3));
        let deadline = tokio::time::sleep(Duration::from_secs(120));
        tokio::pin!(deadline);
        loop {
            tokio::select! {r=&mut future=>break r,_=&mut deadline=>break Err(AppError::Unavailable("collection operation timed out".into())),_=heartbeat.tick()=>{if !tokio::time::timeout(Duration::from_secs(3),sqlx::query("SELECT 1").execute(&mut guard)).await.is_ok_and(|r|r.is_ok()){break Err(AppError::Unavailable("collection lock lost".into()));}}}
        }
    };
    if result.is_err() {
        // Reload the committed intent before recording failure; never
        // overwrite a concurrently confirmed outcome or a write marker.
        let value: Option<Value> =
            sqlx::query_scalar("SELECT result FROM vr2_jobs WHERE id=$1 AND state='succeeded'")
                .bind(r.id)
                .fetch_optional(&db.pool)
                .await?
                .flatten();
        if let Some(value) = value {
            if value["collection_sync"] == r.result["collection_sync"] {
                r.result = value;
                let mut persisted: Collection =
                    serde_json::from_value(r.result["collection_sync"].clone()).map_err(|_| {
                        AppError::Invalid("collection recovery state invalid".into())
                    })?;
                if matches!(
                    persisted.state.as_str(),
                    "pending" | "creating" | "attaching"
                ) {
                    defer(
                        &mut persisted,
                        "collection attempt failed; reconcile before writing",
                    );
                    persist(db, &mut r, &persisted).await?;
                }
            }
        }
    }
    Ok(true)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn collection_real_postgres_http() {
        use axum::{
            Json, Router,
            extract::State,
            http::StatusCode,
            response::IntoResponse,
            routing::{get, post},
        };
        use std::sync::{Arc, Mutex};
        let Ok(url) = std::env::var("VIDEOROLL_TEST_COLLECTION_DB_URL") else {
            return;
        };
        let parsed = Url::parse(&url).unwrap();
        assert_eq!(parsed.host_str(), Some("127.0.0.1"));
        assert_eq!(parsed.path(), "/videoroll_test");
        let db = Database::connect(&url).await.unwrap();
        sqlx::raw_sql("CREATE TABLE vr2_jobs(id uuid PRIMARY KEY,task_id uuid,workflow_id uuid,stage text,state text,result jsonb,updated_at timestamptz DEFAULT now(),finished_at timestamptz DEFAULT now());CREATE TABLE vr2_workflows(id uuid PRIMARY KEY,configuration jsonb);CREATE TABLE publish_jobs(id uuid PRIMARY KEY,task_id uuid,state text,bvid text,response_json jsonb,updated_at timestamptz DEFAULT now());").execute(&db.pool).await.unwrap();
        #[derive(Default)]
        struct Platform {
            created: bool,
            attached: bool,
            create_calls: u32,
            attach_calls: u32,
            manual: bool,
        }
        async fn archive() -> Json<Value> {
            Json(
                json!({"code":0,"data":{"archive":{"aid":1,"bvid":"BVfixture","title":"原视频","cover":"https://i0.hdslb.com/a.jpg"},"videos":[{"cid":42}]}}),
            )
        }
        async fn season(State(s): State<Arc<Mutex<Platform>>>) -> Json<Value> {
            let s = s.lock().unwrap();
            Json(if s.manual {
                json!({"code":0,"data":{"id":99,"title":"人工合集"}})
            } else if s.attached {
                json!({"code":0,"data":{"season":{"id":10,"title":"作者"}}})
            } else {
                json!({"code":0,"data":null})
            })
        }
        async fn seasons(State(s): State<Arc<Mutex<Platform>>>) -> Json<Value> {
            let s = s.lock().unwrap();
            let rows = if s.created {
                vec![json!({"season":{"id":10,"title":"作者"},"sections":{"sections":[{"id":11}]}})]
            } else {
                vec![]
            };
            Json(json!({"code":0,"data":{"seasons":rows,"total":rows.len()}}))
        }
        async fn create(State(s): State<Arc<Mutex<Platform>>>) -> impl IntoResponse {
            let mut s = s.lock().unwrap();
            s.create_calls += 1;
            s.created = true;
            (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"code":-500})))
        }
        async fn attach(
            State(s): State<Arc<Mutex<Platform>>>,
            Json(body): Json<Value>,
        ) -> impl IntoResponse {
            assert_eq!(body["sectionId"], 11);
            assert_eq!(body["episodes"][0]["cid"], 42);
            let mut s = s.lock().unwrap();
            s.attach_calls += 1;
            s.attached = true;
            (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"code":-500})))
        }
        let state = Arc::new(Mutex::new(Platform::default()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = Url::parse(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
        let router = Router::new()
            .route("/x/vupre/web/archive/view", get(archive))
            .route("/x2/creative/web/season/aid", get(season))
            .route("/x2/creative/web/seasons", get(seasons))
            .route("/x2/creative/web/season/add", post(create))
            .route("/x2/creative/web/season/section/episodes/add", post(attach))
            .with_state(state.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let client = BilibiliClient::new(
            "SESSDATA=isolated-fixture; bili_jct=0123456789abcdef",
            endpoint,
        )
        .unwrap();
        let mut c = Collection {
            state: "pending".into(),
            title: "作者".into(),
            account_id: None,
            credential_sha256: credential_hash("fixture", "fixture"),
            attempts: 0,
            due_at: 0,
            season_id: None,
            section_id: None,
            reason: None,
        };
        let mut r = Record {
            id: Uuid::new_v4(),
            task_id: Uuid::new_v4(),
            workflow_id: Uuid::new_v4(),
            result: json!({"confirmed":true,"native_bilibili":true,"aid":1,"bvid":"BVfixture","collection_sync":c}),
        };
        let publish = Uuid::new_v4();
        sqlx::query("INSERT INTO vr2_jobs(id,task_id,workflow_id,stage,state,result) VALUES($1,$2,$3,'publish','succeeded',$4)").bind(r.id).bind(r.task_id).bind(r.workflow_id).bind(&r.result).execute(&db.pool).await.unwrap();
        sqlx::query("INSERT INTO vr2_workflows VALUES($1,$2)")
            .bind(r.workflow_id)
            .bind(json!({"bilibili_direct_v1":{"publish_job_id":publish}}))
            .execute(&db.pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO publish_jobs(id,task_id,state,bvid,response_json) VALUES($1,$2,'published','BVfixture',$3)").bind(publish).bind(r.task_id).bind(json!({"confirmed":true,"bvid":"BVfixture"})).execute(&db.pool).await.unwrap();
        assert!(
            step(&db, &mut r, &mut c, &client, "0123456789abcdef")
                .await
                .is_err()
        );
        assert_eq!(r.result["collection_sync"]["state"], "creating");
        assert!(
            step(&db, &mut r, &mut c, &client, "0123456789abcdef")
                .await
                .is_err()
        );
        assert_eq!(r.result["collection_sync"]["state"], "attaching");
        step(&db, &mut r, &mut c, &client, "0123456789abcdef")
            .await
            .unwrap();
        assert_eq!(c.state, "succeeded");
        let result: Value = sqlx::query_scalar(
            "SELECT response_json FROM publish_jobs WHERE id=$1 AND state='published'",
        )
        .bind(publish)
        .fetch_one(&db.pool)
        .await
        .unwrap();
        assert_eq!(result["confirmed"], true);
        assert_eq!(result["collection"]["ok"], true);
        assert!(!result.to_string().contains("credential_sha256"));
        {
            let mut platform = state.lock().unwrap();
            assert_eq!(platform.create_calls, 1);
            assert_eq!(platform.attach_calls, 1);
            platform.manual = true;
        }
        step(&db, &mut r, &mut c, &client, "0123456789abcdef")
            .await
            .unwrap();
        assert_eq!(c.state, "skipped");
        {
            let platform = state.lock().unwrap();
            assert_eq!(platform.create_calls, 1);
            assert_eq!(platform.attach_calls, 1);
        }
        server.abort();
        println!(
            "PASS durable collection create/attach intent after ambiguous HTTP, single write each, read-only confirmation, manual collection preserved, publish receipt unchanged"
        );
    }
    #[test]
    fn collection_author_assignment_and_cover_are_strict() {
        assert_eq!(
            author(&json!({"uploader":"  原  博主 ","channel":"ignored"})).as_deref(),
            Some("原 博主")
        );
        assert_eq!(
            assigned(&json!({"code":0,"data":{"season":{"id":10,"title":"作者"}}})).unwrap(),
            Some((10, "作者".into()))
        );
        assert!(
            assigned(&json!({"code":0,"data":{"season":null}}))
                .unwrap()
                .is_none()
        );
        assert!(assigned(&json!({"code":-500})).is_err());
        assert!(assigned(&json!({"code":0,"data":{"unexpected":1}})).is_err());
        assert!(safe_cover("//i0.hdslb.com/a.jpg").is_some());
        assert!(safe_cover("https://hdslb.com.invalid/a.jpg").is_none());
        assert!(safe_cover("https://i0.hdslb.com@evil.invalid/a.jpg").is_none());
    }
}
