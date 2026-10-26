from __future__ import annotations

import os
import threading
from types import SimpleNamespace
import time
import uuid
from pathlib import Path

from videoroll.apps.orchestrator_api.render_worker_schemas import ArtifactSpec, RenderSpec
from videoroll.apps.render_worker.checkpoint import (
    cache_path, load_completed_render, prune_completed_renders, save_completed_render,
)
from videoroll.apps.render_worker.runtime import RenderDevice, RenderWorkerRuntime


def _spec() -> RenderSpec:
    return RenderSpec(
        render_job_id=uuid.uuid4(), task_id=uuid.uuid4(), mode="burn_in",
        request={"render": {"video_codec": "av1", "crf": 22}},
        artifacts=[ArtifactSpec(
            role="input", storage_key="raw/task/source.mp4", sha256="a" * 64,
            size_bytes=500, download_url="https://example.test/asset",
        )],
    )


def _device() -> RenderDevice:
    return RenderDevice(id="vaapi:renderD128", name="A380", backend="vaapi", path="/dev/dri/renderD128")


def test_completed_render_is_available_after_execution_work_dir_deleted(tmp_path: Path) -> None:
    spec = _spec()
    destination = cache_path(tmp_path, spec, _device(), ".mp4")
    original = tmp_path / "execution" / "video_burnin.mp4"
    original.parent.mkdir()
    original.write_bytes(b"complete-video")
    assert save_completed_render(destination, original) == original
    original.unlink()  # Simulate the execution work directory being cleaned up.
    assert load_completed_render(destination) == destination
    assert destination.read_bytes() == b"complete-video"

    changed = spec.model_copy(update={"request": {"render": {"video_codec": "av1", "crf": 25}}})
    assert cache_path(tmp_path, changed, _device(), ".mp4") != destination
    assert load_completed_render(cache_path(tmp_path, changed, _device(), ".mp4")) is None


def test_recovered_completed_render_skips_download_and_encoder(tmp_path: Path) -> None:
    spec = _spec()
    cached = cache_path(tmp_path, spec, _device(), ".mp4")
    source = tmp_path / "finished.mp4"
    source.write_bytes(b"fully-rendered-media")
    save_completed_render(cached, source)
    source.unlink()

    events: list[tuple[str, dict]] = []
    uploaded: list[Path] = []
    executor = SimpleNamespace(
        settings=SimpleNamespace(work_dir=str(tmp_path), request_timeout_seconds=5),
        _active_lock=threading.Lock(), _active={}, _failed_devices_by_job={},
        _execution_post=lambda _eid, name, payload, **_kw: events.append((name, payload)),
        _execution_cancel_state=lambda _eid, **_kw: (False, None),
        _upload_output=lambda _eid, _fence, path: (uploaded.append(path), uuid.uuid4())[1],
        _download=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no redownload")),
        _load_cached_output=lambda _spec: None,
        _clear_cached_output=lambda _spec: None,
    )
    claim = SimpleNamespace(
        execution=SimpleNamespace(id=uuid.uuid4(), fence_token="x" * 40),
        render_spec=spec,
    )
    RenderWorkerRuntime._run_execution(executor, claim, _device())

    assert len(uploaded) == 1
    assert any(name == "complete" for name, _ in events)
    assert any(payload.get("metrics", {}).get("cache_hit") for name, payload in events if name == "progress")
    assert not uploaded[0].exists()  # Per-attempt work directory was cleaned.
    assert load_completed_render(cached) is not None


def test_torn_or_corrupt_render_checkpoint_is_never_used(tmp_path: Path) -> None:
    destination = cache_path(tmp_path, _spec(), _device(), ".mp4")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"partial mp4")
    assert load_completed_render(destination) is None  # no commit manifest

    source = tmp_path / "valid.mp4"
    source.write_bytes(b"complete-video")
    save_completed_render(destination, source)
    destination.write_bytes(b"corrupt!-video")  # same size, different digest
    assert load_completed_render(destination) is None


def test_completed_render_cache_prunes_expired_output(tmp_path: Path) -> None:
    destination = cache_path(tmp_path, _spec(), _device(), ".mp4")
    original = tmp_path / "complete.mp4"
    original.write_bytes(b"complete-video")
    save_completed_render(destination, original)
    old = time.time() - 25 * 3600
    os.utime(destination, (old, old))
    os.utime(destination.with_name(destination.name + ".json"), (old, old))
    prune_completed_renders(tmp_path)
    assert load_completed_render(destination) is None
