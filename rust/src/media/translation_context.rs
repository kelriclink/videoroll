//! Deterministic scene boundaries and original translation-memory contract.
use crate::{AppResult, Database, domain::WorkItem, subtitles::SubtitleCue};
use serde_json::{Value, json};
use sqlx::Row;
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

fn clean(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}
fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
fn terminal(s: &str) -> bool {
    s.trim_end()
        .trim_end_matches(['"', '\'', '”', '’', '）', ')', ']'])
        .ends_with(['.', '!', '?', '。', '！', '？'])
}
fn weak(s: &str) -> bool {
    s.trim_end()
        .trim_end_matches(['"', '\'', '”', '’', '）', ')', ']'])
        .ends_with([',', '，', '、', ';', '；', ':', '：'])
}
fn gap(a: &SubtitleCue, b: &SubtitleCue) -> f64 {
    b.start_ms.saturating_sub(a.end_ms) as f64 / 1000.
}
fn percentile(v: &[f64], p: f64) -> f64 {
    let x = p * (v.len() - 1) as f64;
    let lo = x.floor() as usize;
    let hi = (lo + 1).min(v.len() - 1);
    v[lo] * (1. - x.fract()) + v[hi] * x.fract()
}
#[derive(Debug, Clone)]
pub struct ScenePlan {
    pub soft: f64,
    pub hard: f64,
    pub scenes: Vec<(usize, usize)>,
}
impl ScenePlan {
    pub fn new(cues: &[SubtitleCue]) -> Self {
        let mut gaps = cues
            .windows(2)
            .map(|w| gap(&w[0], &w[1]))
            .filter(|x| (0.08..=8.).contains(x))
            .collect::<Vec<_>>();
        gaps.sort_by(f64::total_cmp);
        let (soft, hard) = if gaps.len() < 4 {
            (1.2, 2.5)
        } else {
            let soft = percentile(&gaps, 0.75).clamp(0.7, 1.6);
            (
                soft,
                (soft + 0.75).max((percentile(&gaps, 0.9) * 1.35).clamp(2., 4.)),
            )
        };
        let (soft, hard) = (
            (soft * 1000.).round() / 1000.,
            (hard * 1000.).round() / 1000.,
        );
        let mut scenes = Vec::new();
        let mut start = 0;
        for i in 1..cues.len() {
            let prev = &cues[i - 1];
            let g = gap(prev, &cues[i]);
            let n = i - start;
            if g >= hard
                || (g >= soft && n >= 4 && terminal(&prev.text))
                || n >= 24
                || prev.end_ms.saturating_sub(cues[start].start_ms) >= 90000
            {
                scenes.push((start, i));
                start = i;
            }
        }
        if start < cues.len() {
            scenes.push((start, cues.len()));
        }
        Self { soft, hard, scenes }
    }
    pub fn batches(&self, cues: &[SubtitleCue], width: usize) -> Vec<(usize, usize)> {
        let mut out = Vec::new();
        for &(begin, end) in &self.scenes {
            let mut start = begin;
            while start < end {
                let hard = end.min(start + width.max(1));
                let mut chosen = hard;
                if hard > start + 1 && hard != end {
                    let earliest =
                        start + (((hard - start) as f64 * 0.6).round_ties_even() as usize).max(1);
                    let mut best = 0.;
                    for i in earliest..=hard {
                        let prev = &cues[i - 1];
                        let g = if i < cues.len() {
                            gap(prev, &cues[i])
                        } else {
                            0.
                        };
                        let score = (if terminal(&prev.text) {
                            65.
                        } else if weak(&prev.text) {
                            28.
                        } else {
                            0.
                        }) + (if g >= 2.5 {
                            100.
                        } else if g >= 1.2 {
                            70.
                        } else if g >= 0.6 {
                            35.
                        } else {
                            0.
                        }) + (i - start) as f64 * 0.75;
                        if score >= best {
                            best = score;
                            chosen = i;
                        }
                    }
                    if best < 45. {
                        chosen = hard;
                    }
                }
                out.push((start, chosen));
                start = chosen;
            }
        }
        out
    }
    pub fn context(
        &self,
        cues: &[SubtitleCue],
        start: usize,
        end: usize,
        translated: &[String],
    ) -> Value {
        let previous=(start.saturating_sub(3)..start).map(|i|json!({"idx":i+1,"text":take(&clean(&cues[i].text),240),"translation":translated.get(i).map(|s|take(s,240))})).collect::<Vec<_>>();
        let following = (end..(end + 2).min(cues.len()))
            .map(|i| json!({"idx":i+1,"text":take(&clean(&cues[i].text),240)}))
            .collect::<Vec<_>>();
        let scene=self.scenes.iter().enumerate().find(|(_,s)|s.0<=start&&start<s.1).map(|(id,(a,b))|json!({"scene_id":id+1,"segment_start":a+1,"segment_end":b,"time_start":cues[*a].start_ms as f64/1000.,"time_end":cues[b-1].end_ms as f64/1000.,"batch_offset":start-a,"batch_count":end-start}));
        json!({"batch":{"segment_start":start+1,"segment_end":end},"previous_source":previous,"next_source":following,"scene":scene,"adaptive_scene_profile":{"soft_gap_seconds":self.soft,"hard_gap_seconds":self.hard}})
    }
}
// NFKC and Unicode lowercase; explicitly account for common casefold
// differences. Full Unicode casefold parity requires a pinned casefold table.
fn norm(s: &str) -> String {
    take(
        &clean(
            &s.nfkc()
                .flat_map(char::to_lowercase)
                .collect::<String>()
                .replace('ß', "ss")
                .replace('ς', "σ"),
        ),
        4000,
    )
}
fn memory_id(task: Uuid, lang: &str, source: &str) -> Uuid {
    Uuid::new_v5(
        &Uuid::from_u128(0x35a14acaebce46a096a1bae6b30d83e1),
        format!("videoroll-translation-memory|{task}|{lang}|{source}").as_bytes(),
    )
}

pub async fn recall(
    db: &Database,
    task: Uuid,
    lang: &str,
    domain: &str,
    batch: &[SubtitleCue],
    start: usize,
) -> AppResult<Value> {
    // Exact matches only in this first port. Bounded query, no global
    // machine-generated examples; never trust recalled text as instructions.
    let norms = batch
        .iter()
        .map(|s| norm(&s.text))
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>();
    let rows=sqlx::query("SELECT source_text,source_norm,target_text,task_id=$1 AS same_task FROM translation_memory_entries WHERE target_lang=$2 AND source_norm=ANY($3) AND (task_id=$1 OR status='approved') AND (task_id=$1 OR $4='' OR domain='' OR domain=$4) ORDER BY (task_id=$1) DESC NULLS LAST,(status='approved') DESC,updated_at DESC,id LIMIT 400")
        .bind(task).bind(take(lang,16)).bind(&norms).bind(take(domain,500)).fetch_all(&db.pool).await?;
    let mut out = Vec::new();
    for (i, cue) in batch.iter().enumerate() {
        let n = norm(&cue.text);
        for row in rows
            .iter()
            .filter(|r| r.get::<String, _>("source_norm") == n)
            .take(2)
        {
            out.push(json!({"source":take(&row.get::<String,_>("source_text"),240),"target":take(&row.get::<String,_>("target_text"),240),"similarity":1.0,"scope":if row.get::<Option<bool>,_>("same_task")==Some(true){"same_task"}else{"approved_global"},"applies_to_blocks":[start+i+1]}));
            if out.len() == 6 {
                return Ok(json!(out));
            }
        }
    }
    Ok(json!(out))
}
pub async fn remember(
    db: &Database,
    job: &WorkItem,
    lang: &str,
    domain: &str,
    source: &[SubtitleCue],
    target: &[String],
) -> AppResult<()> {
    let mut tx = db.pool.begin().await?;
    // Fence the original memory write on the current job lease within the
    // transaction; lock the original durable inbox row, not an added table.
    let held:Option<Uuid>=sqlx::query_scalar("SELECT id FROM operation_inbox WHERE id=$1 AND runtime_engine='rust-v2' AND lease_token=$2 AND status='running' AND lease_until>now() AND EXISTS(SELECT 1 FROM pipeline_runs w WHERE w.id=operation_inbox.workflow_id AND w.state='running') FOR UPDATE").bind(job.id).bind(job.lease_token).fetch_optional(&mut *tx).await?;
    if held.is_none() {
        return Err(crate::AppError::Conflict(
            "stale translation memory lease".into(),
        ));
    }
    for (s, t) in source.iter().zip(target) {
        let n = norm(&s.text);
        if n.is_empty() || t.trim().is_empty() {
            continue;
        }
        sqlx::query("INSERT INTO translation_memory_entries(id,source_text,source_norm,target_text,target_lang,domain,task_id,subtitle_job_id,source_kind,status,quality_score,usage_count) VALUES($1,$2,$3,$4,$5,$6,$7,NULL,'machine','machine',0,0) ON CONFLICT(id) DO UPDATE SET target_text=EXCLUDED.target_text,domain=EXCLUDED.domain,updated_at=now() WHERE translation_memory_entries.status='machine' AND translation_memory_entries.source_kind='machine'")
            .bind(memory_id(job.task_id,lang,&n)).bind(take(s.text.trim(),8000)).bind(n).bind(take(t.trim(),8000)).bind(take(lang,16)).bind(take(domain,500)).bind(job.task_id).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(())
}
pub fn quality(source: &[SubtitleCue], target: &[String], start: usize) -> Vec<Value> {
    let mut issues = Vec::new();
    for (i, (s, t)) in source.iter().zip(target).enumerate() {
        let digits = |text: &str| {
            text.split(|c: char| !c.is_ascii_digit())
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect::<Vec<_>>()
        };
        let mut numbers = digits(t);
        let mut missing = Vec::new();
        for n in digits(&s.text) {
            if let Some(j) = numbers.iter().position(|v| v == &n) {
                numbers.remove(j);
            } else {
                missing.push(n);
            }
        }
        if !missing.is_empty() {
            issues.push(json!({"idx":start+i+1,"type":"number_mismatch","severity":"warning","expected":missing.into_iter().take(12).collect::<Vec<_>>()}));
        }
        if i > 0
            && norm(&source[i - 1].text) != norm(&s.text)
            && norm(&target[i - 1]) == norm(t)
            && norm(t).chars().count() >= 2
            && norm(&s.text).chars().count() >= 3
            && norm(&source[i - 1].text).chars().count() >= 3
        {
            for (idx, neighbor) in [(start + i, start + i + 1), (start + i + 1, start + i)] {
                issues.push(json!({"idx":idx,"type":"adjacent_duplicate_translation","severity":"warning","neighbor_idx":neighbor}));
            }
        }
    }
    issues
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cue(start: u64, end: u64, text: &str) -> SubtitleCue {
        SubtitleCue {
            start_ms: start,
            end_ms: end,
            text: text.into(),
            secondary_text: None,
        }
    }
    #[test]
    fn scene_pause_boundary_and_context() {
        let c = vec![
            cue(0, 1000, "one"),
            cue(1100, 2000, "two."),
            cue(6000, 7000, "three"),
        ];
        let p = ScenePlan::new(&c);
        assert_eq!(p.scenes, vec![(0, 2), (2, 3)]);
        assert_eq!(p.batches(&c, 24), p.scenes);
        assert_eq!(
            p.context(&c, 2, 3, &["一".into(), "二".into()])["previous_source"][1]["translation"],
            "二"
        );
    }
    #[test]
    fn normalization_and_quality() {
        assert_eq!(norm(" ＡＢＣ  Straße "), "abc strasse");
        let c = vec![cue(0, 1000, "10 volts"), cue(1000, 2000, "20 volts")];
        assert_eq!(quality(&c, &["10伏".into(), "10伏".into()], 0).len(), 3);
    }
}
