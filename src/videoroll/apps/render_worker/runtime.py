from __future__ import annotations

import hashlib
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
from videoroll.apps.render_worker.checkpoint import (
    cache_path, load_completed_render, prune_completed_renders, save_completed_render,
)
from videoroll.utils.hashing import sha256_file

logger = logging.getLogger(__name__)
WORKER_VERSION = "1"
DEVICE_COOLDOWN_SECONDS = 120
DEVICE_COOLDOWN_MAX_SECONDS = 900
DISABLED_DEVICE_LABEL = "disabled_render_device_ids"


@dataclass(frozen=True)
class WorkerIdentity:
    id: uuid.UUID
    credential: str


def _api_base(settings: RenderWorkerSettings) -> str:
    base = settings.server_url.strip().rstrip("/")
    return f"{base}/render-workers/v1"


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

    def payload(self, *, active_jobs: int = 0, execution_ids: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "path": self.path,
            "index": self.index,
            "encoders": list(self.encoders),
            "active_jobs": active_jobs,
            "status": "busy" if active_jobs else "idle",
            "execution_ids": execution_ids or [],
        }


@dataclass
class ActiveExecution:
    thread: threading.Thread
    device: RenderDevice


def _hardware_encoder_available(
    ffmpeg_path: str,
    encoder: str,
    *,
    backend: str,
    index: int | None = None,
    device_path: str | None = None,
) -> bool:
    command = [ffmpeg_path, "-hide_banner", "-loglevel", "error"]
    if backend == "vaapi":
        if not device_path:
            return False
        command.extend(["-init_hw_device", f"vaapi=va:{device_path}", "-filter_hw_device", "va"])
    command.extend([
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=128x128:r=1",
    ])
    if backend == "vaapi":
        command.extend(["-vf", "format=nv12,hwupload"])
    command.extend([
        "-frames:v",
        "1",
        "-c:v",
        encoder,
    ])
    if backend == "nvidia" and index is not None:
        command.extend(["-gpu", str(index)])
    command.extend(["-f", "null", "-"])
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=8, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _windows_intel_devices(
    encoders: set[str],
    *,
    ffmpeg_path: str,
) -> list[RenderDevice]:
    qsv_candidates = tuple(sorted(x for x in encoders if x.endswith("_qsv")))
    if not qsv_candidates:
        return []
    name = "Intel Quick Sync Video"
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    detected_intel = False
    if powershell:
        command = [
            powershell,
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | Where-Object {$_.Name -match 'Intel'} | Select-Object -First 1 -ExpandProperty Name",
        ]
        try:
            detected = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            if detected:
                name = detected
                detected_intel = True
        except (OSError, subprocess.SubprocessError):
            pass
    qsv = tuple(
        encoder
        for encoder in qsv_candidates
        if _hardware_encoder_available(ffmpeg_path, encoder, backend="qsv")
    )
    if not qsv:
        return []
    if not detected_intel:
        name = "Intel Quick Sync Video"
    return [
        RenderDevice(
            id="qsv:intel",
            name=name,
            backend="qsv",
            encoders=qsv,
        )
    ]


def _drm_pci_identity(path: Path) -> tuple[int, int, str] | None:
    """Resolve render-node PCI identity through sysfs, not its filename."""
    pci = Path("/sys/class/drm") / path.name / "device"
    try:
        return (int((pci / "vendor").read_text().strip(), 16),
                int((pci / "device").read_text().strip(), 16), pci.resolve(strict=True).name)
    except (OSError, ValueError):
        return None


def _vaapi_device(path: Path, encoders: set[str], *, ffmpeg_path: str) -> RenderDevice | None:
    pci = _drm_pci_identity(path)
    # NVIDIA DRM render nodes exist, but they are not Intel/AMD VAAPI encoders.
    if pci is None or pci[0] not in {0x8086, 0x1002}:
        return None
    supported = tuple(
        encoder for encoder in sorted(encoders) if encoder.endswith("_vaapi")
        and _hardware_encoder_available(ffmpeg_path, encoder, backend="vaapi", device_path=str(path))
    )
    if not supported:
        return None
    vendor = "Intel" if pci[0] == 0x8086 else "AMD"
    return RenderDevice(
        id=f"vaapi:{pci[2]}", name=f"{vendor} VAAPI {path.name} ({pci[0]:04x}:{pci[1]:04x})",
        backend="vaapi", path=str(path), encoders=supported,
    )


def _intel_devices(encoders: set[str], *, ffmpeg_path: str) -> list[RenderDevice]:
    if os.name == "nt":
        return _windows_intel_devices(encoders, ffmpeg_path=ffmpeg_path)
    paths = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    return [
        device for path in paths
        if (device := _vaapi_device(path, encoders, ffmpeg_path=ffmpeg_path)) is not None
    ]


def _nvidia_devices(encoders: set[str], *, ffmpeg_path: str) -> list[RenderDevice]:
    nvenc = tuple(sorted(x for x in encoders if x.endswith("_nvenc")))
    if not nvenc:
        return []
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi and os.name == "nt":
        windows_dir = Path(os.getenv("WINDIR") or r"C:\\Windows")
        candidates = [
            windows_dir / "System32" / "nvidia-smi.exe",
            Path(os.getenv("ProgramW6432") or r"C:\\Program Files") / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ]
        nvidia_smi = next((str(path) for path in candidates if path.is_file()), None)
    if not nvidia_smi:
        return []
    command = [nvidia_smi, "--query-gpu=index,uuid,name,memory.total", "--format=csv,noheader,nounits"]
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
        supported = tuple(
            encoder
            for encoder in nvenc
            if _hardware_encoder_available(ffmpeg_path, encoder, backend="nvidia", index=index)
        )
        if not supported:
            continue
        devices.append(RenderDevice(id=f"nvidia:{gpu_uuid}", name=parts[2], backend="nvidia", index=index, encoders=supported))
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
        if backend == "vaapi":
            device = _vaapi_device(Path(configured_device), encoders, ffmpeg_path=settings.ffmpeg_path)
            if device is None:
                raise ValueError(f"configured VAAPI device does not pass hardware encoding probe: {configured_device}")
            devices.append(device)
        else:
            devices.append(RenderDevice(
                id=f"{backend}:{Path(configured_device).name}",
                name=configured_device, backend=backend, path=configured_device,
                encoders=tuple(sorted(x for x in encoders if x.endswith(f"_{backend}") or backend == "software")),
            ))
    else:
        devices.extend(_intel_devices(encoders, ffmpeg_path=settings.ffmpeg_path))
        devices.extend(_nvidia_devices(encoders, ffmpeg_path=settings.ffmpeg_path))
    if not devices:
        software = tuple(sorted(x for x in encoders if x in {"libx264", "libx265", "libsvtav1", "libaom-av1"}))
        devices.append(RenderDevice(id="software:cpu", name=platform.processor() or "CPU", backend="software", encoders=software))
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
        "device_count": len(devices),
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
        if not required or bool(required & set(device.encoders))
    ]
    if not candidates:
        raise RuntimeError("no local render device supports the requested render")
    # Prefer hardware, then the least occupied device. For burn-in subtitles,
    # VAAPI offers a GPU overlay path; NVIDIA's current ASS path renders the
    # entire 4K frame on the CPU before NVENC. Prefer VAAPI on an idle tie
    # instead of accidentally preferring NVIDIA because "n" sorts before "v".
    return min(candidates, key=lambda device: (
        device.backend == "software",
        counts.get(device.id, 0),
        0 if spec.mode == "burn_in" and device.backend == "vaapi" else 1,
        device.id,
    ))


class RenderWorkerRuntime:
    def __init__(self, settings: RenderWorkerSettings | None = None) -> None:
        self.settings = settings or get_render_worker_settings()
        self.api_base = _api_base(self.settings)
        self.capabilities, self.resources, self.devices = probe_capabilities(self.settings)
        self.identity: WorkerIdentity | None = None
        self._stop = threading.Event()
        self._active: dict[uuid.UUID, ActiveExecution] = {}
        self._active_lock = threading.Lock()
        self._disabled_device_ids: set[str] = set()
        self._device_cooldowns: dict[str, float] = {}
        self._device_failures: dict[str, int] = {}
        self._failed_devices_by_job: dict[uuid.UUID, set[str]] = {}
        self._prune_stale_render_cache()

    def _eligible_devices(self) -> list[RenderDevice]:
        now = time.monotonic()
        return [device for device in self.devices if device.id not in self._disabled_device_ids
                and self._device_cooldowns.get(device.id, 0) <= now]

    def _select_device_for_job(self, spec: RenderSpec) -> RenderDevice:
        eligible = self._eligible_devices()
        excluded = self._failed_devices_by_job.get(spec.render_job_id, set())
        cfg = spec.request.get("render") if isinstance(spec.request.get("render"), dict) else {}
        required = _codec_encoder_names(str(cfg.get("video_codec") or "av1"))
        alternatives = [
            device for device in eligible
            if device.id not in excluded and (not required or bool(required & set(device.encoders)))
        ]
        # Avoid the same failed GPU when a compatible alternative is present.
        return select_device(alternatives or eligible, self._active, spec)

    def _quarantine_device(self, device: RenderDevice) -> None:
        failures = self._device_failures.get(device.id, 0) + 1
        self._device_failures[device.id] = failures
        delay = min(DEVICE_COOLDOWN_MAX_SECONDS, DEVICE_COOLDOWN_SECONDS * 2 ** min(failures - 1, 3))
        self._device_cooldowns[device.id] = time.monotonic() + delay
        logger.warning("quarantining render device %s for %ss after encoder probe failure", device.id, delay)

    def _device_encoder_broken(self, device: RenderDevice, spec: RenderSpec) -> bool:
        if device.backend not in {"vaapi", "nvidia", "qsv"}:
            return False
        cfg = spec.request.get("render") if isinstance(spec.request.get("render"), dict) else {}
        required = _codec_encoder_names(str(cfg.get("video_codec") or "av1"))
        encoder = next((name for name in device.encoders if name in required), None)
        if encoder is None:
            return True
        return not _hardware_encoder_available(
            self.settings.ffmpeg_path, encoder,
            backend="nvidia" if device.backend == "nvidia" else device.backend,
            index=device.index, device_path=device.path,
        )

    def _headers(self) -> dict[str, str]:
        if self.identity is None:
            raise RuntimeError("render worker is not enrolled")
        return {"Authorization": f"Bearer {self.identity.credential}"}

    def stop(self) -> None:
        """Stop claiming new work while allowing accepted executions to settle."""
        self._stop.set()

    def enroll(self) -> WorkerIdentity:
        credential = _load_credential(self.settings)
        token = self.settings.enrollment_token.strip()
        if credential and not token:
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
            # This is only the initial coordinator value for a newly enrolled
            # worker. After enrollment, the coordinator-side admin setting is
            # authoritative for total node concurrency.
            "max_concurrency": self.settings.max_concurrency,
        }
        endpoint = "enroll" if token else "local-enroll"
        if token:
            body["enrollment_token"] = token
        headers = (
            {}
            if token
            else {"Authorization": f"Bearer {self.settings.admin_bootstrap_secret}"}
        )
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(f"{self.api_base}/{endpoint}", json=body, headers=headers)
            if response.is_error:
                logger.error(
                    "render worker enrollment failed: status=%s body=%s",
                    response.status_code,
                    response.text[:1000],
                )
            response.raise_for_status()
            enrolled = WorkerEnrollResponse.model_validate(response.json())
        stored = json.dumps({
            "worker_id": str(enrolled.worker.id),
            "worker_key": enrolled.worker.worker_key,
            "credential": enrolled.credential,
        })
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
            {
                **device.payload(
                    active_jobs=len(execution_ids_by_device.get(device.id, [])),
                    execution_ids=execution_ids_by_device.get(device.id, []),
                ),
                "enabled": device.id not in self._disabled_device_ids,
                "status": ("disabled" if device.id in self._disabled_device_ids else
                           "cooldown" if self._device_cooldowns.get(device.id, 0) > time.monotonic() else
                           "busy" if execution_ids_by_device.get(device.id) else "idle"),
            }
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
            worker = response.json()
            labels = worker.get("labels", {}) if isinstance(worker, dict) else {}
            disabled = labels.get(DISABLED_DEVICE_LABEL, []) if isinstance(labels, dict) else []
            if isinstance(disabled, list):
                with self._active_lock:
                    self._disabled_device_ids = {str(item) for item in disabled if isinstance(item, str)}

    def _claim(self) -> ClaimResponse:
        assert self.identity is not None
        with self._active_lock:
            # The coordinator is the single source of truth for node-wide
            # concurrency. A GPU can run multiple FFmpeg jobs; locally we only
            # advertise whether another compatible job can be accepted.
            eligible = self._eligible_devices()
            available = 1 if eligible else 0
            available_encoders = sorted({encoder for device in eligible for encoder in device.encoders})
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

    def _execution_post(
        self,
        execution_id: uuid.UUID,
        suffix: str,
        body: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = self.settings.request_timeout_seconds if timeout_seconds is None else timeout_seconds
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self.api_base}/executions/{execution_id}/{suffix}", headers=self._headers(), json=body
            )
            response.raise_for_status()
            return response.json()

    def _execution_cancel_state(self, execution_id: uuid.UUID, *, timeout_seconds: float) -> tuple[bool, str | None]:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(
                f"{self.api_base}/executions/{execution_id}/cancel",
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
            return bool(payload.get("cancel_requested")), payload.get("reason")

    def _download(
        self,
        execution_id: uuid.UUID,
        role: str,
        target: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "GET", f"{self.api_base}/executions/{execution_id}/artifacts/{role}", headers=self._headers()
            ) as response:
                response.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("render execution canceled during artifact download")
                        handle.write(chunk)

    def _prune_stale_render_cache(self) -> None:
        """Bound disk retention of our *own* verified, failed-upload outputs.

        Never sweep pre-existing execution working directories or live renders.
        Cached outputs younger than 72 hours survive worker restarts.
        """
        base = Path(self.settings.work_dir) / "_render_output_cache"
        cutoff = time.time() - 72 * 3600
        if not base.is_dir():
            return
        for metadata in base.glob("*/*.json"):
            try:
                if metadata.stat().st_mtime < cutoff:
                    video = metadata.with_name(metadata.name[:-5])
                    video.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    try:
                        metadata.parent.rmdir()
                    except OSError:
                        pass
            except OSError:
                logger.warning("could not prune expired render cache manifest %s", metadata, exc_info=True)

    def _render_cache_path(self, spec: RenderSpec) -> Path:
        # Digest only semantic render inputs, not execution/fence/worker URLs.
        # A new attempt may reuse the output only for an identical render job
        # with identical source artifact IDs/checksums and rendering request.
        semantic_request = dict(spec.request)
        artifact_inputs = []
        for x in spec.artifacts:
            key = x.storage_key
            # A generated ASS asset has a unique execution path, even though
            # the ASS bytes are identical between retries. Canonicalize ONLY
            # if the coordinator supplies a verified content hash; otherwise
            # keep the original path as part of the fingerprint (fail closed).
            generated = (
                x.role == "ass" and x.sha256 is not None
                and key.startswith("render-input/")
                and key.endswith("/subtitle_runtime.ass")
            )
            if generated:
                middle = key[len("render-input/"):-len("/subtitle_runtime.ass")]
                try:
                    uuid.UUID(middle)
                except ValueError:
                    generated = False
            if generated:
                if semantic_request.get("ass_key") == key:
                    semantic_request["ass_key"] = f"generated-ass:sha256:{x.sha256}"
                key = f"generated-ass:sha256:{x.sha256}"
            artifact_inputs.append({"role": x.role, "key": key,
                                    "sha256": x.sha256, "size_bytes": x.size_bytes})
        payload = {"schema": spec.schema_version, "mode": spec.mode,
                   "request": semantic_request, "artifacts": artifact_inputs}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        suffix = ".mkv" if spec.mode == "soft_sub" else ".mp4"
        return Path(self.settings.work_dir) / "_render_output_cache" / str(spec.render_job_id) / f"{fingerprint}{suffix}"

    def _load_cached_output(self, spec: RenderSpec) -> Path | None:
        video = self._render_cache_path(spec)
        metadata = video.with_name(video.name + ".json")
        if not video.is_file() or not metadata.is_file():
            return None
        try:
            manifest = json.loads(metadata.read_text(encoding="utf-8"))
            if (video.stat().st_size > 0 and video.stat().st_size == manifest["size_bytes"]
                    and sha256_file(video) == manifest["sha256"]):
                return video
        except (OSError, ValueError, KeyError, TypeError):
            pass
        logger.warning("render cache validation failed; will render again: %s", video)
        return None

    def _persist_cached_output(self, spec: RenderSpec, output: Path) -> Path:
        video = self._render_cache_path(spec)
        video.parent.mkdir(parents=True, exist_ok=True)
        # Cache is on the same work filesystem: atomic rename, not an extra
        # 10-40 GB copy. Never mark partial/unfinished FFmpeg outputs reusable.
        size = output.stat().st_size
        if size <= 0:
            raise RuntimeError("cannot cache empty render output")
        digest = sha256_file(output)
        os.replace(output, video)
        meta = video.with_name(video.name + ".json")
        tmp = meta.with_name(meta.name + f".{uuid.uuid4().hex}.partial")
        try:
            tmp.write_text(json.dumps({"sha256": digest, "size_bytes": size}), encoding="utf-8")
            os.replace(tmp, meta)
        finally:
            tmp.unlink(missing_ok=True)
        return video

    def _clear_cached_output(self, spec: RenderSpec) -> None:
        video = self._render_cache_path(spec)
        video.unlink(missing_ok=True)
        video.with_name(video.name + ".json").unlink(missing_ok=True)
        try:
            video.parent.rmdir()
        except OSError:
            pass

    def _probe_registered_output(self, client: httpx.Client, execution_id: uuid.UUID, fence: str) -> uuid.UUID | None:
        try:
            response = client.get(
                f"{self.api_base}/executions/{execution_id}/output",
                headers=self._headers(), params={"fence_token": fence}, timeout=12,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return uuid.UUID(response.json()["id"])
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError):
            return None

    def _upload_output(self, execution_id: uuid.UUID, fence: str, path: Path) -> uuid.UUID:
        with httpx.Client(timeout=None) as client:
            previous = self._probe_registered_output(client, execution_id, fence)
            if previous is not None:
                return previous
            for attempt in range(4):
                try:
                    with path.open("rb") as handle:
                        response = client.post(
                            f"{self.api_base}/executions/{execution_id}/output",
                            headers=self._headers(),
                            data={"fence_token": fence},
                            files={"file": (path.name, handle, "application/octet-stream")},
                        )
                    response.raise_for_status()
                    return uuid.UUID(response.json()["id"])
                except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                    recovered = self._probe_registered_output(client, execution_id, fence)
                    if recovered is not None:
                        return recovered
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {500, 502, 503, 504}:
                        raise
                    if attempt == 3:
                        raise
                    delay = min(12, 2 ** attempt * 2)
                    logger.warning("render output transfer failed (%s); retrying saved file after %ss, attempt %d/4: %s",
                                   type(exc).__name__, delay, attempt + 1, execution_id)
                    time.sleep(delay)
        raise AssertionError("render output upload did not return")

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
        cancel_event = threading.Event()
        cancel_reason: list[str] = []
        execution_metrics: dict[str, Any] = {
            "device_id": device.id,
            "device_name": device.name,
            "backend": device.backend,
            "stage": "downloading",
        }

        def current_execution_progress() -> int:
            stage = str(execution_metrics.get("stage") or "").strip().lower()
            if stage in {"claimed", "downloading"}:
                return 10
            if stage == "uploading":
                return 90
            if stage == "complete":
                return 100
            if stage == "rendering":
                try:
                    render_percent = float(execution_metrics.get("render_percent") or 0.0)
                except (TypeError, ValueError):
                    render_percent = 0.0
                return int(round(20.0 + max(0.0, min(100.0, render_percent)) * 0.7))
            return 5

        def heartbeat_loop() -> None:
            # Render telemetry is user-facing, so keep execution heartbeats
            # reasonably fresh even when the node-level heartbeat is slower.
            heartbeat_interval = 5.0
            request_timeout = max(2.0, min(float(self.settings.request_timeout_seconds), 10.0))
            next_heartbeat = 0.0
            next_cancel_poll = 0.0
            heartbeat_failures = 0
            while not heartbeat_stop.is_set() and not cancel_event.is_set():
                now = time.monotonic()
                if now >= next_heartbeat:
                    try:
                        self._execution_post(
                            execution_id,
                            "heartbeat",
                            {
                                "fence_token": fence,
                                "progress": current_execution_progress(),
                                "metrics": dict(execution_metrics),
                            },
                            timeout_seconds=request_timeout,
                        )
                        heartbeat_failures = 0
                    except httpx.HTTPStatusError as exc:
                        heartbeat_failures += 1
                        if exc.response.status_code in {409, 410}:
                            cancel_reason.append(f"coordinator fenced execution ({exc.response.status_code})")
                            cancel_event.set()
                            break
                        logger.warning(
                            "render execution heartbeat rejected: execution=%s status=%s",
                            execution_id,
                            exc.response.status_code,
                        )
                    except Exception:
                        heartbeat_failures += 1
                        logger.exception("render execution heartbeat failed: %s", execution_id)
                    if heartbeat_failures >= 3:
                        cancel_reason.append("execution heartbeat unavailable")
                        cancel_event.set()
                        break
                    next_heartbeat = now + heartbeat_interval

                if now >= next_cancel_poll and not cancel_event.is_set():
                    try:
                        requested, reason = self._execution_cancel_state(
                            execution_id,
                            timeout_seconds=request_timeout,
                        )
                        if requested:
                            cancel_reason.append(str(reason or "coordinator requested cancellation"))
                            cancel_event.set()
                            break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code in {409, 410}:
                            cancel_reason.append(f"coordinator fenced execution ({exc.response.status_code})")
                            cancel_event.set()
                            break
                        logger.warning(
                            "render execution cancel poll rejected: execution=%s status=%s",
                            execution_id,
                            exc.response.status_code,
                        )
                    except Exception:
                        logger.debug("render execution cancel poll failed: %s", execution_id, exc_info=True)
                    next_cancel_poll = now + 5.0
                heartbeat_stop.wait(0.5)

        hb = threading.Thread(target=heartbeat_loop, name=f"render-hb-{execution_id}", daemon=True)
        hb.start()
        try:
            checkpoint = None
            if spec.mode in {"burn_in", "soft_sub"}:
                suffix = ".mp4" if spec.mode == "burn_in" else ".mkv"
                checkpoint = cache_path(Path(self.settings.work_dir), spec, device, suffix)
                prune_completed_renders(Path(self.settings.work_dir))
            output = self._load_cached_output(spec)
            if output is None and checkpoint is not None:
                cached = load_completed_render(checkpoint)
                if cached is not None:
                    # Pin cached data by hardlink while the upload is active.
                    # Concurrent cache cleanup must not remove its bytes.
                    active_cached = root / f"cached_render{suffix}"
                    os.link(cached, active_cached)
                    output = active_cached
                    execution_metrics.update(stage="uploading", cache_hit=True)
                    logger.info("reusing verified completed render for %s", spec.render_job_id)
            if output is None:
                paths: dict[str, Path] = {}
                for artifact in spec.artifacts:
                    suffix = Path(artifact.storage_key).suffix or ".bin"
                    target = root / f"{artifact.role}{suffix}"
                    self._download(execution_id, artifact.role, target, cancel_event=cancel_event)
                    if artifact.sha256 and sha256_file(target) != artifact.sha256:
                        raise RuntimeError(f"artifact checksum mismatch: {artifact.role}")
                    paths[artifact.role] = target
                request = dict(spec.request or {})
                render_cfg = request.get("render") if isinstance(request.get("render"), dict) else {}
                input_path = paths.get("input")
                srt_path = paths.get("srt")
                ass_path = paths.get("ass")
                audio_path = paths.get("audio")
                if request.get("dubbing_job_id") and audio_path is None:
                    raise RuntimeError("P2 render requires its mixed audio artifact")
                if input_path is None:
                    raise RuntimeError("render input artifact is missing")
                if spec.mode == "burn_in" and ass_path is None:
                    raise RuntimeError("burn-in render requires an ASS artifact")
                if spec.mode == "soft_sub" and srt_path is None:
                    raise RuntimeError("soft-sub render requires an SRT artifact")

                execution_metrics["stage"] = "rendering"
                self._execution_post(
                    execution_id,
                    "progress",
                    {"fence_token": fence, "progress": 20, "metrics": dict(execution_metrics)},
                )
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
                        use_intel_qsv=backend == "qsv",
                        intel_qsv_device_index=device.index,
                        use_nvidia_gpu=backend == "nvidia",
                        nvidia_gpu_index=device.index,
                        preset=render_cfg.get("video_preset"),
                        crf=render_cfg.get("video_crf"),
                        cancel_event=cancel_event,
                        render_metrics=execution_metrics,
                        audio_path=audio_path,
                    )
                elif spec.mode == "soft_sub":
                    output = root / "video_softsub.mkv"
                    mux_soft_sub(
                        self.settings.ffmpeg_path,
                        input_path,
                        srt_path,
                        output,
                        cancel_event=cancel_event,
                        render_metrics=execution_metrics,
                    )
                else:
                    self._execution_post(execution_id, "complete", {"fence_token": fence, "output": {}})
                    return

                output = self._persist_cached_output(spec, output)
            else:
                logger.info("resuming finished render without FFmpeg: job=%s cached=%s", spec.render_job_id, output)
            if cancel_event.is_set():
                raise RuntimeError(cancel_reason[-1] if cancel_reason else "render execution canceled")
            if checkpoint is not None:
                output = save_completed_render(checkpoint, output)
                prune_completed_renders(Path(self.settings.work_dir))
            execution_metrics["stage"] = "uploading"
            self._execution_post(
                execution_id,
                "progress",
                {"fence_token": fence, "progress": 90, "metrics": dict(execution_metrics)},
            )
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
            self._clear_cached_output(spec)
            with self._active_lock:
                self._failed_devices_by_job.pop(spec.render_job_id, None)
        except Exception as exc:
            if cancel_event.is_set():
                logger.warning(
                    "render execution stopped: execution=%s reason=%s",
                    execution_id,
                    cancel_reason[-1] if cancel_reason else str(exc),
                )
            else:
                logger.exception("render execution failed: %s", execution_id)
            try:
                if isinstance(exc, subprocess.CalledProcessError):
                    with self._active_lock:
                        if len(self._failed_devices_by_job) > 512:
                            self._failed_devices_by_job.pop(next(iter(self._failed_devices_by_job)))
                        self._failed_devices_by_job.setdefault(spec.render_job_id, set()).add(device.id)
                    if self._device_encoder_broken(device, spec):
                        with self._active_lock:
                            self._quarantine_device(device)
                # The coordinator imposes a global attempt limit on retryable errors.
                retryable = not isinstance(exc, (ValueError, FileNotFoundError, NotImplementedError))
                self._execution_post(
                    execution_id, "fail", {"fence_token": fence, "error": str(exc)[:8192], "retryable": retryable}
                )
            except httpx.HTTPStatusError as report_exc:
                if report_exc.response.status_code not in {409, 410}:
                    logger.exception("failed to report render execution failure: %s", execution_id)
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
                        device = self._select_device_for_job(claim.render_spec)
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
