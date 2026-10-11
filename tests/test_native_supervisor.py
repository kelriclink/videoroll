from __future__ import annotations

import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/native/supervisor.py"
spec = importlib.util.spec_from_file_location("videoroll_native_supervisor", MODULE_PATH)
assert spec is not None and spec.loader is not None
supervisor_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = supervisor_module
spec.loader.exec_module(supervisor_module)


def test_single_service_manages_all_roles_and_skips_optional_playout() -> None:
    roles = supervisor_module.programs(
        {"VIDEOROLL_SKIP_FFPLAYOUT": "true"}, runtime_dir="/run/videoroll"
    )
    names = [role.name for role in roles]
    assert names[:2] == ["redis", "hatchet"]
    assert "orchestrator" in names
    assert "workflow-worker" in names
    assert "subtitle-workflow-worker" in names
    assert "social-api" in names
    assert "social-worker" in names
    assert "social-scheduler" in names
    assert "web" in names
    assert "ffplayout" not in names
    assert len(names) == len(set(names))
    for role in roles:
        if role.name in {"hatchet", "orchestrator", "social-worker"}:
            assert role.args[0].startswith(str(supervisor_module.LAUNCHERS))


def test_ffplayout_requires_installed_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_path = supervisor_module.Path

    def checked_path(path: str):
        if path == "/usr/local/bin/ffplayout":
            return real_path(sys.executable)
        return real_path(path)

    monkeypatch.setattr(supervisor_module, "Path", checked_path)
    names = {
        program.name
        for program in supervisor_module.programs({}, runtime_dir="/run/videoroll")
    }
    assert "ffplayout" in names


def test_child_shutdown_terminates_process_group(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(
        "import os,subprocess,time\n"
        "subprocess.Popen(['sleep','90'])\n"
        "time.sleep(90)\n",
        encoding="utf-8",
    )
    group = supervisor_module.Supervisor(
        {},
        [supervisor_module.Program("dummy", (sys.executable, str(script)))],
    )
    try:
        group.start("dummy")
        pid = group.children["dummy"].pid
        assert group.children["dummy"].poll() is None
    finally:
        group.stop()
    assert group.children == {}
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_repeated_child_failures_trigger_systemd_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_module.time, "sleep", lambda _: None)
    child = supervisor_module.Program("bad", ("/bin/sh", "-c", "exit 9"))
    group = supervisor_module.Supervisor({}, [child])
    try:
        group.start("bad")
        for _ in range(supervisor_module.RESTART_LIMIT - 1):
            proc = group.children["bad"]
            proc.wait(timeout=5)
            group.monitor_once()
        group.children["bad"].wait(timeout=5)
        with pytest.raises(RuntimeError, match="critical process failure"):
            group.monitor_once()
    finally:
        group.stop()


def test_systemd_service_uses_one_supervisor_not_old_target() -> None:
    service = (ROOT / "deploy/systemd/videoroll.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 /opt/videoroll/current/scripts/native/supervisor.py" in service
    assert "RuntimeDirectory=videoroll" in service
    assert "KillMode=control-group" in service
    assert "Conflicts=videoroll.target" in service
    assert "Environment=PYTHONPATH=/opt/videoroll/current/src:" in service


def test_legacy_update_alias_delegates_to_isolated_native_control() -> None:
    update = (ROOT / "scripts/native/update-code.sh").read_text(encoding="utf-8")
    assert "videorollctl.py" in update
    assert "systemctl restart videoroll.service" not in update
    assert "docker build" not in update
