#!/usr/bin/env bash
# Compile the actual Windows worker modules (except its GUI) on Linux.
# This is not a Windows executable or a D3D11/QSV hardware qualification.
set -euo pipefail
cd "$(dirname "$0")/.."
repo="$(cd .. && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
mkdir "$tmp/src"
# Use pinned repository dependency versions where available. The headless
# loopback test uses rustls webpki roots, not the Windows native root store.
cp Cargo.lock "$tmp/Cargo.lock"
cat > "$tmp/Cargo.toml" <<'TOML'
[package]
name = "videoroll-render-headless-test"
version = "0.1.0"
edition = "2021"
[dependencies]
anyhow = "1"
reqwest = { version = "=0.12.28", default-features = false, features = ["blocking","json","multipart","rustls-tls"] }
serde = {version="1", features=["derive"]}
serde_json = "1"
sha2 = "0.10"
uuid = {version="1",features=["serde","v4"]}
[dev-dependencies]
tempfile = "3"
TOML
python3 - "$repo" "$tmp/src/lib.rs" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])/"native/windows-render-worker/src"
modules=["api","config","ffmpeg","hardware","logging","paths","protocol","render_cache","worker"]
Path(sys.argv[2]).write_text("#![allow(dead_code)]\n"+"".join(f"#[path = {json.dumps(str(root/(name+'.rs')))}]\nmod {name};\n" for name in modules))
PY
CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$repo/rust/target}" cargo test --offline --manifest-path "$tmp/Cargo.toml" --lib -- --nocapture
