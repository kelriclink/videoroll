//! Shared retrieval contract for the existing serial and specialist agents.
use super::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(super) enum RetrievalKind {
    Knowledge,
    Web,
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
