use anyhow::{Context, Result};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct AppPaths {
    pub root: PathBuf,
    pub bin: PathBuf,
    pub config: PathBuf,
    pub logs: PathBuf,
    pub cache: PathBuf,
    pub work: PathBuf,
}

impl AppPaths {
    pub fn discover() -> Result<Self> {
        let exe = env::current_exe().context("cannot locate executable")?;
        let root = exe
            .parent()
            .context("executable has no parent directory")?
            .to_path_buf();
        Ok(Self {
            bin: root.join("bin"),
            config: root.join("config"),
            logs: root.join("logs"),
            cache: root.join("cache"),
            work: root.join("work"),
            root,
        })
    }

    pub fn ensure(&self) -> Result<()> {
        for dir in [&self.bin, &self.config, &self.logs, &self.cache, &self.work] {
            fs::create_dir_all(dir)
                .with_context(|| format!("cannot create {}", dir.display()))?;
        }
        Ok(())
    }

    pub fn config_file(&self) -> PathBuf {
        self.config.join("config.json")
    }

    pub fn credential_file(&self) -> PathBuf {
        self.config.join("credential.json")
    }

    pub fn log_file(&self) -> PathBuf {
        self.logs.join("render-worker.log")
    }

    pub fn ffmpeg(&self) -> PathBuf {
        executable_or_path(&self.bin.join("ffmpeg.exe"), "ffmpeg")
    }

    pub fn ffprobe(&self) -> PathBuf {
        executable_or_path(&self.bin.join("ffprobe.exe"), "ffprobe")
    }
}

fn executable_or_path(preferred: &Path, fallback: &str) -> PathBuf {
    if preferred.is_file() {
        return preferred.to_path_buf();
    }
    PathBuf::from(fallback)
}
