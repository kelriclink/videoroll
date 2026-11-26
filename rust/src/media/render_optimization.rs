//! Conservative, pure planning helpers for the Python-compatible ASS/VAAPI path.
use std::collections::{BTreeSet, HashMap};

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct EventCachePlan {
    pub boundaries_us: Vec<u64>,
    pub output_frame_count: u64,
    pub savings_ratio: f64,
}
pub(crate) fn parse_rational(value: &str) -> Option<f64> {
    let (n, d) = value.trim().split_once('/')?;
    let n = n.parse::<f64>().ok()?;
    let d = d.parse::<f64>().ok()?;
    (n.is_finite() && d.is_finite() && n > 0.0 && d > 0.0).then_some(n / d)
}
pub(crate) fn reduced_overlay_rate(rate: &str, cap_fps: f64) -> String {
    match parse_rational(rate) {
        Some(value) if value > cap_fps && cap_fps.is_finite() && cap_fps > 0.0 => {
            format!("{}/1", cap_fps.floor() as u64)
        }
        Some(_) => rate.trim().to_owned(),
        None => "15/1".into(),
    }
}
pub(crate) fn pixel_format_bit_depth(pixel_format: &str, raw_depth: Option<u32>) -> u32 {
    if let Some(depth) = raw_depth.filter(|depth| (8..=16).contains(depth)) {
        return depth;
    }
    let value = pixel_format.trim().to_ascii_lowercase();
    if value.starts_with("p010") || value.contains("p010") || value.contains("p10le") {
        return 10;
    }
    if value.starts_with("p012") || value.contains("p012") || value.contains("p12le") {
        return 12;
    }
    if value.starts_with("p016") || value.contains("p016") || value.contains("p16le") {
        return 16;
    }
    for depth in [16, 14, 12, 10, 9] {
        if value.contains(&format!("p{depth}")) {
            return depth;
        }
    }
    if ["rgb48", "bgr48", "rgba64", "bgra64"]
        .iter()
        .any(|prefix| value.starts_with(prefix))
    {
        return 16;
    }
    8
}
fn timestamp_us(value: &str) -> Option<u64> {
    let mut parts = value.trim().split(':');
    let h = parts.next()?.parse::<u64>().ok()?;
    let m = parts.next()?.parse::<u64>().ok()?;
    let s = parts.next()?.parse::<f64>().ok()?;
    if parts.next().is_some() || m >= 60 || !s.is_finite() || !(0.0..60.0).contains(&s) {
        return None;
    }
    let total = h as f64 * 3600.0 + m as f64 * 60.0 + s;
    (total.is_finite() && total >= 0.0 && total <= u64::MAX as f64 / 1_000_000.0)
        .then_some((total * 1_000_000.0).round() as u64)
}
fn dialogue_fields(line: &str) -> Option<Vec<&str>> {
    let fields = line
        .strip_prefix("Dialogue:")?
        .trim_start()
        .splitn(10, ',')
        .collect::<Vec<_>>();
    (fields.len() == 10).then_some(fields)
}
fn has_ass_function(text: &str, tag: &str) -> bool {
    let mut offset = 0;
    while let Some(found) = text[offset..].find(tag) {
        let mut index = offset + found + tag.len();
        while text
            .as_bytes()
            .get(index)
            .is_some_and(u8::is_ascii_whitespace)
        {
            index += 1;
        }
        if text.as_bytes().get(index) == Some(&b'(') {
            return true;
        }
        offset = index;
        if offset >= text.len() {
            break;
        }
    }
    false
}
pub(crate) fn ass_is_static(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    if [r"\t", r"\move", r"\fad", r"\fade"]
        .iter()
        .any(|tag| has_ass_function(&lower, tag))
    {
        return false;
    }
    for tag in [r"\k", r"\K", r"\kf", r"\ko", r"\kt"] {
        let mut offset = 0;
        while let Some(found) = text[offset..].find(tag) {
            let mut index = offset + found + tag.len();
            while text
                .as_bytes()
                .get(index)
                .is_some_and(u8::is_ascii_whitespace)
            {
                index += 1;
            }
            if text.as_bytes().get(index).is_some_and(u8::is_ascii_digit) {
                return false;
            }
            offset = index;
            if offset >= text.len() {
                break;
            }
        }
    }
    !text
        .lines()
        .filter(|line| line.starts_with("Dialogue:"))
        .any(|line| dialogue_fields(line).is_some_and(|fields| !fields[8].trim().is_empty()))
}

pub(crate) fn plan_event_cache(
    text: &str,
    rate: &str,
    duration_seconds: f64,
) -> (Option<EventCachePlan>, String) {
    if !ass_is_static(text) {
        return (None, "complex-or-animated-ass".into());
    }
    let Some(fps) = parse_rational(rate) else {
        return (None, "invalid-overlay-frame-rate".into());
    };
    if !duration_seconds.is_finite() || duration_seconds <= 0.0 {
        return (None, "zero-duration-events".into());
    }
    let duration_us = (duration_seconds * 1_000_000.0).round() as u64;
    let mut boundaries = BTreeSet::from([0_u64]);
    for line in text.lines().filter(|line| line.starts_with("Dialogue:")) {
        let Some(fields) = dialogue_fields(line) else {
            continue;
        };
        let (Some(start), Some(end)) = (timestamp_us(fields[1]), timestamp_us(fields[2])) else {
            continue;
        };
        if end <= start {
            continue;
        }
        if start < duration_us.saturating_sub(1) {
            boundaries.insert(start);
        }
        if end < duration_us.saturating_sub(1) {
            boundaries.insert(end);
        }
    }
    let boundaries = boundaries.into_iter().collect::<Vec<_>>();
    if boundaries.len() < 2 {
        return (None, "insufficient-event-boundaries".into());
    }
    if boundaries.len() > 4096 {
        return (None, format!("too-many-states:{}>4096", boundaries.len()));
    }
    let frames = (duration_seconds * fps).ceil() as u64 + 1;
    if frames < 90 {
        return (None, format!("too-short:{frames}<90"));
    }
    let savings_ratio = boundaries.len() as f64 / frames.max(1) as f64;
    if savings_ratio > 0.20 {
        return (None, format!("high-churn:{savings_ratio:.3}>0.200"));
    }
    (
        Some(EventCachePlan {
            boundaries_us: boundaries,
            output_frame_count: frames,
            savings_ratio,
        }),
        "eligible".into(),
    )
}
/// Returns a resized ASS script and bottom-aligned overlay height. Uncertain geometry uses full-frame.
pub(crate) fn prepare_overlay_band(text: &str, width: u32, height: u32) -> Option<(String, u32)> {
    if width == 0 || height == 0 {
        return None;
    }
    let lower = text.to_ascii_lowercase();
    if [r"\pos", r"\move", r"\org", r"\clip", r"\iclip"]
        .iter()
        .any(|tag| has_ass_function(&lower, tag))
        || [
            r"\an1", r"\an2", r"\an3", r"\an4", r"\an5", r"\an6", r"\an7", r"\an8", r"\an9",
            r"\p1", r"\p2", r"\p3", r"\p4", r"\p5", r"\p6", r"\p7", r"\p8", r"\p9",
        ]
        .iter()
        .any(|tag| lower.contains(tag))
    {
        return None;
    }
    let res_x = text.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        key.trim()
            .eq_ignore_ascii_case("PlayResX")
            .then(|| value.trim().parse::<u32>().ok())
            .flatten()
    })?;
    if res_x != width
        || !text.lines().any(|line| {
            line.split_once(':').is_some_and(|(key, value)| {
                key.trim().eq_ignore_ascii_case("PlayResY")
                    && value.trim().parse::<u32>().ok() == Some(height)
            })
        })
    {
        return None;
    }
    let mut styles: HashMap<String, (f64, f64, u32)> = HashMap::new();
    for line in text.lines().filter(|line| line.starts_with("Style: ")) {
        let fields = line[7..].split(',').map(str::trim).collect::<Vec<_>>();
        if fields.len() < 23 {
            continue;
        }
        let (Ok(font), Ok(outline), Ok(alignment), Ok(margin)) = (
            fields[2].parse::<f64>(),
            fields[16].parse::<f64>(),
            fields[18].parse::<u32>(),
            fields[21].parse::<u32>(),
        ) else {
            continue;
        };
        if !font.is_finite() || !outline.is_finite() || !(1..=3).contains(&alignment) {
            return None;
        }
        styles.insert(
            fields[0].to_owned(),
            (font.max(1.0), outline.max(0.0), margin),
        );
    }
    if styles.is_empty() {
        return None;
    }
    let default_style = if styles.contains_key("Default") {
        "Default".to_owned()
    } else {
        return None;
    };
    let mut required_height = 0.0_f64;
    for line in text.lines().filter(|line| line.starts_with("Dialogue:")) {
        let fields = line.splitn(10, ',').collect::<Vec<_>>();
        if fields.len() < 10 {
            continue;
        }
        let event_style = fields[3].trim();
        let mut current_style = if styles.contains_key(event_style) {
            event_style.to_owned()
        } else {
            default_style.clone()
        };
        let mut line_height_sum = 0.0_f64;
        let mut margin_v = styles
            .get(&current_style)?
            .2
            .max(fields[7].trim().parse::<u32>().ok()?);
        for logical_line in fields[9].split(r"\N") {
            let mut search = logical_line;
            let mut requested = None;
            while let Some(index) = search.find(r"{\r") {
                let after = &search[index + 3..];
                let Some(close) = after.find('}') else {
                    break;
                };
                let style_name = after[..close].split('\\').next().unwrap_or("").trim();
                if !style_name.is_empty() {
                    requested = Some(style_name.to_owned());
                }
                search = &after[close + 1..];
            }
            if let Some(name) = requested.filter(|name| styles.contains_key(name)) {
                current_style = name;
            }
            let (font, outline, style_margin) = *styles.get(&current_style)?;
            margin_v = margin_v.max(style_margin);
            line_height_sum += font * 1.22 + outline * 2.0;
        }
        required_height = required_height.max(line_height_sum + margin_v as f64 + 32.0);
    }
    let min_band = 96_u32.max(((height as f64 * 0.12).round() as u32).min(192));
    let mut band = min_band.max((required_height + 1.0) as u32).min(height);
    if band % 2 == 1 {
        band = band.saturating_add(1).min(height);
    }
    if band as f64 >= height as f64 * 0.85 {
        return None;
    }
    let mut replaced = false;
    let script = text
        .lines()
        .map(|line| {
            if !replaced
                && line
                    .split_once(':')
                    .is_some_and(|(key, _)| key.trim().eq_ignore_ascii_case("PlayResY"))
            {
                replaced = true;
                format!("PlayResY: {band}")
            } else {
                line.to_owned()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    Some((script, band))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn ass(events: &str) -> String {
        format!(
            "[Script Info]\nPlayResX: 3840\nPlayResY: 2160\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,54,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,40,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n{events}\n"
        )
    }
    #[test]
    fn rate_and_bit_depth() {
        assert_eq!(reduced_overlay_rate("60/1", 15.0), "15/1");
        assert_eq!(reduced_overlay_rate("12/1", 15.0), "12/1");
        assert_eq!(pixel_format_bit_depth("yuv420p10le", None), 10);
        assert_eq!(pixel_format_bit_depth("p010le", None), 10);
        assert_eq!(pixel_format_bit_depth("yuv420p", None), 8);
    }
    #[test]
    fn event_plan_clips_eof_and_rejects_animation() {
        let text = ass(
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,hello\nDialogue: 0,0:00:03.00,0:00:05.00,Default,,0,0,0,,world\nDialogue: 0,0:00:10.00,0:00:11.00,Default,,0,0,0,,eof",
        );
        let (plan, reason) = plan_event_cache(&text, "15/1", 10.0);
        assert_eq!(reason, "eligible");
        assert!(!plan.unwrap().boundaries_us.contains(&10_000_000));
        let animated =
            ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\fad(100,100)}hello");
        assert_eq!(
            plan_event_cache(&animated, "15/1", 120.0).1,
            "complex-or-animated-ass"
        );
    }
    #[test]
    fn event_cache_respects_short_clip_churn_and_state_caps() {
        let short = ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,hello");
        assert!(plan_event_cache(&short, "15/1", 5.0).0.is_none());
        let mut churn = String::new();
        for i in 0..100 {
            let start = i * 10;
            let end = start + 5;
            churn.push_str(&format!(
                "Dialogue: 0,0:00:{:02}.{:02},0:00:{:02}.{:02},Default,,0,0,0,,x\n",
                start / 100,
                start % 100,
                end / 100,
                end % 100
            ));
        }
        assert!(
            plan_event_cache(&ass(&churn), "15/1", 20.0)
                .1
                .starts_with("high-churn:")
        );
        let mut many = String::new();
        for i in 0..2049 {
            let start = i * 10;
            let end = start + 5;
            many.push_str(&format!(
                "Dialogue: 0,0:{:02}:{:02}.{:02},0:{:02}:{:02}.{:02},Default,,0,0,0,,x\n",
                (start / 100) / 60,
                (start / 100) % 60,
                start % 100,
                (end / 100) / 60,
                (end / 100) % 60,
                end % 100
            ));
        }
        assert!(
            plan_event_cache(&ass(&many), "15/1", 300.0)
                .1
                .starts_with("too-many-states:")
        );
    }
    #[test]
    fn animation_detection_handles_whitespace_and_karaoke_tags() {
        let transform =
            ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\t (0,500,\\fs40)}hello");
        assert!(!ass_is_static(&transform));
        let karaoke = ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\kf 20}hello");
        assert!(!ass_is_static(&karaoke));
        let aligned = ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an2}hello");
        assert!(prepare_overlay_band(&aligned, 3840, 2160).is_none());
    }
    #[test]
    fn overlay_band_rejects_positioned_ass() {
        let text = ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,hello");
        let (script, band) = prepare_overlay_band(&text, 3840, 2160).unwrap();
        assert!(band < 2160 && script.contains(&format!("PlayResY: {band}")));
        let positioned =
            ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\pos(10,10)}hello");
        assert!(prepare_overlay_band(&positioned, 3840, 2160).is_none());
        let spaced = ass("Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\pos (10,10)}hello");
        assert!(prepare_overlay_band(&spaced, 3840, 2160).is_none());
    }
}
