from __future__ import annotations

from pathlib import Path

import yaml

from videoroll.deployment import validate_deployment_secrets


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (ROOT / "compose.yml", ROOT / "docker-compose.yml")


def _compose(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_production_compose_has_only_web_host_port() -> None:
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        assert set(services["web"].get("ports", ()))
        for name, service in services.items():
            if name != "web":
                assert not service.get("ports"), f"{path.name}:{name} exposes a host port"


def test_process_roles_are_not_combined() -> None:
    required = {
        "orchestrator": "videoroll.apps.orchestrator_api.main:app",
        "subtitle-service": "videoroll.apps.subtitle_service.main:app",
        "youtube-ingest": "videoroll.apps.youtube_ingest.main:app",
        "bilibili-publisher": "videoroll.apps.bilibili_publisher.main:app",
        "outbox-dispatcher": "outbox-dispatcher",
        "subtitle-worker": "videoroll.apps.subtitle_service.worker:celery_app",
        "subtitle-control-worker": "videoroll.apps.subtitle_service.worker:celery_app",
        "publish-worker": "videoroll.apps.bilibili_publisher.worker:celery_app",
        "egress-gateway": "videoroll.apps.egress_gateway.main:app",
    }
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        for name, expected in required.items():
            command = " ".join(str(part) for part in services[name]["command"])
            assert expected in command
            assert services[name].get("healthcheck"), f"{path.name}:{name} lacks a health check"


def test_production_compose_uses_shared_filesystem_storage() -> None:
    for path in COMPOSE_FILES:
        compose = _compose(path)
        assert "minio" not in compose["services"]
        internal_env = compose["x-internal-environment"]
        assert internal_env["STORAGE_ROOT"] == "${STORAGE_ROOT:-/storage/objects}"
        for name in ("orchestrator", "subtitle-service", "subtitle-worker", "subtitle-control-worker", "publish-worker"):
            assert any("/storage" in str(item) for item in compose["services"][name].get("volumes", ()))


def test_playout_media_root_is_explicit_and_shared_with_ffplayout() -> None:
    for path in (*COMPOSE_FILES, ROOT / "fromprod" / "docker-compose.yml"):
        compose = _compose(path)
        orchestrator = compose["services"]["orchestrator"]
        assert orchestrator["environment"]["PLAYOUT_MEDIA_ROOT"] == "${PLAYOUT_MEDIA_ROOT:-/storage/playout-media}"
        assert "${STORAGE_HOST_ROOT:-./data/storage}:/storage" in orchestrator.get("volumes", [])
        assert "${STORAGE_HOST_ROOT:-./data/storage}/playout-media:/var/lib/ffplayout/media" in compose["services"]["ffplayout"].get("volumes", [])


def test_application_roles_resolve_host_database_gateway() -> None:
    # Proxy trust is part of the production login-rate-limit boundary. Keep the
    # offline production Compose aligned with the normal Compose files.
    for path in (*COMPOSE_FILES, ROOT / "fromprod" / "docker-compose.yml"):
        orchestrator = _compose(path)["services"]["orchestrator"]
        assert orchestrator["environment"]["TRUSTED_PROXY_HOSTS"] == "${TRUSTED_PROXY_HOSTS:-web}"
        assert orchestrator["environment"]["TRUSTED_PROXY_CIDRS"] == "${TRUSTED_PROXY_CIDRS:-}"

    application_roles = {
        "orchestrator",
        "subtitle-service",
        "youtube-ingest",
        "bilibili-publisher",
        "subtitle-worker",
        "subtitle-control-worker",
        "outbox-dispatcher",
        "publish-worker",
        "social-publisher-api",
        "social-publisher-worker",
        "social-publisher-scheduler",
    }

    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        for name in application_roles:
            assert "host.docker.internal:host-gateway" in services[name].get("extra_hosts", [])


def test_rag_processes_stay_on_the_application_network() -> None:
    for path in COMPOSE_FILES:
        compose = _compose(path)
        assert not (compose["networks"]["internal"] or {}).get("internal", False)
        assert set(compose["services"]["egress-gateway"]["networks"]) == {"internal", "egress"}
        for name in ("subtitle-service", "subtitle-worker", "subtitle-control-worker"):
            assert compose["services"][name]["networks"] == ["internal"]


def test_subtitle_control_tasks_have_a_dedicated_worker_and_single_scheduler() -> None:
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        assert "subtitle-scheduler" not in services
        control_command = " ".join(str(part) for part in services["subtitle-control-worker"]["command"])
        assert "worker" in control_command
        assert "subtitle-control" in control_command
        assert services["outbox-dispatcher"]["command"] == ["outbox-dispatcher"]


def test_offline_production_compose_keeps_the_control_worker() -> None:
    services = _compose(ROOT / "fromprod" / "docker-compose.yml")["services"]
    assert "subtitle-control-worker" in services
    control_command = " ".join(str(part) for part in services["subtitle-control-worker"]["command"])
    assert "worker" in control_command
    assert "subtitle-control" in control_command
    assert "subtitle-control-worker" in services["outbox-dispatcher"].get("depends_on", {})
    assert "subtitle-worker" not in services["outbox-dispatcher"].get("depends_on", {})

    normal = _compose(ROOT / "docker-compose.yml")["services"]["subtitle-worker"]
    offline = services["subtitle-worker"]
    assert offline["command"] == normal["command"]
    for key in ("CELERY_SUB_CONCURRENCY_FALLBACK", "CELERY_SUB_CONCURRENCY_DB_ATTEMPTS", "CELERY_SUB_CONCURRENCY_DB_DELAY_SECONDS"):
        assert offline["environment"][key] == normal["environment"][key]


def test_offline_production_compose_keeps_runtime_tuning_environment() -> None:
    normal = _compose(ROOT / "docker-compose.yml")
    offline = _compose(ROOT / "fromprod" / "docker-compose.yml")
    for key in (
        "SUBTITLE_WHISPER_CPU_THREADS",
        "SUBTITLE_WHISPER_NUM_WORKERS",
        "CELERY_SUB_MAX_TASKS_PER_CHILD",
    ):
        assert offline["x-subtitle-environment"][key] == normal["x-subtitle-environment"][key]
    assert offline["services"]["orchestrator"]["environment"]["LIVE_INTERNAL_STREAM_BASE_URL"] == normal["services"]["orchestrator"]["environment"]["LIVE_INTERNAL_STREAM_BASE_URL"]


def test_production_rejects_empty_or_known_default_secrets() -> None:
    assert not validate_deployment_secrets(
        {
            "S3_ACCESS_KEY_ID": "videoroll",
            "S3_SECRET_ACCESS_KEY": "videorollsecret",
            "INTERNAL_API_SECRET": "",
            "ADMIN_BOOTSTRAP_SECRET": "",
        },
        production=True,
    )
    assert validate_deployment_secrets(
        {
            "S3_ACCESS_KEY_ID": "storage-user-7f",
            "S3_SECRET_ACCESS_KEY": "a7e8d9f0",
            "INTERNAL_API_SECRET": "e1f2a3b4",
            "ADMIN_BOOTSTRAP_SECRET": "b4a3f2e1",
        },
        production=True,
    )


def test_entrypoints_do_not_start_passwordless_vnc_or_multiple_roles() -> None:
    app_entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    social_entrypoint = (ROOT / "docker" / "social-publisher-entrypoint.sh").read_text(encoding="utf-8")

    assert "wait -n" not in app_entrypoint
    assert "exec \"$@\"" in app_entrypoint
    assert "-nopw" not in social_entrypoint
    assert "-rfbauth" in social_entrypoint
    assert "/dev/shm" in social_entrypoint


def test_offline_bundle_includes_egress_gateway_image() -> None:
    script = (ROOT / "scripts" / "build_export_prod.sh").read_text(encoding="utf-8")

    assert 'EGRESS_IMAGE="${EGRESS_IMAGE:-videoroll-egress:prod}"' in script
    assert '-t "$EGRESS_IMAGE"' in script
    assert 'FFPLAYOUT_IMAGE="${FFPLAYOUT_IMAGE:-videoroll-ffplayout:prod}"' in script
    assert 'IMAGES=("$APP_IMAGE" "$EGRESS_IMAGE" "$WEB_IMAGE" "$SOCIAL_IMAGE" "$FFPLAYOUT_IMAGE")' in script


def test_web_is_not_hard_blocked_on_ffplayout_health() -> None:
    for path in COMPOSE_FILES:
        web = _compose(path)["services"]["web"]
        assert "ffplayout" not in web.get("depends_on", {})


def test_base_compose_does_not_require_an_intel_gpu() -> None:
    for path in (*COMPOSE_FILES, ROOT / "fromprod" / "docker-compose.yml"):
        services = _compose(path)["services"]
        for name in ("ffplayout", "orchestrator", "subtitle-service", "subtitle-worker"):
            service = services[name]
            assert not service.get("devices"), f"{path}:{name} unexpectedly requires /dev/dri"
            assert not service.get("group_add"), f"{path}:{name} unexpectedly requires an Intel render group"


def test_intel_override_owns_all_gpu_device_mappings() -> None:
    services = _compose(ROOT / "docker-compose.intel.yml")["services"]
    for name in ("ffplayout", "orchestrator", "subtitle-service", "subtitle-worker"):
        service = services[name]
        assert "/dev/dri:/dev/dri" in service.get("devices", [])
        assert service.get("group_add") == ["${INTEL_GPU_RENDER_GID:-992}"]


def test_legacy_live_is_disabled_by_default_in_compose() -> None:
    for path in COMPOSE_FILES:
        orchestrator = _compose(path)["services"]["orchestrator"]
        assert orchestrator["environment"]["LEGACY_LIVE_ENABLED"] == "${LEGACY_LIVE_ENABLED:-false}"


def test_playout_proxy_uses_dynamic_docker_dns_and_frame_ancestors() -> None:
    nginx = (ROOT / "src" / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "resolver 127.0.0.11" in nginx
    assert "proxy_pass $ffplayout_upstream;" in nginx
    assert "frame-ancestors 'self' http://$host:* https://$host:*" in nginx


def test_web_proxy_preserves_outer_https_scheme_for_secure_cookies() -> None:
    nginx = (ROOT / "src" / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "map $http_x_forwarded_proto $videoroll_forwarded_proto" in nginx
    assert "proxy_set_header X-Forwarded-Proto $videoroll_forwarded_proto;" in nginx
    assert "proxy_set_header X-Forwarded-Proto $scheme;" not in nginx
    assert 'https "; Secure";' in nginx


def test_prepare_prod_dirs_script_covers_ffplayout_persistent_paths() -> None:
    script = (ROOT / "scripts" / "prepare_prod_dirs.sh").read_text(encoding="utf-8")
    for expected in (
        "data/ffplayout/db",
        "data/ffplayout/logs",
        "data/ffplayout/playlists",
        "data/ffplayout/public",
        '"$STORAGE_HOST_ROOT/playout-media"',
    ):
        assert expected in script
