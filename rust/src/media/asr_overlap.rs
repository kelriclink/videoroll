//! Preserve the original Python overlap behavior for chunked ASR, not for
//! imported captions. Pure text/timeline logic; no provider or database calls.
use crate::subtitles::SubtitleCue;
use unicode_normalization::UnicodeNormalization;

fn cjk(c: char) -> bool {
    matches!(c, '\u{3040}'..='\u{30ff}' | '\u{3400}'..='\u{9fff}' | '\u{f900}'..='\u{faff}')
}
fn tokens(text: &str) -> Vec<(String, usize)> {
    let mut result = Vec::new();
    let mut chars = text.char_indices().peekable();
    while let Some((start, c)) = chars.next() {
        let mut end = start + c.len_utf8();
        if !cjk(c) && c.is_alphanumeric() {
            let mut apostrophe = false;
            while let Some(&(i, next)) = chars.peek() {
                if !cjk(next) && next.is_alphanumeric() {
                    chars.next();
                    end = i + next.len_utf8();
                } else if !apostrophe && matches!(next, '\'' | '’') {
                    let mut look = chars.clone();
                    look.next();
                    if look
                        .peek()
                        .is_some_and(|(_, c)| c.is_alphanumeric() && !cjk(*c))
                    {
                        apostrophe = true;
                        chars.next();
                        end = i + next.len_utf8();
                    } else {
                        break;
                    }
                } else {
                    break;
                }
            }
        } else if !cjk(c) {
            continue;
        }
        let value: String = text[start..end]
            .nfkc()
            .flat_map(char::to_lowercase)
            .collect();
        result.push((value, end));
    }
    result
}
fn join(left: &str, right: &str) -> String {
    let (left, right) = (left.trim_end(), right.trim_start());
    let (Some(a), Some(b)) = (left.chars().next_back(), right.chars().next()) else {
        return format!("{left}{right}");
    };
    if ",.!?;:，。！？；：、)]}）】》」』”’".contains(b) || cjk(a) || cjk(b) {
        format!("{left}{right}")
    } else {
        format!("{left} {right}")
    }
}
fn meaningful(items: &[&str]) -> bool {
    items.len() >= 3
        || (items.len() >= 2 && items.iter().map(|s| s.chars().count()).sum::<usize>() >= 8)
}
fn contains(haystack: &[&str], needle: &[&str]) -> bool {
    !needle.is_empty()
        && needle.len() <= haystack.len()
        && haystack.windows(needle.len()).any(|w| w == needle)
}
fn prefer(a: &str, b: &str, al: usize, bl: usize) -> String {
    if al > bl || (al == bl && a.trim().chars().count() >= b.trim().chars().count()) {
        a.into()
    } else {
        b.into()
    }
}
// Longest contiguous matching blocks, using the same earliest-first tie
// rule as SequenceMatcher. Bound expensive fuzzy matching on hostile text.
fn matching_blocks(a: &[&str], b: &[&str]) -> Vec<(usize, usize, usize)> {
    if a.len() > 512 || b.len() > 512 {
        return Vec::new();
    }
    let mut pending = vec![(0, a.len(), 0, b.len())];
    let mut blocks = Vec::new();
    while let Some((al, ah, bl, bh)) = pending.pop() {
        let mut best = (0, 0, 0);
        let mut previous = vec![0; bh - bl + 1];
        for (i, x) in a.iter().enumerate().take(ah).skip(al) {
            let mut next = vec![0; bh - bl + 1];
            for (j, y) in b.iter().enumerate().take(bh).skip(bl) {
                if x == y {
                    let size = previous[j - bl] + 1;
                    next[j - bl + 1] = size;
                    if size > best.2 {
                        best = (i + 1 - size, j + 1 - size, size);
                    }
                }
            }
            previous = next;
        }
        let (i, j, n) = best;
        if n > 0 {
            blocks.push(best);
            if al < i && bl < j {
                pending.push((al, i, bl, j));
            }
            if i + n < ah && j + n < bh {
                pending.push((i + n, ah, j + n, bh));
            }
        }
    }
    blocks.sort_unstable();
    let mut merged: Vec<(usize, usize, usize)> = Vec::new();
    for (i, j, n) in blocks {
        if let Some(last) = merged.last_mut() {
            if last.0 + last.2 == i && last.1 + last.2 == j {
                last.2 += n;
                continue;
            }
        }
        merged.push((i, j, n));
    }
    merged
}
fn stitch(previous: &str, current: &str) -> Option<String> {
    let at = tokens(previous);
    let bt = tokens(current);
    let a = at.iter().map(|(s, _)| s.as_str()).collect::<Vec<_>>();
    let b = bt.iter().map(|(s, _)| s.as_str()).collect::<Vec<_>>();
    if a.is_empty() || b.is_empty() {
        return None;
    }
    if a == b {
        return Some(prefer(previous, current, a.len(), b.len()));
    }
    if contains(&b, &a) {
        return Some(current.into());
    }
    if contains(&a, &b) {
        return Some(previous.into());
    }
    let suffix = |n: usize| {
        if n >= bt.len() {
            ""
        } else if n == 0 {
            current
        } else {
            &current[bt[n - 1].1..]
        }
    };
    for n in (1..=a.len().min(b.len())).rev() {
        if a[a.len() - n..] == b[..n] && meaningful(&a[a.len() - n..]) {
            return Some(join(previous, suffix(n)));
        }
    }
    let blocks = matching_blocks(&a, &b);
    if let Some(&(i, j, n)) = blocks
        .iter()
        .filter(|&&(i, j, n)| a.len() - (i + n) <= 1 && j <= 1 && meaningful(&a[i..i + n]))
        .max_by_key(|&&(_, _, n)| n)
    {
        return Some(if n as f64 / a.len().min(b.len()) as f64 >= 0.65 {
            prefer(previous, current, a.len(), b.len())
        } else {
            let _ = i;
            join(previous, suffix(j + n))
        });
    }
    let matched: usize = blocks.iter().map(|b| b.2).sum();
    if 2.0 * matched as f64 / (a.len() + b.len()) as f64 >= 0.78 && meaningful(&a[..a.len().min(3)])
    {
        return Some(if b.len() > a.len() {
            current.into()
        } else {
            previous.into()
        });
    }
    None
}

pub(crate) fn reconcile(mut cues: Vec<SubtitleCue>) -> Vec<SubtitleCue> {
    cues.sort_by(|a, b| (a.start_ms, a.end_ms, &a.text).cmp(&(b.start_ms, b.end_ms, &b.text)));
    let mut out: Vec<SubtitleCue> = Vec::new();
    for mut cue in cues {
        cue.text = cue.text.split_whitespace().collect::<Vec<_>>().join(" ");
        if cue.text.is_empty() || cue.end_ms <= cue.start_ms {
            continue;
        }
        let Some(prev) = out.last_mut() else {
            out.push(cue);
            continue;
        };
        if cue.start_ms >= prev.end_ms {
            out.push(cue);
            continue;
        }
        let overlap = prev.end_ms.min(cue.end_ms).saturating_sub(cue.start_ms);
        let shorter = (prev.end_ms - prev.start_ms).min(cue.end_ms - cue.start_ms);
        let substantial = overlap >= 240.max(500.min(shorter / 2))
            || overlap.saturating_mul(5) >= shorter.saturating_mul(4);
        if substantial {
            prev.text =
                stitch(&prev.text, &cue.text).unwrap_or_else(|| join(&prev.text, &cue.text));
            prev.end_ms = prev.end_ms.max(cue.end_ms);
        } else if cue.start_ms.saturating_sub(prev.start_ms) >= 120 {
            prev.end_ms = cue.start_ms;
            out.push(cue);
        } else if cue.end_ms.saturating_sub(prev.end_ms) >= 120 {
            cue.start_ms = prev.end_ms;
            out.push(cue);
        } else {
            prev.text = join(&prev.text, &cue.text);
            prev.end_ms = prev.end_ms.max(cue.end_ms);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cue(a: u64, b: u64, text: &str) -> SubtitleCue {
        SubtitleCue {
            start_ms: a,
            end_ms: b,
            text: text.into(),
            secondary_text: None,
        }
    }
    fn assert_one(rows: Vec<SubtitleCue>, a: u64, b: u64, text: &str) {
        let got = reconcile(rows);
        assert_eq!(got.len(), 1, "{got:?}");
        assert_eq!(
            (got[0].start_ms, got[0].end_ms, got[0].text.as_str()),
            (a, b, text)
        );
    }
    #[test]
    fn python_overlap_stitches_continuation_and_fuzzy_duplicate() {
        assert_one(
            vec![
                cue(39000, 44000, "The best way of making it square"),
                cue(
                    40500,
                    46000,
                    "making it square is to calculate the diagonal length",
                ),
            ],
            39000,
            46000,
            "The best way of making it square is to calculate the diagonal length",
        );
        assert_one(
            vec![
                cue(40000, 44000, "then we install the panel"),
                cue(40200, 45300, "and then we install the panels here"),
            ],
            40000,
            45300,
            "and then we install the panels here",
        );
        assert_one(
            vec![
                cue(39000, 41000, "the best way"),
                cue(41000, 44000, "is to calculate the diagonal"),
                cue(
                    40200,
                    45500,
                    "the best way is to calculate the diagonal length",
                ),
            ],
            39000,
            45500,
            "the best way is to calculate the diagonal length",
        );
    }
    #[test]
    fn python_overlap_preserves_real_repetitions_and_distinct_text() {
        for (a, b) in [
            ("Turn the valve on", "Turn the valve off"),
            ("The answer is yes", "The answer is no"),
            ("yes", "yes"),
        ] {
            let out = reconcile(vec![cue(10000, 12000, a), cue(11990, 14000, b)]);
            assert_eq!(out.len(), 2);
            assert_eq!(out[0].end_ms, 11990);
            assert_eq!(out[1].text, b);
        }
        assert_eq!(
            reconcile(vec![cue(10000, 12000, "yes"), cue(12000, 14000, "yes")]).len(),
            2
        );
        for d in [80, 200] {
            assert_one(
                vec![cue(40000, 40000 + d, "yes"), cue(40000, 40000 + d, "yes")],
                40000,
                40000 + d,
                "yes",
            );
        }
        assert_one(
            vec![
                cue(10000, 15000, "main speaker continues talking"),
                cue(11000, 12000, "short audience response"),
            ],
            10000,
            15000,
            "main speaker continues talking short audience response",
        );
        assert_one(
            vec![
                cue(0, 2000, "今天学习电路"),
                cue(1000, 3000, "学习电路原理"),
            ],
            0,
            3000,
            "今天学习电路原理",
        );
    }
}
