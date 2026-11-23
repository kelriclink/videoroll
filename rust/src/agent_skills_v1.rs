//! Original Agent Skill registry listing, implemented without Python imports.
//! Reads same configured builtin/user skill.json (or SKILL.md) tree; never
//! executes skill instructions and never follows symlinks outside the root.
use crate::{AppResult, api::AppState, settings};
use axum::{Json, Router, extract::State, routing::get};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};

fn flag(cfg: &Value, key: &str, default: bool) -> bool {
    cfg.get(key).and_then(Value::as_bool).unwrap_or(default)
}
fn builtin_root() -> PathBuf {
    if let Some(path) = std::env::var_os("VIDEOROLL_AGENT_BUILTIN_SKILLS_DIR") {
        return PathBuf::from(path);
    }
    let base = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for path in [
        base.join("../src/videoroll/apps/subtitle_service/skills"),
        base.join("src/videoroll/apps/subtitle_service/skills"),
        base.join("videoroll/apps/subtitle_service/skills"),
    ] {
        if path.is_dir() {
            return path;
        }
    }
    // Installed app packaging can explicitly override this via the variable.
    base.join("src/videoroll/apps/subtitle_service/skills")
}
fn user_root() -> PathBuf {
    std::env::var_os("VIDEOROLL_AGENT_SKILLS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("data/agent_skills"))
}
fn strings(value: Option<&Value>) -> Vec<String> {
    let values: Vec<String> = match value {
        Some(Value::Array(xs)) => xs
            .iter()
            .filter_map(|v| v.as_str().map(str::to_owned))
            .collect(),
        Some(Value::String(s)) => s.split([',', '\n']).map(str::to_owned).collect(),
        _ => Vec::new(),
    };
    let mut result = Vec::new();
    let mut seen = HashSet::new();
    for v in values {
        let trimmed = v.split_whitespace().collect::<Vec<_>>().join(" ");
        if trimmed.is_empty() {
            continue;
        }
        let key = trimmed.to_lowercase();
        if seen.insert(key) {
            result.push(trimmed.chars().take(120).collect());
        }
    }
    result
}
fn markdown_metadata(s: &str, fallback: &str) -> Value {
    if !s.starts_with("---\n") {
        return json!({"name":fallback});
    }
    let Some((_head, remaining)) = s[4..].split_once("\n---") else {
        return json!({"name":fallback});
    };
    let mut data = serde_json::Map::new();
    let mut current = String::new();
    for line in remaining.lines().take(256) {
        if let Some(value) = line.trim().strip_prefix("- ") {
            if let Some(Value::Array(a)) = data.get_mut(&current) {
                a.push(json!(value.trim()));
            }
            continue;
        }
        let Some((k, v)) = line.split_once(':') else {
            continue;
        };
        let key = k.trim().to_string();
        if key.is_empty() {
            continue;
        }
        current = key.clone();
        if v.trim().is_empty() {
            data.insert(key, json!([]));
        } else {
            data.insert(key, json!(v.trim()));
        }
    }
    data.entry("name".to_string()).or_insert(json!(fallback));
    Value::Object(data)
}
fn one(root: &Path, child: &Path, source: &str) -> Option<Value> {
    let canonical_root = fs::canonicalize(root).ok()?;
    let canonical = fs::canonicalize(child).ok()?;
    if !canonical.starts_with(&canonical_root) || !canonical.is_dir() {
        return None;
    }
    let json_path = child.join("skill.json");
    let md_path = child.join("SKILL.md");
    let data: Value = if json_path.is_file() {
        let bytes = fs::read(json_path).ok()?;
        if bytes.len() > 512 * 1024 {
            return None;
        }
        serde_json::from_slice(&bytes).ok()?
    } else if md_path.is_file() {
        let bytes = fs::read_to_string(md_path).ok()?;
        if bytes.len() > 512 * 1024 {
            return None;
        }
        markdown_metadata(&bytes, &child.file_name()?.to_string_lossy())
    } else {
        return None;
    };
    let object = data.as_object()?;
    let fallback = child.file_name()?.to_string_lossy();
    let name = object
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(&fallback)
        .trim()
        .chars()
        .take(120)
        .collect::<String>();
    if name.is_empty() {
        return None;
    }
    let desc = object
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(500)
        .collect::<String>();
    let resources = object
        .get("resources")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter(|spec| {
                    let path = match spec {
                        Value::String(s) => s.as_str(),
                        Value::Object(o) => o.get("path").and_then(Value::as_str).unwrap_or(""),
                        _ => "",
                    };
                    if path.is_empty() {
                        return false;
                    }
                    let target = canonical.join(path);
                    match fs::canonicalize(target) {
                        Ok(p) => p.starts_with(&canonical) && p.is_file(),
                        Err(_) => false,
                    }
                })
                .count()
        })
        .unwrap_or(0);
    let mode = object
        .get("run_mode")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or("agent_guidance");
    let runnable = object
        .get("runnable")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    Some(json!({
        "name":name,"description":desc,"domain":strings(object.get("domain")),
        "triggers":strings(object.get("triggers")).into_iter().take(20).collect::<Vec<_>>(),
        "allowed_tools":strings(object.get("allowed_tools")),
        "runnable":runnable,"run_mode":mode,
        "source":source,"path":canonical.to_string_lossy(),
        "resource_count":resources
    }))
}
fn registry(root: &Path, source: &str) -> Vec<Value> {
    let Ok(entries) = fs::read_dir(root) else {
        return Vec::new();
    };
    let mut children = entries
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect::<Vec<_>>();
    children.sort_by_key(|p| {
        p.file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_lowercase()
    });
    children
        .into_iter()
        .filter_map(|p| one(root, &p, source))
        .collect()
}
fn gather(builtin: &Path, user: &Path, include_builtin: bool, include_user: bool) -> Vec<Value> {
    let mut seen = HashSet::new();
    let mut all = Vec::new();
    let mut add = |rows: Vec<Value>| {
        for row in rows {
            let name = row.get("name").and_then(Value::as_str).unwrap_or("");
            if seen.insert(name.to_lowercase()) {
                all.push(row);
            }
        }
    };
    if include_builtin {
        add(registry(builtin, "builtin"));
    }
    if include_user {
        add(registry(user, "user"));
    }
    all
}
async fn list(State(state): State<AppState>) -> AppResult<Json<Vec<Value>>> {
    let cfg = settings::effective(&state.db, "subtitle.translate").await?;
    let root = builtin_root();
    let user = user_root();
    // The Python V1 list API forces SkillRegistry.load even when agent
    // research is disabled; only the per-source toggles affect listing.
    let builtin = flag(&cfg, "rag_agent_builtin_skills_enabled", true);
    let user_enabled = flag(&cfg, "rag_agent_user_skills_enabled", true);
    Ok(Json(
        tokio::task::spawn_blocking(move || gather(&root, &user, builtin, user_enabled))
            .await
            .map_err(|e| crate::AppError::Unavailable(format!("skill scan failed: {e}")))?,
    ))
}
pub fn router() -> Router<AppState> {
    Router::new().route("/subtitle/agent/skills", get(list))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn respects_toggle_and_only_loads_real_files() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../src/videoroll/apps/subtitle_service/skills");
        let skills = gather(&root, Path::new("/nonexistent/skills"), true, true);
        assert!(
            skills
                .iter()
                .any(|s| s["name"] == "translation-term-research-core")
        );
        assert!(skills.iter().all(|s| s["source"] == "builtin"));
        assert!(gather(&root, Path::new("/nonexistent"), false, false).is_empty());
        let mut keys = HashSet::new();
        for s in skills {
            assert!(keys.insert(s["name"].as_str().unwrap().to_lowercase()));
        }
    }
}
