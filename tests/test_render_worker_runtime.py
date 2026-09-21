from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import Mock

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
        max_concurrency=2,
        ffmpeg_path="ffmpeg.exe",
    )
    assert len(devices) == 1
    assert devices[0].backend == "qsv"
    assert devices[0].encoders == ("h264_qsv", "hevc_qsv")
    assert devices[0].max_concurrency == 2


def test_probe_assigns_node_concurrency_to_each_gpu(tmp_path: Path, monkeypatch) -> None:
    from videoroll.apps.render_worker import runtime

    monkeypatch.setattr(runtime, "_ffmpeg_supported_encoders", lambda _path: {"av1_nvenc"})
    monkeypatch.setattr(runtime, "_ffmpeg_supported_filters", lambda _path: set())
    monkeypatch.setattr(runtime, "_intel_devices", lambda _encoders, max_concurrency, ffmpeg_path: [])
    monkeypatch.setattr(
        runtime, "_nvidia_devices",
        lambda _encoders, max_concurrency, ffmpeg_path: [
            runtime.RenderDevice(
                id="nvidia:0", name="GPU 0", backend="nvidia", index=0,
                encoders=("av1_nvenc",), max_concurrency=max_concurrency,
            ),
            runtime.RenderDevice(
                id="nvidia:1", name="GPU 1", backend="nvidia", index=1,
                encoders=("av1_nvenc",), max_concurrency=max_concurrency,
            ),
        ],
    )
    _caps, _resources, devices = runtime.probe_capabilities(
        settings(tmp_path, RENDER_WORKER_MAX_CONCURRENCY=4)
    )
    assert [device.max_concurrency for device in devices] == [4, 4]
