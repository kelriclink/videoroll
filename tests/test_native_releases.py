from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/native"))
import releases
from check_idle import busy_jobs


def make_source(root: Path) -> Path:
    for name in (*releases.SOURCE_FILES, "src/videoroll/__init__.py",
                 "scripts/native/videorollctl.py", "deploy/systemd/app.service",
                 "migrations/versions/initial.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# initial\n")
    (root / "data/web").mkdir(parents=True)
    (root / "data/web/index.html").write_text("old frontend")
    (root / "data/video.mp4").write_text("media must survive")
    (root / "social-auto-upload").mkdir()
    (root / ".env").write_text("SECRET=do-not-package")
    return root


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    old = make_source(tmp_path / "checkout")
    updated = tmp_path / "updated"
    shutil.copytree(old, updated)
    (updated / "src/videoroll/__init__.py").write_text("# new code\n")
    install = tmp_path / "install"
    install.mkdir()
    (install / "current").symlink_to(old, target_is_directory=True)
    commands = []
    ctl = SimpleNamespace(
        INSTALL_ROOT=install, CORE_PYTHON=Path(sys.executable), NATIVE=old / "scripts/native",
        ENV_FILE=old / ".env", UNIT_TARGET="videoroll.target",
        ROLE_UNITS=("videoroll@orchestrator.service", "videoroll@render-worker.service"),
        require_root=lambda: None, check_not_legacy=lambda: None,
        is_active=lambda unit: True,
        run=lambda *args, **kwargs: commands.append(args),
    )
    monkeypatch.setattr(releases, "require_idle", lambda ctl: None)
    monkeypatch.setattr(releases, "wait_healthy", lambda ctl: None)
    bundle = tmp_path / "app.tar.gz"
    return ctl, old, updated, bundle, commands


def test_bundle_roundtrip_excludes_data_credentials_and_caches(tmp_path):
    root = make_source(tmp_path / "source")
    cache = root / "src/videoroll/__pycache__"
    cache.mkdir()
    (cache / "private.pyc").write_bytes(b"cache")
    bundle = tmp_path / "app.tar.gz"
    manifest = releases.pack(root, bundle, web=root / "data/web")
    restored = releases.unpack(bundle, tmp_path / "restore")
    assert restored == manifest
    assert "web/index.html" in manifest["files"]
    assert not any(".env" == name or name.startswith("data/") or "__pycache__" in name for name in manifest["files"])
    assert bundle.with_name(bundle.name + ".sha256").read_text().startswith(releases.digest(bundle.read_bytes()))


@pytest.mark.parametrize("name,kind", [
    ("../escaped", tarfile.REGTYPE), ("/tmp/escaped", tarfile.REGTYPE),
    ("src/videoroll/link", tarfile.SYMTYPE), ("src/videoroll/link", tarfile.LNKTYPE),
    ("data/secret", tarfile.REGTYPE), ("src/videoroll/.env", tarfile.REGTYPE),
])
def test_unsafe_archives_are_rejected_without_leaving_files(tmp_path, name, kind):
    bundle = tmp_path / "bad.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd" if kind != tarfile.REGTYPE else ""
        archive.addfile(info, io.BytesIO())
    with pytest.raises(RuntimeError, match="Invalid archive"):
        releases.unpack(bundle, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()
    assert not (tmp_path / "escaped").exists()


def test_modified_content_fails_manifest_verification(tmp_path):
    source = make_source(tmp_path / "source")
    good, bad = tmp_path / "good.tar.gz", tmp_path / "bad.tar.gz"
    releases.pack(source, good)
    with tarfile.open(good, "r:gz") as incoming, tarfile.open(bad, "w:gz") as outgoing:
        for member in incoming:
            content = incoming.extractfile(member).read()
            if member.name == "src/videoroll/__init__.py":
                content = b"# corrupted\n"
            member.size = len(content)
            outgoing.addfile(member, io.BytesIO(content))
    with pytest.raises(RuntimeError, match="checksums"):
        releases.unpack(bad, tmp_path / "dest")


def test_preview_has_no_service_or_filesystem_mutations(deployment):
    ctl, old, updated, bundle, commands = deployment
    releases.pack(updated, bundle)
    releases.deploy(ctl, bundle, apply=False)
    assert commands == []
    assert (ctl.INSTALL_ROOT / "current").resolve() == old
    assert not (ctl.INSTALL_ROOT / "releases").exists()


@pytest.mark.parametrize("name,message", [
    ("requirements.lock", "Runtime"), ("migrations/versions/initial.py", "migrations"),
    ("scripts/native/videorollctl.py", "Runtime"),
])
def test_incompatible_release_refused_before_stopping_services(deployment, name, message):
    ctl, old, updated, bundle, commands = deployment
    (updated / name).write_text("# changed\n")
    releases.pack(updated, bundle)
    with pytest.raises(RuntimeError, match=message):
        releases.deploy(ctl, bundle, apply=True)
    assert commands == []
    assert (ctl.INSTALL_ROOT / "current").resolve() == old


def test_deploy_and_rollback_preserve_media_and_freeze_old_checkout(deployment):
    ctl, old, updated, bundle, commands = deployment
    (updated / "data/web/index.html").write_text("new frontend")
    releases.pack(updated, bundle, web=updated / "data/web")
    releases.deploy(ctl, bundle, apply=True)
    current = (ctl.INSTALL_ROOT / "current").resolve()
    previous = (ctl.INSTALL_ROOT / "previous").resolve()
    assert current.parent == previous.parent == ctl.INSTALL_ROOT / "releases"
    assert (current / "web/index.html").read_text() == "new frontend"
    assert not (current / "data").exists()  # Media tree is external; no symlink needed
    assert (current / "social-auto-upload").resolve() == old / "social-auto-upload"
    (old / "src/videoroll/__init__.py").write_text("# subsequent development")
    releases.rollback(ctl, apply=True)
    assert (ctl.INSTALL_ROOT / "current").resolve() == previous
    assert (previous / "src/videoroll/__init__.py").read_text() == "# initial\n"
    assert (previous / "web/index.html").read_text() == "old frontend"
    assert (old / "data/video.mp4").read_text() == "media must survive"
    for command in commands:
        assert "redis" not in " ".join(command)
        assert "migrate" not in " ".join(command)


def test_disabled_local_renderer_stays_off_during_upgrade_and_rollback(deployment):
    ctl, old, updated, bundle, commands = deployment
    ctl.is_active = lambda unit: unit != "videoroll@render-worker.service"
    releases.pack(updated, bundle)
    releases.deploy(ctl, bundle, apply=True)
    releases.rollback(ctl, apply=True)
    assert not any(
        "videoroll@render-worker.service" in command for command in commands
    ), "A release must not implicitly start a disabled local render node"
    assert any("videoroll@orchestrator.service" in command for command in commands)


def test_unhealthy_release_restores_previous_code(deployment, monkeypatch):
    ctl, old, updated, bundle, commands = deployment
    releases.pack(updated, bundle)
    calls = []
    def health(_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unhealthy")
    monkeypatch.setattr(releases, "wait_healthy", health)
    with pytest.raises(RuntimeError, match="unhealthy"):
        releases.deploy(ctl, bundle, apply=True)
    current = (ctl.INSTALL_ROOT / "current").resolve()
    assert (current / "src/videoroll/__init__.py").read_text() == "# initial\n"
    assert json.loads((ctl.INSTALL_ROOT / "deployment.json").read_text())["state"] == "reverted"
    assert len(calls) == 2


def test_busy_tasks_prevent_any_service_stop(deployment, monkeypatch):
    ctl, old, updated, bundle, commands = deployment
    releases.pack(updated, bundle)
    def busy(_):
        raise RuntimeError("busy")
    monkeypatch.setattr(releases, "require_idle", busy)
    with pytest.raises(RuntimeError, match="busy"):
        releases.deploy(ctl, bundle, apply=True)
    assert not any(command[:2] == ("systemctl", "stop") for command in commands)
    assert (ctl.INSTALL_ROOT / "current").resolve() == old


def test_idle_check_is_read_only_and_reports_unfinished_work():
    queries = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): queries.append(sql)
        def fetchone(self): return (2 if "subtitle_jobs" in queries[-1] else 0,)
    connection = SimpleNamespace(cursor=Cursor)
    assert busy_jobs(connection) == {"subtitle_jobs": 2}
    assert queries[0] == "SET TRANSACTION READ ONLY"
    assert all(sql.startswith(("SET ", "SELECT ")) for sql in queries)


def test_native_youdub_bridge_is_application_code_but_native_installer_stays_protected(tmp_path):
    before = make_source(tmp_path / "base")
    after = tmp_path / "updated"
    shutil.copytree(before, after)
    (before / "scripts/native/youdub_bridge.py").write_text("# original bridge\n")
    (after / "scripts/native/youdub_bridge.py").write_text("# fixed bridge\n")
    releases.verify_compatible(before, after)
    (before / "scripts/native/install.sh").write_text("# base installer\n")
    (after / "scripts/native/install.sh").write_text("# malicious altered installer\n")
    with pytest.raises(RuntimeError, match="Runtime inputs changed"):
        releases.verify_compatible(before, after)
    (after / "scripts/native/install.sh").write_text("# base installer\n")
    (before / "scripts/native/releases.py").write_text("# base deployer\n")
    (after / "scripts/native/releases.py").write_text("# modified deployer\n")
    with pytest.raises(RuntimeError, match="Runtime inputs changed"):
        releases.verify_compatible(before, after)
