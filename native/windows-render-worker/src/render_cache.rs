//! Durable, verified finished-render cache; separate from ASS event images.
//! Scope is only our own cache subtree, with no traversal of execution dirs.
use crate::{ffmpeg, protocol::RenderSpec};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};
use uuid::Uuid;

const TTL: Duration = Duration::from_secs(72 * 3600);
const ROOT: &str = "_render_output_cache";
#[derive(Debug, Clone)]
pub struct CacheKey {
    parent: PathBuf,
    extension: &'static str,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Manifest {
    sha256: String,
    size_bytes: u64,
}
#[derive(Debug, Clone)]
pub struct CachedOutput {
    pub path: PathBuf,
    pub sha256: String,
    pub size: u64,
    directory: PathBuf,
}
fn hex_hash(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}
fn is_dir(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_dir())
}
fn is_file(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_file())
}
fn fresh(path: &Path) -> bool {
    fs::metadata(path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| SystemTime::now().duration_since(t).ok())
        .is_some_and(|age| age < TTL)
}
fn generated_ass(role: &str, key: &str) -> bool {
    role == "ass"
        && key
            .strip_prefix("render-input/")
            .and_then(|s| s.strip_suffix("/subtitle_runtime.ass"))
            .is_some_and(|s| Uuid::parse_str(s).is_ok())
}
impl CacheKey {
    pub fn new(work: &Path, spec: &RenderSpec, worker_version: &str, device: &str) -> Option<Self> {
        let extension = match spec.mode.as_str() {
            "burn_in" | "dub_burn_in" => "mp4",
            "soft_sub" => "mkv",
            _ => return None,
        };
        if spec.artifacts.is_empty() {
            return None;
        }
        let mut request = spec.request.clone();
        let mut artifacts = Vec::new();
        let mut roles = std::collections::BTreeSet::new();
        for artifact in &spec.artifacts {
            // Stronger than the old Python cache: do not trust just a path
            // when the coordinator supplied no content hash or length.
            let hash = artifact
                .sha256
                .as_deref()
                .filter(|v| hex_hash(v))?
                .to_ascii_lowercase();
            let size = artifact.size_bytes.filter(|n| *n > 0)?;
            if !roles.insert(artifact.role.as_str()) {
                return None;
            }
            let key = if generated_ass(&artifact.role, &artifact.storage_key) {
                let replacement = format!("generated-ass:sha256:{hash}");
                if request["ass_key"].as_str() == Some(&artifact.storage_key) {
                    request["ass_key"] = json!(replacement);
                }
                replacement
            } else {
                artifact.storage_key.clone()
            };
            artifacts.push(json!({"role":artifact.role,"key":key,"sha256":hash,"size_bytes":size}));
        }
        if !roles.contains("input")
            || !roles.contains(if extension == "mp4" { "ass" } else { "srt" })
        {
            return None;
        }
        artifacts.sort_by_key(|v| v["role"].as_str().unwrap_or("").to_owned());
        let semantic = json!({"cache_schema":1,"worker_version":worker_version,"device":device,"schema":spec.schema_version,"mode":spec.mode,"request":request,"artifacts":artifacts});
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&semantic).ok()?));
        Some(Self {
            parent: work
                .join(ROOT)
                .join(spec.render_job_id.to_string())
                .join(digest),
            extension,
        })
    }
    pub fn load(&self) -> Option<CachedOutput> {
        if !is_dir(&self.parent) {
            return None;
        }
        let mut candidates = fs::read_dir(&self.parent)
            .ok()?
            .filter_map(|v| v.ok())
            .filter(|e| {
                Uuid::parse_str(&e.file_name().to_string_lossy()).is_ok() && is_dir(&e.path())
            })
            .map(|e| e.path())
            .collect::<Vec<_>>();
        candidates.sort_by_key(|p| {
            std::cmp::Reverse(
                fs::metadata(p.join("manifest.json"))
                    .and_then(|m| m.modified())
                    .ok(),
            )
        });
        for directory in candidates.into_iter().take(32) {
            let manifest = directory.join("manifest.json");
            let path = directory.join(format!("video.{}", self.extension));
            if !is_file(&manifest)
                || !is_file(&path)
                || !fresh(&manifest)
                || fs::metadata(&manifest).ok()?.len() > 4096
            {
                continue;
            }
            let Ok(data) = fs::read(&manifest) else {
                continue;
            };
            let Ok(m) = serde_json::from_slice::<Manifest>(&data) else {
                continue;
            };
            if !hex_hash(&m.sha256)
                || m.size_bytes == 0
                || fs::metadata(&path).map(|v| v.len()).ok() != Some(m.size_bytes)
            {
                continue;
            }
            if ffmpeg::sha256_file(&path).ok().as_deref() != Some(m.sha256.as_str()) {
                continue;
            }
            // Refresh TTL on an actively reused output. Failure is harmless.
            if let Ok(file) = fs::OpenOptions::new().write(true).open(&manifest) {
                let _ = file.set_modified(SystemTime::now());
            }
            return Some(CachedOutput {
                path,
                sha256: m.sha256,
                size: m.size_bytes,
                directory,
            });
        }
        None
    }
    pub fn persist(&self, source: &Path) -> Result<CachedOutput> {
        let size = fs::metadata(source)?.len();
        anyhow::ensure!(size > 0, "cannot cache empty render");
        let sha256 = ffmpeg::sha256_file(source)?;
        fs::create_dir_all(&self.parent)?;
        let directory = self.parent.join(Uuid::new_v4().to_string());
        fs::create_dir(&directory)?;
        let path = directory.join(format!("video.{}", self.extension));
        let result = (|| -> Result<()> {
            fs::rename(source, &path).context("atomic cache rename failed")?;
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(directory.join("manifest.partial"))?;
            file.write_all(&serde_json::to_vec(&Manifest {
                sha256: sha256.clone(),
                size_bytes: size,
            })?)?;
            file.sync_all()?;
            drop(file);
            fs::rename(
                directory.join("manifest.partial"),
                directory.join("manifest.json"),
            )?;
            Ok(())
        })();
        if let Err(error) = result {
            // Keep the original render available for direct upload if
            // the manifest cannot be created. Never delete the only copy.
            if path.exists() && fs::rename(&path, source).is_err() {
                return Err(error.context(format!("cached media retained at {}", path.display())));
            }
            let _ = fs::remove_dir_all(&directory);
            return Err(error);
        }
        Ok(CachedOutput {
            path,
            sha256,
            size,
            directory,
        })
    }
}
impl CachedOutput {
    pub fn clear(self) {
        // Only this immutable attempt, never a sibling retry.
        let _ = fs::remove_dir_all(&self.directory);
    }
}
pub fn prune(work: &Path) -> Result<usize> {
    let root = work.join(ROOT);
    if !is_dir(&root) {
        return Ok(0);
    }
    let mut count = 0;
    for job in fs::read_dir(&root)?.filter_map(|e| e.ok()) {
        if !is_dir(&job.path()) || Uuid::parse_str(&job.file_name().to_string_lossy()).is_err() {
            continue;
        }
        for key in fs::read_dir(job.path())?.filter_map(|e| e.ok()) {
            if !is_dir(&key.path()) || !hex_hash(&key.file_name().to_string_lossy()) {
                continue;
            }
            for attempt in fs::read_dir(key.path())?.filter_map(|e| e.ok()) {
                if !is_dir(&attempt.path())
                    || Uuid::parse_str(&attempt.file_name().to_string_lossy()).is_err()
                {
                    continue;
                }
                let manifest = attempt.path().join("manifest.json");
                let stamp = if is_file(&manifest) {
                    manifest
                } else {
                    attempt.path()
                };
                // Unknown times and future timestamps fail closed.
                let expired = fs::metadata(stamp)
                    .and_then(|m| m.modified())
                    .ok()
                    .and_then(|t| SystemTime::now().duration_since(t).ok())
                    .is_some_and(|age| age >= TTL);
                if expired && fs::remove_dir_all(attempt.path()).is_ok() {
                    count += 1;
                }
            }
            let _ = fs::remove_dir(key.path());
        }
        let _ = fs::remove_dir(job.path());
    }
    Ok(count)
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::ArtifactSpec;
    fn spec() -> RenderSpec {
        let ass = format!("render-input/{}/subtitle_runtime.ass", Uuid::new_v4());
        RenderSpec {
            schema_version: 1,
            render_job_id: Uuid::new_v4(),
            task_id: Uuid::new_v4(),
            mode: "burn_in".into(),
            request: json!({"ass_key":ass,"render":{"video_codec":"h264"}}),
            artifacts: vec![
                ArtifactSpec {
                    role: "input".into(),
                    storage_key: "video/input.mp4".into(),
                    size_bytes: Some(10),
                    sha256: Some("a".repeat(64)),
                    download_url: "unused".into(),
                },
                ArtifactSpec {
                    role: "ass".into(),
                    storage_key: ass,
                    size_bytes: Some(10),
                    sha256: Some("b".repeat(64)),
                    download_url: "unused".into(),
                },
            ],
        }
    }
    #[test]
    fn semantic_key_and_atomic_verified_output() {
        let root = tempfile::tempdir().unwrap();
        let mut spec = spec();
        let key = CacheKey::new(root.path(), &spec, "v1", "qsv").unwrap();
        spec.artifacts[1].storage_key =
            format!("render-input/{}/subtitle_runtime.ass", Uuid::new_v4());
        spec.request["ass_key"] = json!(spec.artifacts[1].storage_key);
        spec.artifacts[1].download_url = "new-fenced-url".into();
        assert_eq!(
            key.parent,
            CacheKey::new(root.path(), &spec, "v1", "qsv")
                .unwrap()
                .parent
        );
        let source = root.path().join("output.mp4");
        fs::write(&source, b"verified media bytes").unwrap();
        let cached = key.persist(&source).unwrap();
        assert!(!source.exists());
        assert_eq!(key.load().unwrap().sha256, cached.sha256);
        fs::write(&cached.path, b"corrupted media bytes").unwrap();
        assert!(key.load().is_none());
        cached.clear();
        spec.artifacts[0].sha256 = None;
        assert!(CacheKey::new(root.path(), &spec, "v1", "qsv").is_none());
    }
    #[test]
    fn changed_inputs_and_expired_manifest_never_reused() {
        let root = tempfile::tempdir().unwrap();
        let mut spec = spec();
        let key = CacheKey::new(root.path(), &spec, "v1", "qsv").unwrap();
        let source = root.path().join("output.mp4");
        fs::write(&source, b"valid render").unwrap();
        let cached = key.persist(&source).unwrap();
        spec.request["render"]["video_codec"] = json!("av1");
        assert!(CacheKey::new(root.path(), &spec, "v1", "qsv")
            .unwrap()
            .load()
            .is_none());
        let manifest = fs::OpenOptions::new()
            .write(true)
            .open(cached.directory.join("manifest.json"))
            .unwrap();
        manifest
            .set_modified(SystemTime::now() - TTL - Duration::from_secs(1))
            .unwrap();
        drop(manifest);
        assert!(key.load().is_none());
        assert_eq!(prune(root.path()).unwrap(), 1);
        assert!(!cached.path.exists());
    }
}
