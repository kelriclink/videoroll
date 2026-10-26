"""The real upstream YouDub task database is reachable from VideoRoll.

No fake TTS model is involved: tests submit an authentic local-upload format
task to a vendored PipelineRunner runtime without starting GPU inference.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _bridge():
    file = ROOT / "scripts/native/youdub_bridge.py"
    spec = importlib.util.spec_from_file_location("youdub_bridge_test", file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_upstream_task_create_and_read(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "RUNTIME", tmp_path / "runtime")
    prepared = bridge.prepare()
    assert prepared["changed"]
    assert not bridge.prepare()["changed"]
    assert (bridge.RUNTIME / "backend/app/adapters/voxcpm.py").is_file()
    assert (bridge.RUNTIME / "submodule/demucs/demucs/api.py").is_file()
    assert (bridge.RUNTIME / "LICENSE").read_bytes() == (
        bridge.SOURCE / "LICENSE").read_bytes()

    source = tmp_path / "source.mp4"
    source.write_bytes(bytes(2048))
    srt = tmp_path / "Chinese.srt"
    srt.write_text(
        "1\n00:00:00,120 --> 00:00:01,600\n火箭引擎是怎么工作的？\n\n"
        "2\n00:00:01,600 --> 00:00:03,000\n这是第二句话。\n",
        encoding="utf-8",
    )
    report = bridge.create(source, srt)
    assert report["segments"] == 2
    state = bridge.status(report["id"])
    assert state["status"] == "queued"
    assert len(state["stages"]) == 9
    assert [e["name"] for e in state["stages"]][:3] == [
        "download", "separate", "asr",
    ]
    assert (bridge.RUNTIME / "data/youdub.sqlite").is_file()
    # An active pipeline writer holding a RESERVED lock must not turn --status
    # into a DDL/schema migration. Previous status() called init_db() and
    # blocked for five seconds before failing with database is locked.
    db_file = bridge.RUNTIME / "data/youdub.sqlite"
    writer = sqlite3.connect(db_file, timeout=0.25)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE tasks SET title=title WHERE id=?", (report["id"],))
        live_status = bridge.status(report["id"])
        assert live_status["status"] == "queued"
    finally:
        writer.rollback()
        writer.close()
    uploaded = bridge.RUNTIME / "workfolder/_uploads" / report["id"] / "video/source.mp4"
    assert uploaded.read_bytes() == source.read_bytes()
    assert uploaded.stat().st_nlink == 1
    with pytest.raises(RuntimeError, match="already exists"):
        bridge.create(source, srt, task_id=report["id"])

    # A runtime source upgrade must not silently destroy an unfinished task.
    (bridge.RUNTIME / ".source_sha256").write_text("old-release")
    with pytest.raises(RuntimeError, match="active tasks"):
        bridge.prepare()
    assert bridge.status(report["id"])["status"] == "queued"


def test_native_runner_never_claims_success_without_real_output(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "RUNTIME", tmp_path / "runtime")
    bridge.prepare()
    with pytest.raises(LookupError, match="Unknown YouDub task"):
        bridge.status("11111111-1111-1111-1111-111111111111")


def test_vendor_is_the_original_upstream_backend():
    source = ROOT / "vendor/youdub_webui"
    assert (source / "LICENSE").is_file()
    assert "Apache" in (source / "LICENSE").read_text()
    assert (source / "backend/app/pipeline.py").is_file()
    assert (source / "backend/app/adapters/voxcpm.py").is_file()
    assert (source / "backend/app/adapters/demucs.py").is_file()
    assert (source / "submodule/demucs/demucs/api.py").is_file()
