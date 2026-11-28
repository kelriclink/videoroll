//! Decode original DB-only and retained pre-DB Fernet credentials.
//! AES-CBC is provided by system OpenSSL EVP; authenticate before decryption.
//! Never accept unauthenticated ciphertext or log key/plaintext.
use crate::{AppError, AppResult};
use base64::{Engine, engine::general_purpose::URL_SAFE};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::{
    ffi::{c_int, c_void},
    io::Read,
    path::{Path, PathBuf},
    ptr::NonNull,
};

const MAX_SECRET: usize = 1024 * 1024;
const MAX_ENCODED: usize = 2 * 1024 * 1024;

fn invalid() -> AppError {
    AppError::Unavailable("legacy credential cannot be recovered with the configured key".into())
}
fn key_path() -> AppResult<PathBuf> {
    let path = std::env::var_os("VIDEOROLL_FERNET_KEY_PATH")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("VIDEOROLL_DATA_ROOT")
                .filter(|v| !v.is_empty())
                .map(|v| PathBuf::from(v).join("secrets/fernet.key"))
        })
        .ok_or_else(invalid)?;
    if !path.is_absolute() || path == Path::new("/secrets/fernet.key") {
        return Err(invalid());
    }
    Ok(path)
}
#[cfg(unix)]
fn read_key(path: &Path) -> AppResult<Key> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| invalid())?;
    let meta = file.metadata().map_err(|_| invalid())?;
    if !meta.is_file()
        || meta.len() > 128
        || meta.mode() & 0o077 != 0
        || meta.nlink() != 1
        || meta.uid() != unsafe { libc::geteuid() }
    {
        return Err(invalid());
    }
    let mut raw = Vec::new();
    file.take(129)
        .read_to_end(&mut raw)
        .map_err(|_| invalid())?;
    let result = std::str::from_utf8(&raw)
        .ok()
        .and_then(|s| URL_SAFE.decode(s.trim()).ok());
    cleanse(&mut raw);
    let mut decoded = result.ok_or_else(invalid)?;
    if decoded.len() != 32 {
        cleanse(&mut decoded);
        return Err(invalid());
    }
    let mut key = Key([0; 32]);
    key.0.copy_from_slice(&decoded);
    cleanse(&mut decoded);
    Ok(key)
}
#[cfg(not(unix))]
fn read_key(_path: &Path) -> AppResult<Key> {
    Err(invalid())
}
struct Key([u8; 32]);
impl Drop for Key {
    fn drop(&mut self) {
        cleanse(&mut self.0);
    }
}
fn cleanse(bytes: &mut [u8]) {
    unsafe { OPENSSL_cleanse(bytes.as_mut_ptr().cast(), bytes.len()) }
}

// Thin wrapper of OpenSSL 3 stable public EVP APIs. The buffer allocated
// by the caller is ciphertext length + 16, as required by EVP_DecryptUpdate.
#[link(name = "crypto")]
unsafe extern "C" {
    fn EVP_CIPHER_CTX_new() -> *mut c_void;
    fn EVP_CIPHER_CTX_free(ctx: *mut c_void);
    fn EVP_aes_128_cbc() -> *const c_void;
    fn EVP_DecryptInit_ex(
        ctx: *mut c_void,
        cipher: *const c_void,
        engine: *mut c_void,
        key: *const u8,
        iv: *const u8,
    ) -> c_int;
    fn EVP_DecryptUpdate(
        ctx: *mut c_void,
        out: *mut u8,
        outl: *mut c_int,
        input: *const u8,
        inl: c_int,
    ) -> c_int;
    fn EVP_DecryptFinal_ex(ctx: *mut c_void, out: *mut u8, outl: *mut c_int) -> c_int;
    fn OPENSSL_cleanse(ptr: *mut c_void, len: usize);
}
struct Context(NonNull<c_void>);
impl Drop for Context {
    fn drop(&mut self) {
        unsafe { EVP_CIPHER_CTX_free(self.0.as_ptr()) }
    }
}
fn decrypt(token: &str, key: &Key) -> AppResult<String> {
    if token.len() > MAX_ENCODED {
        return Err(invalid());
    }
    let data = URL_SAFE.decode(token).map_err(|_| invalid())?;
    // Fernet v0x80, timestamp(8), IV(16), padded ciphertext, HMAC(32).
    if data.len() < 73 || data[0] != 0x80 || (data.len() - 57) % 16 != 0 {
        return Err(invalid());
    }
    let end = data.len() - 32;
    let mut mac = Hmac::<Sha256>::new_from_slice(&key.0[..16]).map_err(|_| invalid())?;
    mac.update(&data[..end]);
    mac.verify_slice(&data[end..]).map_err(|_| invalid())?;
    let ciphertext = &data[25..end];
    if ciphertext.len() > MAX_SECRET + 16 {
        return Err(invalid());
    }
    let ctx = Context(NonNull::new(unsafe { EVP_CIPHER_CTX_new() }).ok_or_else(invalid)?);
    let mut output = vec![0u8; ciphertext.len() + 16];
    let mut first = 0;
    let mut last = 0;
    let result = (|| {
        if unsafe {
            EVP_DecryptInit_ex(
                ctx.0.as_ptr(),
                EVP_aes_128_cbc(),
                std::ptr::null_mut(),
                key.0[16..].as_ptr(),
                data[9..25].as_ptr(),
            )
        } != 1
        {
            return Err(invalid());
        }
        if unsafe {
            EVP_DecryptUpdate(
                ctx.0.as_ptr(),
                output.as_mut_ptr(),
                &mut first,
                ciphertext.as_ptr(),
                ciphertext.len() as c_int,
            )
        } != 1
        {
            return Err(invalid());
        }
        if first < 0 || first as usize > ciphertext.len() {
            return Err(invalid());
        }
        if unsafe {
            EVP_DecryptFinal_ex(
                ctx.0.as_ptr(),
                output.as_mut_ptr().add(first as usize),
                &mut last,
            )
        } != 1
        {
            return Err(invalid());
        }
        if last < 0 || last > 16 || first as usize + last as usize > MAX_SECRET {
            return Err(invalid());
        }
        let text = std::str::from_utf8(&output[..first as usize + last as usize])
            .map_err(|_| invalid())?;
        Ok(text.to_owned())
    })();
    cleanse(&mut output);
    result
}
/// Match the original Python database representation for recoverable settings.
pub(crate) fn encode(value: &str) -> String {
    let value = value.trim();
    if value.is_empty() { String::new() } else { format!("dbplain:v1:{value}") }
}

pub(crate) fn decode(encoded: &str) -> AppResult<Option<String>> {
    let encoded = encoded.trim();
    if encoded.is_empty() {
        return Ok(None);
    }
    if encoded.len() > MAX_ENCODED {
        return Err(AppError::Invalid("stored credential is oversized".into()));
    }
    let text = if encoded.starts_with("gAAAA") {
        decrypt(encoded, &read_key(&key_path()?)?)?
    } else {
        encoded
            .strip_prefix("dbplain:v1:")
            .unwrap_or(encoded)
            .to_owned()
    };
    if text.len() > MAX_SECRET {
        return Err(AppError::Invalid("stored credential is oversized".into()));
    }
    Ok((!text.is_empty()).then_some(text))
}

#[cfg(test)]
mod tests {
    use super::*;
    // The fixtures below are generated by Python cryptography.fernet using
    // a synthetic key, never from production.
    #[test]
    fn python_fernet_compatibility() {
        let key = Key([42; 32]);
        let token = include_str!("../tests/fixtures/fernet-synthetic.txt").trim();
        assert_eq!(decrypt(token, &key).unwrap(), "fixture secret\nmultiline");
        assert!(decrypt(token, &Key([43; 32])).is_err());
        let mut bytes = URL_SAFE.decode(token).unwrap();
        bytes[28] ^= 1;
        assert!(decrypt(&URL_SAFE.encode(bytes), &key).is_err());
        assert!(decrypt("gAAAA", &key).is_err());
        assert_eq!(
            decode("dbplain:v1:fixture").unwrap().as_deref(),
            Some("fixture")
        );
        assert_eq!(decode("").unwrap(), None);
    }
    #[cfg(unix)]
    #[test]
    fn private_key_file_policy() {
        use std::io::Write;
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt, symlink};
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("key");
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&p)
            .unwrap();
        f.write_all(URL_SAFE.encode([42; 32]).as_bytes()).unwrap();
        drop(f);
        assert_eq!(read_key(&p).unwrap().0, [42; 32]);
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_key(&p).is_err());
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600)).unwrap();
        let link = dir.path().join("link");
        symlink(&p, &link).unwrap();
        assert!(read_key(&link).is_err());
        assert!(read_key(&dir.path().join("missing")).is_err());
    }
}
