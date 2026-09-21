use crate::paths::AppPaths;
use std::collections::VecDeque;
use std::fs::OpenOptions;
use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_LINES: usize = 800;

#[derive(Clone)]
pub struct SharedLog {
    path: std::path::PathBuf,
    lines: Arc<Mutex<VecDeque<String>>>,
}

impl SharedLog {
    pub fn new(paths: &AppPaths) -> Self {
        Self {
            path: paths.log_file(),
            lines: Arc::new(Mutex::new(VecDeque::new())),
        }
    }

    pub fn info(&self, message: impl AsRef<str>) {
        self.write("INFO", message.as_ref());
    }

    pub fn warn(&self, message: impl AsRef<str>) {
        self.write("WARN", message.as_ref());
    }

    pub fn error(&self, message: impl AsRef<str>) {
        self.write("ERROR", message.as_ref());
    }

    pub fn snapshot(&self) -> Vec<String> {
        self.lines
            .lock()
            .map(|lines| lines.iter().cloned().collect())
            .unwrap_or_default()
    }

    fn write(&self, level: &str, message: &str) {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_secs())
            .unwrap_or(0);
        let line = format!("[{ts}] {level} {message}");

        if let Ok(mut lines) = self.lines.lock() {
            lines.push_back(line.clone());
            while lines.len() > MAX_LINES {
                lines.pop_front();
            }
        }

        if let Some(parent) = self.path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = writeln!(file, "{line}");
        }
    }
}
