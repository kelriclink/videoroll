from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import uuid

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_root_build_context_excludes_local_social_credentials() -> None:
    # Both Compose and the export script build the social image from this root,
    # so the submodule's own .dockerignore cannot protect its runtime files.
    exclusions = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        "**/.venv",
        "**/__pycache__",
        "**/*.py[cod]",
        "social-auto-upload/cookies",
        "social-auto-upload/cookiesFile",
        "social-auto-upload/logs",
        "social-auto-upload/db/*.db",
        "social-auto-upload/db/*.db-*",
        "social-auto-upload/conf.py",
        "social-auto-upload/.env",
        "services/ffplayout",
        "social-auto-upload-kelric",
    }
    assert required <= exclusions, f"unprotected runtime files: {sorted(required - exclusions)}"


@pytest.mark.skipif(
    os.getenv("VIDEOROLL_DOCKER_CONTEXT_TESTS") != "1",
    reason="set VIDEOROLL_DOCKER_CONTEXT_TESTS=1 to run the isolated Docker context test",
)
def test_docker_context_excludes_runtime_secrets_and_nested_caches(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    assert docker is not None, "the opt-in context test requires Docker"
    context = tmp_path / "context"
    context.mkdir()
    # Never use the repository as a Docker context. Only its ignore rules are
    # copied; every other input below is artificial and contains no credentials.
    shutil.copyfile(ROOT / ".dockerignore", context / ".dockerignore")
    (context / "Dockerfile").write_text("FROM scratch\nCOPY . /context\n", encoding="utf-8")
    source_files = {
        "src/videoroll/config.py",
        "social-auto-upload/sau_cli.py",
        "social-auto-upload/conf.example.py",
        "social-auto-upload/utils/log.py",
        "social-auto-upload/db/create_table.py",
    }
    runtime_files = {
        ".venv/lib/local_config.py",
        "__pycache__/root.cpython-312.pyc",
        "root.pyc",
        "src/videoroll/__pycache__/config.cpython-312.pyc",
        "social-auto-upload/.venv/lib/local_config.py",
        "social-auto-upload/nested/.venv/lib/local_config.py",
        "social-auto-upload/__pycache__/conf.cpython-312.pyc",
        "social-auto-upload/utils/__pycache__/log.cpython-312.pyc",
        "social-auto-upload/nested/conf.pyc",
        "social-auto-upload/nested/conf.pyo",
        "social-auto-upload/nested/local.pyd",
        "social-auto-upload/cookies/offline-test.json",
        "social-auto-upload/cookiesFile/offline-test.json",
        "social-auto-upload/conf.py",
        "social-auto-upload/.env",
        "social-auto-upload/.env.local",
        "social-auto-upload/logs/offline-test.log",
        "social-auto-upload/db/database.db",
        "social-auto-upload/db/database.db-wal",
    }
    for relative_path in source_files | runtime_files:
        target = context / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# artificial offline Docker context fixture\n", encoding="utf-8")

    suffix = uuid.uuid4().hex
    image_name = f"videoroll-context-test:{suffix}"
    container_name = f"videoroll-context-test-{suffix}"
    image_created = container_created = False

    def run_docker(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [docker, *args], cwd=context, capture_output=True, check=False, timeout=30,
        )

    try:
        result = run_docker("build", "--network=none", "--pull=false", "--no-cache", "-t", image_name, ".")
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        image_created = True
        # Creation/export inspect the filesystem without running any process.
        result = run_docker("create", "--name", container_name, image_name, "/not-executed")
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        container_created = True
        result = run_docker("export", container_name)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            exported = {entry.name.removeprefix("./") for entry in archive if entry.isfile()}
        assert {f"context/{path}" for path in source_files} <= exported
        leaked = {path for path in runtime_files if f"context/{path}" in exported}
        assert not leaked, f"Docker copied excluded runtime fixtures into the image: {sorted(leaked)}"
    finally:
        failures = []
        if container_created:
            result = run_docker("rm", "-f", container_name)
            if result.returncode:
                failures.append(result.stderr.decode("utf-8", errors="replace"))
        if image_created:
            result = run_docker("image", "rm", "-f", image_name)
            if result.returncode:
                failures.append(result.stderr.decode("utf-8", errors="replace"))
        assert not failures, f"failed to clean this test's Docker resources: {failures}"


@pytest.mark.parametrize("filename", ["compose.yml", "docker-compose.yml"])
def test_compose_builds_every_application_image_for_its_runtime_user(filename: str) -> None:
    compose = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        if "user" not in service:
            continue
        args = service["build"].get("args", {})
        assert args.get("APP_UID") == "${APP_UID:-10001}", name
        assert args.get("APP_GID") == "${APP_GID:-10001}", name
        assert service["user"] == "${APP_UID:-10001}:${APP_GID:-10001}", name


@pytest.mark.parametrize("filename", ["compose.yml", "docker-compose.yml"])
def test_compose_trusts_web_proxy_by_default_without_trusting_the_whole_network(filename: str) -> None:
    compose = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))
    environment = compose["services"]["orchestrator"]["environment"]
    assert environment.get("TRUSTED_PROXY_HOSTS") == "${TRUSTED_PROXY_HOSTS:-web}"
    assert environment.get("TRUSTED_PROXY_CIDRS") == "${TRUSTED_PROXY_CIDRS:-}"


def _user_creation_layer(dockerfile: str) -> str:
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    return next(line.removeprefix("RUN ") for line in logical_lines if line.startswith("RUN ") and "useradd" in line)


@pytest.mark.parametrize("filename", ["Dockerfile", "docker/social-publisher.Dockerfile"])
def test_image_user_creation_uses_build_ids_even_when_the_base_image_already_has_them(
    tmp_path: Path, filename: str,
) -> None:
    dockerfile = (ROOT / filename).read_text(encoding="utf-8")
    command_log = tmp_path / "commands.jsonl"
    recorder = tmp_path / "record-command"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['UID_TEST_COMMAND_LOG'], 'a') as output:\n"
        "    output.write(json.dumps([name, *args]) + '\\n')\n"
        "if name in {'groupadd', 'useradd'}:\n"
        "    flag = '--gid' if name == 'groupadd' else '--uid'\n"
        "    value = args[args.index(flag) + 1]\n"
        "    if value in {'1000', '1002'} and not {'-o', '--non-unique'}.intersection(args):\n"
        "        sys.exit(9)  # These numeric IDs already exist in the base image.\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    for name in ("groupadd", "useradd", "install", "chown"):
        (tmp_path / name).symlink_to(recorder)
    result = subprocess.run(
        ["sh", "-ec", _user_creation_layer(dockerfile)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
             "APP_UID": "1000", "APP_GID": "1002", "UID_TEST_COMMAND_LOG": str(command_log)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()]
    user = next(args for name, *args in commands if name == "useradd")
    group = next((args for name, *args in commands if name == "groupadd"), [])
    assert user[user.index("--uid") + 1] == "1000"
    assert "--gid" in group and group[group.index("--gid") + 1] == "1002"
    assert user[user.index("--gid") + 1] == "videoroll"
    assert re.search(r"(?m)^ARG APP_UID=10001$", dockerfile)
    assert re.search(r"(?m)^ARG APP_GID=10001$", dockerfile)


@pytest.mark.parametrize("uid,gid", [("1000", "1002"), ("", "")])
def test_export_script_passes_the_same_uid_and_gid_to_all_application_builds(
    tmp_path: Path, uid: str, gid: str,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/build_export_prod.sh", scripts / "build_export_prod.sh")
    (tmp_path / "social-auto-upload").mkdir()
    (tmp_path / "social-auto-upload/sau_cli.py").touch()
    env_file = tmp_path / "build.env"
    env_file.write_text(
        f"APP_UID={uid}\nAPP_GID={gid}\nVITE_FFPLAYOUT_URL=https://playout.example.test\n",
        encoding="utf-8",
    )
    command_log = tmp_path / "docker-commands.jsonl"
    docker = tmp_path / "docker-stub"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['DOCKER_TEST_COMMAND_LOG'], 'a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'save':\n"
        "    pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'offline-test-image')\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(scripts / "build_export_prod.sh")], cwd=tmp_path,
        env={**os.environ, "ENV_FILE": str(env_file), "DOCKER_BIN": str(docker),
             "DOCKER_TEST_COMMAND_LOG": str(command_log), "INCLUDE_BASE_IMAGES": "0",
             "OUTPUT_TAR": str(tmp_path / "images.tar")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()]
    application_builds = [args for args in commands if args[0] == "build" and args[args.index("-f") + 1] != "src/web/Dockerfile"]
    assert len(application_builds) == 5
    for args in application_builds:
        build_args = {args[index + 1] for index, value in enumerate(args) if value == "--build-arg"}
        assert f"APP_UID={uid or '10001'}" in build_args
        assert f"APP_GID={gid or '10001'}" in build_args
