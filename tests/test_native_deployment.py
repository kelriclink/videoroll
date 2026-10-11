from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_target_contains_core_roles_but_not_social_publisher() -> None:
    target = (ROOT / "deploy/systemd/videoroll.target").read_text(encoding="utf-8")
    for role in (
        "orchestrator",
        "egress-gateway",
        "subtitle-service",
        "youtube-ingest",
        "bilibili-publisher",
        "workflow-api",
        "workflow-worker",
        "subtitle-workflow-worker",
        "subtitle-control-worker",
        "publish-worker",
        "outbox-dispatcher",
    ):
        assert f"videoroll@{role}.service" in target
    assert "social-publisher" not in target
    # Local render worker is optional; external Rust nodes remain primary.
    assert "Wants=videoroll@render-worker.service" not in target


def test_native_http_services_bind_loopback_only() -> None:
    launcher = (ROOT / "scripts/native/run-role.sh").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" not in launcher
    for port in (8000, 8001, 8002, 8003, 8020, 8030):
        assert f"--host 127.0.0.1 --port {port}" in launcher


def test_native_runtime_keeps_hatchet_dependency_isolation() -> None:
    installer = (ROOT / "scripts/native/install.sh").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts/native/run-role.sh").read_text(encoding="utf-8")
    assert "venvs/core" in launcher
    assert "venvs/subtitle" in launcher
    assert "venvs/workflow" in launcher
    assert "venvs/subtitle-workflow" in launcher
    assert '"protobuf==6.33.6"' in installer
    assert '"hatchet-sdk==1.40.3"' in installer


def test_native_environment_uses_loopback_service_urls() -> None:
    env = (ROOT / "deploy/systemd/videoroll.env.example").read_text(encoding="utf-8")
    assert "REDIS_URL=redis://127.0.0.1:6379/0" in env
    assert "ORCHESTRATOR_URL=http://127.0.0.1:8000" in env
    assert "HATCHET_CLIENT_SERVER_URL=http://127.0.0.1:8888" in env
    assert "HATCHET_CLIENT_HOST_PORT=127.0.0.1:7077" in env
    assert "INTERNAL_API_SECRET=CHANGE_ME" in env
    assert "ADMIN_BOOTSTRAP_SECRET=CHANGE_ME" in env


def test_hatchet_companion_exposes_only_loopback_ports() -> None:
    compose = (ROOT / "deploy/systemd/hatchet.compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8888:8888"' in compose
    assert '"127.0.0.1:7077:7077"' in compose
    assert "0.0.0.0:8888" not in compose
    assert "0.0.0.0:7077" not in compose


def test_native_nginx_has_no_docker_dns_upstreams() -> None:
    nginx = (ROOT / "deploy/systemd/nginx-videoroll.conf").read_text(encoding="utf-8")
    assert "127.0.0.11" not in nginx
    assert "server 127.0.0.1:8000;" in nginx
    assert "http://127.0.0.1:8787" in nginx
    assert "http://127.0.0.1:8888" in nginx
    assert "http://127.0.0.1:6080" in nginx
    assert "http://127.0.0.1:6081" in nginx
    assert "root /opt/videoroll/current/web;" in nginx

    assert "listen 127.0.0.1:3001;" in nginx


def test_native_redis_companion_uses_fresh_rebuildable_volume() -> None:
    compose = (ROOT / "deploy/systemd/redis.compose.yml").read_text(encoding="utf-8")
    assert "redis:7" in compose
    assert '"redis-server", "--appendonly", "yes"' in compose
    assert '"127.0.0.1:6379:6379"' in compose
    assert "videoroll-redis-data:/data" in compose
    assert "/data/redis:/data" not in compose


def test_social_companion_is_loopback_reachable_without_vnc_port_collisions() -> None:
    compose = (ROOT / "deploy/systemd/social.compose.yml").read_text(encoding="utf-8")
    assert "network_mode: host" in compose
    assert "REDIS_URL: redis://127.0.0.1:6379/0" in compose
    assert "SOCIAL_VNC_WEBSOCKIFY_PORT: \"6080\"" in compose
    assert "SOCIAL_VNC_WEBSOCKIFY_PORT: \"6081\"" in compose
    assert "SOCIAL_VNC_DISABLED: \"true\"" in compose


def test_cutover_starts_native_infrastructure_and_enables_after_health() -> None:
    cutover = (ROOT / "scripts/native/cutover.sh").read_text(encoding="utf-8")
    assert "scripts/native/companions.sh" not in cutover
    assert "start_native_infra.sh" in cutover
    assert "systemctl start videoroll.target" in cutover
    assert "systemctl enable videoroll.target" in cutover
    assert cutover.index("start_native_infra.sh") < cutover.index("systemctl start videoroll.target")
    assert cutover.index("systemctl start videoroll.target") < cutover.index("systemctl enable videoroll.target")


def test_native_installer_does_not_enable_stack_before_cutover() -> None:
    installer = (ROOT / "scripts/native/install.sh").read_text(encoding="utf-8")
    assert "systemctl enable videoroll.target" not in installer
    assert "redis-server.service" in installer
    assert "systemctl disable redis-server.service" in installer
    assert "systemctl enable redis-server.service" not in installer
    assert "policy-rc.d" in installer
    assert 'APP_UID="${APP_UID:-10001}"' in installer
    assert 'APP_GID="${APP_GID:-10001}"' in installer


def test_db_only_deployment_has_no_filesystem_secret_dependency() -> None:
    paths = [
        ROOT / "deploy/systemd/videoroll.env.example",
        ROOT / "deploy/systemd/social.compose.yml",
        ROOT / "scripts/native/install.sh",
        ROOT / "scripts/native/restore_from_database.sh",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "VIDEOROLL_SECRETS_DIR" not in combined
    assert "/secrets" not in combined
    assert "fernet.key" not in combined


def test_database_only_restore_requires_only_business_db_plus_rebuildable_hatchet_db() -> None:
    restore = (ROOT / "scripts/native/restore_from_database.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/native/bootstrap_env.py").read_text(encoding="utf-8")
    assert "DATABASE_URL is required" in restore
    assert "HATCHET_DATABASE_URL" in restore
    assert "INTERNAL_API_SECRET" in bootstrap
    assert "secrets.token_urlsafe" in bootstrap


def test_native_target_includes_redis_hatchet_and_social_units() -> None:
    target = (ROOT / "deploy/systemd/videoroll.target").read_text(encoding="utf-8")
    assert "Wants=videoroll-redis.service" in target
    assert "Wants=videoroll-hatchet.service" in target
    for role in ("api", "worker", "scheduler"):
        assert f"Wants=videoroll-social@{role}.service" in target


def test_native_social_uses_separate_displays_and_loopback_vnc() -> None:
    launcher = (ROOT / "scripts/native/run-social-role.sh").read_text(encoding="utf-8")
    assert "start_display_stack :99 5900 5901 6080" in launcher
    assert "start_display_stack :100 5910 5911 6081" in launcher
    assert '"127.0.0.1:$websockify_port"' in launcher
    assert 'SAU_COOKIES_DIR="$RUNTIME_DIR/cookies"' in launcher


def test_native_redis_is_private_and_rebuildable() -> None:
    config = (ROOT / "deploy/systemd/redis.conf").read_text(encoding="utf-8")
    unit = (ROOT / "deploy/systemd/videoroll-redis.service").read_text(encoding="utf-8")
    assert "bind 127.0.0.1" in config
    assert "appendonly yes" in config
    assert "ReadWritePaths=/opt/videoroll/current/data/redis-native" in unit


def test_native_hatchet_uses_generated_source_build() -> None:
    dockerfile = (ROOT / "services/hatchet/Dockerfile.videoroll").read_text(encoding="utf-8")
    build = (ROOT / "scripts/native/build_hatchet.sh").read_text(encoding="utf-8")
    assert "go generate ./..." in dockerfile
    assert "hack/proto/proto.sh" in dockerfile
    assert 'docker build --file "$SRC/Dockerfile.videoroll"' in build
    assert 'docker cp "$CID:/$binary"' in build
    assert "docker run --detach" not in build


def test_database_only_restore_has_no_docker_runtime() -> None:
    restore = (ROOT / "scripts/native/restore_from_database.sh").read_text(encoding="utf-8")
    assert "companions.sh" not in restore
    assert "start_native_infra.sh" in restore
    assert "systemctl start videoroll.target" in restore


def test_in_place_recovery_can_preserve_file_metadata() -> None:
    recovery = (ROOT / "scripts/native/db_only_recovery.py").read_text(encoding="utf-8")
    assert "--preserve-files" in recovery
    assert "if preserve_files:" in recovery
