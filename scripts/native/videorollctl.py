#!/usr/bin/env python3
"""VideoRoll native/systemd management CLI, independent process isolation."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "scripts" / "native"
INSTALL_ROOT = Path("/opt/videoroll")
ENV_FILE = Path("/opt/videoroll/config/videoroll.env")
CORE_PYTHON = INSTALL_ROOT / "venvs" / "core" / "bin" / "python"
UNIT_TARGET = "videoroll.target"
LEGACY_UNIT = "videoroll.service"
ROLE_UNITS = (
    "videoroll@egress-gateway.service",
    "videoroll@orchestrator.service",
    "videoroll@subtitle-service.service",
    "videoroll@youtube-ingest.service",
    "videoroll@bilibili-publisher.service",
    "videoroll@workflow-api.service",
    "videoroll@workflow-worker.service",
    "videoroll@subtitle-workflow-worker.service",
    "videoroll@dubbing-workflow-worker.service",
    "videoroll@dubbing-service.service",
    "videoroll@subtitle-control-worker.service",
    "videoroll@publish-worker.service",
    "videoroll@outbox-dispatcher.service",
    "videoroll@render-worker.service",
    "videoroll-social@api.service",
    "videoroll-social@worker.service",
    "videoroll-social@scheduler.service",
    "videoroll-web.service",
)
INFRA_UNITS = (
    "videoroll-redis.service",
    "videoroll-hatchet.service",
    "videoroll-hatchet-token.service",
)
SUBTITLE_UNITS = (
    "videoroll@subtitle-service.service",
    "videoroll@subtitle-control-worker.service",
    "videoroll@subtitle-workflow-worker.service",
    "videoroll@render-worker.service",
    "videoroll@outbox-dispatcher.service",
)
WORKFLOW_UNITS = (
    "videoroll@workflow-api.service",
    "videoroll@workflow-worker.service",
    "videoroll@subtitle-workflow-worker.service",
    "videoroll@dubbing-workflow-worker.service",
)
SOCIAL_UNITS = (
    "videoroll-social@api.service",
    "videoroll-social@worker.service",
    "videoroll-social@scheduler.service",
)


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), check=check, text=True)


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("This command requires root (use sudo).")


def is_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def check_not_legacy() -> None:
    if is_active(LEGACY_UNIT):
        raise RuntimeError(
            "The old single-service videoroll.service is active. Stop and disable it "
            "before starting videoroll.target; automatic production cutover is refused."
        )


def filesystem_layout() -> dict[str, Path]:
    # This intentionally does not migrate any files.
    old_data = Path(os.environ.get("VIDEOROLL_DATA_ROOT", "/opt/videoroll/data"))
    return {
        "git_checkout": ROOT,
        "legacy_project": Path("/DATA/videoroll"),
        "app_entrypoint": INSTALL_ROOT / "current",
        "python_environments": INSTALL_ROOT / "venvs",
        "runtime_state": Path(os.environ.get("VIDEOROLL_DATA_ROOT", "/opt/videoroll/data")),
        "project_data": old_data,
        "video_storage": old_data / "storage",
        "project_work": old_data / "work",
        "compose_expected_work": old_data / "storage" / "work",
        "playout_state": old_data / "ffplayout",
        "independent_postgres": Path("/root/compose/PostgreSQL/pgdata"),
    }


def print_layout() -> None:
    print("Existing files are not moved or deleted.")
    for label, location in filesystem_layout().items():
        try:
            present = location.exists()
            marker = "exists" if present else "not confirmed on this host"
        except OSError:
            marker = "not accessible (historical path)"
        print(f"{label:21} {str(location):58} [{marker}]")
    print("\nNOTE: /DATA/videoroll and PostgreSQL paths are historical references;")
    print("verify the production machine before enabling or changing services.")


def health() -> None:
    run("bash", str(NATIVE / "health.sh"))


def install(*, apply: bool) -> None:
    if not apply:
        print("Dry run: would invoke native/install.sh to prepare dependencies and systemd units.")
        print("It does not intentionally start/stop existing VideoRoll containers.")
        print("Hatchet, ffplayout and Web build artifacts require separate setup.")
        print_layout()
        return
    require_root()
    if is_active(UNIT_TARGET) or is_active(LEGACY_UNIT):
        raise RuntimeError(
            "VideoRoll is running; refusing to reinstall native components while live."
        )
    if (INSTALL_ROOT / "current").exists():
        actual = (INSTALL_ROOT / "current").resolve()
        if actual != ROOT:
            raise RuntimeError(f"/opt/videoroll/current points to {actual}, not {ROOT}")
    run("bash", str(NATIVE / "install.sh"))
    print("Native dependencies and service units prepared; no production cutover performed.")
    print("Build/install Hatchet, Web, ffplayout; configure PostgreSQL, then run 'videorollctl doctor'.")


def check_configured_data_paths() -> None:
    """Reject old native/container data paths without modifying existing files."""
    if not ENV_FILE.is_file():
        return
    from migrate_env import parse_env_file

    data_keys = (
        "STORAGE_ROOT", "TMPDIR", "WORK_DIR", "PLAYOUT_MEDIA_ROOT",
        "SUBTITLE_DICTIONARY_DATA_DIR", "SUBTITLE_WHISPER_MODEL_DIR",
        "SUBTITLE_RAG_EMBEDDING_MODEL_DIR", "RENDER_WORKER_CREDENTIAL_FILE",
        "RENDER_WORKER_WORK_DIR", "SOCIAL_PUBLISH_WORK_DIR",
    )
    values = parse_env_file(ENV_FILE)
    for key in data_keys:
        value = values.get(key, "").strip()
        if value.startswith(("/var/lib/videoroll/", "/var/lib/ffplayout/", "/storage/", "/work/")):
            raise RuntimeError(
                f"{key} uses legacy location {value}. "
                "Back up/migrate old state and update /opt/videoroll/config/videoroll.env "
                "before starting native services; existing data will not be moved automatically."
            )


def doctor(*, check_database: bool) -> None:
    print_layout()
    check_configured_data_paths()
    missing = []
    for path in (
        ENV_FILE,
        Path("/etc/systemd/system/videoroll.target"),
        Path("/etc/systemd/system/videoroll-hatchet-token.service"),
        Path("/opt/videoroll/config/redis.conf"),
        Path("/opt/videoroll/libexec/videoroll-init-hatchet-token"),
        Path("/opt/videoroll/bin/hatchet-lite"),
        Path("/opt/videoroll/bin/hatchet-admin"),
        Path("/opt/videoroll/bin/hatchet-migrate"),
        Path("/opt/videoroll/bin/ffplayout"),
        INSTALL_ROOT / "current" / "web" / "index.html",
        CORE_PYTHON,
        *(INSTALL_ROOT / "venvs" / name / "bin/python" for name in
          ("subtitle", "workflow", "subtitle-workflow", "social")),
    ):
        print(f"{'OK' if path.exists() else 'MISSING':7} {path}")
        if not path.exists() and path != Path("/opt/videoroll/bin/ffplayout"):
            missing.append(str(path))
    for executable in ("ffmpeg", "nginx", "redis-server", "redis-cli", "Xvfb", "x11vnc", "websockify", "fluxbox"):
        if shutil.which(executable) is None:
            print(f"MISSING {executable}")
            missing.append(executable)
    if check_database and not missing:
        run(
            str(CORE_PYTHON), str(NATIVE / "env_exec.py"), str(ENV_FILE),
            str(CORE_PYTHON), str(NATIVE / "preflight.py"), "--connect",
        )
    if missing:
        raise RuntimeError(f"{len(missing)} required prerequisites are missing; no services changed")


def start() -> None:
    require_root()
    check_not_legacy()
    if not ENV_FILE.exists():
        raise RuntimeError("Configure /opt/videoroll/config/videoroll.env first")
    doctor(check_database=True)
    run("systemctl", "start", UNIT_TARGET)
    if Path("/opt/videoroll/bin/ffplayout").exists():
        run("systemctl", "start", "videoroll-ffplayout.service")
    from releases import wait_healthy

    wait_healthy(sys.modules[__name__])
    health()


def stop() -> None:
    require_root()
    # ffplayout is separate (not PartOf the old target).
    units = [UNIT_TARGET]
    if is_active("videoroll-ffplayout.service"):
        units.append("videoroll-ffplayout.service")
    run("systemctl", "stop", *units)


def restart() -> None:
    require_root()
    check_not_legacy()
    # Explicit stop/start ensures the target's PartOf= children are relaunched
    # and the Hatchet token is refreshed before any workflow process starts.
    stop()
    start()


def enable() -> None:
    require_root()
    check_not_legacy()
    doctor(check_database=True)
    run("systemctl", "enable", UNIT_TARGET)
    if Path("/opt/videoroll/bin/ffplayout").exists():
        run("systemctl", "enable", "videoroll-ffplayout.service")
    print("VideoRoll will start at boot. Existing processes were not restarted.")


def disable() -> None:
    require_root()
    run("systemctl", "disable", UNIT_TARGET)
    if Path("/etc/systemd/system/videoroll-ffplayout.service").exists():
        run("systemctl", "disable", "videoroll-ffplayout.service")
    print("VideoRoll boot autostart disabled. Existing processes were not stopped.")


def status() -> None:
    run(
        "systemctl", "--no-pager", "--full", "status",
        UNIT_TARGET, *INFRA_UNITS, *ROLE_UNITS, "videoroll-ffplayout.service",
        check=False,
    )


def logs(unit: str | None, *, lines: int) -> None:
    if unit is None:
        # Journalctl matching several units (there is no ordinary log stream
        # for systemd targets).
        names = (*INFRA_UNITS, *ROLE_UNITS, "videoroll-ffplayout.service")
    elif unit in (*INFRA_UNITS, *ROLE_UNITS, "videoroll-ffplayout.service"):
        names = (unit,)
    else:
        raise ValueError(f"unknown unit {unit}; use 'videorollctl status' for exact names")
    args = ["journalctl", "--no-pager", "-n", str(lines)]
    for name in names:
        args += ["-u", name]
    run(*args, check=False)


def changed_services(paths: list[str]) -> tuple[str, ...]:
    """Return restart subset, or raise if dependencies/binaries changed."""
    heavy = (
        "pyproject.toml", "requirements.lock", ".gitmodules",
    )
    heavy_prefixes = (
        "docker/", "services/", "deploy/", "scripts/native/",
        "social-auto-upload/",
    )
    if any(
        p in heavy or p.startswith(heavy_prefixes) or p == "social-auto-upload"
        for p in paths
    ):
        raise RuntimeError(
            "Dependency/runtime or deployment inputs changed. Do not hot-reload; "
            "run a reviewed native runtime update instead."
        )
    restarts: set[str] = set()
    for path in paths:
        if path.startswith(("docs/", "tests/")) or path in ("README.md", "AGENTS.md"):
            continue
        if path.startswith("src/web/"):
            raise RuntimeError(
                "Web frontend changed. Build frontend with build_web.sh before deploying."
            )
        if path.startswith("src/videoroll/apps/subtitle_service/"):
            restarts.update(SUBTITLE_UNITS)
        elif path.startswith("src/videoroll/apps/social_publisher/"):
            restarts.update(SOCIAL_UNITS)
        elif path.startswith("src/videoroll/workflows/"):
            restarts.update(WORKFLOW_UNITS)
        elif path.startswith("src/videoroll/apps/dubbing/"):
            restarts.update(("videoroll@dubbing-workflow-worker.service",
                             "videoroll@dubbing-service.service"))
        elif path.startswith("src/videoroll/apps/orchestrator_api/"):
            restarts.add("videoroll@orchestrator.service")
        elif path.startswith("src/videoroll/apps/egress_gateway/"):
            restarts.add("videoroll@egress-gateway.service")
        elif path.startswith("src/videoroll/apps/bilibili_publisher/"):
            restarts.update(("videoroll@bilibili-publisher.service", "videoroll@publish-worker.service"))
        elif path.startswith("src/videoroll/apps/youtube_ingest/"):
            restarts.add("videoroll@youtube-ingest.service")
        elif path.startswith("src/videoroll/apps/render_worker/"):
            restarts.add("videoroll@render-worker.service")
        else:
            # Shared DB/settings and unknown sources can be imported anywhere.
            restarts.update(ROLE_UNITS)
    return tuple(unit for unit in ROLE_UNITS if unit in restarts)


def update(*, apply: bool, bundle: Path | None = None) -> None:
    if bundle is None:
        raise RuntimeError("Use update --bundle RELEASE.tar.gz; create it with scripts/native/releases.py --output FILE")
    from releases import deploy

    deploy(sys.modules[__name__], bundle, apply=apply)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("layout", help="show known paths without changing files")
    install_cmd = sub.add_parser("install", help="prepare native runtime without production cutover")
    install_cmd.add_argument("--apply", action="store_true")
    doc = sub.add_parser("doctor", help="check native prerequisites")
    doc.add_argument("--database", action="store_true")
    for action in ("start", "stop", "restart", "enable", "disable", "status", "health"):
        sub.add_parser(action)
    logs_cmd = sub.add_parser("logs", help="read journal logs")
    logs_cmd.add_argument("unit", nargs="?")
    logs_cmd.add_argument("-n", "--lines", type=int, default=100)
    update_cmd = sub.add_parser("update", help="preview/apply a verified native application bundle")
    update_cmd.add_argument("--bundle", type=Path, required=True)
    update_cmd.add_argument("--apply", action="store_true")
    rollback_cmd = sub.add_parser("rollback", help="restore the previous native application release")
    rollback_cmd.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "layout":
            print_layout()
        elif args.command == "install":
            install(apply=args.apply)
        elif args.command == "doctor":
            doctor(check_database=args.database)
        elif args.command == "start":
            start()
        elif args.command == "stop":
            stop()
        elif args.command == "restart":
            restart()
        elif args.command == "enable":
            enable()
        elif args.command == "disable":
            disable()
        elif args.command == "status":
            status()
        elif args.command == "logs":
            logs(args.unit, lines=args.lines)
        elif args.command == "health":
            health()
        elif args.command == "update":
            update(apply=args.apply, bundle=args.bundle)
        elif args.command == "rollback":
            from releases import rollback

            rollback(sys.modules[__name__], apply=args.apply)
    except (RuntimeError, ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"videorollctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
