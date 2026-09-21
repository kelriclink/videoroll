from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from videoroll.apps.render_worker.runtime import RenderWorkerRuntime, WorkerIdentity, _api_base, _worker_key
from videoroll.config import RenderWorkerSettings


def settings(tmp_path: Path, **overrides) -> RenderWorkerSettings:
    values = {
        "RENDER_WORKER_SERVER_URL": "https://video.example.com/api",
        "RENDER_WORKER_CREDENTIAL_FILE": str(tmp_path / "credential.json"),
        "RENDER_WORKER_KEY": "a380-1",
        "RENDER_WORKER_NAME": "A380 #1",
        "RENDER_WORKER_BACKEND": "vaapi",
        "RENDER_WORKER_GPU_DEVICE": "",
        "RENDER_WORKER_WORK_DIR": str(tmp_path / "work"),
        "DATABASE_URL": "unused",
        "REDIS_URL": "unused",
    }
    values.update(overrides)
    return RenderWorkerSettings(**values)


def test_worker_api_base_uses_public_api_prefix(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    assert _api_base(cfg) == "https://video.example.com/api/render-workers/v1"


def test_worker_key_is_stable_when_configured(tmp_path: Path) -> None:
    assert _worker_key(settings(tmp_path)) == "a380-1"


def test_existing_identity_is_loaded_without_database_or_redis(tmp_path: Path, monkeypatch) -> None:
    worker_id = uuid.uuid4()
    credential_file = tmp_path / "credential.json"
    credential_file.write_text(json.dumps({"worker_id": str(worker_id), "credential": "vrw_test"}), encoding="utf-8")
    monkeypatch.setattr("videoroll.apps.render_worker.runtime.probe_capabilities", lambda _cfg: ({"encoders": []}, {}, []))
    runtime = RenderWorkerRuntime(settings(tmp_path))
    identity = runtime.enroll()
    assert identity == WorkerIdentity(worker_id, "vrw_test")


def test_select_device_prefers_idle_compatible_gpu() -> None:
    from videoroll.apps.orchestrator_api.render_worker_schemas import RenderSpec
    from videoroll.apps.render_worker.runtime import ActiveExecution, RenderDevice, select_device
    import threading

    gpu0 = RenderDevice(id="vaapi:renderD128", name="A380 #1", backend="vaapi", path="/dev/dri/renderD128", encoders=("av1_vaapi",))
    gpu1 = RenderDevice(id="vaapi:renderD129", name="A380 #2", backend="vaapi", path="/dev/dri/renderD129", encoders=("av1_vaapi",))
    spec = RenderSpec(render_job_id=uuid.uuid4(), task_id=uuid.uuid4(), mode="burn_in", request={"render": {"video_codec": "av1"}})
    active = {uuid.uuid4(): ActiveExecution(thread=threading.Thread(), device=gpu0)}
    assert select_device([gpu0, gpu1], active, spec).id == gpu1.id


def test_select_device_filters_by_codec_capability() -> None:
    from videoroll.apps.orchestrator_api.render_worker_schemas import RenderSpec
    from videoroll.apps.render_worker.runtime import RenderDevice, select_device

    old_gpu = RenderDevice(id="nvidia:v100", name="V100", backend="nvidia", index=0, encoders=("h264_nvenc", "hevc_nvenc"))
    new_gpu = RenderDevice(id="nvidia:ada", name="RTX 4060", backend="nvidia", index=1, encoders=("av1_nvenc", "h264_nvenc"))
    spec = RenderSpec(render_job_id=uuid.uuid4(), task_id=uuid.uuid4(), mode="burn_in", request={"render": {"video_codec": "av1"}})
    assert select_device([old_gpu, new_gpu], {}, spec).id == new_gpu.id


def test_nvidia_render_uses_selected_gpu_and_av1_nvenc(tmp_path: Path, monkeypatch) -> None:
    from videoroll.apps.subtitle_service import processing

    video = tmp_path / "input.mp4"
    ass = tmp_path / "subtitle.ass"
    output = tmp_path / "output.mp4"
    video.touch()
    ass.write_text("[Script Info]\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(processing, "_ffmpeg_supports_encoder", lambda _ffmpeg, encoder: encoder == "av1_nvenc")
    monkeypatch.setattr(processing, "_run_logged", lambda command, **_kwargs: commands.append(command))

    processing.render_burn_in(
        "ffmpeg", video, ass, output,
        video_codec="av1",
        use_nvidia_gpu=True,
        nvidia_gpu_index=1,
        preset="fast",
        crf=27,
    )

    command = commands[0]
    assert command[command.index("-c:v") + 1] == "av1_nvenc"
    assert command[command.index("-gpu") + 1] == "1"
    assert command[command.index("-preset") + 1] == "p3"
    assert command[command.index("-cq") + 1] == "27"


def test_nvidia_render_rejects_missing_codec_encoder(tmp_path: Path, monkeypatch) -> None:
    import pytest
    from videoroll.apps.subtitle_service import processing

    video = tmp_path / "input.mp4"
    ass = tmp_path / "subtitle.ass"
    video.touch()
    ass.touch()
    monkeypatch.setattr(processing, "_ffmpeg_supports_encoder", lambda *_args: False)
    with pytest.raises(RuntimeError, match="av1_nvenc"):
        processing.render_burn_in(
            "ffmpeg", video, ass, tmp_path / "out.mp4",
            video_codec="av1",
            use_nvidia_gpu=True,
            nvidia_gpu_index=0,
        )


def test_intel_qsv_render_uses_hardware_encoder(tmp_path: Path, monkeypatch) -> None:
    from videoroll.apps.subtitle_service import processing

    video = tmp_path / "input.mp4"
    ass = tmp_path / "subtitle.ass"
    output = tmp_path / "output.mp4"
    video.touch()
    ass.write_text("[Script Info]\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(processing, "_ffmpeg_supports_encoder", lambda _ffmpeg, encoder: encoder == "h264_qsv")
    monkeypatch.setattr(processing, "_run_logged", lambda command, **_kwargs: commands.append(command))

    processing.render_burn_in(
        "ffmpeg", video, ass, output,
        video_codec="h264",
        use_intel_qsv=True,
        preset="fast",
        crf=21,
    )

    command = commands[0]
    assert command[command.index("-c:v") + 1] == "h264_qsv"
    assert command[command.index("-preset") + 1] == "fast"
    assert command[command.index("-global_quality") + 1] == "21"


def test_windows_intel_device_reports_only_working_qsv_encoders(monkeypatch) -> None:
    from videoroll.apps.render_worker import runtime

    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        runtime,
        "_hardware_encoder_available",
        lambda _ffmpeg, encoder, **_kwargs: encoder in {"h264_qsv", "hevc_qsv"},
    )
    devices = runtime._windows_intel_devices(
        {"h264_qsv", "hevc_qsv", "av1_qsv"},
        ffmpeg_path="ffmpeg.exe",
    )
    assert len(devices) == 1
    assert devices[0].backend == "qsv"
    assert devices[0].encoders == ("h264_qsv", "hevc_qsv")


def test_probe_reports_devices_without_per_gpu_capacity(tmp_path: Path, monkeypatch) -> None:
    from videoroll.apps.render_worker import runtime

    monkeypatch.setattr(runtime, "_ffmpeg_supported_encoders", lambda _path: {"av1_nvenc"})
    monkeypatch.setattr(runtime, "_ffmpeg_supported_filters", lambda _path: set())
    monkeypatch.setattr(runtime, "_intel_devices", lambda _encoders, ffmpeg_path: [])
    monkeypatch.setattr(
        runtime, "_nvidia_devices",
        lambda _encoders, ffmpeg_path: [
            runtime.RenderDevice(
                id="nvidia:0", name="GPU 0", backend="nvidia", index=0,
                encoders=("av1_nvenc",),
            ),
            runtime.RenderDevice(
                id="nvidia:1", name="GPU 1", backend="nvidia", index=1,
                encoders=("av1_nvenc",),
            ),
        ],
    )
    _caps, resources, devices = runtime.probe_capabilities(
        settings(tmp_path, RENDER_WORKER_MAX_CONCURRENCY=4)
    )
    assert len(devices) == 2
    assert resources["device_count"] == 2
    assert all("max_concurrency" not in device.payload() for device in devices)
    assert all("available_slots" not in device.payload() for device in devices)


def test_claim_stays_open_when_all_devices_already_have_jobs(tmp_path: Path, monkeypatch) -> None:
    from videoroll.apps.render_worker import runtime

    devices = [
        runtime.RenderDevice(id="vaapi:0", name="GPU 0", backend="vaapi", encoders=("av1_vaapi",)),
        runtime.RenderDevice(id="vaapi:1", name="GPU 1", backend="vaapi", encoders=("av1_vaapi",)),
    ]
    monkeypatch.setattr(
        runtime,
        "probe_capabilities",
        lambda _settings: (
            {"encoders": ["av1_vaapi"], "devices": [device.payload() for device in devices]},
            {"devices": [device.payload() for device in devices]},
            devices,
        ),
    )
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {}

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, _url: str, *, headers: dict[str, str], json: dict[str, object]):
            captured.update(json)
            return Response()

    monkeypatch.setattr(runtime.httpx, "Client", Client)
    worker = runtime.RenderWorkerRuntime(settings(tmp_path, RENDER_WORKER_MAX_CONCURRENCY=1))
    worker.identity = WorkerIdentity(uuid.uuid4(), "vrw_test")
    worker._active = {
        uuid.uuid4(): runtime.ActiveExecution(thread=threading.Thread(), device=devices[0]),
        uuid.uuid4(): runtime.ActiveExecution(thread=threading.Thread(), device=devices[1]),
    }

    claim = worker._claim()

    assert claim.execution is None
    assert captured["available_slots"] == 1
    assert captured["available_encoders"] == ["av1_vaapi"]


def test_select_device_balances_multiple_jobs_per_gpu() -> None:
    from videoroll.apps.orchestrator_api.render_worker_schemas import RenderSpec
    from videoroll.apps.render_worker.runtime import ActiveExecution, RenderDevice, select_device

    gpu0 = RenderDevice(id="vaapi:renderD128", name="A380 #1", backend="vaapi", encoders=("av1_vaapi",))
    gpu1 = RenderDevice(id="vaapi:renderD129", name="A380 #2", backend="vaapi", encoders=("av1_vaapi",))
    spec = RenderSpec(
        render_job_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        mode="burn_in",
        request={"render": {"video_codec": "av1"}},
    )
    active = {
        uuid.uuid4(): ActiveExecution(thread=threading.Thread(), device=gpu0),
        uuid.uuid4(): ActiveExecution(thread=threading.Thread(), device=gpu0),
        uuid.uuid4(): ActiveExecution(thread=threading.Thread(), device=gpu1),
    }

    assert select_device([gpu0, gpu1], active, spec).id == gpu1.id


def test_cancellable_process_runner_terminates_child() -> None:
    from videoroll.apps.subtitle_service.processing import _run_logged

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match="canceled"):
        _run_logged(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            log_path=None,
            cancel_event=cancel,
        )
