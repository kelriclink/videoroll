//! Streaming dictionary parsers matching Python's core glossary formats.
//! Parser emits bounded batches through tokio::sync::mpsc from a blocking
//! worker; the HTTP handler performs transactional SQL without loading a
//! million-entry upload in memory.
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs::File,
    io::{BufRead, BufReader},
    path::Path,
};
use tokio::sync::mpsc::Sender;

#[derive(Debug, Clone, Default)]
pub struct Draft {
    pub term: String,
    pub translations: Vec<String>,
    pub source_lang: String,
    pub target_lang: String,
    pub pos: String,
    pub definition: String,
    pub domain: String,
    pub tags: Vec<String>,
    pub aliases: Vec<String>,
    pub examples: Vec<Value>,
    pub metadata: Value,
    pub quality: f64,
}
#[derive(Clone)]
pub struct Defaults {
    pub source_lang: String,
    pub target_lang: String,
    pub domain: String,
}
pub fn normalize(term: &str) -> String {
    term.trim()
        .to_lowercase()
        .split(|c: char| c.is_whitespace() || matches!(c, '_' | '-' | ':' | '/' | '|'))
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
pub fn compact(values: impl IntoIterator<Item = String>, limit: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::<String>::new();
    for item in values {
        let clean = item.split_whitespace().collect::<Vec<_>>().join(" ");
        if clean.is_empty() {
            continue;
        }
        let key = clean.to_lowercase();
        if seen.insert(key) {
            out.push(clean.chars().take(240).collect::<String>());
            if out.len() >= limit {
                break;
            }
        }
    }
    out
}
fn split_items(input: &str) -> Vec<String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Vec::new();
    }
    if trimmed.starts_with('[') {
        if let Ok(Value::Array(items)) = serde_json::from_str::<Value>(trimmed) {
            return compact(
                items
                    .into_iter()
                    .filter_map(|v| v.as_str().map(str::to_owned)),
                24,
            );
        }
    }
    let normalized = trimmed.replace("\\r\\n", "\n").replace("\\n", "\n");
    compact(
        normalized
            .split(|c| matches!(c, '\r' | '\n' | ';' | '；' | '|'))
            .map(str::to_owned),
        24,
    )
}
fn base(defaults: &Defaults) -> Draft {
    Draft {
        source_lang: defaults.source_lang.clone(),
        target_lang: if defaults.target_lang.is_empty() {
            "zh".into()
        } else {
            defaults.target_lang.clone()
        },
        domain: defaults.domain.clone(),
        metadata: json!({}),
        quality: 0.55,
        ..Default::default()
    }
}
fn get<'a>(row: &'a HashMap<String, String>, keys: &[&str]) -> &'a str {
    keys.iter()
        .find_map(|k| {
            row.get(*k)
                .map(String::as_str)
                .filter(|s| !s.trim().is_empty())
        })
        .map(str::trim)
        .unwrap_or("")
}
const TERM_KEYS: &[&str] = &[
    "term",
    "word",
    "headword",
    "source",
    "source_term",
    "source text",
    "source_text",
    "expression",
    "simplified",
    "traditional",
    "kanji",
];
const TRANS_KEYS: &[&str] = &[
    "translation",
    "translations",
    "target",
    "target_term",
    "target text",
    "target_text",
    "zh",
    "chinese",
    "meaning_zh",
];
const DEF_KEYS: &[&str] = &[
    "definition",
    "definitions",
    "meaning",
    "gloss",
    "glosses",
    "content",
    "description",
];
const DOMAIN_KEYS: &[&str] = &["domain", "field", "topic"];
fn row_to_draft(row: HashMap<String, String>, defaults: &Defaults) -> Option<Draft> {
    let term = get(&row, TERM_KEYS).to_owned();
    let definition = get(&row, DEF_KEYS).to_owned();
    let mut translations = split_items(get(&row, TRANS_KEYS));
    if translations.is_empty() && defaults.target_lang.to_lowercase().starts_with("en") {
        translations = split_items(&definition);
    }
    if term.is_empty() || translations.is_empty() {
        return None;
    }
    let mut draft = base(defaults);
    draft.term = term;
    draft.translations = translations;
    draft.source_lang = nonempty(get(&row, &["source_lang", "from"]), &defaults.source_lang);
    draft.target_lang = nonempty(
        get(&row, &["target_lang", "to"]),
        &nonempty(&defaults.target_lang, "zh"),
    );
    draft.domain = nonempty(get(&row, DOMAIN_KEYS), &defaults.domain);
    draft.pos = get(&row, &["pos", "part_of_speech", "part of speech"]).to_owned();
    draft.definition = definition;
    draft.tags = split_items(get(&row, &["tag", "tags", "labels", "subject", "category"]));
    draft.aliases = split_items(get(&row, &["alias", "aliases", "variants", "exchange"]));
    draft.quality = 0.55;
    for key in ["collins", "oxford"] {
        if row
            .get(key)
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.)
            > 0.
        {
            draft.quality += 0.12;
        }
    }
    if get(&row, &["frq", "frequency"])
        .parse::<f64>()
        .unwrap_or(0.)
        > 0.
    {
        draft.quality += 0.08;
    }
    draft.quality = draft.quality.clamp(0., 1.);
    let meta = row
        .into_iter()
        .filter(|(k, _)| !matches!(k.as_str(), "word" | "term" | "translation" | "definition"))
        .map(|(k, v)| (k, Value::String(v)))
        .collect::<serde_json::Map<String, Value>>();
    draft.metadata = Value::Object(meta);
    Some(draft)
}
fn nonempty(value: &str, fallback: &str) -> String {
    if value.trim().is_empty() {
        fallback.trim().to_owned()
    } else {
        value.trim().to_owned()
    }
}
fn send(tx: &Sender<Result<Draft, String>>, draft: Draft) -> Result<(), String> {
    tx.blocking_send(Ok(draft))
        .map_err(|_| "dictionary parser receiver closed".into())
}
fn parse_delimited(
    path: &Path,
    delimiter: u8,
    defaults: &Defaults,
    tx: &Sender<Result<Draft, String>>,
) -> Result<(), String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .delimiter(delimiter)
        .from_reader(file);
    let mut records = reader.byte_records();
    let first = match records.next() {
        Some(v) => v.map_err(|e| e.to_string())?,
        None => return Ok(()),
    };
    let strings = |record: &csv::ByteRecord| {
        record
            .iter()
            .map(|s| {
                String::from_utf8_lossy(s)
                    .trim_start_matches('\u{feff}')
                    .to_owned()
            })
            .collect::<Vec<_>>()
    };
    let first_values = strings(&first);
    let headers = first_values
        .iter()
        .map(|s| s.trim().to_lowercase())
        .collect::<Vec<_>>();
    let has_header = headers.iter().any(|s| TERM_KEYS.contains(&s.as_str()))
        && headers
            .iter()
            .any(|s| TRANS_KEYS.contains(&s.as_str()) || DEF_KEYS.contains(&s.as_str()));
    let push_values = |values: Vec<String>| -> Result<(), String> {
        if has_header {
            let row = headers
                .iter()
                .zip(values.into_iter())
                .map(|(k, v)| (k.clone(), v))
                .collect();
            if let Some(draft) = row_to_draft(row, defaults) {
                send(tx, draft)?;
            }
        } else if values.len() >= 2 {
            let term = values[0].trim().to_owned();
            let translations = split_items(&values[1]);
            if !term.is_empty() && !translations.is_empty() {
                let mut draft = base(defaults);
                draft.term = term;
                draft.translations = translations;
                draft.quality = 0.6;
                send(tx, draft)?;
            }
        }
        Ok(())
    };
    if !has_header {
        push_values(first_values)?;
    }
    for record in records {
        push_values(strings(&record.map_err(|e| e.to_string())?))?;
    }
    Ok(())
}
fn parse_cedict(
    path: &Path,
    defaults: &Defaults,
    tx: &Sender<Result<Draft, String>>,
) -> Result<(), String> {
    for line in BufReader::new(File::open(path).map_err(|e| e.to_string())?).lines() {
        let line = line.map_err(|e| e.to_string())?;
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut fields = line.split_whitespace();
        let (Some(traditional), Some(simplified)) = (fields.next(), fields.next()) else {
            continue;
        };
        let Some(open) = line.find('[') else {
            continue;
        };
        let Some(close_rel) = line[open + 1..].find(']') else {
            continue;
        };
        let close = open + 1 + close_rel;
        let pinyin = &line[open + 1..close];
        let gloss = line[close + 1..].trim();
        if !(gloss.starts_with('/') && gloss.ends_with('/')) {
            continue;
        }
        let translations = compact(gloss.trim_matches('/').split('/').map(str::to_owned), 12);
        if translations.is_empty() {
            continue;
        }
        let mut draft = base(defaults);
        draft.term = simplified.to_owned();
        draft.translations = translations;
        draft.source_lang = nonempty(&defaults.source_lang, "zh");
        draft.target_lang = nonempty(&defaults.target_lang, "en");
        if traditional != simplified {
            draft.aliases.push(traditional.to_owned());
        }
        draft.tags = vec!["cc-cedict".into()];
        draft.metadata = json!({"traditional":traditional,"pinyin":pinyin});
        draft.quality = 0.72;
        send(tx, draft)?;
    }
    Ok(())
}
fn parse_jsonl(
    path: &Path,
    defaults: &Defaults,
    tx: &Sender<Result<Draft, String>>,
) -> Result<(), String> {
    let mut reader = BufReader::new(File::open(path).map_err(|e| e.to_string())?);
    let mut bytes = Vec::new();
    loop {
        bytes.clear();
        let read = reader
            .read_until(b'\n', &mut bytes)
            .map_err(|e| e.to_string())?;
        if read == 0 {
            break;
        }
        if bytes.len() > 4 * 1024 * 1024 {
            return Err("dictionary JSONL line exceeds 4 MiB".into());
        }
        let Ok(Value::Object(item)) = serde_json::from_slice::<Value>(&bytes) else {
            continue;
        };
        let as_str = |key: &str| item.get(key).and_then(Value::as_str).unwrap_or("").trim();
        let word = nonempty(as_str("word"), as_str("term"));
        let language = nonempty(
            as_str("lang_code"),
            &nonempty(as_str("source_lang"), &defaults.source_lang),
        );
        if !defaults.source_lang.is_empty()
            && !language.is_empty()
            && language != defaults.source_lang
        {
            continue;
        }
        let mut translations = Vec::new();
        for item in item
            .get("translations")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if !item.is_object() {
                continue;
            }
            let lang = item
                .get("lang_code")
                .or_else(|| item.get("target_lang"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if !defaults.target_lang.is_empty() && !lang.is_empty() && lang != defaults.target_lang
            {
                continue;
            }
            let value = item
                .get("word")
                .or_else(|| item.get("translation"))
                .and_then(Value::as_str)
                .unwrap_or("");
            translations.push(value.to_owned());
        }
        let definition = item
            .get("senses")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|s| s.get("glosses").and_then(Value::as_array))
            .find_map(|s| s.first().and_then(Value::as_str))
            .unwrap_or("")
            .trim()
            .to_owned();
        if translations.is_empty()
            && defaults.target_lang.to_lowercase().starts_with("en")
            && !definition.is_empty()
        {
            translations.push(definition.clone());
        }
        let translations = compact(translations, 12);
        if word.is_empty() || translations.is_empty() {
            continue;
        }
        let mut draft = base(defaults);
        draft.term = word;
        draft.translations = translations;
        draft.source_lang = nonempty(&language, &defaults.source_lang);
        draft.definition = definition;
        draft.quality = 0.65;
        draft.metadata = json!({"source":"jsonl"});
        draft.tags = item
            .get("tags")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        send(tx, draft)?;
    }
    Ok(())
}
fn xml_lang(node: roxmltree::Node<'_, '_>) -> String {
    node.attributes()
        .find(|attr| attr.name() == "lang")
        .map(|a| a.value().trim().to_owned())
        .unwrap_or_default()
}
fn text_of(node: roxmltree::Node<'_, '_>) -> String {
    node.descendants()
        .filter_map(|n| n.text().filter(|_| n.is_text()))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
fn parse_xml(
    path: &Path,
    fmt: &str,
    defaults: &Defaults,
    tx: &Sender<Result<Draft, String>>,
) -> Result<(), String> {
    let md = std::fs::metadata(path).map_err(|e| e.to_string())?;
    if md.len() > 64 * 1024 * 1024 {
        return Err("XML dictionary exceeds 64 MiB safe parsing limit".into());
    }
    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
    let xml = std::str::from_utf8(&bytes).map_err(|e| e.to_string())?;
    let doc = roxmltree::Document::parse(xml).map_err(|e| e.to_string())?;
    for concept in doc.descendants().filter(|n| n.is_element()) {
        let n = concept.tag_name().name().to_ascii_lowercase();
        if (fmt == "tmx" && n != "tu") || (fmt == "tbx" && n != "termentry" && n != "conceptentry")
        {
            continue;
        }
        let mut variants = Vec::<(String, Vec<String>)>::new();
        let candidate = if fmt == "tmx" { "tuv" } else { "langset" };
        for group in concept
            .descendants()
            .filter(|g| g.is_element() && g.tag_name().name().eq_ignore_ascii_case(candidate))
        {
            let lang = xml_lang(group);
            if lang.is_empty() {
                continue;
            }
            let part = if fmt == "tmx" { "seg" } else { "term" };
            for item in group
                .descendants()
                .filter(|g| g.is_element() && g.tag_name().name().eq_ignore_ascii_case(part))
            {
                let contents = text_of(item);
                if contents.is_empty() {
                    continue;
                }
                match variants.iter_mut().find(|(key, _)| key == &lang) {
                    Some((_, list)) => list.push(contents),
                    None => variants.push((lang.clone(), vec![contents])),
                }
                if fmt == "tmx" {
                    break;
                }
            }
        }
        let source = nonempty(
            &defaults.source_lang,
            variants.first().map(|(s, _)| s.as_str()).unwrap_or(""),
        );
        let target = nonempty(&defaults.target_lang, "zh");
        let get_terms = |lang: &str| {
            variants
                .iter()
                .find(|(key, _)| key == lang)
                .map(|(_, items)| items.clone())
                .unwrap_or_default()
        };
        let translations = compact(get_terms(&target), if fmt == "tmx" { 8 } else { 12 });
        if translations.is_empty() {
            continue;
        }
        for term in compact(get_terms(&source), if fmt == "tmx" { 8 } else { 12 }) {
            let mut draft = base(defaults);
            draft.term = term;
            draft.translations = translations.clone();
            draft.source_lang = source.clone();
            draft.target_lang = target.clone();
            draft.tags = vec![fmt.to_owned()];
            draft.quality = if fmt == "tmx" { 0.8 } else { 0.82 };
            send(tx, draft)?;
        }
    }
    Ok(())
}
pub fn parse(
    path: &Path,
    format: &str,
    defaults: &Defaults,
    tx: &Sender<Result<Draft, String>>,
) -> Result<(), String> {
    match format {
        "csv" | "ecdict" => parse_delimited(path, b',', defaults, tx),
        "tsv" | "tab" => parse_delimited(path, b'\t', defaults, tx),
        "cedict" | "cc-cedict" => parse_cedict(path, defaults, tx),
        "jsonl" | "wiktextract" => parse_jsonl(path, defaults, tx),
        "tmx" | "tbx" => parse_xml(path, format, defaults, tx),
        _ => Err(format!("unsupported dictionary format: {format}")),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn parse_fixture(format: &str, contents: &str) -> Vec<Draft> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("terms.dat");
        std::fs::write(&path, contents).unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        parse(
            &path,
            format,
            &Defaults {
                source_lang: "en".into(),
                target_lang: "zh".into(),
                domain: "pcb".into(),
            },
            &tx,
        )
        .unwrap();
        drop(tx);
        let mut out = Vec::new();
        while let Some(Ok(d)) = rx.blocking_recv() {
            out.push(d);
        }
        out
    }
    #[test]
    fn csv_and_tsv() {
        let a = parse_fixture(
            "csv",
            "word,translation,definition,collins,aliases\ntrace,走线,PCB line,1,wire|route\n",
        );
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].quality, 0.67);
        assert_eq!(a[0].aliases, vec!["wire", "route"]);
        let b = parse_fixture("tsv", "trace\t走线\nresistor\t电阻\n");
        assert_eq!(b.len(), 2);
        assert_eq!(b[1].translations, vec!["电阻"]);
    }
    #[test]
    fn jsonl_and_cedict() {
        let a=parse_fixture("jsonl",r#"{"word":"PCB","lang_code":"en","translations":[{"lang_code":"zh","word":"电路板"}]}\n"#.replace("\\n","\n").as_str());
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].quality, 0.65);
        let b = parse_fixture("cc-cedict", "電路 电路 [dian4 lu4] /circuit/electricity/\n");
        assert_eq!(b.len(), 1);
        assert_eq!(b[0].aliases, vec!["電路"]);
    }
    #[test]
    fn xml_terms() {
        let tmx = parse_fixture(
            "tmx",
            r#"<tmx><body><tu><tuv xml:lang="en"><seg>trace</seg></tuv><tuv xml:lang="zh"><seg>走线</seg></tuv></tu></body></tmx>"#,
        );
        assert_eq!(tmx.len(), 1);
        assert_eq!(tmx[0].translations, vec!["走线"]);
        let tbx = parse_fixture(
            "tbx",
            r#"<tbx><conceptEntry><langSet xml:lang="en"><term>trace</term></langSet><langSet xml:lang="zh"><term>走线</term></langSet></conceptEntry></tbx>"#,
        );
        assert_eq!(tbx.len(), 1);
        assert_eq!(tbx[0].tags, vec!["tbx"]);
    }
    #[test]
    fn normalize_and_split() {
        assert_eq!(normalize("PCB / Trace-Width"), "pcb trace width");
        assert_eq!(split_items(" A; a | B "), vec!["A", "B"]);
    }
}
