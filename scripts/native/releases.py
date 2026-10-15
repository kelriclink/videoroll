"""Small, verified application releases. Runtime dependencies are installed separately.

No database migrations, dependency installs or recovery operations happen here.
All processes using Python code are stopped before changing the current symlink.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Iterator

FORMAT = 1
MANIFEST = "native-release.json"
SOURCE_DIRS = ("src/videoroll", "migrations", "scripts/native", "deploy/systemd")
OPTIONAL_SOURCE_DIRS = ("vendor/youdub_webui",)
SOURCE_FILES = (
    "pyproject.toml", "requirements.lock", "alembic.ini", "README.md",
    "docker/workflow-requirements.txt", "docker/social-publisher-requirements.txt",
)
MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 20000


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def source_content(path: Path) -> bytes:
    content = path.read_bytes()
    if path.suffix in {".py", ".sh", ".service", ".target", ".conf", ".toml", ".lock", ".txt", ".yml", ".md", ".ini"} or path.name == "videorollctl":
        content = content.replace(b"\r\n", b"\n")
    return content


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or "\\" in name or ":" in name:
        return False
    if any(part in {"..", ".", "__pycache__", "node_modules", ".git"}
           or part.startswith(".env") for part in path.parts):
        return False
    return name in SOURCE_FILES or any(name.startswith(d + "/") for d in (*SOURCE_DIRS, *OPTIONAL_SOURCE_DIRS, "web"))


def runtime_path(name: str) -> bool:
    return name in {"pyproject.toml", "requirements.lock"} or name.startswith(
        ("docker/", "scripts/native/", "deploy/")
    )


def schema_path(name: str) -> bool:
    return name == "alembic.ini" or name.startswith("migrations/")


def source_files(root: Path) -> dict[str, Path]:
    files = {}
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Missing or linked release input: {name}")
        files[name] = path
    for directory in (*SOURCE_DIRS, *OPTIONAL_SOURCE_DIRS):
        if not (root / directory).is_dir():
            if directory in OPTIONAL_SOURCE_DIRS and not (root / directory).exists():
                continue  # Support older minimal fixtures and non-YouDub releases.
            raise RuntimeError(f"Missing release source directory: {directory}")
        if (root / directory).is_symlink():
            raise RuntimeError(f"Linked release source directory: {directory}")
        for path in sorted((root / directory).rglob("*")):
            name = path.relative_to(root).as_posix()
            if not allowed_path(name) or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise RuntimeError(f"Symlinks are not release inputs: {name}")
            if path.is_file():
                files[name] = path
    return files


def pack(root: Path, output: Path, *, web: Path | None = None) -> dict:
    """Package an explicit source allowlist; never include .env, media or models."""
    root = root.resolve()
    files = source_files(root)
    if web is not None:
        web = web.resolve()
        if not (web / "index.html").is_file():
            raise RuntimeError("Web artifacts must contain index.html (run npm run build first)")
        for path in sorted(web.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("Web artifacts must not contain symlinks")
            if path.is_file():
                name = "web/" + path.relative_to(web).as_posix()
                if not allowed_path(name):
                    raise RuntimeError(f"Invalid Web artifact: {name}")
                files[name] = path
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    total = 0
    try:
        with tarfile.open(output, "w:gz") as archive:
            for name, path in sorted(files.items()):
                if total + path.stat().st_size > MAX_BYTES:
                    raise RuntimeError("Application release exceeds the size/file limit")
                content = source_content(path)
                total += len(content)
                if total > MAX_BYTES or len(files) > MAX_FILES:
                    raise RuntimeError("Application release exceeds the size/file limit")
                hashes[name] = digest(content)
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name.startswith("scripts/native/") else 0o644
                archive.addfile(info, io.BytesIO(content))
            release = {
                "format": FORMAT,
                "id": digest(json.dumps(hashes, sort_keys=True).encode())[:20],
                "files": hashes,
                "web": web is not None,
            }
            content = json.dumps(release, indent=2, sort_keys=True).encode() + b"\n"
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    output.with_name(output.name + ".sha256").write_text(
        f"{digest(output.read_bytes())}  {output.name}\n", encoding="utf-8",
    )
    return release


def unpack(bundle: Path, destination: Path) -> dict:
    """Extract regular allowlisted files only, then verify their complete manifest."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True, mode=0o755)
    try:
        total = 0
        seen = set()
        hashes = {}
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive:
                name = member.name
                if (not member.isfile() or name in seen or
                    (name != MANIFEST and not allowed_path(name))):
                    raise RuntimeError(f"Invalid archive member: {name}")
                seen.add(name)
                total += member.size
                if member.size < 0 or total > MAX_BYTES or len(seen) > MAX_FILES + 1:
                    raise RuntimeError("Application release exceeds the size/file limit")
                handle = archive.extractfile(member)
                assert handle is not None
                content = handle.read()
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o755 if name.startswith("scripts/native/") else 0o644)
                if name != MANIFEST:
                    hashes[name] = digest(content)
        release = json.loads((destination / MANIFEST).read_text(encoding="utf-8"))
        expected_id = digest(json.dumps(hashes, sort_keys=True).encode())[:20]
        if (release.get("format") != FORMAT or release.get("files") != hashes or
            release.get("id") != expected_id):
            raise RuntimeError("Release manifest or checksums do not match")
        for name in (*SOURCE_FILES, "src/videoroll/__init__.py", "scripts/native/videorollctl.py"):
            if name not in hashes:
                raise RuntimeError(f"Incomplete release: {name}")
        if bool(release.get("web")) != ("web/index.html" in hashes):
            raise RuntimeError("Invalid Web manifest")
        destination.chmod(0o755)
        for path in destination.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        return release
    except BaseException:
        shutil.rmtree(destination)
        raise


def verify_compatible(old: Path, new: Path) -> None:
    before = {name: digest(source_content(path)) for name, path in source_files(old).items()}
    after = {name: digest(source_content(path)) for name, path in source_files(new).items()}
    changed = [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
    if any(runtime_path(name) for name in changed):
        raise RuntimeError("Runtime inputs changed; stop the stack and prepare its dependencies/units separately")
    if any(schema_path(name) for name in changed):
        raise RuntimeError("Database migrations changed; use a planned schema upgrade with a database backup")


def atomic_link(link: Path, target: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"Refusing to replace a real directory: {link}")
    temporary = link.with_name(f".{link.name}-{os.getpid()}")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def write_journal(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".deployment-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def deployment_lock(install_root: Path) -> Iterator[None]:
    import fcntl

    install_root.mkdir(parents=True, exist_ok=True)
    with (install_root / ".deploy.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another deployment is already running") from exc
        yield


def attach_shared_paths(old: Path, new: Path) -> None:
    # Persistent media and credentials now live outside the Git checkout.
    for name in ("social-auto-upload",):
        target = (old / name).resolve(strict=True)
        (new / name).symlink_to(target, target_is_directory=True)
    if not (new / "web").exists():
        target = old / "web"
        if not target.exists():
            target = old / "data" / "web"
        if not (target / "index.html").is_file():
            raise RuntimeError("No installed frontend; include --web-dist in the release")
        (new / "web").symlink_to(target.resolve(), target_is_directory=True)


def require_idle(ctl) -> None:
    ctl.run(
        str(ctl.CORE_PYTHON), str(ctl.NATIVE / "env_exec.py"), str(ctl.ENV_FILE),
        str(ctl.CORE_PYTHON), str(ctl.NATIVE / "check_idle.py"),
    )


def wait_healthy(ctl, *, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    successes = 0
    while True:
        result = subprocess.run(
            ["bash", str(ctl.INSTALL_ROOT / "current/scripts/native/health.sh")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60,
        )
        successes = successes + 1 if result.returncode == 0 else 0
        if successes >= 3:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("New release did not pass service health checks")
        time.sleep(2)


def activate(ctl, new: Path, old: Path) -> None:
    """Leave Redis, Hatchet and playout running; restore code on activation failure."""
    require_idle(ctl)
    link = ctl.INSTALL_ROOT / "current"
    journal = ctl.INSTALL_ROOT / "deployment.json"
    # Preserve the exact set of active roles. Starting every declared role on
    # upgrade would unexpectedly enable optional local rendering or workers.
    active_units = tuple(unit for unit in ctl.ROLE_UNITS if ctl.is_active(unit))
    write_journal(journal, {"previous": str(old), "candidate": str(new), "state": "switching"})
    try:
        ctl.run("systemctl", "stop", *active_units)
        # Detect work racing with the first check. Never clean leases or mark jobs failed.
        require_idle(ctl)
        atomic_link(link, new)
        ctl.run("systemctl", "start", *active_units)
        wait_healthy(ctl)
    except BaseException:
        ctl.run("systemctl", "stop", *active_units)
        atomic_link(link, old)
        ctl.run("systemctl", "start", *active_units)
        wait_healthy(ctl)
        write_journal(journal, {"previous": str(old), "candidate": str(new), "state": "reverted"})
        raise
    atomic_link(ctl.INSTALL_ROOT / "previous", old)
    write_journal(journal, {"previous": str(old), "candidate": str(new), "state": "complete"})


def deploy(ctl, bundle: Path, *, apply: bool) -> None:
    bundle = bundle.resolve(strict=True)
    old = (ctl.INSTALL_ROOT / "current").resolve(strict=True)
    # Preview is completely independent of systemd and does not require root.
    with tempfile.TemporaryDirectory(prefix="videoroll-release-") as temporary:
        candidate = Path(temporary) / "candidate"
        release = unpack(bundle, candidate)
        verify_compatible(old, candidate)
        print(f"Release {release['id']}: {len(release['files'])} files; Web included: {release['web']}")
        if not apply:
            print("Preview only. --apply checks idle tasks, switches application services and verifies health.")
            return
        ctl.require_root()
        ctl.check_not_legacy()
        with deployment_lock(ctl.INSTALL_ROOT):
            if old != (ctl.INSTALL_ROOT / "current").resolve(strict=True):
                raise RuntimeError("Current release changed; retry deployment")
            if not ctl.is_active(ctl.UNIT_TARGET):
                raise RuntimeError("videoroll.target must be active before an application update")
            journal = ctl.INSTALL_ROOT / "deployment.json"
            if journal.exists() and json.loads(journal.read_text()).get("state") == "switching":
                raise RuntimeError("Interrupted deployment detected; run videorollctl rollback --apply")
            releases = ctl.INSTALL_ROOT / "releases"
            releases.mkdir(exist_ok=True)
            releases.chmod(0o755)
            new = releases / (release["id"] + "-" + str(time.time_ns()))
            # Copy the already verified snapshot, not a potentially changed input archive.
            shutil.copytree(candidate, new)
            attach_shared_paths(old, new)
            ctl.run(str(ctl.CORE_PYTHON), "-m", "compileall", "-q", str(new / "src/videoroll"))
            # Freeze an initial Git checkout too, so rollback cannot point to mutable code.
            if old.parent != releases:
                baseline_bundle = Path(temporary) / "baseline.tar.gz"
                web = old / "web" if (old / "web").exists() else old / "data/web"
                baseline_manifest = pack(old, baseline_bundle, web=web)
                baseline = releases / ("baseline-" + baseline_manifest["id"] + "-" + str(time.time_ns()))
                unpack(baseline_bundle, baseline)
                attach_shared_paths(old, baseline)
                old = baseline
            activate(ctl, new, old)
            print(f"Activated {release['id']}. Previous release retained; runtime dependencies unchanged.")


def rollback(ctl, *, apply: bool) -> None:
    if apply:
        ctl.require_root()
    lock = deployment_lock(ctl.INSTALL_ROOT) if apply else contextlib.nullcontext()
    with lock:
        old = (ctl.INSTALL_ROOT / "current").resolve(strict=True)
        journal = ctl.INSTALL_ROOT / "deployment.json"
        state = json.loads(journal.read_text()) if journal.exists() else {}
        interrupted = state.get("state") == "switching"
        new = Path(state["previous"]) if interrupted else (ctl.INSTALL_ROOT / "previous").resolve(strict=True)
        if new.parent != ctl.INSTALL_ROOT / "releases" or not (new / MANIFEST).is_file():
            raise RuntimeError("Previous release is not a managed application snapshot")
        verify_compatible(old, new)
        print(f"Rollback to {new.name}; PostgreSQL and media are unchanged")
        if apply:
            ctl.check_not_legacy()
            activate(ctl, new, old)


def pack_main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Package VideoRoll code without Docker or large dependencies")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--web-dist", type=Path)
    args = parser.parse_args()
    release = pack(Path(__file__).resolve().parents[2], args.output, web=args.web_dist)
    print(f"Created {args.output} ({args.output.stat().st_size:,} bytes), release {release['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(pack_main())
