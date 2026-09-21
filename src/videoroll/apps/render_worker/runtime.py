from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from videoroll.apps.orchestrator_api.render_worker_schemas import ClaimResponse, RenderSpec, WorkerEnrollResponse
from videoroll.apps.subtitle_service.processing import (
    _ffmpeg_supported_encoders,
    _ffmpeg_supported_filters,
    mux_soft_sub,
    render_burn_in,
)
from videoroll.config import RenderWorkerSettings, get_render_worker_settings
from videoroll.utils.hashing import sha256_file

logger = logging.getLogger(__name__)
WORKER_VERSION = "1"


@dataclass(frozen=True)
class WorkerIdentity:
    id: uuid.UUID
    credential: str


def _api_base(settings: RenderWorkerSettings) -> str:
    base = settings.server_url.strip().rstrip("/")
    if base.endswith("/api"):
        return f"{base}/render-workers/v1"
    return f"{base}/api/render-workers/v1"


def _credential_path(settings: RenderWorkerSettings) -> Path:
    return Path(settings.credential_file).expanduser()


def _load_credential(settings: RenderWorkerSettings) -> str:
    if settings.credential.strip():
        return settings.credential.strip()
    path = _credential_path(settings)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _save_credential(settings: RenderWorkerSettings, value: str) -> None:
    path = _credential_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _worker_key(settings: RenderWorkerSettings) -> str:
    configured = settings.worker_key.strip()
    return configured or socket.gethostname()[:128]


@dataclass(frozen=True)
class RenderDevice:
    id: str
    name: str
    backend: str
    path: str = ""
    index: int | None = None
    encoders: tuple[str, ...] = ()
    max_concurrency: int = 1

    def payload(self, *, active_jobs: int = 0, execution_ids: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "path": self.path,
            "index": self.index,
            "encoders": list(self.encoders),
            "max_concurrency": self.max_concurrency,
            "active_jobs": active_jobs,
            "available_slots": max(0, self.max_concurrency - active_jobs),
            "status": "busy" if active_jobs else "idle",
            "execution_ids": execution_ids or [],
        }


@dataclass
class ActiveExecution:
    thread: threading.Thread
    device: RenderDevice


def _intel_devices(encoders: set[str], *, max_concurrency: int) -> list[RenderDevice]:
    if os.name == "nt":
        return []
    paths = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    vaapi = tuple(sorted(x for x in encoders if x.endswith("_vaapi")))
    return [
        RenderDevice(id=f"vaapi:{path.name}", name=f"Intel/VAAPI {path.name}", backend="vaapi", path=str(path), encoders=vaapi, max_concurrency=max_concurrency)
        for path in paths
    ]


def _nvidia_devices(encoders: set[str], *, max_concurrency: int) -> list[RenderDevice]:
    nvenc = tuple(sorted(x for x in encoders if x.endswith("_nvenc")))
    if not nvenc:
        return []
    command = ["nvidia-smi", "--query-gpu=index,uuid,name,memory.total", "--format=csv,noheader,nounits"]
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[RenderDevice] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 3)]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        gpu_uuid = parts[1] or f"gpu-{index}"
        devices.append(RenderDevice(id=f"nvidia:{gpu_uuid}", name=parts[2], backend="nvidia", index=index, encoders=nvenc, max_concurrency=max_concurrency))
    return devices


def probe_capabilities(settings: RenderWorkerSettings) -> tuple[dict[str, Any], dict[str, Any], list[RenderDevice]]:
    encoders = set(_ffmpeg_supported_encoders(settings.ffmpeg_path))
    filters = sorted(_ffmpeg_supported_filters(settings.ffmpeg_path))
    devices: list[RenderDevice] = []
    configured_device = settings.gpu_device.strip()
    configured_backend = settings.backend.strip().lower()
    if configured_device:
        if os.name != "nt" and not Path(configured_device).exists():
            raise FileNotFoundError(f"render GPU device not found: {configured_device}")
        backend = "vaapi" if configured_backend in {"", "auto", "intel", "vaapi"} else configured_backend
        devices.append(RenderDevice(
            id=f"{backend}:{Path(configured_device).name}",
            name=configured_device,
            backend=backend,
            path=configured_device,
            encoders=tuple(sorted(x for x in encoders if x.endswith(f"_{backend}") or backend == "software")),
            max_concurrency=settings.max_concurrency,
        ))
    else:
        devices.extend(_intel_devices(encoders, max_concurrency=settings.max_concurrency))
        devices.extend(_nvidia_devices(encoders, max_concurrency=settings.max_concurrency))
    if not devices:
        software = tuple(sorted(x for x in encoders if x in {"libx264", "libx265", "libsvtav1", "libaom-av1"}))
        devices.append(RenderDevice(id="software:cpu", name=platform.processor() or "CPU", backend="software", encoders=software, max_concurrency=settings.max_concurrency))
    union_encoders = sorted({encoder for device in devices for encoder in device.encoders})
    caps = {
        "backend": "multi" if len({device.backend for device in devices}) > 1 or len(devices) > 1 else devices[0].backend,
        "gpu_model": ", ".join(device.name for device in devices if device.backend != "software") or "CPU",
        "encoders": union_encoders or sorted(encoders),
        "filters": filters,
        "devices": [device.payload() for device in devices],
    }
    resources = {
        "hostname": socket.gethostname(),
        "cpu_count": os.cpu_count(),
        "devices": [device.payload() for device in devices],
    }
    return caps, resources, devices


def _codec_encoder_names(codec: str) -> set[str]:
    codec = codec.lower()
    if codec == "av1":
        return {"av1_nvenc", "av1_vaapi", "av1_qsv", "libsvtav1", "libaom-av1"}
    if codec in {"h264", "avc"}:
        return {"h264_nvenc", "h264_vaapi", "h264_qsv", "libx264"}
    if codec in {"hevc", "h265"}:
        return {"hevc_nvenc", "hevc_vaapi", "hevc_qsv", "libx265"}
    return set()


def select_device(devices: list[RenderDevice], active: dict[uuid.UUID, ActiveExecution], spec: RenderSpec) -> RenderDevice:
    render_cfg = spec.request.get("render") if isinstance(spec.request.get("render"), dict) else {}
    required = _codec_encoder_names(str(render_cfg.get("video_codec") or "av1"))
    counts = {device.id: 0 for device in devices}
    for item in active.values():
        counts[item.device.id] = counts.get(item.device.id, 0) + 1
    candidates = [
        device for device in devices
        if counts.get(device.id, 0) < device.max_concurrency and (not required or bool(required & set(device.encoders)))
    ]
    if not candidates:
        raise RuntimeError("no local render device has a compatible free slot")
    # Prefer hardware, then the least occupied device. Stable id breaks ties so
    # scheduling is deterministic and easy to diagnose.
    return min(candidates, key=lambda device: (device.backend == "software", counts.get(device.id, 0), device.id))


class RenderWorkerRuntime:
    def __init__(self, settings: RenderWorkerSettings | None = None) -> None:
        self.settings = settings or get_render_worker_settings()
        self.api_base = _api_base(self.settings)
        self.capabilities, self.resources, self.devices = probe_capabilities(self.settings)
        self.identity: WorkerIdentity | None = None
        self._stop = threading.Event()
        self._active: dict[uuid.UUID, ActiveExecution] = {}
        self._active_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        if self.identity is None:
            raise RuntimeError("render worker is not enrolled")
        return {"Authorization": f"Bearer {self.identity.credential}"}

    def enroll(self) -> WorkerIdentity:
        credential = _load_credential(self.settings)
        if credential:
            # The worker id is recovered by enrolling only when necessary. With
            # an existing credential, heartbeat lookup is not available by key,
            # so persist the id next to the credential as JSON.
            try:
                payload = json.loads(credential)
                identity = WorkerIdentity(uuid.UUID(payload["worker_id"]), str(payload["credential"]))
                self.identity = identity
                return identity
            except Exception as exc:
                raise RuntimeError("render worker credential must contain worker_id and credential; pair this worker again") from exc

        token = self.settings.enrollment_token.strip()
        body = {
            "worker_key": _worker_key(self.settings),
            "name": self.settings.name,
            "platform": platform.system().lower(),
            "architecture": platform.machine() or None,
            "version": WORKER_VERSION,
            "protocol_version": 1,
            "render_spec_versions": [1],
            "capabilities": self.capabilities,
            "resources": self.resources,
            "labels": {"standalone": True},
            "max_concurrency": self.settings.max_concurrency,
        }
        endpoint = "enroll" if token else "local-enroll"
        if token:
            body["enrollment_token"] = token
        headers = {} if token else {"X-Internal-Secret": self.settings.admin_bootstrap_secret}
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(f"{self.api_base}/{endpoint}", json=body, headers=headers)
            response.raise_for_status()
            enrolled = WorkerEnrollResponse.model_validate(response.json())
        stored = json.dumps({"worker_id": str(enrolled.worker.id), "credential": enrolled.credential})
        _save_credential(self.settings, stored)
        self.identity = WorkerIdentity(enrolled.worker.id, enrolled.credential)
        logger.info("render worker enrolled: id=%s key=%s", enrolled.worker.id, enrolled.worker.worker_key)
        return self.identity

    def _heartbeat(self) -> None:
        assert self.identity is not None
        with self._active_lock:
            active = dict(self._active)
        execution_ids_by_device: dict[str, list[str]] = {device.id: [] for device in self.devices}
        for execution_id, item in active.items():
            execution_ids_by_device.setdefault(item.device.id, []).append(str(execution_id))
        device_payloads = [
            device.payload(
                active_jobs=len(execution_ids_by_device.get(device.id, [])),
                execution_ids=execution_ids_by_device.get(device.id, []),
            )
            for device in self.devices
        ]
        self.resources = {**self.resources, "devices": device_payloads}
        self.capabilities = {**self.capabilities, "devices": device_payloads}
        body = {
            "status": "busy" if active else "online",
            "resources": self.resources,
            "capabilities": self.capabilities,
            "active_execution_ids": [str(x) for x in active],
        }
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(
                f"{self.api_base}/{self.identity.id}/heartbeat", headers=self._headers(), json=body
            )
            response.raise_for_status()

    def _claim(self) -> ClaimResponse:
        assert self.identity is not None
        with self._active_lock:
            active = dict(self._active)
            node_slots = max(0, self.settings.max_concurrency - len(active))
            device_slots = sum(
                max(0, device.max_concurrency - sum(1 for item in active.values() if item.device.id == device.id))
                for device in self.devices
            )
            available = min(node_slots, device_slots)
            free_devices = [
                device for device in self.devices
                if sum(1 for item in active.values() if item.device.id == device.id) < device.max_concurrency
            ]
            available_encoders = sorted({encoder for device in free_devices for encoder in device.encoders})
        if available <= 0:
            return ClaimResponse()
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(
                f"{self.api_base}/{self.identity.id}/claim",
                headers=self._headers(),
                json={
                    "available_slots": available,
                    "accepted_transfer_modes": ["http"],
                    "available_encoders": available_encoders,
                },
            )
            response.raise_for_status()
            return ClaimResponse.model_validate(response.json())

    def _execution_post(self, execution_id: uuid.UUID, suffix: str, body: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(
                f"{self.api_base}/executions/{execution_id}/{suffix}", headers=self._headers(), json=body
            )
            response.raise_for_status()
            return response.json()

    def _download(self, execution_id: uuid.UUID, role: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "GET", f"{self.api_base}/executions/{execution_id}/artifacts/{role}", headers=self._headers()
            ) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)

    def _upload_output(self, execution_id: uuid.UUID, fence: str, path: Path) -> uuid.UUID:
        with path.open("rb") as handle, httpx.Client(timeout=None) as client:
            response = client.post(
                f"{self.api_base}/executions/{execution_id}/output",
                headers=self._headers(),
                data={"fence_token": fence},
                files={"file": (path.name, handle, "application/octet-stream")},
            )
            response.raise_for_status()
            return uuid.UUID(response.json()["id"])

    def _run_execution(self, claim: ClaimResponse, device: RenderDevice) -> None:
        execution = claim.execution
        spec = claim.render_spec
        if execution is None or spec is None:
            return
        fence = execution.fence_token
        execution_id = execution.id
        root = Path(self.settings.work_dir) / str(execution_id)
        root.mkdir(parents=True, exist_ok=True)
        heartbeat_stop = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(30.0):
                try:
                    self._execution_post(
                        execution_id, "heartbeat", {"fence_token": fence, "progress": None, "metrics": {"device_id": device.id, "device_name": device.name, "backend": device.backend}}
                    )
                except Exception:
                    logger.exception("render execution heartbeat failed: %s", execution_id)

        hb = threading.Thread(target=heartbeat_loop, name=f"render-hb-{execution_id}", daemon=True)
        hb.start()
        try:
            paths: dict[str, Path] = {}
            for artifact in spec.artifacts:
                suffix = Path(artifact.storage_key).suffix or ".bin"
                target = root / f"{artifact.role}{suffix}"
                self._download(execution_id, artifact.role, target)
                if artifact.sha256 and sha256_file(target) != artifact.sha256:
                    raise RuntimeError(f"artifact checksum mismatch: {artifact.role}")
                paths[artifact.role] = target

            request = dict(spec.request or {})
            render_cfg = request.get("render") if isinstance(request.get("render"), dict) else {}
            input_path = paths.get("input")
            srt_path = paths.get("srt")
            ass_path = paths.get("ass")
            if input_path is None:
                raise RuntimeError("render input artifact is missing")
            if spec.mode == "burn_in" and ass_path is None:
                raise RuntimeError("burn-in render requires an ASS artifact")
            if spec.mode == "soft_sub" and srt_path is None:
                raise RuntimeError("soft-sub render requires an SRT artifact")

            self._execution_post(execution_id, "progress", {"fence_token": fence, "progress": 20, "metrics": {"device_id": device.id, "device_name": device.name, "backend": device.backend}})
            if spec.mode == "burn_in":
                output = root / "video_burnin.mp4"
                backend = device.backend
                render_burn_in(
                    self.settings.ffmpeg_path,
                    input_path,
                    ass_path,
                    output,
                    video_codec=str(render_cfg.get("video_codec") or "av1"),
                    use_intel_gpu=backend in {"intel", "vaapi"},
                    intel_gpu_render_device=device.path or "/dev/dri/renderD128",
                    use_nvidia_gpu=backend == "nvidia",
                    nvidia_gpu_index=device.index,
                    preset=render_cfg.get("video_preset"),
                    crf=render_cfg.get("video_crf"),
                )
            elif spec.mode == "soft_sub":
                output = root / "video_softsub.mkv"
                mux_soft_sub(self.settings.ffmpeg_path, input_path, srt_path, output)
            else:
                self._execution_post(execution_id, "complete", {"fence_token": fence, "output": {}})
                return

            self._execution_post(execution_id, "progress", {"fence_token": fence, "progress": 90, "metrics": {"device_id": device.id, "device_name": device.name, "backend": device.backend}})
            output_asset_id = self._upload_output(execution_id, fence, output)
            self._execution_post(
                execution_id,
                "complete",
                {
                    "fence_token": fence,
                    "output_asset_id": str(output_asset_id),
                    "output": {"sha256": sha256_file(output), "size_bytes": output.stat().st_size},
                },
            )
        except Exception as exc:
            logger.exception("render execution failed: %s", execution_id)
            try:
                self._execution_post(
                    execution_id, "fail", {"fence_token": fence, "error": str(exc)[:8192], "retryable": True}
                )
            except Exception:
                logger.exception("failed to report render execution failure: %s", execution_id)
        finally:
            heartbeat_stop.set()
            hb.join(timeout=5)
            shutil.rmtree(root, ignore_errors=True)
            with self._active_lock:
                self._active.pop(execution_id, None)

    def run_forever(self) -> None:
        self.enroll()
        next_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    self._heartbeat()
                except Exception:
                    logger.exception("render worker heartbeat failed")
                next_heartbeat = now + self.settings.heartbeat_interval_seconds
            try:
                claim = self._claim()
                if claim.execution is not None and claim.render_spec is not None:
                    with self._active_lock:
                        device = select_device(self.devices, self._active, claim.render_spec)
                        thread = threading.Thread(
                            target=self._run_execution,
                            args=(claim, device),
                            name=f"render-{claim.execution.id}",
                            daemon=True,
                        )
                        self._active[claim.execution.id] = ActiveExecution(thread=thread, device=device)
                    thread.start()
                    continue
            except Exception:
                logger.exception("render worker claim failed")
            self._stop.wait(self.settings.poll_interval_seconds)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    RenderWorkerRuntime().run_forever()


if __name__ == "__main__":
    main()
