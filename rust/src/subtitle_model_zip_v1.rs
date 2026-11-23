//! ZIP64-capable bounded extraction for Python's _safe_extract_zip counterpart.
//! All decoding is native Rust. The destination becomes visible only after
//! member validation and CRC-checked extraction.
use crate::{AppError, AppResult};
use std::{
    fs::{self, File},
    io::{self, Read},
    path::{Path, PathBuf},
};
use uuid::Uuid;
use zip::ZipArchive;
const MAX_FILES: usize = 100_000;
const MAX_UNCOMPRESSED: u64 = 40 * 1024 * 1024 * 1024;
fn member_path(name: &str, is_dir: bool) -> AppResult<PathBuf> {
    let normalized = name.replace('\\', "/");
    if normalized.starts_with('/') || normalized.is_empty() {
        return Err(AppError::Invalid(format!("unsafe zip entry: {name}")));
    }
    let value = if is_dir {
        normalized.trim_end_matches('/')
    } else {
        normalized.as_str()
    };
    let parts: Vec<_> = value.split('/').collect();
    if parts.is_empty()
        || parts
            .iter()
            .any(|p| p.is_empty() || *p == "." || *p == "..")
        || value.as_bytes().contains(&0)
    {
        return Err(AppError::Invalid(format!("unsafe zip entry: {name}")));
    }
    let mut path = PathBuf::new();
    for piece in parts {
        path.push(piece);
    }
    Ok(path)
}
pub(crate) fn extract(archive_path: &Path, destination: &Path) -> AppResult<()> {
    if destination.exists() {
        return Err(AppError::Invalid(
            "model already exists; delete it first".into(),
        ));
    }
    let source = File::open(archive_path)?;
    let mut archive = ZipArchive::new(source)
        .map_err(|e| AppError::Invalid(format!("invalid ZIP archive: {e}")))?;
    if archive.len() > MAX_FILES {
        return Err(AppError::TooLarge(format!(
            "zip contains too many entries (max {MAX_FILES})"
        )));
    }
    let mut expected_size = 0u64;
    for i in 0..archive.len() {
        let entry = archive
            .by_index(i)
            .map_err(|e| AppError::Invalid(format!("invalid ZIP entry: {e}")))?;
        member_path(entry.name(), entry.is_dir())?;
        if entry
            .unix_mode()
            .is_some_and(|mode| mode & 0o170000 == 0o120000)
        {
            return Err(AppError::Invalid("zip contains symbolic link".into()));
        }
        if !entry.is_dir() {
            expected_size = expected_size
                .checked_add(entry.size())
                .ok_or_else(|| AppError::TooLarge("zip uncompressed size overflow".into()))?;
            if expected_size > MAX_UNCOMPRESSED {
                return Err(AppError::TooLarge(format!(
                    "zip uncompressed size too large (max {MAX_UNCOMPRESSED} bytes)"
                )));
            }
        }
    }
    let parent = destination
        .parent()
        .ok_or_else(|| AppError::Invalid("model output parent missing".into()))?;
    let file_name = destination
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or_else(|| AppError::Invalid("model output name invalid".into()))?;
    let staging = parent.join(format!(".{file_name}.extract-{}", Uuid::new_v4().simple()));
    fs::create_dir(&staging)?;
    let extracted = (|| -> AppResult<()> {
        let mut actual_total = 0u64;
        for i in 0..archive.len() {
            let entry = archive
                .by_index(i)
                .map_err(|e| AppError::Invalid(format!("invalid ZIP entry: {e}")))?;
            let rel = member_path(entry.name(), entry.is_dir())?;
            let dest = staging.join(rel);
            if entry.is_dir() {
                fs::create_dir_all(dest)?;
                continue;
            }
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent)?;
            }
            let mut outfile = File::create(dest)?;
            let remaining = MAX_UNCOMPRESSED.saturating_sub(actual_total);
            let copied = io::copy(&mut entry.take(remaining + 1), &mut outfile)
                .map_err(|e| AppError::Invalid(format!("invalid ZIP member: {e}")))?;
            actual_total = actual_total.saturating_add(copied);
            if actual_total > MAX_UNCOMPRESSED {
                return Err(AppError::TooLarge("zip uncompressed size too large".into()));
            }
            outfile.sync_all()?;
        }
        if destination.exists() {
            return Err(AppError::Invalid(
                "model already exists; delete it first".into(),
            ));
        }
        fs::rename(&staging, destination)?;
        Ok(())
    })();
    if extracted.is_err() {
        let _ = fs::remove_dir_all(staging);
    }
    extracted
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn blocks_unsafe_members() {
        for name in ["../escape", "/etc/passwd", "path//double", "./dot", ""] {
            assert!(member_path(name, false).is_err(), "{name}");
        }
        assert_eq!(
            member_path("weights\\large.bin", false).unwrap(),
            PathBuf::from("weights/large.bin")
        );
    }
}
