use crate::paths::AppPaths;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppConfig {
    #[serde(default)]
    pub server_url: String,
    #[serde(default = "default_node_name")]
    pub node_name: String,
    #[serde(default = "default_worker_key")]
    pub worker_key: String,
    #[serde(default = "default_concurrency")]
    pub max_concurrency: usize,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            server_url: String::new(),
            node_name: default_node_name(),
            worker_key: default_worker_key(),
            max_concurrency: default_concurrency(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Credential {
    pub worker_id: uuid::Uuid,
    pub credential: String,
}

impl AppConfig {
    pub fn load(paths: &AppPaths) -> Self {
        fs::read_to_string(paths.config_file())
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, paths: &AppPaths) -> Result<()> {
        paths.ensure()?;
        let text = serde_json::to_string_pretty(self)?;
        fs::write(paths.config_file(), text)
            .context("cannot save config.json")
    }

    pub fn normalized_server_url(&self) -> String {
        normalize_server_url(&self.server_url)
    }
}

impl Credential {
    pub fn load(paths: &AppPaths) -> Option<Self> {
        fs::read_to_string(paths.credential_file())
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
    }

    pub fn save(&self, paths: &AppPaths) -> Result<()> {
        paths.ensure()?;
        let text = serde_json::to_string_pretty(self)?;
        fs::write(paths.credential_file(), text)
            .context("cannot save credential.json")
    }

    pub fn remove(paths: &AppPaths) -> Result<()> {
        match fs::remove_file(paths.credential_file()) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error).context("cannot remove credential.json"),
        }
    }
}

pub fn normalize_server_url(value: &str) -> String {
    let mut raw = value.trim().trim_end_matches('/').to_string();
    if raw.is_empty() {
        return raw;
    }
    if !raw.contains("://") {
        raw = format!("https://{raw}");
    }
    if raw.ends_with("/render-workers/v1") {
        return raw;
    }
    if !raw.ends_with("/api") {
        raw.push_str("/api");
    }
    format!("{raw}/render-workers/v1")
}

fn default_concurrency() -> usize {
    1
}

fn default_node_name() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "Windows Render Node".to_string())
}

fn default_worker_key() -> String {
    let name = default_node_name()
        .to_ascii_lowercase()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' { ch } else { '-' })
        .collect::<String>();
    format!("windows-native-{}", name.trim_matches('-'))
}
