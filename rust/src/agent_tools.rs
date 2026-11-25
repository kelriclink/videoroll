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

#[cfg(test)]
mod tests {
    use super::*;
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
