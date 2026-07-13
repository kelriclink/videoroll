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
        "publish-worker": "videoroll.apps.bilibili_publisher.worker:celery_app",
        "egress-gateway": "videoroll.apps.egress_gateway.main:app",
    }
    for path in COMPOSE_FILES:
        services = _compose(path)["services"]
        for name, expected in required.items():
            command = " ".join(str(part) for part in services[name]["command"])
            assert expected in command
            assert services[name].get("healthcheck"), f"{path.name}:{name} lacks a health check"


def test_minio_healthcheck_uses_supported_readiness_endpoint() -> None:
    expected = ["CMD", "curl", "-fsS", "http://127.0.0.1:9000/minio/health/ready"]

    for path in COMPOSE_FILES:
        healthcheck = _compose(path)["services"]["minio"]["healthcheck"]
        assert healthcheck["test"] == expected


def test_rag_processes_have_no_direct_egress_network() -> None:
    for path in COMPOSE_FILES:
        compose = _compose(path)
        assert compose["networks"]["internal"]["internal"] is True
        assert set(compose["services"]["egress-gateway"]["networks"]) == {"internal", "egress"}
        for name in ("subtitle-service", "subtitle-worker"):
            assert compose["services"][name]["networks"] == ["internal"]


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
    assert 'IMAGES=("$APP_IMAGE" "$EGRESS_IMAGE" "$WEB_IMAGE" "$SOCIAL_IMAGE")' in script
