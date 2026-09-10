# ASR Build Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ordinary VideoRoll application-image builds install local ASR dependencies by default while keeping the egress gateway intentionally minimal.

**Architecture:** The Dockerfiles are the authoritative fallback for every build caller, so their `INSTALL_ASR` default changes from `0` to `1`. Compose and the offline export script retain explicit values: application image builds use `1`, while the URL-fetch-only egress image is the only `0` exception. A source-level deployment-contract test detects accidental drift without an expensive Docker build.

**Tech Stack:** Docker, Docker Compose YAML, Bash, Python 3.12, pytest.

## Global Constraints

- Preserve the current ASR dependency extraction command and `asr` optional dependency group.
- Default all normal `videoroll:prod` build paths to `INSTALL_ASR=1`.
- Keep only `egress-gateway` and its offline-export build command at `INSTALL_ASR=0`.
- Do not modify ASR engine selection, model download configuration, GPU configuration, or unrelated uncommitted work.
- Test the contract before changing Dockerfile defaults and run existing OpenVINO/deployment tests afterward.

---

### Task 1: Lock the Build Contract with a Failing Test

**Files:**
- Create: `tests/test_asr_build_defaults.py`
- Test: `tests/test_asr_build_defaults.py`

**Interfaces:**
- Consumes: repository source files at `Dockerfile`, `deploy_compose/Dockerfile`, `compose.yml`, `docker-compose.yml`, and `scripts/build_export_prod.sh`.
- Produces: pytest assertions describing the required default ASR build contract.

- [ ] **Step 1: Write the failing test**

```python
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    marker = f"  {service}:\\n"
    start = compose.index(marker)
    following = compose[start + len(marker):]
    next_service = re.search(r"^  [a-z0-9][a-z0-9-]*:\\n", following, re.MULTILINE)
    return compose[start:] if next_service is None else compose[start:start + len(marker) + next_service.start()]


def test_application_dockerfiles_install_asr_by_default() -> None:
    for relative_path in ("Dockerfile", "deploy_compose/Dockerfile"):
        assert "ARG INSTALL_ASR=1" in _text(relative_path)


def test_default_build_paths_enable_asr_except_egress_gateway() -> None:
    for relative_path in ("compose.yml", "docker-compose.yml"):
        compose = _text(relative_path)
        assert "INSTALL_ASR: ${INSTALL_ASR:-1}" in compose
        assert 'INSTALL_ASR: "0"' in _service_block(compose, "egress-gateway")

    export_script = _text("scripts/build_export_prod.sh")
    app_block = export_script[export_script.index('echo "Building app image: $APP_IMAGE"'):export_script.index('echo "Building egress gateway image: $EGRESS_IMAGE"')]
    egress_block = export_script[export_script.index('echo "Building egress gateway image: $EGRESS_IMAGE"'):export_script.index('echo "Building web image: $WEB_IMAGE"')]
    assert '--build-arg INSTALL_ASR="${INSTALL_ASR:-1}"' in app_block
    assert "--build-arg INSTALL_ASR=0" in egress_block
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python -m pytest tests/test_asr_build_defaults.py -v`

Expected: `test_application_dockerfiles_install_asr_by_default` fails because both Dockerfiles contain `ARG INSTALL_ASR=0`.

- [ ] **Step 3: Leave the test unchanged while implementing Task 2**

The test must remain as written; its failure proves the Dockerfile defaults are the defect rather than a test typo.

- [ ] **Step 4: Re-run after Task 2**

Run: `python -m pytest tests/test_asr_build_defaults.py -v`

Expected: 2 passed.

### Task 2: Make Application Builds ASR-Complete by Default

**Files:**
- Modify: `Dockerfile:34`
- Modify: `deploy_compose/Dockerfile:31`
- Modify: `.env.example:83`
- Test: `tests/test_asr_build_defaults.py`

**Interfaces:**
- Consumes: `INSTALL_ASR` Docker build argument.
- Produces: an ASR-enabled dependency layer for `docker build -f Dockerfile .` and `docker build -f deploy_compose/Dockerfile .` when no build argument is supplied.

- [ ] **Step 1: Change the root Dockerfile default**

Replace:

```dockerfile
ARG INSTALL_ASR=0
```

with:

```dockerfile
# Normal application builds must include the ASR engines selectable at runtime.
ARG INSTALL_ASR=1
```

- [ ] **Step 2: Change the legacy deployment Dockerfile default**

Replace:

```dockerfile
ARG INSTALL_ASR=0
```

with:

```dockerfile
# Normal application builds must include the ASR engines selectable at runtime.
ARG INSTALL_ASR=1
```

- [ ] **Step 3: Clarify the example environment file**

Place this comment immediately above its existing `INSTALL_ASR=1` line:

```dotenv
# Local ASR engines are included in normal application images by default.
# Set to 0 only for a deliberate custom reduced build.
```

- [ ] **Step 4: Run the focused contract test**

Run: `python -m pytest tests/test_asr_build_defaults.py -v`

Expected: 2 passed.

- [ ] **Step 5: Commit the implementation**

```bash
git add Dockerfile deploy_compose/Dockerfile .env.example tests/test_asr_build_defaults.py
git commit -m "fix: enable ASR in default application builds"
```

### Task 3: Verify Deployment and ASR Behavior

**Files:**
- Verify: `tests/test_asr_build_defaults.py`
- Verify: `tests/test_openvino_asr.py`
- Verify: `tests/test_deployment_security.py`

**Interfaces:**
- Consumes: the build-default contract and existing ASR/deployment test suites.
- Produces: fresh verification evidence that the new default did not alter egress isolation or OpenVINO processing behavior.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
python -m pytest tests/test_asr_build_defaults.py tests/test_openvino_asr.py tests/test_deployment_security.py -v
```

Expected: all collected tests pass.

- [ ] **Step 2: Validate changed files and repository status**

Run:

```bash
git diff --check
git diff -- Dockerfile deploy_compose/Dockerfile .env.example tests/test_asr_build_defaults.py
git status --short
```

Expected: no whitespace errors; only the ASR-default implementation files are part of this change, alongside pre-existing user changes.

- [ ] **Step 3: Give the required deployment action**

After merging or applying the implementation, rebuild and replace the application image before retrying the failed subtitle task:

```bash
docker compose build orchestrator subtitle-service subtitle-worker subtitle-control-worker
docker compose up -d --no-deps orchestrator subtitle-service subtitle-worker subtitle-control-worker
```

The egress gateway is intentionally excluded because its image remains dependency-minimal.
