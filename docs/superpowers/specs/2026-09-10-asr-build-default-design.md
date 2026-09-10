# ASR Build Default Design

## Goal

Ensure every normal application-image build includes local ASR dependencies so
selecting `faster-whisper` or `openvino` cannot fail merely because the build
caller omitted a Docker build argument.

## Root Cause

`faster-whisper` and `openvino-genai` are in the `asr` optional dependency
group. The root `Dockerfile` and the legacy deployment Dockerfile install that
group only when `INSTALL_ASR=1`, but both Dockerfiles declare the argument with
a default of `0`. Compose and the production export script normally pass `1`
for the application image, while a bare `docker build` silently uses `0`.
That produced an application image that accepts the `openvino` ASR setting but
lacks `openvino_genai` at task execution time.

## Design

### Application-image dependency contract

Set `ARG INSTALL_ASR=1` in both `Dockerfile` and
`deploy_compose/Dockerfile`. The existing dependency-generation command stays
unchanged: with the Dockerfile default, every ordinary application build
installs the `asr` optional group.

The standard Compose application build anchor and the production export script
continue to pass `INSTALL_ASR=1` explicitly. This documents the same contract
at every normal build entry point and protects against a future Dockerfile
default change.

### Egress boundary

Keep `egress-gateway` on its explicit `INSTALL_ASR=0` build path. It is an
authenticated, network-constrained URL-fetching gateway and never executes
subtitle recognition, uses ASR models, or receives the model mount. Keeping
ASR out of that image preserves its narrow operational and security boundary;
it does not restrict subtitle functionality, which runs from `videoroll:prod`.

An operator can still pass `--build-arg INSTALL_ASR=0` to intentionally create
a reduced custom application image, but that is no longer a repository default
or documented deployment path.

### Regression protection and operator action

Add a focused Python deployment-contract test that reads the two Dockerfiles,
both Compose files, and `scripts/build_export_prod.sh`. It will require:

- both Dockerfiles default `INSTALL_ASR` to `1`;
- normal application builds pass `1`;
- egress is the sole intentional `0` path in the default Compose/export
  topology.

Document that changing a build argument or `.env` does not modify an existing
image. Deploying this repair requires rebuilding and replacing `videoroll:prod`
before retrying the failed subtitle task.

## Scope and Non-goals

This change does not alter the selected ASR engine, download an OpenVINO model,
or change GPU device permissions. Those remain runtime configuration concerns.
It also does not merge the separate `compose.yml` and `docker-compose.yml`
files; their broader GPU-setting differences are outside this dependency
contract repair.

## Verification

The new regression test is written first and is expected to fail against the
current `ARG INSTALL_ASR=0` defaults. After the Dockerfile changes, run the
focused test and the existing OpenVINO ASR and deployment tests. A clean
`docker build` without an `INSTALL_ASR` argument then takes the ASR-enabled
dependency branch by construction.
