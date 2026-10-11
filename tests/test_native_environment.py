from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "native"))

from bootstrap_env import build_environment  # noqa: E402
from migrate_env import build_native_environment  # noqa: E402


def test_bootstrap_password_always_meets_hatchet_requirements() -> None:
    for _ in range(30):
        env = build_environment(
            "postgresql+psycopg://u:password@localhost/videoroll",
            "postgresql://u:password@localhost/hatchet",
        )
        password = env["HATCHET_ADMIN_PASSWORD"]
        assert 8 <= len(password) <= 64
        assert re.search(r"[A-Z]", password)
        assert re.search(r"[a-z]", password)
        assert re.search(r"[0-9]", password)


def test_bootstrap_uses_data_directory_under_project_root() -> None:
    env = build_environment(
        "postgresql+psycopg://u:password@localhost/videoroll",
        "postgresql://u:password@localhost/hatchet",
    )
    data = ROOT / "data"
    assert env["STORAGE_ROOT"] == str(data / "storage" / "objects")
    assert env["WORK_DIR"] == str(data / "work" / "videoroll")
    assert env["SUBTITLE_DICTIONARY_DATA_DIR"] == str(data / "dictionaries")
    assert env["RENDER_WORKER_CREDENTIAL_FILE"] == str(data / "state" / "render-worker" / "credential")


def test_migration_rewrites_only_compose_hatchet_login_defaults(tmp_path: Path) -> None:
    old = tmp_path / "compose.env"
    old.write_text(
        "HATCHET_SERVER_URL=http://localhost:3000\n"
        "HATCHET_COOKIE_DOMAIN=localhost\n"
        "HATCHET_COOKIE_INSECURE=true\n",
        encoding="utf-8",
    )
    native = build_native_environment(old)
    assert native["HATCHET_SERVER_URL"] == "http://127.0.0.1:3001"
    assert native["HATCHET_FRONTEND_URL"] == "http://127.0.0.1:3001/workflow-ui/"
    assert native["HATCHET_COOKIE_DOMAIN"] == "127.0.0.1"

    old.write_text(
        "HATCHET_SERVER_URL=https://media.example.net\n"
        "HATCHET_FRONTEND_URL=https://media.example.net/workflow-ui/\n"
        "HATCHET_COOKIE_DOMAIN=media.example.net\n"
        "HATCHET_COOKIE_INSECURE=false\n",
        encoding="utf-8",
    )
    configured = build_native_environment(old)
    assert configured["HATCHET_SERVER_URL"] == "https://media.example.net"
    assert configured["HATCHET_COOKIE_DOMAIN"] == "media.example.net"
    assert configured["HATCHET_COOKIE_INSECURE"] == "false"

    old.write_text("DATABASE_URL=postgresql+psycopg://u:p@localhost/app\n", encoding="utf-8")
    missing = build_native_environment(old)
    assert missing["HATCHET_SERVER_URL"] == "http://127.0.0.1:3001"
    assert missing["HATCHET_COOKIE_DOMAIN"] == "127.0.0.1"


def test_old_container_directories_are_replaced_with_native_paths(tmp_path: Path) -> None:
    dotenv = tmp_path / "old.env"
    dotenv.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+psycopg://u:p@localhost/videoroll",
                "WORK_DIR=/work/videoroll",
                "SUBTITLE_DICTIONARY_DATA_DIR=/storage/state/dictionaries",
                "RENDER_WORKER_WORK_DIR=/work/render-worker",
                "SOCIAL_PUBLISH_WORK_DIR=/storage/work/social-publisher",
                "SAU_RUNTIME_DIR=/opt/social-auto-upload",
                "SAU_EXECUTABLE=/usr/local/bin/sau",
                "FFMPEG_PATH=/usr/local/bin/ffmpeg",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    native = build_native_environment(dotenv)
    data = ROOT / "data"
    assert native["STORAGE_ROOT"] == str(data / "storage" / "objects")
    assert native["WORK_DIR"] == str(data / "work" / "videoroll")
    assert native["RENDER_WORKER_WORK_DIR"] == str(data / "work" / "render-worker")
    assert native["SUBTITLE_DICTIONARY_DATA_DIR"] == str(data / "dictionaries")
    assert native["SOCIAL_PUBLISH_WORK_DIR"] == str(data / "work" / "social-publisher")
    assert native["SUBTITLE_WHISPER_MODEL_DIR"] == str(data / "models" / "whisper")
    assert native["PLAYOUT_MEDIA_ROOT"] == str(data / "storage" / "playout-media")
    assert native["SAU_RUNTIME_DIR"] == "/opt/videoroll/current/social-auto-upload"
    assert native["SAU_EXECUTABLE"] == "/opt/videoroll/venvs/social/bin/sau"
    assert native["FFMPEG_PATH"] == "/usr/bin/ffmpeg"


def test_external_data_root_preserves_existing_compose_storage_paths(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "original-data"
    for subdir in ("storage/work/videoroll", "storage/state/dictionaries", "storage/state/render-worker"):
        (data / subdir).mkdir(parents=True)
    source = tmp_path / "old.env"
    source.write_text("DATABASE_URL=postgresql+psycopg://u:p@localhost/app\n", encoding="utf-8")
    monkeypatch.setenv("VIDEOROLL_DATA_ROOT", str(data))
    env = build_native_environment(source)
    assert env["STORAGE_ROOT"] == str(data / "storage/objects")
    assert env["WORK_DIR"] == str(data / "storage/work/videoroll")
    assert env["SUBTITLE_DICTIONARY_DATA_DIR"] == str(data / "storage/state/dictionaries")
    assert env["RENDER_WORKER_CREDENTIAL_FILE"] == str(data / "storage/state/render-worker/credential")
    assert env["PLAYOUT_MEDIA_ROOT"] == str(data / "storage/playout-media")
