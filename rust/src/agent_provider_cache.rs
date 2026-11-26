//! Bounded process-local cache, single-flight and circuit breaker for public
//! retrieval. Never cache private documents, local knowledge, or AI replies.
use super::*;
use sqlx::{Connection, PgConnection};
use std::{future::Future, sync::Arc, time::Instant};
use tokio::sync::Mutex;

const CACHE_MAX: usize = 128;
const GATE_MAX: usize = 32;
type Evidence = Vec<WebEvidence>;
#[cfg(test)]
struct Entry {
    expires: Instant,
    value: Evidence,
}
#[derive(Default)]
struct Health {
    #[cfg(test)]
    failures: u8,
    until: Option<Instant>,
}
#[derive(Default)]
struct Gate {
    #[cfg(test)]
    serial: Mutex<()>,
    health: Mutex<Health>,
}
#[cfg(test)]
static CACHE: Mutex<Vec<(String, Entry)>> = Mutex::const_new(Vec::new());
static GATES: Mutex<Vec<(String, Arc<Gate>)>> = Mutex::const_new(Vec::new());
pub(super) fn identity(kind: &str, endpoint: &str, credential: Option<&str>) -> String {
    let mut hash = Sha256::new();
    for value in [kind, endpoint, credential.unwrap_or_default()] {
        hash.update((value.len() as u64).to_be_bytes());
        hash.update(value.as_bytes());
    }
    hex::encode(hash.finalize())
}
async fn gate(provider: &str) -> AppResult<Arc<Gate>> {
    let mut gates = GATES.lock().await;
    if let Some((_, g)) = gates.iter().find(|(key, _)| key == provider) {
        return Ok(g.clone());
    }
    if gates.len() >= GATE_MAX {
        // Evict only an idle gate. Never split an active provider's mutex.
        if let Some(i) = gates.iter().position(|(_, g)| Arc::strong_count(g) == 1) {
            gates.remove(i);
        } else {
            return Err(AppError::Unavailable(
                "RAG provider gate capacity exhausted".into(),
            ));
        }
    }
    let g = Arc::new(Gate::default());
    gates.push((provider.to_owned(), g.clone()));
    Ok(g)
}
#[cfg(test)]
async fn cached(key: &str) -> Option<Evidence> {
    let mut cache = CACHE.lock().await;
    let now = Instant::now();
    cache.retain(|(_, e)| e.expires > now);
    let i = cache.iter().position(|(k, _)| k == key)?;
    let entry = cache.remove(i);
    let value = entry.1.value.clone();
    cache.push(entry);
    Some(value)
}
#[cfg(test)]
async fn store(key: &str, value: &Evidence, ttl: Duration) {
    let mut cache = CACHE.lock().await;
    cache.retain(|(k, e)| k != key && e.expires > Instant::now());
    while cache.len() >= CACHE_MAX {
        cache.remove(0);
    }
    cache.push((
        key.to_owned(),
        Entry {
            expires: Instant::now() + ttl,
            value: value.clone(),
        },
    ));
}
#[cfg(test)]
async fn admit(g: &Gate) -> AppResult<()> {
    let h = g.health.lock().await;
    if h.until.is_some_and(|until| until > Instant::now()) {
        return Err(AppError::Unavailable(
            "RAG provider circuit or cooldown active".into(),
        ));
    }
    Ok(())
}
pub(super) async fn check_status(provider: &str, response: &reqwest::Response) -> AppResult<()> {
    let status = response.status();
    if status.is_success() {
        return Ok(());
    }
    if status.as_u16() == 429 {
        let delay = response
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| v.is_finite())
            .unwrap_or(2.0)
            .clamp(1., 60.);
        let g = gate(provider).await?;
        let mut h = g.health.lock().await;
        let until = Instant::now() + Duration::from_secs_f64(delay);
        h.until = Some(h.until.map_or(until, |old| old.max(until)));
    }
    Err(AppError::Unavailable(format!("RAG provider HTTP {status}")))
}
pub(super) async fn bounded_body(
    mut response: reqwest::Response,
    limit: usize,
) -> AppResult<Vec<u8>> {
    if response.content_length().is_some_and(|n| n > limit as u64) {
        return Err(AppError::Unavailable(
            "RAG provider response too large".into(),
        ));
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        if chunk.len() > limit.saturating_sub(bytes.len()) {
            return Err(AppError::Unavailable(
                "RAG provider response exceeds budget".into(),
            ));
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}
#[cfg(test)]
pub(super) async fn retrieve<F, Fut>(
    provider: &str,
    key: &str,
    ttl: Duration,
    call: F,
) -> AppResult<Evidence>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = AppResult<Evidence>>,
{
    if let Some(value) = cached(key).await {
        return Ok(value);
    }
    let g = gate(provider).await?;
    let _serial = tokio::time::timeout(Duration::from_secs(15), g.serial.lock())
        .await
        .map_err(|_| AppError::Unavailable("RAG provider admission timeout".into()))?;
    if let Some(value) = cached(key).await {
        return Ok(value);
    }
    admit(&g).await?;
    let result = call().await;
    let mut h = g.health.lock().await;
    match &result {
        Ok(value) => {
            *h = Health::default();
            store(key, value, ttl).await;
        }
        Err(_) => {
            h.failures = h.failures.saturating_add(1);
            if h.failures >= 4 {
                let until = Instant::now() + Duration::from_secs(20);
                h.until = Some(h.until.map_or(until, |old| old.max(until)));
            }
        }
    }
    result
}
// Bounded shared state in the original settings table. All keys are hashes;
// no raw query, API key or HTTP response headers are stored. Serialize only
// tiny state mutations, never a transaction across provider I/O.
const SHARED_KEY: &str = "runtime.rag.public_retrieval.v1";
#[derive(Clone, Serialize, Deserialize)]
struct SharedEntry {
    key: String,
    expires: f64,
    value: Evidence,
}
#[derive(Clone, Serialize, Deserialize)]
struct SharedHealth {
    provider: String,
    failures: u8,
    until: f64,
    expires: f64,
}
#[derive(Default, Serialize, Deserialize)]
struct Shared {
    entries: Vec<SharedEntry>,
    health: Vec<SharedHealth>,
}
impl Shared {
    fn prune(&mut self, now: f64) {
        self.entries.retain(|v| v.expires > now);
        self.health.retain(|v| v.expires > now);
    }
    fn record(
        &mut self,
        provider: &str,
        key: &str,
        result: &AppResult<Evidence>,
        ttl: Duration,
        cooldown: f64,
        now: f64,
    ) {
        self.prune(now);
        let old = self.health.iter().find(|h| h.provider == provider).cloned();
        self.health.retain(|h| h.provider != provider);
        match result {
            Ok(value) => {
                self.entries.retain(|v| v.key != key);
                while self.entries.len() >= CACHE_MAX {
                    self.entries.remove(0);
                }
                self.entries.push(SharedEntry {
                    key: key.into(),
                    expires: now + ttl.as_secs_f64(),
                    value: value.clone(),
                });
            }
            Err(_) => {
                let failures = old.as_ref().map_or(1, |h| h.failures.saturating_add(1));
                let until = old
                    .map_or(0., |h| h.until)
                    .max(now + cooldown)
                    .max(if failures >= 4 { now + 20. } else { 0. });
                while self.health.len() >= GATE_MAX {
                    self.health.remove(0);
                }
                self.health.push(SharedHealth {
                    provider: provider.into(),
                    failures,
                    until,
                    expires: now + 300.,
                });
            }
        }
    }
}
async fn shared_read(db: &Database) -> AppResult<(Shared, f64)> {
    let now: f64 =
        sqlx::query_scalar("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision")
            .fetch_one(&db.pool)
            .await?;
    let value: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(SHARED_KEY)
            .fetch_optional(&db.pool)
            .await?;
    let shared = value
        .map(|v| {
            serde_json::from_value(v)
                .map_err(|_| AppError::Unavailable("invalid RAG shared state".into()))
        })
        .transpose()?
        .unwrap_or_default();
    Ok((shared, now))
}
async fn shared_record(
    db: &Database,
    provider: &str,
    key: &str,
    result: &AppResult<Evidence>,
    ttl: Duration,
    cooldown: f64,
) -> AppResult<()> {
    let mut tx = db.pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))")
        .bind(SHARED_KEY)
        .execute(&mut *tx)
        .await?;
    let now: f64 =
        sqlx::query_scalar("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision")
            .fetch_one(&mut *tx)
            .await?;
    let value: Option<Value> =
        sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(SHARED_KEY)
            .fetch_optional(&mut *tx)
            .await?;
    let mut shared: Shared = value
        .map(|v| {
            serde_json::from_value(v)
                .map_err(|_| AppError::Unavailable("invalid RAG shared state".into()))
        })
        .transpose()?
        .unwrap_or_default();
    shared.record(provider, key, result, ttl, cooldown, now);
    let value = serde_json::to_value(shared)
        .map_err(|_| AppError::Unavailable("RAG shared state encode failed".into()))?;
    sqlx::query("INSERT INTO app_settings(key,value_json,version) VALUES($1,$2,1) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,version=app_settings.version+1,updated_at=now()")
        .bind(SHARED_KEY).bind(value).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}
pub(super) async fn retrieve_shared<F, Fut>(
    db: &Database,
    job: &WorkItem,
    provider: &str,
    key: &str,
    ttl: Duration,
    call: F,
) -> AppResult<Evidence>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = AppResult<Evidence>>,
{
    lease(db, job).await?;
    let (shared, now) = shared_read(db).await?;
    if let Some(v) = shared
        .entries
        .into_iter()
        .find(|v| v.key == key && v.expires > now)
    {
        return Ok(v.value);
    }
    let gate_key = format!("runtime.rag.provider.{provider}");
    let mut connection = tokio::time::timeout(
        Duration::from_secs(5),
        PgConnection::connect_with(&db.pool.connect_options()),
    )
    .await
    .map_err(|_| AppError::Unavailable("RAG provider connection timeout".into()))??;
    sqlx::query("SET idle_session_timeout=0")
        .execute(&mut connection)
        .await?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    loop {
        lease(db, job).await?;
        let held: bool =
            sqlx::query_scalar("SELECT pg_try_advisory_lock(hashtextextended($1::text,0))")
                .bind(&gate_key)
                .fetch_one(&mut connection)
                .await?;
        if held {
            break;
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(AppError::Unavailable(
                "RAG provider admission timeout".into(),
            ));
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    let (shared, now) = shared_read(db).await?;
    if let Some(v) = shared
        .entries
        .into_iter()
        .find(|v| v.key == key && v.expires > now)
    {
        return Ok(v.value);
    }
    if shared
        .health
        .iter()
        .any(|h| h.provider == provider && h.until > now)
    {
        return Err(AppError::Unavailable(
            "RAG shared provider circuit or cooldown active".into(),
        ));
    }
    lease(db, job).await?;
    let g = gate(provider).await?;
    // The local gate carries the typed Retry-After from the HTTP parser.
    // Only the dedicated PostgreSQL session owns actual cross-process admission.
    let result = {
        let request = call();
        tokio::pin!(request);
        let mut tick = tokio::time::interval(Duration::from_secs(3));
        loop {
            tokio::select! {
                result=&mut request=>break result,
                _=tick.tick()=>{
                    tokio::time::timeout(Duration::from_secs(3),sqlx::query("SELECT 1").execute(&mut connection)).await
                        .map_err(|_|AppError::Unavailable("RAG provider lease session lost".into()))??;
                    lease(db,job).await?;
                }
            }
        }
    };
    lease(db, job).await?;
    let cooldown = {
        let mut h = g.health.lock().await;
        let seconds = h
            .until
            .map(|t| t.saturating_duration_since(Instant::now()).as_secs_f64())
            .unwrap_or(0.);
        *h = Health::default();
        seconds
    };
    shared_record(db, provider, key, &result, ttl, cooldown).await?;
    drop(connection);
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn shared_cache_real_postgres() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let Ok(url) = std::env::var("VIDEOROLL_TEST_RAG_SHARED_DB_URL") else {
            return;
        };
        let parsed = Url::parse(&url).unwrap();
        assert_eq!(parsed.host_str(), Some("127.0.0.1"));
        assert_eq!(parsed.path(), "/videoroll_test");
        let db = Database::connect(&url).await.unwrap();
        let other = Database::connect(&url).await.unwrap();
        sqlx::raw_sql("CREATE TABLE pipeline_runs(id uuid PRIMARY KEY,state text);
            CREATE TABLE operation_inbox(id uuid PRIMARY KEY,workflow_id uuid,lease_token uuid,status text,lease_until timestamptz);
            CREATE TABLE app_settings(key text PRIMARY KEY,value_json jsonb,version int,updated_at timestamptz DEFAULT now());")
            .execute(&db.pool).await.unwrap();
        let mut jobs = Vec::new();
        for _ in 0..2 {
            let job = WorkItem {
                id: Uuid::new_v4(),
                workflow_id: Uuid::new_v4(),
                task_id: Uuid::new_v4(),
                stage: crate::domain::Stage::Subtitle,
                generation: 1,
                operation_key: Uuid::new_v4().to_string(),
                lease_token: Uuid::new_v4(),
                attempts: 1,
                max_attempts: 3,
                payload: json!({}),
            };
            sqlx::query("INSERT INTO pipeline_runs VALUES($1,'running')")
                .bind(job.workflow_id)
                .execute(&db.pool)
                .await
                .unwrap();
            sqlx::query("INSERT INTO operation_inbox VALUES($1,$2,$3,'running',now()+interval '10 minutes')").bind(job.id).bind(job.workflow_id).bind(job.lease_token).execute(&db.pool).await.unwrap();
            jobs.push(job);
        }
        let provider = identity("search", "fixture", Some("secret-fixture"));
        let key = identity(&provider, "query", None);
        let calls = AtomicUsize::new(0);
        let fetch = || async {
            calls.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(350)).await;
            Ok(vec![WebEvidence {
                title: "public fixture".into(),
                url: "https://example.com/".into(),
                snippet: "text".into(),
            }])
        };
        let (a, b) = tokio::join!(
            retrieve_shared(
                &db,
                &jobs[0],
                &provider,
                &key,
                Duration::from_secs(60),
                fetch
            ),
            retrieve_shared(
                &other,
                &jobs[1],
                &provider,
                &key,
                Duration::from_secs(60),
                fetch
            )
        );
        assert_eq!(a.unwrap()[0].title, b.unwrap()[0].title);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let raw: Value = sqlx::query_scalar("SELECT value_json FROM app_settings WHERE key=$1")
            .bind(SHARED_KEY)
            .fetch_one(&other.pool)
            .await
            .unwrap();
        assert!(!raw.to_string().contains("secret-fixture"));
        for i in 0..4 {
            let result = retrieve_shared(
                &db,
                &jobs[0],
                &provider,
                &format!("failure-{i}"),
                Duration::from_secs(10),
                || async { Err(AppError::Unavailable("fixture".into())) },
            )
            .await;
            assert!(result.is_err());
        }
        let blocked = retrieve_shared(
            &other,
            &jobs[1],
            &provider,
            "different-key",
            Duration::from_secs(10),
            fetch,
        )
        .await;
        assert!(blocked.is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(
            retrieve_shared(
                &other,
                &jobs[1],
                &provider,
                &key,
                Duration::from_secs(10),
                fetch
            )
            .await
            .is_ok()
        );
        sqlx::query("UPDATE operation_inbox SET status='canceled' WHERE id=$1")
            .bind(jobs[1].id)
            .execute(&other.pool)
            .await
            .unwrap();
        assert!(
            retrieve_shared(
                &other,
                &jobs[1],
                &provider,
                &key,
                Duration::from_secs(10),
                fetch
            )
            .await
            .is_err()
        );
        println!(
            "PASS real PostgreSQL shared evidence cache, two independent pools, single-flight across sessions, persistent circuit and cancellation"
        );
    }
    #[test]
    fn shared_state_is_bounded_and_expires() {
        let mut shared = Shared::default();
        let result = Ok(vec![WebEvidence {
            title: "x".into(),
            url: "https://example.com/".into(),
            snippet: "y".into(),
        }]);
        for i in 0..200 {
            shared.record(
                "provider",
                &format!("key-{i}"),
                &result,
                Duration::from_secs(60),
                0.,
                100.,
            );
        }
        assert_eq!(shared.entries.len(), 128);
        for _ in 0..4 {
            shared.record(
                "provider",
                "bad",
                &Err(AppError::Unavailable("fixture".into())),
                Duration::from_secs(60),
                0.,
                100.,
            );
        }
        assert_eq!(shared.health[0].failures, 4);
        assert_eq!(shared.health[0].until, 120.);
        shared.prune(161.);
        assert!(shared.entries.is_empty());
        shared.prune(401.);
        assert!(shared.health.is_empty());
    }
    #[tokio::test]
    async fn cross_task_cache_single_flight_and_provider_isolation() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let provider = identity("test", &Uuid::new_v4().to_string(), Some("key1"));
        let key = identity(&provider, "query", None);
        let calls = AtomicUsize::new(0);
        let fetch = || async {
            calls.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(30)).await;
            Ok(vec![WebEvidence {
                title: "fixture".into(),
                url: "https://example.com/".into(),
                snippet: "evidence".into(),
            }])
        };
        let (a, b) = tokio::join!(
            retrieve(&provider, &key, Duration::from_secs(10), fetch),
            retrieve(&provider, &key, Duration::from_secs(10), fetch)
        );
        assert_eq!(a.unwrap()[0].title, b.unwrap()[0].title);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let other = identity("test", "different", Some("key2"));
        let other_key = identity(&other, "query", None);
        retrieve(&other, &other_key, Duration::from_secs(10), fetch)
            .await
            .unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        for i in 0..4 {
            assert!(
                retrieve(
                    &provider,
                    &format!("failure-{provider}-{i}"),
                    Duration::from_secs(1),
                    || async { Err(AppError::Unavailable("fixture".into())) }
                )
                .await
                .is_err()
            );
        }
        let denied = retrieve(
            &provider,
            &format!("new-{provider}"),
            Duration::from_secs(1),
            fetch,
        )
        .await;
        assert!(denied.is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(
            retrieve(&provider, &key, Duration::from_secs(1), fetch)
                .await
                .is_ok()
        );
    }
}
