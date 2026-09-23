import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(service)}:\n", compose)
    assert match is not None, f"service not found: {service}"
    start = match.start()
    following = compose[match.end() :]
    next_service = re.search(r"^  [a-z0-9][a-z0-9-]*:\n", following, re.MULTILINE)
    return compose[start:] if next_service is None else compose[start : match.end() + next_service.start()]


def test_application_dockerfiles_default_to_core_runtime() -> None:
    for relative_path in ("Dockerfile", "deploy_compose/Dockerfile"):
        dockerfile = _text(relative_path)
        assert "ARG INSTALL_SUBTITLE=0" in dockerfile
        assert "ARG INSTALL_ASR=0" in dockerfile


def test_compose_splits_core_and_subtitle_builds() -> None:
    for relative_path in ("compose.yml", "docker-compose.yml"):
        compose = _text(relative_path)
        assert "x-core-build: &core-build" in compose
        assert "x-subtitle-build: &subtitle-build" in compose
        assert 'INSTALL_SUBTITLE: "0"' in compose
        assert 'INSTALL_SUBTITLE: "1"' in compose
        assert 'INSTALL_ASR: "0"' in compose
        assert "INSTALL_ASR: ${INSTALL_ASR:-1}" in compose
        assert "build: *subtitle-build" in _service_block(compose, "subtitle-service")
        subtitle_workflow = _service_block(compose, "subtitle-workflow-worker")
        assert 'INSTALL_SUBTITLE: "1"' in subtitle_workflow
        assert 'INSTALL_HATCHET: "1"' in subtitle_workflow
        assert "dockerfile: docker/workflow.Dockerfile" in _service_block(compose, "workflow-api")
        assert "build: *subtitle-build" in _service_block(compose, "subtitle-control-worker")
        assert "build: *core-build" in _service_block(compose, "orchestrator")
        assert "build: *core-build" in _service_block(compose, "youtube-ingest")

    export_script = _text("scripts/build_export_prod.sh")
    app_block = export_script[
        export_script.index('echo "Building app image: $APP_IMAGE"') : export_script.index(
            'echo "Building subtitle image: $SUBTITLE_IMAGE"'
        )
    ]
    subtitle_block = export_script[
        export_script.index('echo "Building subtitle image: $SUBTITLE_IMAGE"') : export_script.index(
            'echo "Building egress gateway image: $EGRESS_IMAGE"'
        )
    ]
    egress_block = export_script[
        export_script.index('echo "Building egress gateway image: $EGRESS_IMAGE"') : export_script.index(
            'echo "Building web image: $WEB_IMAGE"'
        )
    ]
    assert '--build-arg INSTALL_SUBTITLE="0"' in app_block
    assert '--build-arg INSTALL_ASR="0"' in app_block
    assert '--build-arg INSTALL_SUBTITLE="1"' in subtitle_block
    assert '--build-arg INSTALL_ASR="${INSTALL_ASR:-1}"' in subtitle_block
    assert '--build-arg INSTALL_SUBTITLE="0"' in egress_block
    assert '--build-arg INSTALL_ASR="0"' in egress_block
