//! V1 multipart knowledge import: CSV, JSON and bounded SRT chunks.
//! Reuse the original translation_knowledge_items upsert/embedding path.
//! The import is intentionally per-item, like the Python endpoint; no partial
//! failure is reported as an all-or-nothing success.
use crate::{AppError, AppResult, api::AppState};
use axum::{
    Json,
    extract::{DefaultBodyLimit, Multipart, State},
};
use serde_json::{Map, Value, json};
use std::path::Path;
use uuid::Uuid;

const MAX_FILE_BYTES: usize = 5 * 1024 * 1024;
const MAX_ROWS: usize = 2_000;

fn invalid(message: &str) -> AppError {
    AppError::Invalid(message.to_owned())
}

fn resolve_format(filename: &str, format: &str) -> AppResult<String> {
    let format = format.trim().to_ascii_lowercase();
    let format = if format.is_empty() || format == "auto" {
        Path::new(filename)
            .extension()
            .and_then(|v| v.to_str())
            .unwrap_or("")
            .to_ascii_lowercase()
    } else {
        format
    };
    if !matches!(format.as_str(), "json" | "csv" | "srt") {
        return Err(invalid("knowledge import format must be csv, json, or srt"));
    }
    Ok(format)
}

fn srt_entries(text: &str, filename: &str, lang: &str, domain: &str) -> Vec<Value> {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut result = Vec::new();
    let mut chunk = Vec::<String>::new();
    let mut length = 0;
    let mut part = 1;
    let basename = filename
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or("subtitle.srt");
    let basename = if basename.is_empty() {
        "subtitle.srt"
    } else {
        basename
    };
    for block in normalized.split("\n\n") {
        let mut lines = block
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .collect::<Vec<_>>();
        if lines
            .first()
            .is_some_and(|line| line.chars().all(|c| c.is_ascii_digit()))
        {
            lines.remove(0);
        }
        if lines.first().is_some_and(|line| line.contains("-->")) {
            lines.remove(0);
        }
        let cue = lines.join(" ");
        if cue.is_empty() {
            continue;
        }
        if !chunk.is_empty() && (chunk.len() >= 50 || length + cue.len() + 1 > 4000) {
            result.push(json!({"item_type":"document", "target_lang":lang,
                "domain":domain,"title":format!("{basename} · part {part}"),
                "content":chunk.join("\n"),"created_by":"bulk:srt"}));
            part += 1;
            chunk.clear();
            length = 0;
        }
        length += cue.len() + 1;
        chunk.push(cue);
    }
    if !chunk.is_empty() {
        result.push(json!({"item_type":"document", "target_lang":lang,
            "domain":domain,"title":format!("{basename} · part {part}"),
            "content":chunk.join("\n"),"created_by":"bulk:srt"}));
    }
    result
}

fn parse(
    filename: &str,
    raw: &[u8],
    format: &str,
    lang: &str,
    domain: &str,
) -> AppResult<(String, Vec<Value>)> {
    if raw.len() > MAX_FILE_BYTES {
        return Err(invalid("knowledge import file exceeds 5 MiB"));
    }
    let format = resolve_format(filename, format)?;
    let raw = raw.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(raw);
    let text =
        std::str::from_utf8(raw).map_err(|_| invalid("knowledge import file must be UTF-8"))?;
    let rows = match format.as_str() {
        "json" => {
            let payload: Value =
                serde_json::from_str(text).map_err(|_| invalid("invalid knowledge import JSON"))?;
            let entries = if let Some(items) = payload.get("items") {
                items
            } else {
                &payload
            };
            let entries = entries.as_array().ok_or_else(|| {
                invalid("JSON import must be an array or object with items array")
            })?;
            entries
                .iter()
                .filter(|row| row.is_object())
                .cloned()
                .collect::<Vec<_>>()
        }
        "csv" => {
            let mut reader = csv::ReaderBuilder::new().from_reader(text.as_bytes());
            let headers = reader
                .headers()
                .map_err(|_| invalid("invalid CSV header"))?
                .clone();
            if headers.is_empty() {
                return Err(invalid("CSV import has no header"));
            }
            let mut rows = Vec::new();
            for record in reader.records() {
                let record = record.map_err(|_| invalid("invalid CSV row"))?;
                let mut item = Map::new();
                for (header, value) in headers.iter().zip(record.iter()) {
                    let key = header.trim();
                    if !key.is_empty() {
                        item.insert(key.into(), json!(value.trim()));
                    }
                }
                let aliases = item.get("aliases").and_then(Value::as_str).unwrap_or("");
                let aliases = aliases
                    .split(['|', ';'])
                    .map(str::trim)
                    .filter(|v| !v.is_empty())
                    .collect::<Vec<_>>();
                item.insert("aliases".into(), json!(aliases));
                rows.push(Value::Object(item));
                if rows.len() > MAX_ROWS {
                    return Err(invalid("knowledge import contains more than 2000 items"));
                }
            }
            rows
        }
        "srt" => srt_entries(text, filename, lang, domain),
        _ => unreachable!(),
    };
    if rows.len() > MAX_ROWS {
        return Err(invalid("knowledge import contains more than 2000 items"));
    }
    Ok((format, rows))
}

pub async fn import(State(state): State<AppState>, mut form: Multipart) -> AppResult<Json<Value>> {
    if !matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("1" | "true")
    ) {
        return Err(AppError::Conflict("V1 knowledge writes disabled".into()));
    }
    let mut filename = "knowledge".to_owned();
    let mut file: Option<axum::body::Bytes> = None;
    let mut format = "auto".to_owned();
    let mut lang = "zh".to_owned();
    let mut domain = String::new();
    while let Some(field) = form
        .next_field()
        .await
        .map_err(|_| invalid("invalid knowledge import multipart"))?
    {
        match field.name().unwrap_or("") {
            "file" if file.is_none() => {
                filename = field.file_name().unwrap_or("knowledge").to_owned();
                let bytes = field
                    .bytes()
                    .await
                    .map_err(|_| invalid("invalid knowledge upload"))?;
                if bytes.len() > MAX_FILE_BYTES {
                    return Err(invalid("knowledge import file exceeds 5 MiB"));
                }
                file = Some(bytes);
            }
            "import_format" => {
                format = field
                    .text()
                    .await
                    .map_err(|_| invalid("invalid import_format"))?
            }
            "target_lang" => {
                lang = field
                    .text()
                    .await
                    .map_err(|_| invalid("invalid target_lang"))?
            }
            "domain" => domain = field.text().await.map_err(|_| invalid("invalid domain"))?,
            _ => return Err(invalid("unexpected knowledge import form field")),
        }
        if format.len() > 20 || lang.len() > 40 || domain.len() > 200 || filename.len() > 255 {
            return Err(invalid("knowledge import form field too long"));
        }
    }
    let file = file.ok_or_else(|| invalid("knowledge import file missing"))?;
    let (format, rows) = parse(&filename, &file, &format, &lang, &domain)?;
    let parsed = rows.len();
    let mut ids: Vec<Uuid> = Vec::new();
    let mut errors = Vec::new();
    let mut skipped = 0;
    let mut failed = 0;
    for (index, row) in rows.into_iter().enumerate() {
        let mut row = row.as_object().cloned().unwrap_or_default();
        row.entry("target_lang").or_insert_with(|| json!(lang));
        row.entry("domain").or_insert_with(|| json!(domain));
        row.entry("created_by")
            .or_insert_with(|| json!("bulk:import"));
        if let Some(aliases) = row.get("aliases").and_then(Value::as_str) {
            let values = aliases
                .split(['|', ';'])
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .collect::<Vec<_>>();
            row.insert("aliases".into(), json!(values));
        }
        let kind = row
            .get("item_type")
            .and_then(Value::as_str)
            .unwrap_or("term");
        let nonempty = |key: &str| {
            row.get(key)
                .and_then(Value::as_str)
                .is_some_and(|v| !v.trim().is_empty())
        };
        if (kind == "term" && (!nonempty("term") || !nonempty("translation")))
            || (kind == "document" && !nonempty("title") && !nonempty("content"))
        {
            skipped += 1;
            continue;
        }
        let result = serde_json::from_value::<crate::subtitle_knowledge_write_v1::Upsert>(
            Value::Object(row),
        );
        let result = match result {
            Ok(payload) => {
                crate::subtitle_knowledge_write_v1::create(State(state.clone()), Json(payload))
                    .await
                    .map(|Json(v)| v)
            }
            Err(_) => Err(invalid("knowledge import item schema invalid")),
        };
        match result {
            Ok(value) => {
                match value
                    .get("id")
                    .and_then(Value::as_str)
                    .and_then(|id| Uuid::parse_str(id).ok())
                {
                    Some(id) => ids.push(id),
                    None => {
                        failed += 1;
                        if errors.len() < 50 {
                            errors.push(json!({"row":(index+1).to_string(),"error":"knowledge upsert missing id"}));
                        }
                    }
                }
            }
            Err(err) => {
                failed += 1;
                if errors.len() < 50 {
                    errors.push(json!({"row":(index+1).to_string(), "error":err.to_string().chars().take(1000).collect::<String>()}));
                }
            }
        }
    }
    Ok(Json(
        json!({"format":format,"parsed":parsed,"imported":ids.len(),
        "failed":failed,"skipped":skipped,"ids":ids,"errors":errors}),
    ))
}

pub fn router() -> axum::Router<AppState> {
    axum::Router::new()
        .route("/subtitle/knowledge/import", axum::routing::post(import))
        .layer(DefaultBodyLimit::max(MAX_FILE_BYTES + 16 * 1024))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn json_csv_and_srt_parsing_matches_legacy_structure() {
        let (_, rows) = parse(
            "a.json",
            r#"{"items":[{"term":"GPU","translation":"图形处理器"},17]}"#.as_bytes(),
            "auto",
            "zh",
            "",
        )
        .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["term"], "GPU");
        let (_, rows) = parse(
            "a.csv",
            b"term,translation,aliases\nGPU,graphics,card|processor\n",
            "auto",
            "zh",
            "",
        )
        .unwrap();
        assert_eq!(rows[0]["aliases"], json!(["card", "processor"]));
        let (_, rows) = parse(
            "sub.srt",
            b"1\n00:00:01,000 --> 00:00:02,000\nhello\n\n2\n00:00:02,000 --> 00:00:03,000\nworld\n",
            "auto",
            "zh",
            "science",
        )
        .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["content"], "hello\nworld");
        assert_eq!(rows[0]["domain"], "science");
    }
    #[test]
    fn import_has_finite_limits_and_rejects_unknown_formats() {
        assert!(parse("unknown.txt", b"data", "auto", "zh", "").is_err());
        assert!(parse("a.json", b"{}", "json", "zh", "").is_err());
        assert!(parse("a.csv", b"term,translation\n", "csv", "zh", "").is_ok());
        assert!(parse("a.srt", &[b'a'; MAX_FILE_BYTES + 1], "srt", "zh", "").is_err());
        let cues = (0..51)
            .map(|n| format!("{n}\n00:00:00,000 --> 00:00:01,000\na\n"))
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(
            parse("sub.srt", cues.as_bytes(), "auto", "zh", "")
                .unwrap()
                .1
                .len(),
            2
        );
    }
}
