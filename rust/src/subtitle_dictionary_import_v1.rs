//! Multipart dictionary upload against the original translation_dictionary_*
//! tables. The model/file upload API is unrelated: this handler owns archived
//! source uploads, per-batch receipts and bounded streaming glossary ingestion.
//! No Python subprocess, V2-only dictionary schema or production-service fallback.
use crate::{AppError, AppResult, api::AppState, subtitle_dictionary_parse_v1 as parser};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Multipart, State},
    routing::post,
};
use chrono::Utc;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};
use tokio::{fs, io::AsyncWriteExt, sync::mpsc};
use uuid::Uuid;

const MAX_UPLOAD: u64 = 512 * 1024 * 1024;
const MAX_FORM_FIELD: usize = 8 * 1024;
fn invalid(message: impl Into<String>) -> AppError {
    AppError::Invalid(message.into())
}
fn enabled() -> bool {
    matches!(
        std::env::var("VIDEOROLL_V2_ALLOW_V1_SUBTITLE_SETTINGS_WRITES")
            .ok()
            .as_deref(),
        Some("true" | "1")
    )
}
fn safe_filename(filename: &str) -> String {
    let basename = filename.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned = basename
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '-'
            }
        })
        .collect::<String>();
    let cleaned = cleaned
        .trim_matches(['.', '-'])
        .chars()
        .take(180)
        .collect::<String>();
    if cleaned.is_empty() {
        "dictionary.dat".into()
    } else {
        cleaned
    }
}
fn slug(raw: &str) -> String {
    let clean = raw
        .trim()
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '-'
            }
        })
        .collect::<String>();
    clean
        .trim_matches(['-', '.', '_'])
        .chars()
        .take(96)
        .collect()
}
fn get<'a>(map: &'a HashMap<String, String>, key: &str) -> &'a str {
    map.get(key).map(String::as_str).unwrap_or("").trim()
}
fn parse_bool(raw: &str, default: bool) -> AppResult<bool> {
    if raw.is_empty() {
        return Ok(default);
    }
    match raw.to_ascii_lowercase().as_str() {
        "true" | "1" | "on" | "yes" => Ok(true),
        "false" | "0" | "off" | "no" => Ok(false),
        _ => Err(invalid("invalid boolean form field")),
    }
}
fn parse_i64(raw: &str, default: i64, name: &str) -> AppResult<i64> {
    if raw.is_empty() {
        return Ok(default);
    }
    raw.parse::<i64>()
        .map_err(|_| invalid(format!("{name} must be an integer")))
}
#[derive(Debug)]
struct ImportConfig {
    name: String,
    slug: String,
    description: String,
    source_lang: String,
    target_lang: String,
    format_name: String,
    parsed_format: String,
    license: String,
    license_url: String,
    source_url: String,
    version: String,
    attribution: String,
    domain: String,
    priority: i32,
    enabled: bool,
    mode: String,
    max_entries: i64,
}
impl ImportConfig {
    fn from_fields(fields: &HashMap<String, String>, filename: &str) -> AppResult<Self> {
        let preset = get(fields, "dictionary_preset");
        if !preset.is_empty() && slug(preset) != "ecdict" {
            return Err(invalid(format!("unsupported dictionary_preset: {preset}")));
        }
        let use_preset = !preset.is_empty();
        let stem = Path::new(filename)
            .file_stem()
            .and_then(|n| n.to_str())
            .unwrap_or("Dictionary");
        let first_nonempty = |key: &str, preset_value: &str| -> String {
            let explicit = get(fields, key);
            if !explicit.is_empty() {
                explicit.to_owned()
            } else if use_preset {
                preset_value.to_owned()
            } else {
                String::new()
            }
        };
        let name = if !get(fields, "name").is_empty() {
            get(fields, "name").to_owned()
        } else if use_preset {
            "ECDICT".into()
        } else {
            stem.to_owned()
        };
        let name = if name.split_whitespace().next().is_none() {
            "Dictionary".into()
        } else {
            name.split_whitespace().collect::<Vec<_>>().join(" ")
        };
        let name_slug = first_nonempty("slug", "ecdict");
        let slug = slug(if name_slug.is_empty() {
            &name
        } else {
            &name_slug
        });
        let slug = if slug.is_empty() {
            format!("dictionary-{}", &Uuid::new_v4().simple().to_string()[..12])
        } else {
            slug
        };
        // Python dictionary_preset takes precedence over a submitted format_name.
        let format_name = if use_preset {
            "ecdict".to_owned()
        } else {
            first_nonempty("format_name", "")
        };
        let format_name = if format_name.is_empty() {
            "auto".into()
        } else {
            format_name.to_lowercase()
        };
        let parsed_format = if format_name == "auto" {
            Path::new(filename)
                .extension()
                .and_then(|v| v.to_str())
                .unwrap_or("csv")
                .to_ascii_lowercase()
        } else {
            format_name.clone()
        };
        if !matches!(
            parsed_format.as_str(),
            "csv"
                | "ecdict"
                | "tsv"
                | "tab"
                | "cedict"
                | "cc-cedict"
                | "jsonl"
                | "wiktextract"
                | "tmx"
                | "tbx"
        ) {
            return Err(invalid(format!(
                "unsupported dictionary format: {format_name}"
            )));
        }
        let mode = get(fields, "import_mode").to_ascii_lowercase();
        let mode = if mode.is_empty() {
            "upsert".into()
        } else {
            mode
        };
        if mode != "upsert" && mode != "replace" {
            return Err(invalid("import_mode must be upsert or replace"));
        }
        let mut priority = parse_i64(get(fields, "priority"), 0, "priority")?;
        if priority == 0 && use_preset {
            priority = 80;
        }
        let mut full_import = parse_bool(get(fields, "full_import"), false)?;
        let max_raw = parse_i64(get(fields, "max_entries"), 250_000, "max_entries")?;
        if use_preset && max_raw == 250_000 {
            full_import = true;
        }
        let max_entries = if full_import {
            0
        } else if max_raw == 0 {
            // The Python FastAPI form uses int(max_entries or 250000).
            250_000
        } else {
            max_raw.clamp(1, 1_000_000)
        };
        let target_lang = first_nonempty("target_lang", "zh");
        Ok(Self {
            name,
            slug,
            description: first_nonempty(
                "description",
                "Free English to Chinese dictionary database.",
            ),
            source_lang: first_nonempty("source_lang", "en"),
            target_lang: if target_lang.is_empty() {
                "zh".into()
            } else {
                target_lang
            },
            format_name,
            parsed_format,
            license: first_nonempty("license", "MIT"),
            license_url: first_nonempty(
                "license_url",
                "https://github.com/skywind3000/ECDICT/blob/master/LICENSE",
            ),
            source_url: first_nonempty("source_url", "https://github.com/skywind3000/ECDICT"),
            version: first_nonempty("version", ""),
            attribution: get(fields, "attribution").to_owned(),
            domain: first_nonempty("domain", ""),
            priority: priority.clamp(-1000, 1000) as i32,
            enabled: parse_bool(get(fields, "enabled"), true)?,
            mode,
            max_entries,
        })
    }
}
struct Upload {
    path: PathBuf,
    filename: String,
    bytes: u64,
    hash: String,
    fields: HashMap<String, String>,
}
async fn receive(mut multipart: Multipart) -> AppResult<Upload> {
    let root = PathBuf::from(
        std::env::var("SUBTITLE_DICTIONARY_DATA_DIR")
            .unwrap_or_else(|_| "data/dictionaries".to_owned()),
    )
    .join("imports");
    let directory = root
        .join(Utc::now().format("%Y%m%d-%H%M%S").to_string())
        .join(Uuid::new_v4().simple().to_string()[..12].to_owned());
    let mut file = None;
    let mut fields = HashMap::new();
    let result = async {
        let mut filename = String::new();
        let mut bytes = 0u64;
        let mut hasher = Sha256::new();
        while let Some(mut field) = multipart
            .next_field()
            .await
            .map_err(|_| invalid("malformed dictionary multipart upload"))?
        {
            let name = field.name().unwrap_or("").to_owned();
            if name == "file" {
                if file.is_some() {
                    return Err(invalid("only one dictionary file may be uploaded"));
                }
                filename = field.file_name().unwrap_or("dictionary.dat").to_owned();
                let safe = safe_filename(&filename);
                fs::create_dir_all(&directory).await?;
                let path = directory.join(safe);
                let mut out = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&path)
                    .await?;
                while let Some(chunk) = field
                    .chunk()
                    .await
                    .map_err(|_| invalid("malformed dictionary upload chunk"))?
                {
                    bytes = bytes.checked_add(chunk.len() as u64).ok_or_else(|| {
                        AppError::TooLarge("dictionary upload size overflow".into())
                    })?;
                    if bytes > MAX_UPLOAD {
                        return Err(AppError::TooLarge(
                            "dictionary upload exceeds 512 MiB".into(),
                        ));
                    }
                    hasher.update(&chunk);
                    out.write_all(&chunk).await?;
                }
                out.sync_all().await?;
                file = Some(path);
            } else {
                let mut value = Vec::new();
                while let Some(chunk) = field
                    .chunk()
                    .await
                    .map_err(|_| invalid("malformed dictionary form field"))?
                {
                    if value.len().saturating_add(chunk.len()) > MAX_FORM_FIELD {
                        return Err(AppError::TooLarge(
                            "dictionary form field exceeds 8 KiB".into(),
                        ));
                    }
                    value.extend_from_slice(&chunk);
                }
                let value = String::from_utf8(value)
                    .map_err(|_| invalid("dictionary form field must be UTF-8"))?;
                fields.insert(name, value);
            }
        }
        let path = file.ok_or_else(|| invalid("dictionary upload file is required"))?;
        Ok(Upload {
            path,
            filename,
            bytes,
            hash: hex::encode(hasher.finalize()),
            fields,
        })
    }
    .await;
    if result.is_err() {
        // The archive is retained for a validated import, but never a broken
        // multipart write; do not delete any path outside this UUID directory.
        let _ = fs::remove_dir_all(&directory).await;
    }
    result
}
async fn persist(state: AppState, upload: Upload, config: ImportConfig) -> AppResult<Value> {
    let mut tx = state.db.pool.begin().await?;
    let source_id: Uuid = sqlx::query_scalar(
        "INSERT INTO translation_dictionary_sources
         (id,name,slug,description,source_lang,target_lang,format,license,
          license_url,source_url,version,attribution,domain,priority,enabled,
          metadata,updated_at)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,now())
         ON CONFLICT(slug) DO UPDATE SET
           name=EXCLUDED.name,description=EXCLUDED.description,
           source_lang=EXCLUDED.source_lang,target_lang=EXCLUDED.target_lang,
           format=EXCLUDED.format,license=EXCLUDED.license,
           license_url=EXCLUDED.license_url,source_url=EXCLUDED.source_url,
           version=EXCLUDED.version,attribution=EXCLUDED.attribution,
           domain=EXCLUDED.domain,priority=EXCLUDED.priority,
           enabled=EXCLUDED.enabled,metadata=EXCLUDED.metadata,
           updated_at=now() RETURNING id",
    )
    .bind(Uuid::new_v4())
    .bind(&config.name)
    .bind(&config.slug)
    .bind(&config.description)
    .bind(&config.source_lang)
    .bind(&config.target_lang)
    .bind(&config.format_name)
    .bind(&config.license)
    .bind(&config.license_url)
    .bind(&config.source_url)
    .bind(&config.version)
    .bind(&config.attribution)
    .bind(&config.domain)
    .bind(config.priority)
    .bind(config.enabled)
    .bind(json!({"last_import_sha256":upload.hash}))
    .fetch_one(&mut *tx)
    .await?;
    let batch_id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO translation_dictionary_import_batches
         (id,source_id,status,filename,archive_path,file_sha256,file_size_bytes,
          format,import_mode,requested_by,started_at,updated_at)
         VALUES($1,$2,'running',$3,$4,$5,$6,$7,$8,'manual',now(),now())",
    )
    .bind(batch_id)
    .bind(source_id)
    .bind(&upload.filename)
    .bind(upload.path.to_string_lossy().to_string())
    .bind(&upload.hash)
    .bind(upload.bytes as i64)
    .bind(&config.format_name)
    .bind(&config.mode)
    .execute(&mut *tx)
    .await?;
    if config.mode == "replace" {
        sqlx::query("DELETE FROM translation_dictionary_entries WHERE source_id=$1")
            .bind(source_id)
            .execute(&mut *tx)
            .await?;
    }
    let (sender, mut receiver) = mpsc::channel::<Result<parser::Draft, String>>(96);
    let path = upload.path;
    let fmt = config.parsed_format.clone();
    let defaults = parser::Defaults {
        source_lang: config.source_lang.clone(),
        target_lang: config.target_lang.clone(),
        domain: config.domain.clone(),
    };
    let parser_job = tokio::task::spawn_blocking(move || {
        let result = parser::parse(&path, &fmt, &defaults, &sender);
        if let Err(err) = &result {
            let _ = sender.blocking_send(Err(err.clone()));
        }
        result
    });
    let mut parsed = 0i64;
    let mut upserted = 0i64;
    let mut skipped = 0i64;
    let insert = async {
        while let Some(result) = receiver.recv().await {
            let draft = result.map_err(invalid)?;
            parsed += 1;
            if config.max_entries > 0 && parsed > config.max_entries {
                skipped += 1;
                continue;
            }
            let term = draft.term.trim();
            let normalized = parser::normalize(term);
            let translations = parser::compact(draft.translations, 24);
            if term.is_empty() || normalized.is_empty() || translations.is_empty() {
                skipped += 1;
                continue;
            }
            let target_lang = if draft.target_lang.trim().is_empty() {
                "zh"
            } else {
                draft.target_lang.trim()
            };
            let text = translations.join(" | ");
            sqlx::query(
                "INSERT INTO translation_dictionary_entries
                 (id,source_id,batch_id,source_lang,target_lang,term,normalized_term,
                  translations,translation_text,pos,definition,domain,tags,aliases,
                  examples,metadata,quality,enabled,updated_at)
                 VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,
                        $13::jsonb,$14::jsonb,$15::jsonb,$16::jsonb,$17,TRUE,now())
                 ON CONFLICT(source_id,source_lang,target_lang,normalized_term,domain)
                 DO UPDATE SET term=EXCLUDED.term,batch_id=EXCLUDED.batch_id,
                    translations=EXCLUDED.translations,
                    translation_text=EXCLUDED.translation_text,pos=EXCLUDED.pos,
                    definition=EXCLUDED.definition,tags=EXCLUDED.tags,
                    aliases=EXCLUDED.aliases,examples=EXCLUDED.examples,
                    metadata=EXCLUDED.metadata,quality=EXCLUDED.quality,
                    enabled=TRUE,updated_at=now()",
            )
            .bind(Uuid::new_v4())
            .bind(source_id)
            .bind(batch_id)
            .bind(draft.source_lang.trim())
            .bind(target_lang)
            .bind(term)
            .bind(normalized)
            .bind(json!(translations))
            .bind(text)
            .bind(draft.pos.trim())
            .bind(draft.definition.trim())
            .bind(draft.domain.trim())
            .bind(json!(parser::compact(draft.tags, 24)))
            .bind(json!(parser::compact(draft.aliases, 24)))
            .bind(json!(
                draft.examples.into_iter().take(12).collect::<Vec<_>>()
            ))
            .bind(draft.metadata)
            .bind(draft.quality.clamp(0., 1.))
            .execute(&mut *tx)
            .await?;
            upserted += 1;
        }
        Ok::<(), AppError>(())
    }
    .await;
    // On SQL error, release the receiver before awaiting the blocking parser:
    // otherwise a full channel could leave the worker parked indefinitely.
    drop(receiver);
    let parser_result = parser_job
        .await
        .map_err(|_| AppError::Unavailable("dictionary parsing worker interrupted".into()))?;
    insert?;
    parser_result.map_err(invalid)?;
    let count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::bigint FROM translation_dictionary_entries WHERE source_id=$1",
    )
    .bind(source_id)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "UPDATE translation_dictionary_sources SET entry_count=$2,updated_at=now() WHERE id=$1",
    )
    .bind(source_id)
    .bind(count.min(i32::MAX as i64) as i32)
    .execute(&mut *tx)
    .await?;
    let stats = json!({"parsed":parsed,"upserted":upserted,"skipped":skipped,
         "max_entries":config.max_entries,"full_import":config.max_entries==0});
    sqlx::query(
        "UPDATE translation_dictionary_import_batches SET
         status='succeeded',stats=$2::jsonb,error='',finished_at=now(),updated_at=now()
         WHERE id=$1",
    )
    .bind(batch_id)
    .bind(&stats)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(
        json!({"source_id":source_id,"batch_id":batch_id,"status":"succeeded",
        "parsed":parsed,"upserted":upserted,"skipped":skipped,
        "max_entries":config.max_entries,"full_import":config.max_entries==0,"sha256":upload.hash}),
    )
}
async fn import_dictionary(
    State(state): State<AppState>,
    multipart: Multipart,
) -> AppResult<Json<Value>> {
    if !enabled() {
        return Err(AppError::Conflict("V1 dictionary writes disabled".into()));
    }
    let upload = receive(multipart).await?;
    let config = ImportConfig::from_fields(&upload.fields, &upload.filename)?;
    Ok(Json(persist(state, upload, config).await?))
}
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/subtitle/dictionaries/import", post(import_dictionary))
        .layer(DefaultBodyLimit::max((MAX_UPLOAD + 1024 * 1024) as usize))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validation_and_preset() {
        let empty = HashMap::new();
        let config = ImportConfig::from_fields(&empty, "glossary.csv").unwrap();
        assert_eq!(config.name, "glossary");
        assert_eq!(config.parsed_format, "csv");
        assert_eq!(config.max_entries, 250000);
        let preset = HashMap::from([("dictionary_preset".to_owned(), "ecdict".to_owned())]);
        let c = ImportConfig::from_fields(&preset, "ECDICT.csv").unwrap();
        assert_eq!(c.priority, 80);
        assert_eq!(c.max_entries, 0);
        assert_eq!(c.format_name, "ecdict");
    }
    #[test]
    fn filename_and_bounds() {
        assert_eq!(safe_filename("../../攻击 test.csv"), "test.csv");
        assert_eq!(safe_filename("C:\\x\\term.csv"), "term.csv");
        assert_eq!(parse_bool("no", true).unwrap(), false);
        assert_eq!(slug("  PCB Width  "), "pcb-width");
    }
}
