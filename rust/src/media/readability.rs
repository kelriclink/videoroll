//! Display-only adaptation of the original Python subtitle_readability and
//! subtitle_quality. Never change the one-to-one translation checkpoint.
use crate::{
    AppResult,
    subtitles::{SubtitleCue, validate},
};
use serde::Serialize;
use serde_json::{Value, json};
#[path = "readability_unicode.rs"]
mod unicode;
fn in_ranges(c: char, ranges: &[(u32, u32)]) -> bool {
    let v = c as u32;
    let i = ranges.partition_point(|r| r.1 < v);
    ranges.get(i).is_some_and(|r| r.0 <= v)
}
fn width(c: char) -> f64 {
    if c.is_whitespace() {
        0.28
    } else if in_ranges(c, unicode::WIDE) {
        0.9
    } else if c.is_ascii() {
        if "ilI.,'`!|:;".contains(c) {
            0.3
        } else if "MW@#%&".contains(c) {
            0.84
        } else {
            0.56
        }
    } else if in_ranges(c, unicode::PUNCT) {
        0.42
    } else {
        0.68
    }
}
fn text_width(s: &str) -> f64 {
    s.chars().map(width).sum()
}
fn clean(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}
fn reading(s: &str) -> usize {
    s.chars().filter(|c| !c.is_whitespace()).count()
}
fn cps(c: &SubtitleCue) -> f64 {
    reading(&c.text) as f64 / (c.end_ms.saturating_sub(c.start_ms) as f64 / 1000.).max(0.25)
}
fn round2(v: f64) -> f64 {
    (v * 100.).round() / 100.
}
fn round3(v: f64) -> f64 {
    (v * 1000.).round() / 1000.
}
#[derive(Clone, Copy, Serialize)]
pub(crate) struct Profile {
    pub max_line_units: f64,
    pub target_cps: f64,
    pub min_display_seconds: f64,
    pub max_gap_extension_seconds: f64,
}
pub(crate) fn profile(cues: &[SubtitleCue], lang: &str) -> Profile {
    let l = lang.trim().to_lowercase();
    let (base, units) = if ["zh", "ja", "ko"].iter().any(|p| l.starts_with(p)) {
        (18.0_f64, 23.)
    } else {
        (17., 28.)
    };
    let mut values = cues
        .iter()
        .filter(|c| c.end_ms > c.start_ms)
        .map(cps)
        .collect::<Vec<_>>();
    values.sort_by(f64::total_cmp);
    let median = values.get(values.len() / 2).copied().unwrap_or(base);
    let target = if median > base * 1.35 {
        (base + 2.).min(20.)
    } else if median < base * 0.65 {
        (base - 1.5).max(14.)
    } else {
        base
    };
    let short = cues
        .iter()
        .filter(|c| c.end_ms.saturating_sub(c.start_ms) < 800)
        .count();
    Profile {
        max_line_units: units,
        target_cps: target,
        min_display_seconds: if short as f64 > 0.2 * cues.len() as f64 {
            1.
        } else {
            0.9
        },
        max_gap_extension_seconds: 0.75,
    }
}
fn punctuation(c: char) -> f64 {
    if ".!?。！？".contains(c) {
        100.
    } else if ",，、;；:：".contains(c) {
        55.
    } else if c.is_whitespace() {
        22.
    } else {
        0.
    }
}
/// Prefix widths avoid Python's repeated slicing and rescanning of long
/// strings. Only split at Unicode scalar boundaries.
struct Text<'a> {
    s: &'a str,
    offsets: Vec<usize>,
    chars: Vec<char>,
    widths: Vec<f64>,
}
impl<'a> Text<'a> {
    fn new(s: &'a str) -> Self {
        let mut offsets = Vec::new();
        let mut chars = Vec::new();
        let mut widths = vec![0.];
        for (i, c) in s.char_indices() {
            offsets.push(i);
            chars.push(c);
            widths.push(widths.last().unwrap() + width(c));
        }
        offsets.push(s.len());
        Self {
            s,
            offsets,
            chars,
            widths,
        }
    }
    fn len(&self) -> usize {
        self.chars.len()
    }
    fn w(&self, a: usize, b: usize) -> f64 {
        self.widths[b] - self.widths[a]
    }
    fn piece(&self, a: usize, b: usize) -> &str {
        self.s[self.offsets[a]..self.offsets[b]].trim()
    }
    fn left_width(&self, a: usize, b: usize) -> f64 {
        self.w(
            a,
            if b > a && self.chars[b - 1].is_whitespace() {
                b - 1
            } else {
                b
            },
        )
    }
    fn right_width(&self, a: usize, b: usize) -> f64 {
        self.w(
            if a < b && self.chars[a].is_whitespace() {
                a + 1
            } else {
                a
            },
            b,
        )
    }
}
fn better(best: Option<(f64, usize)>, score: f64, pos: usize) -> bool {
    best.is_none_or(|(s, p)| score > s + 1e-8 || ((score - s).abs() <= 1e-8 && pos > p))
}
fn split(text: &str, max: f64) -> Vec<String> {
    let t = Text::new(text);
    let mut out = Vec::new();
    let mut a = 0;
    let n = t.len();
    while a < n && t.w(a, n) > max {
        let mut best = None;
        let mut hard = a + 1;
        for b in a + 1..n {
            let w = t.left_width(a, b);
            if w > max * 1.08 + 0.28 {
                break;
            }
            if t.w(a, b) <= max {
                hard = b;
            }
            let p = punctuation(t.chars[b - 1]);
            if p > 0. && w > 0. && w <= max * 1.08 {
                let score = p + (30. - (max * 0.9 - w).abs() * 2.).max(0.);
                if better(best, score, b) {
                    best = Some((score, b));
                }
            }
        }
        let b = best.map(|(_, p)| p).unwrap_or(hard);
        if b >= n || t.piece(a, b).is_empty() || t.piece(b, n).is_empty() {
            break;
        }
        out.push(t.piece(a, b).to_owned());
        a = b;
        while a < n && t.chars[a].is_whitespace() {
            a += 1;
        }
    }
    if a < n {
        out.push(t.piece(a, n).to_owned());
    }
    out
}
fn wrap(text: &str, max: f64) -> String {
    let t = Text::new(text);
    let n = t.len();
    let total = t.w(0, n);
    if n <= 1 || total <= max {
        return text.to_owned();
    }
    let mut best = None;
    for b in 1..n {
        let p = punctuation(t.chars[b - 1]);
        if p == 0. {
            continue;
        }
        let l = t.left_width(0, b);
        let r = t.right_width(b, n);
        if l <= 0. || r <= 0. || l.max(r) > max * 1.15 {
            continue;
        }
        let score = p - (l - r).abs() * 2.2;
        if better(best, score, b) {
            best = Some((score, b));
        }
    }
    let b = if let Some((_, b)) = best {
        b
    } else {
        let mid = t
            .widths
            .partition_point(|w| *w < total / 2.)
            .clamp(1, n - 1);
        (1..n)
            .filter(|&b| punctuation(t.chars[b - 1]) > 0. && b.abs_diff(mid) <= 3.max(n / 8))
            .min_by_key(|&b| b.abs_diff(mid))
            .unwrap_or(mid)
    };
    let (l, r) = (t.piece(0, b), t.piece(b, n));
    if l.is_empty() || r.is_empty() {
        text.to_owned()
    } else {
        format!("{l}\n{r}")
    }
}
#[derive(Debug, Serialize)]
pub(crate) struct Stats {
    input_segments: usize,
    output_segments: usize,
    wrapped_segments: usize,
    split_segments: usize,
    extended_segments: usize,
    max_cps_before: f64,
    max_cps_after: f64,
}
pub(crate) fn optimize(
    input: &[SubtitleCue],
    bilingual: bool,
    p: Profile,
) -> AppResult<(Vec<SubtitleCue>, Stats)> {
    validate(input)?;
    let mut source = input
        .iter()
        .filter_map(|c| {
            let text = clean(&c.text);
            if text.is_empty() {
                return None;
            }
            Some(SubtitleCue {
                text,
                secondary_text: c
                    .secondary_text
                    .as_deref()
                    .map(clean)
                    .filter(|s| !s.is_empty()),
                ..c.clone()
            })
        })
        .collect::<Vec<_>>();
    source.sort_by_key(|c| (c.start_ms, c.end_ms));
    let mut stats = Stats {
        input_segments: source.len(),
        output_segments: 0,
        wrapped_segments: 0,
        split_segments: 0,
        extended_segments: 0,
        max_cps_before: round2(source.iter().map(cps).fold(0., f64::max)),
        max_cps_after: 0.,
    };
    for i in 0..source.len().saturating_sub(1) {
        let next = source[i + 1].start_ms;
        let c = &mut source[i];
        let desired = (p
            .min_display_seconds
            .max(reading(&c.text) as f64 / p.target_cps.max(1.))
            * 1000.)
            .round() as u64;
        let end = c
            .end_ms
            .max(next.saturating_sub(50))
            .min(
                c.end_ms
                    .saturating_add((p.max_gap_extension_seconds * 1000.).round() as u64),
            )
            .min(c.start_ms.saturating_add(desired));
        if end > c.end_ms.saturating_add(10) {
            c.end_ms = end;
            stats.extended_segments += 1;
        }
    }
    let mut out = Vec::new();
    for c in source {
        // Secondary text must never be lost even if a legacy profile has a
        // false bilingual flag. Keep the one-to-one display event in that case.
        let pieces = if bilingual || c.secondary_text.is_some() {
            vec![c.text.clone()]
        } else {
            split(&c.text, p.max_line_units * 2.)
        };
        let dur = c.end_ms - c.start_ms;
        let pieces = if pieces.len() > 1 && dur / pieces.len() as u64 >= 550 {
            pieces
        } else {
            vec![c.text.clone()]
        };
        if pieces.len() > 1 {
            stats.split_segments += 1;
        }
        let weights = pieces
            .iter()
            .map(|s| text_width(s).max(1.))
            .collect::<Vec<_>>();
        let total = weights.iter().sum::<f64>();
        let mut used = 0.;
        let mut cursor = c.start_ms;
        let n = pieces.len();
        let mut changed = false;
        for (i, (text, weight)) in pieces.into_iter().zip(weights).enumerate() {
            used += weight;
            let end = if i + 1 == n {
                c.end_ms
            } else {
                c.start_ms
                    + ((dur as f64 * used / total).round() as u64)
                        .clamp(cursor - c.start_ms + 1, dur - (n - i - 1) as u64)
            };
            let text = wrap(&text, p.max_line_units);
            let secondary = c
                .secondary_text
                .as_ref()
                .map(|s| wrap(s, p.max_line_units / 0.68));
            changed |= text != c.text || secondary != c.secondary_text;
            out.push(SubtitleCue {
                start_ms: cursor,
                end_ms: end,
                text,
                secondary_text: secondary,
                confidence: c.confidence,
            });
            cursor = end;
            if out.len() > 250_000 {
                return Err(crate::AppError::Invalid(
                    "optimized subtitle exceeds cue limit".into(),
                ));
            }
        }
        if changed {
            stats.wrapped_segments += 1;
        }
    }
    validate(&out)?;
    stats.output_segments = out.len();
    stats.max_cps_after = round2(out.iter().map(cps).fold(0., f64::max));
    Ok((out, stats))
}
fn percentile(v: &mut [f64], p: f64) -> f64 {
    if v.is_empty() {
        return 0.;
    }
    v.sort_by(f64::total_cmp);
    let x = p * (v.len() - 1) as f64;
    let a = x.floor() as usize;
    v[a] * (1. - x.fract()) + v[(a + 1).min(v.len() - 1)] * x.fract()
}
pub(crate) fn report(
    source: &[SubtitleCue],
    translated: &[SubtitleCue],
    final_cues: &[SubtitleCue],
    p: Profile,
    memory: &Value,
) -> Value {
    let mut issues = Vec::new();
    let mut warnings = 0u64;
    let mut errors = 0u64;
    let mut push = |v: Value| {
        if v["severity"] == "error" {
            errors += 1;
        } else {
            warnings += 1;
        }
        if issues.len() < 200 {
            issues.push(v);
        }
    };
    let mut high = 0;
    let mut short = 0;
    let mut overlap = 0;
    let mut tight = 0;
    let mut c = Vec::with_capacity(final_cues.len());
    let mut d = Vec::with_capacity(final_cues.len());
    for (i, item) in final_cues.iter().enumerate() {
        let idx = i + 1;
        let rate = cps(item);
        let duration = (item.end_ms - item.start_ms) as f64 / 1000.;
        c.push(rate);
        d.push(duration);
        if rate > p.target_cps * 1.15 {
            high += 1;
            push(json!({"idx":idx,"type":"high_cps","severity":"warning","value":round2(rate)}));
        }
        if duration < 0.65 {
            short += 1;
            push(
                json!({"idx":idx,"type":"short_display","severity":"warning","value":round3(duration)}),
            );
        }
        let lines = item.text.lines().count();
        let widest = item.text.lines().map(text_width).fold(0., f64::max);
        if lines > 2 {
            push(json!({"idx":idx,"type":"too_many_lines","severity":"warning","value":lines}));
        }
        if widest > p.max_line_units * 1.15 {
            push(
                json!({"idx":idx,"type":"line_too_wide","severity":"warning","value":round2(widest)}),
            );
        }
    }
    for (i, w) in final_cues.windows(2).enumerate() {
        let gap = (w[1].start_ms as f64 - w[0].end_ms as f64) / 1000.;
        if gap < -0.01 {
            overlap += 1;
            push(
                json!({"idx":i+2,"type":"timeline_overlap","severity":"error","value":round3(gap)}),
            );
        } else if (0.0..0.05).contains(&gap) {
            tight += 1;
        }
    }
    let mut identical = 0;
    let mut missing = 0;
    if source.len() == translated.len() {
        for (i, (s, t)) in source.iter().zip(translated).enumerate() {
            let a = clean(&s.text).to_lowercase();
            if !a.is_empty() && a == clean(&t.text).to_lowercase() {
                identical += 1;
            }
            for term in memory["terminology"]
                .as_array()
                .into_iter()
                .flatten()
                .take(64)
            {
                let (Some(a), Some(b)) = (term["source"].as_str(), term["target"].as_str()) else {
                    continue;
                };
                if !a.is_empty()
                    && !b.is_empty()
                    && super::translation_context::mentions(&s.text, a)
                    && !super::translation_context::mentions(&t.text, b)
                {
                    missing += 1;
                    push(
                        json!({"idx":i+1,"type":"terminology_inconsistent","severity":"warning","source":a.chars().take(160).collect::<String>(),"expected":b.chars().take(160).collect::<String>()}),
                    );
                }
            }
        }
    }
    let scene = super::translation_context::ScenePlan::new(source);
    let max = c.iter().copied().fold(0., f64::max);
    json!({"version":1,"score":100u64.saturating_sub(errors.saturating_mul(12)+warnings.saturating_mul(2)),"segment_count":final_cues.len(),"source_segment_count":source.len(),"translation_segment_count":translated.len(),
      "metrics":{"cps_p50":round2(percentile(&mut c,0.5)),"cps_p95":round2(percentile(&mut c,0.95)),"cps_max":round2(max),"duration_p50":round3(percentile(&mut d,0.5)),"short_display_count":short,"high_cps_count":high,"overlap_count":overlap,"tight_gap_count":tight,"identical_source_target_count":identical,"terminology_inconsistent_count":missing},
      "adaptive_profile":{"readability":p,"scene":{"soft_gap_seconds":scene.soft,"hard_gap_seconds":scene.hard}},
      "context":{"characters":memory["characters"].as_array().map_or(0,Vec::len),"terminology":memory["terminology"].as_array().map_or(0,Vec::len),"ambiguities":memory["ambiguities"].as_array().map_or(0,Vec::len)},"issues":issues})
}
#[cfg(test)]
mod tests {
    use super::*;
    fn cue(start_ms: u64, end_ms: u64, text: &str) -> SubtitleCue {
        SubtitleCue {
            start_ms,
            end_ms,
            text: text.into(),
            secondary_text: None,
            confidence: None,
        }
    }
    #[test]
    fn python_fixture_differential() {
        let Ok(path) = std::env::var("VIDEOROLL_TEST_READABILITY_FIXTURE") else {
            return;
        };
        let cases: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        for case in cases.as_array().unwrap() {
            let input: Vec<SubtitleCue> = serde_json::from_value(case["input"].clone()).unwrap();
            let lang = case["lang"].as_str().unwrap();
            let p = profile(&input, lang);
            assert_eq!(json!(p), case["profile"], "profile {}", case["name"]);
            let (output, stats) =
                optimize(&input, case["bilingual"].as_bool().unwrap(), p).unwrap();
            let expected: Vec<SubtitleCue> =
                serde_json::from_value(case["output"].clone()).unwrap();
            assert_eq!(output.len(), expected.len(), "{}", case["name"]);
            for (a, b) in output.iter().zip(&expected) {
                assert_eq!(a.text, b.text, "{}", case["name"]);
                assert_eq!(a.secondary_text, b.secondary_text);
                assert!(
                    a.start_ms.abs_diff(b.start_ms) <= 1 && a.end_ms.abs_diff(b.end_ms) <= 1,
                    "{}",
                    case["name"]
                );
            }
            assert_eq!(
                json!(stats)["split_segments"],
                case["stats"]["split_segments"]
            );
            assert_eq!(
                json!(stats)["extended_segments"],
                case["stats"]["extended_segments"]
            );
            let quality = report(&input, &input, &output, p, &json!({}));
            assert_eq!(
                quality["score"], case["report"]["score"],
                "{}",
                case["name"]
            );
        }
        println!(
            "PASS {} real Python readability/quality differential fixtures",
            cases.as_array().unwrap().len()
        );
    }
    #[test]
    fn gap_and_alignment_are_bounded() {
        let source = vec![
            cue(0, 450, "very long caption that needs more time"),
            cue(550, 1500, "next"),
        ];
        let (out, _) = optimize(&source, false, profile(&source, "en")).unwrap();
        assert!(out[0].end_ms <= 500);
        assert_eq!(source[0].end_ms, 450);
        let mut b = cue(
            1000,
            4000,
            "这是一段相当长的中文字幕，需要在显示的时候进行平衡断行",
        );
        b.secondary_text = Some(
            "This is a fairly long source subtitle that should also be balanced for display."
                .into(),
        );
        let (out, _) = optimize(
            &[b.clone()],
            true,
            Profile {
                max_line_units: 12.,
                target_cps: 20.,
                min_display_seconds: 0.9,
                max_gap_extension_seconds: 0.75,
            },
        )
        .unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].start_ms, b.start_ms);
        assert_eq!(out[0].end_ms, b.end_ms);
        assert_eq!(out[0].text.lines().count(), 2);
        assert!(out[0].secondary_text.as_ref().unwrap().lines().count() <= 2);
    }
}
