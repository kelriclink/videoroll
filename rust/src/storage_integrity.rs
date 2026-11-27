//! Read-only storage integrity reporting and explicitly offline V1 partial recovery.
//! Linux descriptor-relative traversal: no untrusted symlink or special file is read.
use crate::{AppError, AppResult, storage::AssetRegistry};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    ffi::CString,
    fs::{self, File, Metadata, OpenOptions},
    io::{self, Read},
    os::{fd::{AsRawFd, FromRawFd}, unix::fs::{MetadataExt, OpenOptionsExt}},
    path::Path,
    time::{Duration, SystemTime},
};

fn open_dir(path: &Path) -> io::Result<File> {
    OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path)
}
fn at(parent: &File, name: &str, flags: i32) -> io::Result<File> {
    let name = CString::new(name).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 { Err(io::Error::last_os_error()) } else { Ok(unsafe { File::from_raw_fd(fd) }) }
}
fn beneath(root: &File, key: &str, directory: bool) -> AppResult<File> {
    AssetRegistry::checked_key(key)?;
    let mut current = root.try_clone()?;
    let mut parts = key.split('/').peekable();
    while let Some(part) = parts.next() {
        current = at(&current, part, if parts.peek().is_some() || directory {
            libc::O_RDONLY | libc::O_DIRECTORY
        } else { libc::O_PATH })?;
    }
    Ok(current)
}
fn fd_path(file: &File) -> String { format!("/proc/self/fd/{}", file.as_raw_fd()) }
fn same(a: &Metadata, b: &Metadata) -> bool {
    a.dev() == b.dev() && a.ino() == b.ino() && a.len() == b.len() &&
    a.mtime() == b.mtime() && a.mtime_nsec() == b.mtime_nsec() &&
    a.ctime() == b.ctime() && a.ctime_nsec() == b.ctime_nsec() && a.nlink() == b.nlink()
}
fn open_root(root: &Path) -> AppResult<File> { Ok(open_dir(&fs::canonicalize(root)?)?) }
fn io_class(error: &AppError) -> &'static str {
    match error {
        AppError::Io(e) if e.kind() == io::ErrorKind::NotFound => "missing",
        AppError::Io(e) if matches!(e.raw_os_error(), Some(libc::ELOOP | libc::ENOTDIR)) => "unsafe_path",
        AppError::Invalid(_) => "unsafe_path",
        _ => "inspection_error",
    }
}
fn partial_name(name: &str) -> bool {
    // Python tempfile.NamedTemporaryFile: .<destination>.<8 chars>.partial.
    // Exclude yt-dlp/download checkpoints and arbitrary nested directories.
    let Some((prefix, nonce)) = name.strip_prefix('.').and_then(|n| n.strip_suffix(".partial")).and_then(|n| n.rsplit_once('.')) else { return false; };
    !prefix.is_empty() && !name.contains('/') && !name.contains('\\') &&
    !name.chars().any(char::is_control) && nonce.len()==8 &&
    nonce.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b==b'_')
}
fn old_single_file(m: &Metadata, cutoff: SystemTime) -> bool {
    m.is_file() && m.nlink()==1 && m.modified().is_ok_and(|v| v<cutoff)
}
fn partial_entry(dir: &File, name: &str, cutoff: SystemTime, remove: bool) -> AppResult<Value> {
    if !partial_name(name) { return Ok(json!({"name":name,"status":"not_legacy_upload"})); }
    let pinned = match at(dir,name,libc::O_PATH) {
        Ok(f)=>f,
        Err(e) if e.kind()==io::ErrorKind::NotFound=>return Ok(json!({"name":name,"status":"missing"})),
        Err(e)=>return Err(e.into()),
    };
    let before=pinned.metadata()?;
    if !old_single_file(&before,cutoff) {return Ok(json!({"name":name,"status":"protected"}))}
    // Opening through a pinned O_PATH descriptor avoids a path swap opening
    // a FIFO/device instead of the regular file just checked.
    let file=File::open(fd_path(&pinned))?;
    if unsafe {libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0 {
        return Ok(json!({"name":name,"status":"locked"}))
    }
    let actual=file.metadata()?;
    let current=at(dir,name,libc::O_PATH)?.metadata()?;
    if !old_single_file(&actual,cutoff) || !same(&before,&actual) || !same(&actual,&current) {
        return Ok(json!({"name":name,"status":"changed"}))
    }
    if remove {
        let name_c=CString::new(name).map_err(|_|AppError::Invalid("invalid partial filename".into()))?;
        if unsafe {libc::unlinkat(dir.as_raw_fd(),name_c.as_ptr(),0)}!=0 {return Err(io::Error::last_os_error().into())}
    }
    Ok(json!({"name":name,"status":if remove {"removed"} else {"candidate"},"size_bytes":actual.len()}))
}

/// The legacy sibling .partial has no writer-side locks or root identity.
/// Deletion is manual only and requires independently verified quiescence
/// and exclusive ownership of this directory. Never infer those from age.
pub fn legacy_partials(root:&Path,older:u64,limit:usize,delete:&[String],quiesced:bool,exclusive_root:bool)->AppResult<Value>{
    if !delete.is_empty() && (!quiesced || !exclusive_root) {
        return Err(AppError::Forbidden("legacy partial deletion requires quiesced writers and exclusive sibling directory".into()))
    }
    if delete.len()>1000 || delete.iter().any(|n|!partial_name(n)){
        return Err(AppError::Invalid("select at most 1000 exact legacy upload filenames".into()))
    }
    let root=fs::canonicalize(root)?;
    let parent=root.parent().ok_or_else(||AppError::Invalid("storage root has no parent".into()))?;
    if root.file_name().is_some_and(|n|n==".partial"){return Err(AppError::Invalid("invalid storage root".into()))}
    let parent=open_dir(parent)?;
    let dir=match at(&parent,".partial",libc::O_RDONLY|libc::O_DIRECTORY){
        Ok(dir)=>dir,
        Err(e) if e.kind()==io::ErrorKind::NotFound=>return Ok(json!({"mode":if delete.is_empty(){"audit"}else{"delete"},"entries":[],"truncated":false})),
        Err(e)=>return Err(e.into()),
    };
    let cutoff=SystemTime::now().checked_sub(Duration::from_secs(older.clamp(60,315360000)))
        .ok_or_else(||AppError::Invalid("invalid partial retention".into()))?;
    let mut entries=Vec::new();
    let mut truncated=false;
    if delete.is_empty(){
        for (i,item) in fs::read_dir(fd_path(&dir))?.enumerate(){
            if i>=limit.clamp(1,1000){truncated=true;break}
            let item=item?;
            let name=item.file_name();
            let Some(name)=name.to_str() else {entries.push(json!({"status":"non_utf8_name"}));continue};
            entries.push(partial_entry(&dir,name,cutoff,false).unwrap_or_else(|e|json!({"name":name,"status":io_class(&e)})));
        }
    } else {
        for name in delete {
            entries.push(partial_entry(&dir,name,cutoff,true).unwrap_or_else(|e|json!({"name":name,"status":io_class(&e)})));
        }
    }
    Ok(json!({"mode":if delete.is_empty(){"audit"}else{"delete"},"entries":entries,"truncated":truncated}))
}

type KeyRecord=(String,Option<String>,Option<i64>,i64,i64,Option<String>);
fn inspect(root:&File,row:KeyRecord,budget:&mut u64)->Value{
    let (key,hash,size,hash_count,size_count,state)=row;
    let base=json!({"key":key,"expected_size":size,"registry_state":state});
    let mut result=base;
    let status=(||->AppResult<&'static str>{
        if state.as_deref().is_some_and(|s|s!="live"){return Ok("registry_not_live")}
        if hash_count>1 || size_count>1{return Ok("metadata_conflict")}
        if hash.as_ref().is_some_and(|h|h.len()!=64 || !h.bytes().all(|b|b.is_ascii_hexdigit())) || size.is_some_and(|s|s<0){return Ok("invalid_metadata")}
        let pinned=beneath(root,&key,false)?;
        let before=pinned.metadata()?;
        if !before.is_file(){return Ok("not_regular_file")}
        result["actual_size"]=json!(before.len());
        if size.is_some_and(|s|s as u64!=before.len()){return Ok("size_mismatch")}
        let Some(hash)=hash else{return Ok("present_no_checksum")};
        if before.len()>*budget{return Ok("hash_deferred")}
        *budget-=before.len();
        let mut file=File::open(fd_path(&pinned))?;
        let mut digest=Sha256::new();
        let mut buf=[0u8;65536];
        let mut remain=before.len();
        while remain>0{
            let nmax=remain.min(buf.len() as u64) as usize;
            let n=file.read(&mut buf[..nmax])?;
            if n==0{return Ok("changed_during_scan")}
            digest.update(&buf[..n]);
            remain-=n as u64;
        }
        let after=file.metadata()?;
        let current=beneath(root,&key,false)?.metadata()?;
        if !same(&before,&after) || !same(&before,&current){return Ok("changed_during_scan")}
        let actual=hex::encode(digest.finalize());
        result["actual_sha256"]=json!(actual);
        Ok(if hash.eq_ignore_ascii_case(&actual){"verified"}else{"checksum_mismatch"})
    })().unwrap_or_else(|e|io_class(&e));
    result["status"]=json!(status);
    result
}

/// Each DB page is a read-only snapshot, filesystem observations are not
/// globally atomic with it. Do not infer a safe deletion from an audit.
pub async fn audit(registry:&AssetRegistry,after:Option<String>,limit:i64,max_hash_mib:u64,directory:Option<String>)->AppResult<Value>{
    let limit=limit.clamp(1,500);
    // Cursor is an opaque database value, never used as a path. Even an
    // invalid registered key must be pageable so the audit can continue.
    if let Some(directory)=directory.as_deref(){AssetRegistry::checked_key(directory)?;}
    // Enumerate a bounded directory before opening the read transaction.
    // Filesystem access can block independently of database timeouts.
    let root=registry.root.clone();
    let candidates=if let Some(directory)=directory{
        let root=root.clone();
        tokio::task::spawn_blocking(move||->AppResult<(Vec<String>,bool)>{
            let root=open_root(&root)?;
            let dir=beneath(&root,&directory,true)?;
            let mut paths=Vec::new();
            let mut truncated=false;
            for (i,item) in fs::read_dir(fd_path(&dir))?.enumerate(){
                if i>=limit as usize{truncated=true;break}
                let item=item?;
                let name=item.file_name();
                let Some(name)=name.to_str()else{continue};
                if !item.file_type()?.is_file() || name.ends_with(".partial") || name.ends_with(".part"){continue}
                let key=format!("{directory}/{name}");
                if AssetRegistry::checked_key(&key).is_ok(){paths.push(key)}
            }
            Ok((paths,truncated))
        }).await.map_err(|_|AppError::Unavailable("storage directory inspection failed".into()))??
    }else{(Vec::new(),false)};
    // Use C collation for bytewise stable pagination independent of the
    // original database collation. Bound each input before merging keys.
    let mut tx=registry.db.pool.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execute(&mut *tx).await?;
    sqlx::query("SET LOCAL statement_timeout = '15s'").execute(&mut *tx).await?;
    let mut rows:Vec<KeyRecord>=sqlx::query_as("WITH keys AS (
        SELECT storage_key FROM (
          (SELECT storage_key FROM storage_objects WHERE ($1::text IS NULL OR storage_key COLLATE \"C\">$1 COLLATE \"C\") ORDER BY storage_key COLLATE \"C\" LIMIT $2)
          UNION
          (SELECT DISTINCT storage_key COLLATE \"C\" FROM assets WHERE ($1::text IS NULL OR storage_key COLLATE \"C\">$1 COLLATE \"C\") ORDER BY 1 LIMIT $2)
          UNION
          (SELECT DISTINCT storage_key COLLATE \"C\" FROM subtitles WHERE ($1::text IS NULL OR storage_key COLLATE \"C\">$1 COLLATE \"C\") ORDER BY 1 LIMIT $2)
        ) merged ORDER BY storage_key COLLATE \"C\" LIMIT $2
    ) SELECT k.storage_key,m.hash,m.size,m.hash_count,m.size_count,o.state
      FROM keys k LEFT JOIN storage_objects o ON o.storage_key=k.storage_key
      CROSS JOIN LATERAL (
        SELECT min(hash) AS hash,min(size) AS size,count(DISTINCT lower(hash)) AS hash_count,count(DISTINCT size) AS size_count
        FROM (SELECT NULLIF(o.checksum_sha256,'') AS hash,o.size_bytes AS size
              UNION ALL SELECT NULLIF(a.sha256,''),a.size_bytes FROM assets a WHERE a.storage_key=k.storage_key) metadata
      ) m ORDER BY k.storage_key COLLATE \"C\"")
        .bind(after).bind(limit+1).fetch_all(&mut *tx).await?;
    let truncated=rows.len()>limit as usize;
    if truncated{rows.truncate(limit as usize);}
    let next=if truncated{rows.last().map(|r|r.0.clone())}else{None};
    // Only database reads occur inside this short read-only transaction.
    let unregistered:Vec<String>=sqlx::query_scalar("SELECT key FROM unnest($1::text[]) AS k(key)
      WHERE NOT EXISTS(SELECT 1 FROM storage_objects WHERE storage_key=key)
      AND NOT EXISTS(SELECT 1 FROM assets WHERE storage_key=key)
      AND NOT EXISTS(SELECT 1 FROM subtitles WHERE storage_key=key)
      AND NOT EXISTS(SELECT 1 FROM storage_object_refs WHERE storage_key=key)
      AND NOT EXISTS(SELECT 1 FROM app_settings WHERE starts_with(app_settings.key,'storage.pending_delete.') AND value_json->>'storage_key'=k.key)
      ORDER BY key COLLATE \"C\"").bind(candidates.0).fetch_all(&mut *tx).await?;
    tx.commit().await?;
    tokio::task::spawn_blocking(move||->AppResult<Value>{
        let root=open_root(&root)?;
        let initial=max_hash_mib.min(10240)*1024*1024;
        let mut budget=initial;
        let entries:Vec<_>=rows.into_iter().map(|row|inspect(&root,row,&mut budget)).collect();
        let mut counts=std::collections::BTreeMap::new();
        for entry in &entries { *counts.entry(entry["status"].as_str().unwrap_or("unknown")).or_insert(0usize)+=1; }
        Ok(json!({"read_only":true,"entries":entries,"counts":counts,"next_after":next,
          "hash_bytes_budget_used":initial-budget,"unregistered_candidates":unregistered,"directory_truncated":candidates.1}))
    }).await.map_err(|_|AppError::Unavailable("storage integrity inspection failed".into()))?
}
