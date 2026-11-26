//! Snap ASR time boundaries toward detected speech without changing text.
//! Uncertain or contradictory timing leaves the original cues intact.
use super::*;
const MIN_MS: u64 = 120;
const SHIFT: u64 = 450;
pub(super) fn parse_spans(value: &Value, offset: u64, duration: u64) -> Option<Vec<(u64, u64)>> {
    if value["available"].as_bool() != Some(true) {
        return None;
    }
    let rows = value["spans"].as_array()?;
    if rows.len() > 2000 {
        return None;
    }
    let mut spans = Vec::new();
    for row in rows {
        let start = row["start_ms"].as_u64()?;
        let end = row["end_ms"].as_u64()?;
        if start >= end || end > duration.saturating_add(50) {
            return None;
        }
        spans.push((
            offset.checked_add(start)?,
            offset.checked_add(end.min(duration))?,
        ));
    }
    spans.sort_unstable();
    Some(spans)
}
pub(super) fn refine(cues: &mut [SubtitleCue], spans: &[(u64, u64)], end_limit: u64) {
    if spans.is_empty() {
        return;
    }
    let original = cues.to_vec();
    for cue in cues.iter_mut() {
        let relevant = spans
            .iter()
            .filter(|(a, b)| *a < cue.end_ms && *b > cue.start_ms);
        let a = relevant.clone().map(|(a, _)| *a).min();
        let b = relevant.map(|(_, b)| *b).max();
        if let (Some(a), Some(b)) = (a, b) {
            let start = a.clamp(
                cue.start_ms.saturating_sub(SHIFT),
                cue.start_ms.saturating_add(SHIFT),
            );
            let end = b
                .clamp(
                    cue.end_ms.saturating_sub(SHIFT),
                    cue.end_ms.saturating_add(SHIFT),
                )
                .min(end_limit);
            if end.saturating_sub(start) >= MIN_MS {
                cue.start_ms = start;
                cue.end_ms = end;
            }
        }
    }
    for i in 1..cues.len() {
        // Chunk ASR can already overlap. Only resolve collisions caused
        // by refinement; semantic stitching still runs after all chunks.
        if original[i - 1].end_ms <= original[i].start_ms && cues[i - 1].end_ms > cues[i].start_ms {
            let low = cues[i - 1].start_ms.saturating_add(MIN_MS);
            let high = cues[i].end_ms.saturating_sub(MIN_MS);
            if low > high {
                cues.clone_from_slice(&original);
                return;
            }
            let boundary = (cues[i - 1].end_ms / 2 + cues[i].start_ms / 2).clamp(low, high);
            cues[i - 1].end_ms = boundary;
            cues[i].start_ms = boundary;
        }
    }
    if cues.iter().zip(&original).any(|(c, o)| {
        c.end_ms <= c.start_ms
            || c.start_ms.abs_diff(o.start_ms) > SHIFT
            || c.end_ms.abs_diff(o.end_ms) > SHIFT
    }) || cues
        .windows(2)
        .zip(original.windows(2))
        .any(|(c, o)| o[0].end_ms <= o[1].start_ms && c[0].end_ms > c[1].start_ms)
    {
        cues.clone_from_slice(&original);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn cue(a: u64, b: u64, text: &str) -> SubtitleCue {
        SubtitleCue {
            start_ms: a,
            end_ms: b,
            text: text.into(),
            secondary_text: Some("译文".into()),
        }
    }
    #[test]
    fn timing_only_moves_bounded_edges_and_keeps_words() {
        let mut cues = vec![cue(1000, 2000, "first"), cue(2100, 3000, "second")];
        refine(&mut cues, &[(700, 2300), (2300, 3200)], 4000);
        assert!(cues[0].end_ms <= cues[1].start_ms);
        assert_eq!(cues[0].text, "first");
        assert_eq!(cues[1].secondary_text.as_deref(), Some("译文"));
        assert!(cues[0].start_ms.abs_diff(1000) <= 450);
        assert!(parse_spans(&json!({"available":false,"spans":[]}), 0, 5000).is_none());
        assert_eq!(
            parse_spans(
                &json!({"available":true,"spans":[{"start_ms":100,"end_ms":900}]}),
                10000,
                1000
            ),
            Some(vec![(10100, 10900)])
        );
    }
}
