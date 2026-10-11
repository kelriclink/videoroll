#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _decode_double_quoted(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            result.append(char)
            index += 1
            continue
        nxt = value[index + 1]
        replacements = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
        result.append(replacements.get(nxt, nxt))
        index += 2
    return "".join(result)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"{path}:{line_number}: invalid environment key {key!r}")
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            value = _decode_double_quoted(value[1:-1])
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"{path}:{line_number}: multiline/NUL environment values are unsupported")
        values[key] = value
    return values


def _quote_systemd(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("multiline/NUL environment values are unsupported")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def native_data_paths(project_root: Path) -> dict[str, str]:
    """Persistent data may live outside the source checkout on a mounted disk."""
    configured = os.environ.get("VIDEOROLL_DATA_ROOT", "").strip()
    if configured and (not Path(configured).is_absolute() or Path(configured) == Path("/")):
        raise ValueError("VIDEOROLL_DATA_ROOT must be an absolute directory other than /")
    data = Path(configured).resolve() if configured else project_root.resolve() / "data"
    storage = data / "storage"
    # Old Compose mounts /storage (host data/storage), so preserve existing
    # work, dictionaries, render-credential paths when converting an old host.
    use_legacy = bool(configured) and (storage / "work").is_dir()
    work = storage / "work" if use_legacy else data / "work"
    state = storage / "state" if use_legacy else data / "state"
    return {
        "STORAGE_ROOT": str(storage / "objects"),
        "TMPDIR": str(storage / ".partial"),
        "WORK_DIR": str(work / "videoroll"),
        "PLAYOUT_MEDIA_ROOT": str(storage / "playout-media"),
        "SUBTITLE_DICTIONARY_DATA_DIR": str((state / "dictionaries") if (state / "dictionaries").is_dir() else (data / "dictionaries")),
        "RENDER_WORKER_WORK_DIR": str(work / "render-worker"),
        "SOCIAL_PUBLISH_WORK_DIR": str(work / "social-publisher"),
        "RENDER_WORKER_CREDENTIAL_FILE": str(state / "render-worker" / "credential"),
        "SUBTITLE_WHISPER_MODEL_DIR": str(data / "models" / "whisper"),
        "SUBTITLE_RAG_EMBEDDING_MODEL_DIR": str(data / "models" / "embeddings"),
    }


def build_native_environment(source: Path) -> dict[str, str]:
    source = source.resolve()
    values = parse_env_file(source)
    overrides = {
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "EGRESS_GATEWAY_URL": "http://127.0.0.1:8020",
        "ORCHESTRATOR_URL": "http://127.0.0.1:8000",
        "SUBTITLE_SERVICE_URL": "http://127.0.0.1:8001",
        "YOUTUBE_INGEST_URL": "http://127.0.0.1:8002",
        "BILIBILI_PUBLISHER_URL": "http://127.0.0.1:8003",
        "SOCIAL_PUBLISHER_URL": "http://127.0.0.1:8010",
        "WORKFLOW_SERVICE_URL": "http://127.0.0.1:8030",
        "HATCHET_CLIENT_SERVER_URL": "http://127.0.0.1:8888",
        "HATCHET_CLIENT_HOST_PORT": "127.0.0.1:7077",
        "HATCHET_CLIENT_TLS_STRATEGY": "none",
        "RENDER_WORKER_SERVER_URL": "http://127.0.0.1:8000",
        "TRUSTED_PROXY_HOSTS": "127.0.0.1,localhost",
        "TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
        "SAU_RUNTIME_DIR": "/opt/videoroll/current/social-auto-upload",
        "SAU_EXECUTABLE": "/opt/videoroll/venvs/social/bin/sau",
        "FFMPEG_PATH": "/usr/bin/ffmpeg",
    }
    values.update(overrides)
    # Compose examples used localhost:3000 for Hatchet, which is not the native
    # gateway port. Convert only the known template defaults; do not overwrite
    # an explicitly configured public HTTPS origin or its cookie domain.
    if values.get("HATCHET_SERVER_URL", "").rstrip("/") in {"http://localhost:3000", "http://127.0.0.1:3000"}:
        values["HATCHET_SERVER_URL"] = "http://127.0.0.1:3001"
        if values.get("HATCHET_FRONTEND_URL", "").rstrip("/") in {"", "http://localhost:3000/workflow-ui", "http://127.0.0.1:3000/workflow-ui"}:
            values["HATCHET_FRONTEND_URL"] = "http://127.0.0.1:3001/workflow-ui/"
        if values.get("HATCHET_COOKIE_DOMAIN", "") == "localhost":
            values["HATCHET_COOKIE_DOMAIN"] = "127.0.0.1"
    # An older file may omit Hatchet browser fields entirely. Provide a valid
    # loopback-only baseline and make operators configure a public origin for
    # non-local access rather than falling back to mismatched cookie defaults.
    if not values.get("HATCHET_SERVER_URL"):
        values["HATCHET_SERVER_URL"] = "http://127.0.0.1:3001"
    if values["HATCHET_SERVER_URL"] == "http://127.0.0.1:3001":
        values.setdefault("HATCHET_FRONTEND_URL", "http://127.0.0.1:3001/workflow-ui/")
        values.setdefault("HATCHET_COOKIE_DOMAIN", "127.0.0.1")
        values.setdefault("HATCHET_COOKIE_INSECURE", "true")
    # Source .env can be an existing Compose file outside the Git checkout.
    # VIDEOROLL_DATA_ROOT preserves the original media tree when migrating.
    values.update(native_data_paths(PROJECT_ROOT))
    return values


def write_native_environment(source: Path, output: Path, *, force: bool) -> None:
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")

    values = build_native_environment(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by scripts/native/migrate_env.py.\n"
        "# Source values are preserved except Docker service addresses and native filesystem paths.\n"
    )
    content = header + "".join(f"{key}={_quote_systemd(value)}\n" for key, value in values.items())

    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert the existing VideoRoll dotenv file for native/systemd runtime")
    parser.add_argument("--source", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=Path("/etc/videoroll/videoroll.env"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    write_native_environment(args.source, args.output, force=args.force)
    print(f"Native environment written to {args.output}; secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
