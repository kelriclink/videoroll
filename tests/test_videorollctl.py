from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "native" / "videorollctl.py"
spec = importlib.util.spec_from_file_location("videorollctl_native", SCRIPT)
assert spec and spec.loader
ctl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ctl
spec.loader.exec_module(ctl)


def test_layout_is_read_only_and_distinguishes_two_work_folders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ctl, "ROOT", Path("/DATA/videoroll"))
    ctl.print_layout()
    output = capsys.readouterr().out
    assert "/DATA/videoroll/data/work" in output
    assert "/DATA/videoroll/data/storage/work" in output
    assert "/root/compose/PostgreSQL/pgdata" in output


def test_old_env_file_paths_are_rejected_without_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_config = tmp_path / "videoroll.env"
    old_config.write_text(
        "STORAGE_ROOT=/var/lib/videoroll/storage/objects\n"
        "WORK_DIR=/var/lib/videoroll/work/videoroll\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "native"))
    monkeypatch.setattr(ctl, "ENV_FILE", old_config)
    original = old_config.read_bytes()
    with pytest.raises(RuntimeError, match="STORAGE_ROOT"):
        ctl.check_configured_data_paths()
    assert old_config.read_bytes() == original


def test_project_data_env_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "videoroll.env"
    config.write_text(
        f"STORAGE_ROOT={tmp_path}/data/storage/objects\n"
        f"WORK_DIR={tmp_path}/data/work/videoroll\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "native"))
    monkeypatch.setattr(ctl, "ENV_FILE", config)
    ctl.check_configured_data_paths()


def test_installer_does_not_reown_or_delete_existing_data_tree() -> None:
    installer = (ROOT / "scripts/native/install.sh").read_text(encoding="utf-8")
    assert 'DATA_ROOT="${VIDEOROLL_DATA_ROOT:-$ROOT_DIR/data}"' in installer
    assert 'if [[ ! -d "$path" ]]; then' in installer
    assert 'install -d -o videoroll -g videoroll -m 0750 "$path"' in installer
    assert 'runuser -u videoroll -- test -w "$work_check"' in installer
    assert 'chown -R videoroll:videoroll "$DATA_ROOT"' not in installer
    assert 'rm -rf "$DATA_ROOT"' not in installer


def test_native_roles_have_separate_units_and_dependent_token() -> None:
    target = (ROOT / "deploy/systemd/videoroll.target").read_text(encoding="utf-8")
    role = (ROOT / "deploy/systemd/videoroll@.service").read_text(encoding="utf-8")
    social = (ROOT / "deploy/systemd/videoroll-social@.service").read_text(encoding="utf-8")
    token = (ROOT / "deploy/systemd/videoroll-hatchet-token.service").read_text(encoding="utf-8")
    assert "Wants=videoroll-hatchet-token.service" in target
    assert "Requires=videoroll-redis.service videoroll-hatchet-token.service" in role
    assert "After=videoroll-hatchet.service" in token
    assert "Requires=videoroll-redis.service videoroll-hatchet-token.service" in social
    assert "Environment=PYTHONPATH=/opt/videoroll/current/src:" in role
    assert "Environment=PYTHONPATH=/opt/videoroll/current/src:" in social
    assert "PartOf=videoroll.target" in role


def test_service_selection_only_restarts_subtitle_for_subtitle_changes() -> None:
    assert set(ctl.changed_services([
        "src/videoroll/apps/subtitle_service/processing.py",
    ])) == set(ctl.SUBTITLE_UNITS)


def test_service_selection_only_restarts_orchestrator_for_orchestrator_changes() -> None:
    assert ctl.changed_services([
        "src/videoroll/apps/orchestrator_api/main.py",
    ]) == ("videoroll@orchestrator.service",)


def test_service_selection_all_app_units_for_shared_code() -> None:
    assert ctl.changed_services(["src/videoroll/config.py"]) == ctl.ROLE_UNITS


def test_service_selection_docs_does_not_restart() -> None:
    assert ctl.changed_services(["docs/README.md", "tests/test_xyz.py"]) == ()


@pytest.mark.parametrize("changed", [
    "pyproject.toml", "requirements.lock", "services/hatchet/go.mod",
    "deploy/systemd/videoroll@.service", "social-auto-upload",
    "scripts/native/install.sh", "src/web/package.json",
])
def test_hot_deploy_refuses_deps_static_or_unit_changes(changed: str) -> None:
    with pytest.raises(RuntimeError):
        ctl.changed_services([changed])


def test_install_default_does_not_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ctl, "run", lambda *args, **kwargs: pytest.fail(f"must not call {args}"),
    )
    ctl.install(apply=False)


def test_update_requires_a_release_and_never_pulls_live_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ctl, "run", lambda *args, **kwargs: pytest.fail("must not fetch"),
    )
    with pytest.raises(RuntimeError, match="--bundle"):
        ctl.update(apply=False)


def test_subtitle_change_restarts_transitive_consumers() -> None:
    services = ctl.changed_services(["src/videoroll/apps/subtitle_service/processing.py"])
    assert "videoroll@render-worker.service" in services
    assert "videoroll@outbox-dispatcher.service" in services


def test_legacy_update_script_delegates_to_videorollctl() -> None:
    shell = (ROOT / "scripts/native/update-code.sh").read_text(encoding="utf-8")
    assert 'videorollctl.py" update "$@"' in shell
    assert "systemctl restart videoroll.service" not in shell
