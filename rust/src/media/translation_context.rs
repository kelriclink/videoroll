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

// Ratcliff/Obershelp matching blocks, like Python SequenceMatcher
// (autojunk=False). Bounded fuzzy candidates; exact long matches remain valid.
fn ratio(a: &str, b: &str) -> f64 {
    if a == b {
        return 1.;
    }
    let a: Vec<char> = a.chars().take(257).collect();
    let b: Vec<char> = b.chars().take(257).collect();
    if a.is_empty() || b.is_empty() || a.len() > 256 || b.len() > 256 {
        return 0.;
    }
    let mut work = vec![(0, a.len(), 0, b.len())];
    let mut matched = 0;
    while let Some((al, ah, bl, bh)) = work.pop() {
        let mut prev = vec![0usize; bh - bl + 1];
        let mut best = (0, al, bl);
        for i in al..ah {
            let mut next = vec![0; bh - bl + 1];
            for j in bl..bh {
                if a[i] == b[j] {
                    let n = prev[j - bl] + 1;
                    next[j - bl + 1] = n;
                    if n > best.0 {
                        best = (n, i + 1 - n, j + 1 - n);
                    }
                }
            }
            prev = next;
        }
        let (n, i, j) = best;
        if n == 0 {
            continue;
        }
        matched += n;
        if i > al && j > bl {
            work.push((al, i, bl, j));
        }
        if i + n < ah && j + n < bh {
            work.push((i + n, ah, j + n, bh));
        }
    }
    2. * matched as f64 / (a.len() + b.len()) as f64
}
pub async fn recall(
    db: &Database,
    task: Uuid,
    lang: &str,
    domain: &str,
    batch: &[SubtitleCue],
    start: usize,
) -> AppResult<Value> {
    let norms = batch.iter().map(|s| norm(&s.text)).collect::<Vec<_>>();
    // Exact candidates first, then original same-task/approved ranking.
    // Limit CPU work even with long pathological texts.
    let rows=sqlx::query("SELECT source_text,source_norm,target_text,task_id=$1 AS same_task FROM translation_memory_entries WHERE target_lang=$2 AND (task_id=$1 OR status='approved') AND (task_id=$1 OR $4='' OR domain='' OR domain=$4) ORDER BY (source_norm=ANY($3)) DESC,(task_id=$1) DESC NULLS LAST,(status='approved') DESC,updated_at DESC,id LIMIT 400")
        .bind(task).bind(take(lang,16)).bind(&norms).bind(take(domain,500)).fetch_all(&db.pool).await?;
    let candidates = rows
        .iter()
        .map(|r| {
            (
                r.get::<String, _>("source_text"),
                r.get::<String, _>("source_norm"),
                r.get::<String, _>("target_text"),
                r.get::<Option<bool>, _>("same_task") == Some(true),
            )
        })
        .collect::<Vec<_>>();
    // CPU-heavy matching must not block Tokio's worker or lease heartbeat.
    let ranked=tokio::task::spawn_blocking(move||{
        let mut ranked=Vec::new();let mut budget=24_000_000usize;
        for (i,n) in norms.iter().enumerate(){
            if n.is_empty(){continue;}let len=n.chars().count();let mut local=Vec::new();
            for (source,cn,target,same) in &candidates{
                if cn.is_empty()||target.trim().is_empty(){continue;}
                let threshold=if len<12{if *same{0.85}else{0.92}}else if *same{0.58}else{0.78};
                let score=if cn==n{1.}else{
                    let m=cn.chars().count();
                    if len>256||m>256||2.*len.min(m) as f64/((len+m)as f64)<threshold{continue;}
                    let cost=len*m;if cost>budget{continue;}budget-=cost;
                    ratio(n,cn)
                };
                if score>=threshold{local.push((score,*same,source,target));}
            }
            local.sort_by(|a,b|b.0.total_cmp(&a.0).then_with(||b.1.cmp(&a.1)).then_with(||a.2.chars().count().cmp(&b.2.chars().count())));
            for (score,same,source,target) in local.into_iter().take(2){
                ranked.push((score,i,json!({"source":take(source,240),"target":take(target,240),"similarity":(score*10000.).round()/10000.,"scope":if same{"same_task"}else{"approved_global"},"applies_to_blocks":[start+i+1]})));
            }
        }
        ranked.sort_by(|a,b|b.0.total_cmp(&a.0).then_with(||a.1.cmp(&b.1)));
        let mut seen=std::collections::HashSet::new();
        ranked.into_iter().filter(|(_,i,v)|seen.insert((*i,norm(v["source"].as_str().unwrap_or("")),v["target"].as_str().unwrap_or("").to_owned()))).take(6).map(|(_,_,v)|v).collect::<Vec<_>>()
    }).await.map_err(|_|crate::AppError::Unavailable("translation memory matcher failed".into()))?;
    Ok(json!(ranked))
}

// Model-provided context is untrusted reference data, never an instruction.
// Only learn named records whose source spelling occurs in this batch.
pub fn mentions(source: &str, needle: &str) -> bool {
    let source = norm(source);
    let needle = norm(needle);
    if needle.is_empty() {
        return false;
    }
    let ascii = needle
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_alphanumeric)
        && needle
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.+- ".contains(&b));
    source.match_indices(&needle).any(|(i, m)| {
        !ascii
            || (!source[..i]
                .as_bytes()
                .last()
                .is_some_and(u8::is_ascii_alphanumeric)
                && !source[i + m.len()..]
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric))
    })
}
fn field(v: &Value, k: &str, limit: usize) -> String {
    take(&clean(v[k].as_str().unwrap_or("")), limit)
}
pub fn merge_context(base: &Value, update: &Value, batch: &[SubtitleCue], scene_id: u64) -> Value {
    let mut out = if base.is_object() {
        base.clone()
    } else {
        json!({"version":1,"topic":"","style_notes":"","characters":[],"terminology":[],"ambiguities":[]})
    };
    for k in ["topic", "style_notes"] {
        let v = field(update, k, 500);
        if !v.is_empty() {
            out[k] = json!(v);
        }
    }
    let grounded = |v: &str| batch.iter().any(|s| mentions(&s.text, v));
    for (kind, key, limit) in [
        ("characters", "name", 32),
        ("terminology", "source", 64),
        ("ambiguities", "term", 32),
    ] {
        let mut rows = out[kind].as_array().cloned().unwrap_or_default();
        for raw in update[kind]
            .as_array()
            .into_iter()
            .flatten()
            .take(limit * 2)
        {
            let name = field(raw, key, if kind == "characters" { 120 } else { 160 });
            let aliases = raw["aliases"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(|v| take(&clean(v), 120))
                .filter(|v| !v.is_empty())
                .take(8)
                .collect::<Vec<_>>();
            if name.is_empty()
                || !(grounded(&name)
                    || (kind == "characters" && aliases.iter().any(|v| grounded(v))))
            {
                continue;
            }
            let row = match kind {
                "characters" => {
                    json!({"name":name,"target_name":field(raw,"target_name",120),"role":field(raw,"role",240),"notes":field(raw,"notes",500),"aliases":aliases})
                }
                "terminology" => {
                    let target = field(raw, "target", 160);
                    if target.is_empty() {
                        continue;
                    }
                    json!({"source":name,"target":target,"meaning":field(raw,"meaning",500)})
                }
                _ => {
                    let res = field(raw, "resolution", 500);
                    if res.is_empty() {
                        continue;
                    }
                    json!({"term":name,"resolution":res})
                }
            };
            if let Some(old) = rows
                .iter_mut()
                .find(|v| norm(v[key].as_str().unwrap_or("")) == norm(&name))
            {
                if let Some(fields) = row.as_object() {
                    for (k, v) in fields {
                        if !v.is_null()
                            && v.as_str() != Some("")
                            && v.as_array().is_none_or(|x| !x.is_empty())
                        {
                            old[k] = v.clone();
                        }
                    }
                }
            } else if rows.len() < limit {
                rows.push(row);
            }
        }
        rows.truncate(limit);
        out[kind] = json!(rows);
    }
    let summary = field(update, "scene_summary", 800);
    let summary = if summary.is_empty() {
        field(&update["recent_scene"], "summary", 800)
    } else {
        summary
    };
    if !summary.is_empty() {
        out["recent_scene"] = json!({"scene_id":scene_id.max(1),"summary":summary});
    }
    out
}
pub fn bounded_context(memory: &Value) -> Value {
    let mut v = memory.clone();
    // Bound prompt independently from persisted memory. Prefer recent
    // records, keeping complete JSON rather than truncating a JSON string.
    for (key, n) in [("characters", 8), ("terminology", 16), ("ambiguities", 8)] {
        if let Some(rows) = v[key].as_array_mut() {
            if rows.len() > n {
                rows.drain(..rows.len() - n);
            }
        }
    }
    while v.to_string().len() > 12000 {
        let mut removed = false;
        for key in ["characters", "ambiguities", "terminology"] {
            if let Some(rows) = v[key].as_array_mut() {
                if !rows.is_empty() {
                    rows.remove(0);
                    removed = true;
                    break;
                }
            }
        }
        if !removed {
            break;
        }
    }
    v
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
        let missing = super::translation_quality::missing_numbers(&s.text, t);
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
            confidence: None,
        }
    }
    #[test]
    fn fuzzy_and_grounded_memory() {
        assert!((ratio("abcd", "abdc") - 0.75).abs() < 1e-8);
        assert_eq!(ratio("abcd", "abcd"), 1.);
        assert!(ratio("The resistor is red.", "The resistor is blue.") > 0.7);
        assert!(!mentions("a transistor", "trans"));
        assert!(mentions("A resistor.", "resistor"));
        let batch = vec![cue(0, 1000, "Alice uses a resistor.")];
        let m = merge_context(
            &Value::Null,
            &json!({"topic":"electronics","characters":[{"name":"Alice","target_name":"爱丽丝"},{"name":"Bob","target_name":"鲍勃"}],"terminology":[{"source":"resistor","target":"电阻"},{"source":"capacitor","target":"电容"}],"scene_summary":"testing"}),
            &batch,
            2,
        );
        assert_eq!(m["characters"].as_array().unwrap().len(), 1);
        assert_eq!(m["terminology"].as_array().unwrap().len(), 1);
        assert_eq!(m["recent_scene"]["scene_id"], 2);
        let next = merge_context(
            &m,
            &json!({"characters":[{"name":"Alice","role":"engineer"}]}),
            &batch,
            2,
        );
        assert_eq!(next["characters"][0]["target_name"], "爱丽丝");
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
