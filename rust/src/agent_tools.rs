//! Shared retrieval contract for the existing serial and specialist agents.
use super::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(super) enum RetrievalKind {
    Knowledge,
    Web,
    Wikipedia,
    PageText,
}

pub(super) struct RetrievalQuery {
    kind: RetrievalKind,
    text: String,
}

impl RetrievalQuery {
    pub(super) fn new(kind: RetrievalKind, raw: &str) -> AppResult<Self> {
        let text = raw.trim();
        if text.is_empty() || text.chars().count() > 250 || text.chars().any(char::is_control) {
            return Err(AppError::Invalid(
                "research query outside bounded limit".into(),
            ));
        }
        Ok(Self {
            kind,
            text: text.into(),
        })
    }

    pub(super) fn key(&self) -> (RetrievalKind, String) {
        (
            self.kind,
            self.text
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .to_lowercase(),
        )
    }

    pub(super) async fn knowledge(
        &self,
        db: &Database,
        task: &ResearchTask<'_>,
        original: &crate::rag_original_v1::Settings,
    ) -> AppResult<Vec<SearchHit>> {
        if self.kind != RetrievalKind::Knowledge {
            return Err(AppError::Invalid("wrong retrieval kind".into()));
        }
        lease(db, task.job).await?;
        let mut rows = if original.enabled {
            crate::rag_original_v1::exact(db, &self.text, task.target_lang, original).await?
        } else {
            Vec::new()
        };
        if task.settings.enabled {
            rows.extend(
                crate::rag::search(
                    db,
                    &SearchKnowledge {
                        query: self.text.clone(),
                        collection: task.settings.collection.clone(),
                        task_id: Some(task.job.task_id),
                        limit: Some(5),
                        embedding: None,
                        embedding_model: None,
                    },
                )
                .await?,
            );
        }
        let mut seen = HashSet::new();
        rows.retain(|h| seen.insert(h.id));
        rows.truncate(5);
        for row in &mut rows {
            row.title = row.title.chars().take(200).collect();
            row.content = row.content.chars().take(650).collect();
        }
        lease(db, task.job).await?;
        Ok(rows)
    }

    pub(super) async fn web(
        &self,
        db: &Database,
        task: &ResearchTask<'_>,
        client: &Client,
        permitted: bool,
    ) -> AppResult<Vec<WebEvidence>> {
        if self.kind != RetrievalKind::Web || !permitted || task.settings.max_web_searches == 0 {
            return Err(AppError::Invalid("web retrieval not permitted".into()));
        }
        lease(db, task.job).await?;
        let results = search_web(client, task.settings, &self.text).await?;
        lease(db, task.job).await?;
        Ok(results)
    }
}

// Only an existing configured search result is eligible. No model-supplied
// URL and no Wikipedia follow-up fetch.
pub(super) const PAGE_BUDGET: usize = 2;
pub(super) async fn page_text(
    db: &Database,
    task: &ResearchTask<'_>,
    hit: &WebEvidence,
) -> AppResult<Option<WebEvidence>> {
    if !task.settings.page_text_enabled || task.settings.max_web_searches == 0 {
        return Err(AppError::Invalid("research page retrieval disabled".into()));
    }
    lease(db, task.job).await?;
    let body = crate::egress_gateway_v1::research_plain_text(&hit.url).await?;
    lease(db, task.job).await?;
    Ok(body.map(|snippet| WebEvidence {
        title: hit.title.clone(),
        url: hit.url.clone(),
        snippet,
    }))
}

// Fixed public Wikipedia API; response text and page IDs are data, never URLs
// to fetch. No redirects or arbitrary page fetches.
const WIKI_API: &str = "https://en.wikipedia.org/w/api.php";
const WIKI_MAX_BYTES: usize = 512 * 1024;
pub(super) const WIKI_BUDGET: usize = 2;
static WIKI_GATE: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn wiki_endpoint() -> AppResult<Url> {
    if let Ok(raw) = std::env::var("VIDEOROLL_V2_TEST_WIKI_ENDPOINT") {
        let url =
            Url::parse(&raw).map_err(|_| AppError::Invalid("invalid wiki test endpoint".into()))?;
        if std::env::var("VIDEOROLL_V2_ALLOW_TEST_WIKI_LOOPBACK").as_deref() != Ok("true")
            || url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || url.path() != "/w/api.php"
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(AppError::Invalid(
                "wiki test endpoint requires explicit loopback".into(),
            ));
        }
        return Ok(url);
    }
    Ok(Url::parse(WIKI_API).expect("fixed wiki endpoint"))
}

fn wiki_pages(payload: &Value) -> AppResult<Vec<WebEvidence>> {
    if payload.get("error").is_some() {
        return Err(AppError::Unavailable(
            "Wikipedia API returned an error".into(),
        ));
    }
    let Some(pages) = payload.pointer("/query/pages").and_then(Value::as_array) else {
        if payload.get("query").is_none() && payload["batchcomplete"] == true {
            return Ok(vec![]);
        }
        return Err(AppError::Unavailable(
            "invalid Wikipedia page response".into(),
        ));
    };
    let mut ranked = Vec::new();
    for page in pages.iter().take(20) {
        if page.get("missing").is_some()
            || page.get("invalid").is_some()
            || page["ns"].as_i64() != Some(0)
        {
            continue;
        }
        let Some(id) = page["pageid"].as_u64().filter(|id| *id > 0) else {
            continue;
        };
        let title = page["title"]
            .as_str()
            .unwrap_or("")
            .trim()
            .chars()
            .take(200)
            .collect::<String>();
        let snippet = page["extract"]
            .as_str()
            .unwrap_or("")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .chars()
            .take(1200)
            .collect::<String>();
        if title.is_empty() || snippet.is_empty() {
            continue;
        }
        ranked.push((
            page["index"].as_u64().unwrap_or(u64::MAX),
            id,
            WebEvidence {
                title,
                url: format!("https://en.wikipedia.org/?curid={id}"),
                snippet,
            },
        ));
    }
    ranked.sort_by_key(|(rank, id, _)| (*rank, *id));
    let mut seen = HashSet::new();
    Ok(ranked
        .into_iter()
        .filter(|(_, id, _)| seen.insert(*id))
        .take(3)
        .map(|(_, _, v)| v)
        .collect())
}

impl RetrievalQuery {
    pub(super) async fn wikipedia(
        &self,
        db: &Database,
        task: &ResearchTask<'_>,
        permitted: bool,
    ) -> AppResult<Vec<WebEvidence>> {
        if self.kind != RetrievalKind::Wikipedia || !permitted || !task.settings.wiki_enabled {
            return Err(AppError::Invalid(
                "Wikipedia retrieval not permitted".into(),
            ));
        }
        let _guard = WIKI_GATE.lock().await;
        tokio::time::sleep(Duration::from_millis(200)).await;
        lease(db, task.job).await?;
        let mut url = wiki_endpoint()?;
        url.query_pairs_mut().extend_pairs([
            ("action", "query"),
            ("generator", "search"),
            ("gsrsearch", self.text.as_str()),
            ("gsrnamespace", "0"),
            ("gsrlimit", "3"),
            ("prop", "extracts"),
            ("exintro", "1"),
            ("explaintext", "1"),
            ("exchars", "1200"),
            ("exlimit", "3"),
            ("format", "json"),
            ("formatversion", "2"),
        ]);
        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(12))
            .user_agent("VideoRoll-Rust/0.1 (https://github.com/kelriclink/videoroll)")
            .build()?;
        let mut response = client
            .get(url)
            .header("Accept", "application/json")
            .send()
            .await?;
        if !response.status().is_success() {
            return Err(AppError::Unavailable(format!(
                "Wikipedia HTTP {}",
                response.status()
            )));
        }
        if response
            .content_length()
            .is_some_and(|n| n > WIKI_MAX_BYTES as u64)
        {
            return Err(AppError::Unavailable("Wikipedia response too large".into()));
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if chunk.len() > WIKI_MAX_BYTES.saturating_sub(body.len()) {
                return Err(AppError::Unavailable("Wikipedia response too large".into()));
            }
            body.extend_from_slice(&chunk);
        }
        let payload: Value = serde_json::from_slice(&body)
            .map_err(|_| AppError::Unavailable("invalid Wikipedia JSON".into()))?;
        let docs = wiki_pages(&payload)?;
        lease(db, task.job).await?;
        Ok(docs)
    }
}

// The specialist can request one additional local lookup or finish.
// Legacy target/note replies are accepted as finish only.
#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
enum SpecialistAction {
    SearchKnowledge {
        query: String,
    },
    Finish {
        target: String,
        #[serde(default)]
        note: String,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyFinish {
    target: String,
    #[serde(default)]
    note: String,
}

pub(super) enum SpecialistDecision {
    Search(String),
    Finish(Option<VerifiedTerm>),
}

pub(super) fn extra_query(raw: &str, term: &str) -> AppResult<String> {
    let query = RetrievalQuery::new(RetrievalKind::Knowledge, raw)?;
    let initial = RetrievalQuery::new(RetrievalKind::Knowledge, term)?;
    if query.key() == initial.key()
        || !crate::media::translation_context::mentions(&query.text, term)
    {
        return Err(AppError::Invalid(
            "duplicate or ungrounded specialist query".into(),
        ));
    }
    Ok(query.text)
}

pub(super) fn specialist_decision(
    raw: Value,
    term: &str,
    can_search: bool,
) -> AppResult<SpecialistDecision> {
    let action = if raw.get("action").is_some() {
        serde_json::from_value::<SpecialistAction>(raw)
            .map_err(|_| AppError::Invalid("unknown or malformed specialist action".into()))?
    } else {
        let value: LegacyFinish = serde_json::from_value(raw)
            .map_err(|_| AppError::Invalid("invalid specialist finish".into()))?;
        SpecialistAction::Finish {
            target: value.target,
            note: value.note,
        }
    };
    match action {
        SpecialistAction::SearchKnowledge { query } if can_search => {
            Ok(SpecialistDecision::Search(extra_query(&query, term)?))
        }
        SpecialistAction::SearchKnowledge { .. } => {
            Err(AppError::Invalid("specialist lookup limit reached".into()))
        }
        SpecialistAction::Finish { target, note } => {
            let target = target.trim();
            if target.chars().count() > 160 || target.chars().any(char::is_control) {
                return Err(AppError::Invalid("invalid specialist translation".into()));
            }
            Ok(SpecialistDecision::Finish((!target.is_empty()).then(
                || VerifiedTerm {
                    source: term.into(),
                    target: target.into(),
                    note: note.chars().take(180).collect(),
                },
            )))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn specialist_action_validation() {
        assert!(matches!(
            specialist_decision(
                json!({"action":"search_knowledge","query":"Rust compiler context"}),
                "Rust compiler",
                true
            )
            .unwrap(),
            SpecialistDecision::Search(_)
        ));
        for bad in [
            json!({"action":"search_knowledge","query":"Rust  compiler"}),
            json!({"action":"search_knowledge","query":"unrelated"}),
            json!({"action":"search_web","query":"Rust compiler"}),
            json!({"action":"search_knowledge","query":"Rust compiler context","other":1}),
        ] {
            assert!(specialist_decision(bad, "Rust compiler", true).is_err());
        }
        assert!(
            specialist_decision(
                json!({"action":"search_knowledge","query":"Rust compiler context"}),
                "Rust compiler",
                false
            )
            .is_err()
        );
        assert!(matches!(
            specialist_decision(json!({"target":"编译器"}), "Rust compiler", false).unwrap(),
            SpecialistDecision::Finish(Some(_))
        ));
    }
    #[test]
    fn wikipedia_response_bounds() {
        let payload = json!({"query":{"pages":[
            {"pageid":44,"ns":0,"index":2,"title":"B","extract":"B text"},
            {"pageid":11,"ns":0,"index":1,"title":"A","extract":"A  text","fullurl":"https://example.invalid"},
            {"pageid":11,"ns":0,"index":3,"title":"duplicate","extract":"dup"},
            {"pageid":12,"ns":1,"title":"Talk","extract":"ignore"},
            {"pageid":13,"ns":0,"title":"missing","missing":true,"extract":"ignore"}
        ]}});
        let docs = wiki_pages(&payload).unwrap();
        assert_eq!(docs.len(), 2);
        assert_eq!(docs[0].url, "https://en.wikipedia.org/?curid=11");
        assert_eq!(docs[0].snippet, "A text");
        assert!(wiki_pages(&json!({"error":{"code":"maxlag"}})).is_err());
        assert!(
            wiki_pages(&json!({"batchcomplete":true}))
                .unwrap()
                .is_empty()
        );
        assert!(wiki_pages(&json!({})).is_err());
    }
    #[test]
    fn query_identity_and_bounds() {
        use RetrievalKind::*;
        let a = RetrievalQuery::new(Knowledge, " Rust  compiler ").unwrap();
        let b = RetrievalQuery::new(Knowledge, "rust compiler").unwrap();
        assert_eq!(a.key(), b.key());
        assert_ne!(
            a.key(),
            RetrievalQuery::new(Web, "rust compiler").unwrap().key()
        );
        for bad in ["", "  ", "test\nquery"] {
            assert!(RetrievalQuery::new(Knowledge, bad).is_err());
        }
        assert!(RetrievalQuery::new(Web, &"a".repeat(251)).is_err());
    }
}
