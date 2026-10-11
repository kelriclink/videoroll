#!/usr/bin/env python3
"""Run VideoRoll as ONE systemd unit while preserving process isolation.

This supervisor is intentionally small: systemd owns the single service and
its cgroup; independent native commands own their respective executables.
No Docker or application-embedded multiprocessing is involved.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

SOURCE = Path("/opt/videoroll/current")
LAUNCHERS = SOURCE / "scripts" / "native"
DATA = Path(os.environ.get("VIDEOROLL_DATA_ROOT", "/opt/videoroll/data"))
HATCHET_CONFIG = DATA / "hatchet" / "config"
REDIS_CONF = Path("/opt/videoroll/config/redis.conf")
NGINX_CONF = Path("/opt/videoroll/config/nginx.conf")
HEALTH_TIMEOUT = 120
RESTART_WINDOW = 60
RESTART_LIMIT = 5


def note(message: str) -> None:
    print(f"[videoroll-supervisor] {message}", flush=True)


@dataclass(frozen=True)
class Program:
    name: str
    args: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


def programs(env: dict[str, str], *, runtime_dir: str) -> list[Program]:
    """Keep the launcher roles identical to the validated Compose roles."""
    service_env = {
        "LD_LIBRARY_PATH": "/opt/videoroll/lib",
        "HOME": str(DATA / "home"),
        "PLAYWRIGHT_BROWSERS_PATH": str(DATA / "browsers"),
        "SAU_RUNTIME_DIR": "/opt/videoroll/current/social-auto-upload",
        "SAU_EXECUTABLE": "/opt/videoroll/venvs/social/bin/sau",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = [
        Program("redis", ("/opt/videoroll/bin/redis-server", str(REDIS_CONF)), env={"LD_LIBRARY_PATH": "/opt/videoroll/lib"}),
        Program("hatchet", (str(LAUNCHERS / "run-hatchet.sh"),)),
    ]
    for name in (
        "egress-gateway", "orchestrator", "subtitle-service", "youtube-ingest",
        "bilibili-publisher", "workflow-api", "workflow-worker",
        "subtitle-workflow-worker", "subtitle-control-worker", "publish-worker",
        "outbox-dispatcher", "render-worker",
    ):
        result.append(
            Program(name, (str(LAUNCHERS / "run-role.sh"), name))
        )
    for role in ("api", "worker", "scheduler"):
        social_runtime = str(Path(runtime_dir) / f"social-{role}")
        result.append(
            Program(
                f"social-{role}",
                (str(LAUNCHERS / "run-social-role.sh"), role),
                env={**service_env, "RUNTIME_DIRECTORY": social_runtime},
            )
        )
    if (
        env.get("VIDEOROLL_SKIP_FFPLAYOUT", "").lower() not in ("1", "true", "yes")
        and Path("/opt/videoroll/bin/ffplayout").is_file()
    ):
        result.append(
            Program(
                "ffplayout",
                (
                    "/opt/videoroll/bin/ffplayout", "--listen", "127.0.0.1:8787",
                    "--db", str(DATA / "ffplayout" / "db" / "ffplayout.db"),
                    "--log-to-console",
                ),
                cwd=str(DATA / "ffplayout"),
            )
        )
    result.append(
        Program("web", ("/opt/videoroll/bin/nginx", "-c", str(NGINX_CONF), "-g", "daemon off;"))
    )
    return result


def redis_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=1.5) as sock:
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            return sock.recv(64).startswith(b"+PONG\r\n")
    except OSError:
        return False


def hatchet_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8888/api/ready", timeout=2) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False


def mint_hatchet_token(env: dict[str, str]) -> str:
    """Generate a token without putting it in logs or persisting it on disk."""
    admin_env = {**env, "DATABASE_URL": env["HATCHET_DATABASE_URL"]}
    result = subprocess.run(
        (
            "/opt/videoroll/bin/hatchet-admin", "token", "create",
            "--config", str(HATCHET_CONFIG),
            "--name", "videoroll-native", "--expiresIn", "87600h",
        ),
        env=admin_env,
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    token = lines[-1] if lines else ""
    if len(token) < 30 or any(char.isspace() for char in token):
        raise RuntimeError("Hatchet did not return a valid worker token")
    return token


class Supervisor:
    def __init__(self, env: dict[str, str], specs: list[Program]) -> None:
        self.env = dict(env)
        self.specs = {spec.name: spec for spec in specs}
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.failures: dict[str, deque[float]] = {
            name: deque() for name in self.specs
        }
        self.stopping = False

    def start(self, name: str) -> None:
        spec = self.specs[name]
        child_env = {**self.env, **spec.env}
        if name.startswith("social-"):
            Path(child_env["RUNTIME_DIRECTORY"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        command = list(spec.args)
        note(f"starting {name}")
        self.children[name] = subprocess.Popen(
            command, env=child_env, cwd=spec.cwd, start_new_session=True
        )

    def wait_ready(self, name: str, probe, timeout: float = HEALTH_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.stopping:
            if self.children[name].poll() is not None:
                raise RuntimeError(f"{name} exited before ready")
            if probe():
                note(f"{name} ready")
                return
            time.sleep(0.5)
        raise RuntimeError(f"{name} not ready within {timeout} seconds")

    def monitor_once(self) -> None:
        for name, child in list(self.children.items()):
            status = child.poll()
            if status is None:
                continue
            if self.stopping:
                continue
            now = time.monotonic()
            recent = self.failures[name]
            while recent and now - recent[0] > RESTART_WINDOW:
                recent.popleft()
            recent.append(now)
            note(f"{name} exited (status={status}, recent restarts={len(recent)})")
            if name in {"redis", "hatchet"} or len(recent) >= RESTART_LIMIT:
                raise RuntimeError(f"critical process failure: {name}")
            # Reap any remaining grandchildren of that failed process.
            self.terminate_group(child)
            time.sleep(1)
            self.start(name)

    @staticmethod
    def terminate_group(child: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def stop(self) -> None:
        self.stopping = True
        # Reverse startup order: stop workers and API before Redis/Hatchet.
        children = list(self.children.values())
        for child in reversed(children):
            self.terminate_group(child)
        deadline = time.monotonic() + 25
        for child in reversed(children):
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
        self.children.clear()
        note("all child processes stopped")


def validate(specs: list[Program]) -> None:
    for spec in specs:
        executable = Path(spec.args[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError(f"{spec.name}: missing executable {executable}")
    for name, path in (("Redis", REDIS_CONF), ("Nginx", NGINX_CONF)):
        if not path.is_file():
            raise RuntimeError(f"{name}: missing configuration {path}")
    if not (DATA / "hatchet").is_dir():
        raise RuntimeError("Hatchet state directory not installed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check commands, do not launch")
    args = parser.parse_args()
    env = dict(os.environ)
    # Loading code from the clone avoids reinstalling wheels for ordinary
    # Python changes; dependency changes still require updating each venv.
    python_paths = (str(SOURCE / "src"), str(SOURCE / "social-auto-upload"))
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        (*python_paths, *((existing_pythonpath,) if existing_pythonpath else ()))
    )
    runtime_dir = env.get("RUNTIME_DIRECTORY", "/run/videoroll")
    specs = programs(env, runtime_dir=runtime_dir)
    validate(specs)
    if args.check:
        note(f"validated {len(specs)} native processes")
        return 0

    supervisor = Supervisor(env, specs)

    def shutdown(_signum, _frame) -> None:
        supervisor.stopping = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        # No application worker starts until Redis and Hatchet are ready.
        supervisor.start("redis")
        supervisor.wait_ready("redis", redis_ready, timeout=45)
        supervisor.start("hatchet")
        supervisor.wait_ready("hatchet", hatchet_ready, timeout=180)
        supervisor.env["HATCHET_CLIENT_TOKEN"] = mint_hatchet_token(supervisor.env)
        note("Hatchet worker token generated (not logged)")
        for spec in specs:
            if spec.name not in {"redis", "hatchet"}:
                supervisor.start(spec.name)
        while not supervisor.stopping:
            supervisor.monitor_once()
            time.sleep(0.5)
    except Exception as exc:
        note(f"fatal: {type(exc).__name__}: {exc}")
        return 1
    finally:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
