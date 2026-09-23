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


def test_production_compose_exposes_only_web_host_port() -> None:
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        assert set(services["web"].get("ports", ()))
        assert not services["hatchet-lite"].get("ports")
        assert {"8888", "7077"}.issubset(set(services["hatchet-lite"].get("expose", ())))
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
        "workflow-api": "videoroll.workflows.api:app",
        "workflow-worker": "videoroll.workflows.worker",
        "subtitle-workflow-worker": "videoroll.workflows.subtitle_worker",
        "render-worker": "videoroll.apps.render_worker.runtime",
        "subtitle-control-worker": "videoroll.apps.subtitle_service.worker:celery_app",
        "publish-worker": "videoroll.apps.bilibili_publisher.worker:celery_app",
        "egress-gateway": "videoroll.apps.egress_gateway.main:app",
    }
    healthchecked = {
        "orchestrator",
        "subtitle-service",
        "youtube-ingest",
        "bilibili-publisher",
        "outbox-dispatcher",
        "workflow-api",
        "subtitle-control-worker",
        "publish-worker",
        "egress-gateway",
    }
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        assert "subtitle-worker" not in services
        for name, expected in required.items():
            command = " ".join(str(part) for part in services[name]["command"])
            assert expected in command
        for name in healthchecked:
            assert services[name].get("healthcheck"), f"{path.name}:{name} lacks a health check"


def test_production_compose_uses_shared_filesystem_storage() -> None:
    for path in COMPOSE_FILES:
        compose = _compose(path)
        assert "minio" not in compose["services"]
        internal_env = compose["x-internal-environment"]
        assert internal_env["STORAGE_ROOT"] == "${STORAGE_ROOT:-/storage/objects}"
        for name in ("orchestrator", "subtitle-service", "subtitle-workflow-worker", "subtitle-control-worker", "publish-worker"):
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
        "workflow-api",
        "workflow-worker",
        "subtitle-workflow-worker",
        "render-worker",
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


def test_application_network_is_internal_and_egress_is_role_scoped() -> None:
    for path in COMPOSE_FILES:
        compose = _compose(path)
        assert (compose["networks"]["internal"] or {}).get("internal", False) is True
        assert "playout-egress" in compose["networks"]
        assert "infrastructure-egress" in compose["networks"]
        assert "web-ingress" in compose["networks"]
        assert set(compose["services"]["egress-gateway"]["networks"]) == {"internal", "egress"}

        for name in ("subtitle-service", "subtitle-control-worker"):
            assert set(compose["services"][name]["networks"]) == {"internal", "subtitle-egress"}
        assert set(compose["services"]["subtitle-workflow-worker"]["networks"]) == {
            "internal",
            "subtitle-egress",
            "infrastructure-egress",
        }
        for name in ("workflow-api", "workflow-worker"):
            assert set(compose["services"][name]["networks"]) == {"internal", "infrastructure-egress"}
        assert compose["services"]["render-worker"]["networks"] == ["internal"]
        assert "hatchet-postgres" not in compose["services"]
        assert set(compose["services"]["hatchet-lite"]["networks"]) == {"internal", "infrastructure-egress"}
        assert "host.docker.internal:host-gateway" in compose["services"]["hatchet-lite"].get("extra_hosts", [])
        assert compose["services"]["hatchet-lite"]["environment"]["DATABASE_URL"] == "${HATCHET_DATABASE_URL:?HATCHET_DATABASE_URL must be set}"
        assert compose["services"]["hatchet-lite"]["environment"]["LITE_FRONTEND_BASE_PATH"] == "/workflow-ui"
        assert compose["services"]["hatchet-lite"]["environment"]["SERVER_FRONTEND_URL"] == "${HATCHET_FRONTEND_URL:-http://localhost:3000/workflow-ui/}"
        assert compose["services"]["hatchet-lite"]["build"] == {
            "context": "./services/hatchet",
            "dockerfile": "Dockerfile.videoroll",
            "args": {"VERSION": "${HATCHET_VERSION:-v0.107.0}"},
        }
        assert compose["services"]["hatchet-lite"]["image"] == "videoroll-hatchet:prod"

        for name in ("workflow-api", "workflow-worker", "subtitle-workflow-worker"):
            environment = compose["services"][name]["environment"]
            assert environment["HATCHET_CLIENT_SERVER_URL"] == "${HATCHET_CLIENT_SERVER_URL:-http://hatchet-lite:8888}"
            assert environment["HATCHET_CLIENT_HOST_PORT"] == "${HATCHET_CLIENT_HOST_PORT:-hatchet-lite:7077}"
            assert environment["HATCHET_CLIENT_TLS_STRATEGY"] == "${HATCHET_CLIENT_TLS_STRATEGY:-none}"

        for name in (
            "orchestrator",
            "youtube-ingest",
            "bilibili-publisher",
            "publish-worker",
            "social-publisher-api",
            "social-publisher-worker",
            "social-publisher-scheduler",
        ):
            assert "internal" in compose["services"][name]["networks"]
            assert "platform-egress" in compose["services"][name]["networks"]

        assert set(compose["services"]["ffplayout"]["networks"]) == {"internal", "playout-egress"}
        assert set(compose["services"]["outbox-dispatcher"]["networks"]) == {
            "internal",
            "infrastructure-egress",
        }

        assert compose["services"]["redis"]["networks"] == ["internal"]
        assert set(compose["services"]["web"]["networks"]) == {"internal", "web-ingress"}


def test_subtitle_control_tasks_have_a_dedicated_worker_and_single_scheduler() -> None:
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        assert "subtitle-scheduler" not in services
        control_command = " ".join(str(part) for part in services["subtitle-control-worker"]["command"])
        assert "worker" in control_command
        assert "subtitle-control" in control_command
        assert services["outbox-dispatcher"]["command"] == ["outbox-dispatcher"]


def test_offline_production_compose_keeps_hatchet_and_control_plane() -> None:
    services = _compose(ROOT / "fromprod" / "docker-compose.yml")["services"]
    assert "subtitle-worker" not in services
    assert "hatchet-postgres" not in services
    for name in (
        "hatchet-lite",
        "workflow-api",
        "workflow-worker",
        "subtitle-workflow-worker",
        "render-worker",
        "subtitle-control-worker",
    ):
        assert name in services
    control_command = " ".join(str(part) for part in services["subtitle-control-worker"]["command"])
    assert "worker" in control_command
    assert "subtitle-control" in control_command
    assert "subtitle-control-worker" in services["outbox-dispatcher"].get("depends_on", {})

    normal = _compose(ROOT / "docker-compose.yml")["services"]["subtitle-workflow-worker"]
    offline = services["subtitle-workflow-worker"]
    assert offline["command"] == normal["command"]
    assert offline["environment"]["HATCHET_SUBTITLE_WORKER_SLOTS"] == normal["environment"]["HATCHET_SUBTITLE_WORKER_SLOTS"]


def test_offline_production_compose_keeps_runtime_tuning_environment() -> None:
    normal = _compose(ROOT / "docker-compose.yml")
    offline = _compose(ROOT / "fromprod" / "docker-compose.yml")
    for key in (
        "SUBTITLE_WHISPER_CPU_THREADS",
        "SUBTITLE_WHISPER_NUM_WORKERS",
        "CELERY_SUB_MAX_TASKS_PER_CHILD",
    ):
        assert offline["x-subtitle-environment"][key] == normal["x-subtitle-environment"][key]


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


def test_offline_bundle_includes_split_application_images() -> None:
    script = (ROOT / "scripts" / "build_export_prod.sh").read_text(encoding="utf-8")

    assert 'SUBTITLE_IMAGE="${SUBTITLE_IMAGE:-videoroll-subtitle:prod}"' in script
    assert 'WORKFLOW_IMAGE="${WORKFLOW_IMAGE:-videoroll-workflow:prod}"' in script
    assert 'SUBTITLE_WORKFLOW_IMAGE="${SUBTITLE_WORKFLOW_IMAGE:-videoroll-subtitle-workflow:prod}"' in script
    assert 'HATCHET_IMAGE="${HATCHET_IMAGE:-videoroll-hatchet:prod}"' in script
    assert 'EGRESS_IMAGE="${EGRESS_IMAGE:-videoroll-egress:prod}"' in script
    assert '-t "$SUBTITLE_IMAGE"' in script
    assert '-t "$WORKFLOW_IMAGE"' in script
    assert '-t "$SUBTITLE_WORKFLOW_IMAGE"' in script
    assert '--build-arg INSTALL_HATCHET="1"' in script
    assert '-t "$EGRESS_IMAGE"' in script
    assert 'FFPLAYOUT_IMAGE="${FFPLAYOUT_IMAGE:-videoroll-ffplayout:prod}"' in script
    assert '-f services/hatchet/Dockerfile.videoroll' in script
    assert 'services/hatchet' in script
    assert '"$HATCHET_IMAGE"' in script


def test_web_is_not_hard_blocked_on_optional_embedded_consoles() -> None:
    for path in COMPOSE_FILES:
        web = _compose(path)["services"]["web"]
        assert "ffplayout" not in web.get("depends_on", {})
        assert "hatchet-lite" not in web.get("depends_on", {})


def test_base_compose_does_not_require_an_intel_gpu() -> None:
    for path in (*COMPOSE_FILES, ROOT / "fromprod" / "docker-compose.yml"):
        services = _compose(path)["services"]
        for name in ("ffplayout", "orchestrator", "subtitle-service", "subtitle-workflow-worker", "render-worker"):
            service = services[name]
            assert not service.get("devices"), f"{path}:{name} unexpectedly requires /dev/dri"
            assert not service.get("group_add"), f"{path}:{name} unexpectedly requires an Intel render group"


def test_intel_override_owns_all_gpu_device_mappings() -> None:
    services = _compose(ROOT / "docker-compose.intel.yml")["services"]
    for name in ("ffplayout", "orchestrator", "subtitle-service", "render-worker"):
        service = services[name]
        assert "/dev/dri:/dev/dri" in service.get("devices", [])
        assert service.get("group_add") == ["${INTEL_GPU_RENDER_GID:-992}"]


def test_legacy_live_environment_is_removed_from_compose() -> None:
    for path in COMPOSE_FILES:
        orchestrator = _compose(path)["services"]["orchestrator"]
        assert "LEGACY_LIVE_ENABLED" not in orchestrator["environment"]
        assert "LIVE_INTERNAL_STREAM_BASE_URL" not in orchestrator["environment"]


def test_playout_proxy_uses_dynamic_docker_dns_and_frame_ancestors() -> None:
    nginx = (ROOT / "src" / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "resolver 127.0.0.11" in nginx
    assert "proxy_pass $ffplayout_upstream;" in nginx
    assert "frame-ancestors 'self' http://$host:* https://$host:*" in nginx


def test_hatchet_dashboard_uses_single_port_subpath_proxy() -> None:
    nginx = (ROOT / "src" / "web" / "nginx.conf").read_text(encoding="utf-8")
    urls = (ROOT / "src" / "web" / "src" / "lib" / "urls.ts").read_text(encoding="utf-8")

    assert "set $hatchet_upstream http://hatchet-lite:8888;" in nginx
    assert "location ^~ /workflow-ui/" in nginx
    assert "location ^~ /api/v1/" in nginx
    assert "location = /api/ready" in nginx
    assert "location = /api/live" in nginx
    assert "proxy_pass $hatchet_upstream$request_uri;" in nginx
    assert "proxy_hide_header X-Frame-Options;" in nginx
    assert 'return "/workflow-ui/";' in urls
    assert 'url.port = "8888"' not in urls


def test_vendored_hatchet_version_keeps_native_subpath_support() -> None:
    vendor = (ROOT / "services" / "hatchet" / "VENDOR_VERSION").read_text(encoding="utf-8")
    lite_main = (ROOT / "services" / "hatchet" / "cmd" / "hatchet-lite" / "main.go").read_text(encoding="utf-8")
    vite = (ROOT / "services" / "hatchet" / "frontend" / "app" / "vite.config.ts").read_text(encoding="utf-8")
    dockerfile = (ROOT / "services" / "hatchet" / "Dockerfile.videoroll").read_text(encoding="utf-8")

    assert "tag=v0.107.0" in vendor
    assert "commit=d6c9e6849526b0d8a51d97bf2f5b6dcfdfee0179" in vendor
    assert 'os.Getenv("LITE_FRONTEND_BASE_PATH")' in lite_main
    assert "{{ .BasePath }}" in vite
    assert "LITE_FRONTEND_BASE_PATH=/workflow-ui" in dockerfile


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
