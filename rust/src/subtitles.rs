//! Typed subtitle timeline and safe SRT/ASS writers; ASR inference is a separate worker.
use crate::{AppError, AppResult};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubtitleCue {
    pub start_ms: u64,
    pub end_ms: u64,
    pub text: String,
    #[serde(default)]
    pub secondary_text: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReadabilityReport {
    pub total_cues: usize,
    pub max_chars_per_second: f64,
    pub average_chars_per_second: f64,
    pub long_cues: usize,
    pub overlapping_cues: usize,
}
fn parse_stamp(raw: &str) -> Option<u64> {
    let time = raw.trim().replace(',', ".");
    let (time, frac) = time.rsplit_once('.')?;
    if frac.len() != 3 {
        return None;
    }
    let millis = frac.parse::<u64>().ok()?;
    let parts: Vec<_> = time.split(':').collect();
    let (hours, minutes, seconds) = match parts.as_slice() {
        [minutes, seconds] => (
            0u64,
            minutes.parse::<u64>().ok()?,
            seconds.parse::<u64>().ok()?,
        ),
        [hours, minutes, seconds] => (
            hours.parse::<u64>().ok()?,
            minutes.parse::<u64>().ok()?,
            seconds.parse::<u64>().ok()?,
        ),
        _ => return None,
    };
    if minutes > 59 || seconds > 59 || millis > 999 {
        return None;
    }
    hours
        .checked_mul(3_600_000)?
        .checked_add(minutes * 60_000)?
        .checked_add(seconds * 1_000)?
        .checked_add(millis)
}
fn format_srt(ms: u64) -> String {
    let seconds = ms / 1000;
    let millis = ms % 1000;
    format!(
        "{:02}:{:02}:{:02},{:03}",
        seconds / 3600,
        (seconds / 60) % 60,
        seconds % 60,
        millis
    )
}
fn format_ass(ms: u64) -> String {
    let centis = ms / 10;
    let sec = centis / 100;
    format!(
        "{}:{:02}:{:02}.{:02}",
        sec / 3600,
        (sec / 60) % 60,
        sec % 60,
        centis % 100
    )
}
pub fn parse_srt(data: &str) -> AppResult<Vec<SubtitleCue>> {
    let input = data
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    let mut cues = Vec::new();
    for (block_index, block) in input.split("\n\n").enumerate() {
        let rows: Vec<_> = block
            .lines()
            .map(str::trim_end)
            .filter(|l| !l.trim().is_empty())
            .collect();
        if rows.is_empty() {
            continue;
        }
        if rows[0].starts_with("WEBVTT") {
            continue;
        }
        let timing_index = rows.iter().position(|r| r.contains("-->"));
        let Some(timing_index) = timing_index else {
            continue;
        };
        let (start, end) = rows[timing_index]
            .split_once("-->")
            .ok_or_else(|| AppError::Invalid("invalid cue time separator".into()))?;
        let start = parse_stamp(start).ok_or_else(|| {
            AppError::Invalid(format!("invalid cue start in block {block_index}"))
        })?;
        let end = end
            .split_whitespace()
            .next()
            .and_then(parse_stamp)
            .ok_or_else(|| AppError::Invalid(format!("invalid cue end in block {block_index}")))?;
        if start >= end {
            return Err(AppError::Invalid(format!(
                "nonpositive cue duration in block {block_index}"
            )));
        }
        let text = rows.get(timing_index + 1..).unwrap_or_default().join("\n");
        if text.trim().is_empty() {
            continue;
        }
        cues.push(SubtitleCue {
            start_ms: start,
            end_ms: end,
            text,
            secondary_text: None,
        });
    }
    Ok(cues)
}
pub fn to_srt(cues: &[SubtitleCue]) -> AppResult<String> {
    validate(cues)?;
    let mut out = String::new();
    for (i, cue) in cues.iter().enumerate() {
        out.push_str(&format!(
            "{}\n{} --> {}\n{}",
            i + 1,
            format_srt(cue.start_ms),
            format_srt(cue.end_ms),
            cue.text.trim()
        ));
        if let Some(secondary) = cue
            .secondary_text
            .as_deref()
            .filter(|v| !v.trim().is_empty())
        {
            out.push('\n');
            out.push_str(secondary.trim());
        }
        out.push_str("\n\n");
    }
    Ok(out)
}
pub fn validate(cues: &[SubtitleCue]) -> AppResult<()> {
    if cues.len() > 250_000 {
        return Err(AppError::Invalid("too many subtitle cues".into()));
    }
    for (index, cue) in cues.iter().enumerate() {
        if cue.start_ms >= cue.end_ms {
            return Err(AppError::Invalid(format!("invalid timing for cue {index}")));
        }
        if cue.text.len() > 20_000 {
            return Err(AppError::Invalid(format!("cue {index} too long")));
        }
    }
    Ok(())
}
pub fn readability(cues: &[SubtitleCue]) -> ReadabilityReport {
    let mut total = 0f64;
    let mut max = 0f64;
    let mut long = 0usize;
    let mut overlaps = 0usize;
    for (i, cue) in cues.iter().enumerate() {
        let seconds = (cue.end_ms.saturating_sub(cue.start_ms) as f64 / 1000.0).max(0.1);
        let cps = cue.text.chars().filter(|c| !c.is_whitespace()).count() as f64 / seconds;
        total += cps;
        max = max.max(cps);
        if cps > 20.0 {
            long += 1;
        }
        if i > 0 && cue.start_ms < cues[i - 1].end_ms {
            overlaps += 1;
        }
    }
    ReadabilityReport {
        total_cues: cues.len(),
        max_chars_per_second: max,
        average_chars_per_second: if cues.is_empty() {
            0.0
        } else {
            total / cues.len() as f64
        },
        long_cues: long,
        overlapping_cues: overlaps,
    }
}
fn escape_ass_text(input: &str) -> String {
    // Override blocks must never be evaluated from untrusted AI model text.
    input
        .replace('\\', "／")
        .replace('{', "（")
        .replace('}', "）")
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\n', r"\N")
}
pub fn to_ass(
    cues: &[SubtitleCue],
    width: u32,
    height: u32,
    font: &str,
    primary_size: u32,
    bilingual: bool,
) -> AppResult<String> {
    to_ass_scaled(cues,width,height,font,primary_size,bilingual,100,100)
}
/// Independently scale the two unscaled base sizes.
pub fn to_ass_scaled(
    cues:&[SubtitleCue],width:u32,height:u32,font:&str,base_size:u32,
    bilingual:bool,primary_percent:u32,secondary_percent:u32,
)->AppResult<String>{
    validate(cues)?;
    if !(160..=16384).contains(&width) || !(120..=16384).contains(&height) {
        return Err(AppError::Invalid("invalid ASS canvas dimensions".into()));
    }
    if !(12..=120).contains(&base_size) || !(25..=300).contains(&primary_percent) || !(25..=300).contains(&secondary_percent) {
        return Err(AppError::Invalid("invalid subtitle font size or scale".into()));
    }
    let primary_size=((base_size*primary_percent+50)/100).max(12);
    let secondary_base=(base_size as f64*0.68).round() as u32;
    let secondary_size=((secondary_base*secondary_percent+50)/100).max(10);
    let sanitized_font: String = font
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == ' ' || *c == '-' || *c == '_')
        .take(80)
        .collect();
    let font = if sanitized_font.is_empty() {
        "Arial"
    } else {
        sanitized_font.as_str()
    };
    let mut result = format!(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
    );
    result.push_str(&format!(
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,{font},{primary_size},&H00FFFFFF,&H00FFFFFF,&H00111111,&H99000000,0,0,0,0,100,100,0,0,1,2,0,2,25,25,32,1\n\n"
    ));
    result.push_str("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n");
    for cue in cues {
        let mut text = escape_ass_text(&cue.text);
        if bilingual
            && let Some(secondary) = cue
                .secondary_text
                .as_deref()
                .filter(|v| !v.trim().is_empty())
        {
            text.push_str(&format!(
                r"\N{{\fs{secondary_size}}}{}{{\fs{primary_size}}}",
                escape_ass_text(secondary)
            ));
        }
        result.push_str(&format!(
            "Dialogue: 0,{},{},Default,,0,0,0,,{}\n",
            format_ass(cue.start_ms),
            format_ass(cue.end_ms),
            text,
        ));
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn srt_roundtrip_and_chinese_line() {
        let s = "1\r\n00:00:01,200 --> 00:00:02,500\r\n你好 Rust\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\n测试\r\n";
        let cues = parse_srt(s).unwrap();
        assert_eq!(cues.len(), 2);
        assert_eq!(cues[0].start_ms, 1200);
        assert_eq!(parse_srt(&to_srt(&cues).unwrap()).unwrap(), cues);
    }
    #[test]
    fn ass_escapes_model_provided_override_tags() {
        let cues = vec![SubtitleCue {
            start_ms: 100,
            end_ms: 3000,
            text: "你好{\\pos(0,0)}".into(),
            secondary_text: Some("world".into()),
        }];
        let ass = to_ass(&cues, 1920, 1080, "Arial", 48, true).unwrap();
        assert!(ass.contains("Dialogue: 0,0:00:00.10,0:00:03.00"));
        assert!(!ass.contains(r"{\pos(0,0)}"));
        assert!(ass.contains("world"));
    }
    #[test]
    fn independent_font_scales(){
        let cues=[SubtitleCue{start_ms:0,end_ms:1000,text:"中文".into(),secondary_text:Some("English".into())}];
        let ass=to_ass_scaled(&cues,1920,1080,"Arial",48,true,150,50).unwrap();
        assert!(ass.contains("Style: Default,Arial,72,"));
        assert!(ass.contains(r"\N{\fs17}English{\fs72}"));
        let other=to_ass_scaled(&cues,1920,1080,"Arial",48,true,50,50).unwrap();
        assert!(other.contains(r"\N{\fs17}English{\fs24}"));
        assert!(to_ass_scaled(&cues,1920,1080,"Arial",48,true,24,100).is_err());
        assert!(to_ass_scaled(&cues,1920,1080,"Arial",48,true,100,301).is_err());
        assert_eq!(to_ass(&cues,1920,1080,"Arial",48,true).unwrap(),
            to_ass_scaled(&cues,1920,1080,"Arial",48,true,100,100).unwrap());
    }
    #[test]
    fn malformed_timestamp_is_not_silently_clamped() {
        assert!(parse_srt("1\n00:99:00,000 --> 00:00:05,000\ntext").is_err());
        let bad = SubtitleCue {
            start_ms: 1000,
            end_ms: 1000,
            text: "x".into(),
            secondary_text: None,
        };
        assert!(to_srt(&[bad]).is_err());
    }
}
