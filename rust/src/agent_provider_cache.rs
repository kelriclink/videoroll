//! Bounded process-local cache, single-flight and circuit breaker for public
//! retrieval. Never cache private documents, local knowledge, or AI replies.
use super::*;
use std::{future::Future, sync::Arc, time::Instant};
use tokio::sync::Mutex;

const CACHE_MAX: usize = 128;
const GATE_MAX: usize = 32;
type Evidence = Vec<WebEvidence>;
struct Entry {
    expires: Instant,
    value: Evidence,
}
#[derive(Default)]
struct Health {
    failures: u8,
    until: Option<Instant>,
}
#[derive(Default)]
struct Gate {
    serial: Mutex<()>,
    health: Mutex<Health>,
}
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
#[cfg(test)]
mod tests {
    use super::*;
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
