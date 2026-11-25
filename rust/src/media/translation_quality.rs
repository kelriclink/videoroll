//! Bounded semantic numeric warnings and grounded per-cue term constraints.
use super::translation_context::mentions;
use crate::subtitles::SubtitleCue;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use unicode_normalization::UnicodeNormalization;

fn norm(s: &str) -> String {
    s.nfkc()
        .flat_map(char::to_lowercase)
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
// Decimal arithmetic using strings, avoiding binary floating point rounding.
fn canonical(token: &str, scale: usize) -> Option<String> {
    if token.len() > 96 {
        return None;
    }
    let sign = if token.starts_with('-') {
        "-"
    } else if token.starts_with('+') {
        "+"
    } else {
        ""
    };
    let raw = token.trim_start_matches(['+', '-']);
    let dot = raw.rfind('.');
    let comma = raw.rfind(',');
    let mut raw = raw.to_owned();
    if let (Some(d), Some(c)) = (dot, comma) {
        let (decimal, group) = if d > c { ('.', ',') } else { (',', '.') };
        raw = raw.replace(group, "").replace(decimal, ".");
    } else if dot.is_some() || comma.is_some() {
        let sep = if dot.is_some() { '.' } else { ',' };
        let parts = raw.split(sep).collect::<Vec<_>>();
        if (parts.len() > 2 && parts[1..].iter().all(|p| p.len() == 3))
            || (parts.len() == 2 && parts[1].len() == 3 && !matches!(parts[0], "" | "0"))
        {
            raw = parts.concat();
        } else {
            raw = parts.join(".");
        }
    }
    let (integer, fraction) = raw.split_once('.').unwrap_or((&raw, ""));
    if integer.is_empty()
        || !integer.bytes().all(|b| b.is_ascii_digit())
        || !fraction.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let mut digits = format!("{integer}{fraction}");
    let point = integer.len() + scale;
    if point > digits.len() {
        digits.push_str(&"0".repeat(point - digits.len()));
    }
    let (a, b) = digits.split_at(point);
    let a = a.trim_start_matches('0');
    let a = if a.is_empty() { "0" } else { a };
    let b = b.trim_end_matches('0');
    Some(if b.is_empty() {
        format!("{sign}{a}")
    } else {
        format!("{sign}{a}.{b}")
    })
}
fn numbers(text: &str, source: bool) -> (BTreeSet<String>, BTreeSet<String>) {
    let text = norm(text);
    let bytes = text.as_bytes();
    let mut found = BTreeSet::new();
    let mut identifiers = BTreeSet::new();
    let mut i = 0;
    while i < bytes.len() {
        let start = i;
        if matches!(bytes[i], b'+' | b'-') && bytes.get(i + 1).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
        if !bytes[i].is_ascii_digit() {
            i += 1;
            continue;
        }
        while i < bytes.len()
            && (bytes[i].is_ascii_digit()
                || (matches!(bytes[i], b'.' | b',')
                    && bytes.get(i + 1).is_some_and(u8::is_ascii_digit)))
        {
            i += 1;
        }
        let token = &text[start..i];
        let identifier =
            token.starts_with('-') && start > 0 && bytes[start - 1].is_ascii_alphabetic();
        let token = if identifier { &token[1..] } else { token };
        let tail = text[i..].trim_start();
        let scale = if tail.starts_with('万') {
            4
        } else if tail.starts_with(['亿', '億']) {
            8
        } else if tail.starts_with('兆') {
            12
        } else {
            [
                ("thousand", 3),
                ("million", 6),
                ("billion", 9),
                ("trillion", 12),
            ]
            .into_iter()
            .find(|(s, _)| {
                tail.strip_prefix(s).is_some_and(|r| {
                    r.as_bytes()
                        .first()
                        .is_none_or(|b| !b.is_ascii_alphanumeric() && *b != b'_')
                })
            })
            .map(|(_, n)| n)
            .unwrap_or(0)
        };
        if let Some(n) = canonical(token, scale) {
            found.insert(n);
        }
        if !source || scale == 0 {
            if let Some(n) = canonical(token, 0) {
                if identifier && source {
                    identifiers.insert(n.clone());
                }
                found.insert(n);
            }
        }
    }
    (found, identifiers)
}
pub fn missing_numbers(source: &str, target: &str) -> Vec<String> {
    let (a, ids) = numbers(source, true);
    let (mut b, _) = numbers(target, false);
    for id in ids {
        if b.contains(&format!("-{id}")) {
            b.insert(id);
        }
    }
    a.difference(&b).take(12).cloned().collect()
}
pub fn validate_glossary(g: &Value) -> crate::AppResult<()> {
    if g.is_null() {
        return Ok(());
    }
    let map = g
        .as_object()
        .ok_or_else(|| crate::AppError::Invalid("translate_glossary must be an object".into()))?;
    let mut identities = BTreeSet::new();
    if map.len() > 64
        || map.iter().any(|(k, v)| {
            k.trim().is_empty()
                || glossary_key(k).is_empty()
                || !identities.insert(glossary_key(k))
                || k.chars().count() > 240
                || v.as_str()
                    .is_none_or(|s| s.trim().is_empty() || s.chars().count() > 240)
        })
    {
        return Err(crate::AppError::Invalid(
            "invalid translation glossary (maximum 64 distinct bounded term mappings)".into(),
        ));
    }
    Ok(())
}
pub(crate) fn glossary_key(raw: &str) -> String {
    crate::subtitle_knowledge_write_v1::normalize_term(&norm(raw))
}
pub(crate) fn glossary_contains(glossary: &Value, term: &str) -> bool {
    let key = glossary_key(term);
    !key.is_empty()
        && glossary
            .as_object()
            .is_some_and(|g| g.keys().any(|s| glossary_key(s) == key))
}
// Drop lower-priority memory entries instead of sending contradictory
// recommendations alongside the explicitly required glossary.
pub(crate) fn protect_glossary(memory: &mut Value, glossary: &Value) {
    for (kind, field) in [("terminology", "source"), ("characters", "name")] {
        if let Some(rows) = memory.get_mut(kind).and_then(Value::as_array_mut) {
            rows.retain(|r| {
                !glossary_contains(glossary, r[field].as_str().unwrap_or(""))
                    && !r["aliases"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                        .any(|s| glossary_contains(glossary, s))
            });
        }
    }
}
pub fn constraints(batch: &[SubtitleCue], glossary: &Value, memory: &Value) -> Value {
    let mut out = Vec::new();
    let mut seen = BTreeSet::new();
    let mut add = |source: &str, target: &str, mode: &str, aliases: &[Value]| {
        let s = source.trim();
        let t = target.trim();
        if s.is_empty() || t.is_empty() || s.chars().count() > 240 || t.chars().count() > 240 {
            return;
        }
        let ids = batch
            .iter()
            .enumerate()
            .filter(|(_, cue)| {
                mentions(&cue.text, s)
                    || aliases
                        .iter()
                        .filter_map(Value::as_str)
                        .any(|v| mentions(&cue.text, v))
            })
            .map(|(i, _)| i + 1)
            .collect::<Vec<_>>();
        if !ids.is_empty() && out.len() < 64 && seen.insert(glossary_key(s)) {
            out.push(json!({"source":s,"target":t,"mode":mode,"applies_to_blocks":ids}));
        }
    };
    if let Some(g) = glossary.as_object() {
        for (s, t) in g.iter().take(64) {
            if let Some(t) = t.as_str() {
                add(s, t, "hard", &[]);
            }
        }
    }
    for row in memory["terminology"]
        .as_array()
        .into_iter()
        .flatten()
        .take(64)
    {
        if glossary_contains(glossary, row["source"].as_str().unwrap_or("")) {
            continue;
        }
        add(
            row["source"].as_str().unwrap_or(""),
            row["target"].as_str().unwrap_or(""),
            "preferred",
            &[],
        );
    }
    for row in memory["characters"]
        .as_array()
        .into_iter()
        .flatten()
        .take(32)
    {
        if glossary_contains(glossary, row["name"].as_str().unwrap_or(""))
            || row["aliases"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .any(|s| glossary_contains(glossary, s))
        {
            continue;
        }
        add(
            row["name"].as_str().unwrap_or(""),
            row["target_name"].as_str().unwrap_or(""),
            "preferred",
            row["aliases"].as_array().map(Vec::as_slice).unwrap_or(&[]),
        );
    }
    json!(out)
}
pub fn issues(batch: &[SubtitleCue], target: &[String], constraints: &Value) -> Vec<Value> {
    let mut issues = super::translation_context::quality(batch, target, 0);
    for (i, _) in batch.iter().enumerate() {
        if target.get(i).is_none_or(|t| t.trim().is_empty()) {
            issues.push(json!({"idx":i+1,"type":"empty_translation","severity":"error"}));
        }
    }
    for c in constraints.as_array().into_iter().flatten() {
        let expected = c["target"].as_str().unwrap_or("");
        let hard = c["mode"] == "hard";
        for id in c["applies_to_blocks"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_u64)
        {
            if !target
                .get((id as usize).saturating_sub(1))
                .is_some_and(|t| norm(t).contains(&norm(expected)))
            {
                issues.push(json!({"idx":id,"type":if hard{"hard_term_missing"}else{"preferred_term_missing"},"severity":if hard{"error"}else{"warning"},"source_span":c["source"],"expected":expected}));
            }
        }
    }
    issues
}
pub fn blocking(issues: &[Value]) -> bool {
    issues
        .iter()
        .any(|v| v["severity"] == "error" && v["type"] != "number_mismatch")
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn semantic_numbers() {
        for (a, b) in [
            ("200 million", "2亿"),
            ("1,200.00", "１２００"),
            ("SCP-145", "异常-145"),
            ("10 10 volts", "10伏"),
            ("-0.50", "-0.5"),
            ("2 billion", "20亿"),
        ] {
            assert!(missing_numbers(a, b).is_empty(), "{a} => {b}");
        }
        assert_eq!(missing_numbers("450W", "4500W"), vec!["450"]);
        assert_eq!(missing_numbers("-12", "12"), vec!["-12"]);
        assert_eq!(missing_numbers("0.125", "125"), vec!["0.125"]);
    }
    #[test]
    fn explicit_glossary_protects_memory() {
        let glossary = json!({"Volts":"伏特","PCB board":"电路板"});
        let mut memory = json!({"terminology":[{"source":"volts","target":"伏"},{"source":"resistor","target":"电阻"}],"characters":[{"name":"PCB","aliases":["PCB-board"],"target_name":"旧译名"}]});
        protect_glossary(&mut memory, &glossary);
        assert_eq!(
            memory["terminology"],
            json!([{"source":"resistor","target":"电阻"}])
        );
        assert_eq!(memory["characters"], json!([]));
        assert!(validate_glossary(&json!({"PCB-board":"甲","pcb board":"乙"})).is_err());
    }
    #[test]
    fn hard_vs_preferred() {
        let batch = vec![SubtitleCue {
            start_ms: 0,
            end_ms: 1000,
            text: "a resistor".into(),
            secondary_text: None,
        }];
        let c = constraints(
            &batch,
            &json!({"resistor":"电阻","capacitor":"电容"}),
            &json!({"terminology":[{"source":"resistor","target":"电阻器"}]}),
        );
        assert_eq!(c.as_array().unwrap().len(), 1);
        assert!(blocking(&issues(&batch, &["元件".into()], &c)));
        assert!(!blocking(&issues(&batch, &["电阻".into()], &c)));
    }
}
