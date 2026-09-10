import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    marker = f"  {service}:\n"
    start = compose.index(marker)
    following = compose[start + len(marker) :]
    next_service = re.search(r"^  [a-z0-9][a-z0-9-]*:\n", following, re.MULTILINE)
    return compose[start:] if next_service is None else compose[start : start + len(marker) + next_service.start()]


def test_application_dockerfiles_install_asr_by_default() -> None:
    for relative_path in ("Dockerfile", "deploy_compose/Dockerfile"):
        assert "ARG INSTALL_ASR=1" in _text(relative_path)


def test_default_build_paths_enable_asr_except_egress_gateway() -> None:
    for relative_path in ("compose.yml", "docker-compose.yml"):
        compose = _text(relative_path)
        assert "INSTALL_ASR: ${INSTALL_ASR:-1}" in compose
        assert 'INSTALL_ASR: "0"' in _service_block(compose, "egress-gateway")

    export_script = _text("scripts/build_export_prod.sh")
    app_block = export_script[
        export_script.index('echo "Building app image: $APP_IMAGE"') : export_script.index(
            'echo "Building egress gateway image: $EGRESS_IMAGE"'
        )
    ]
    egress_block = export_script[
        export_script.index('echo "Building egress gateway image: $EGRESS_IMAGE"') : export_script.index(
            'echo "Building web image: $WEB_IMAGE"'
        )
    ]
    assert '--build-arg INSTALL_ASR="${INSTALL_ASR:-1}"' in app_block
    assert "--build-arg INSTALL_ASR=0" in egress_block
