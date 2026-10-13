from pathlib import Path
from unittest.mock import patch
import pytest

from videoroll.apps.dubbing import model_management as m
from videoroll.apps.dubbing import model_downloader as d


def _make_complete(base: Path, model: str) -> None:
    folder = base / model.split("/")[-1]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"a" * 2048)
    m.atomic_json(folder / ".videoroll-complete.json", {"repo": model})
    tokenizer = base / m.TOKENIZER.split("/")[-1]
    tokenizer.mkdir(parents=True, exist_ok=True)
    m.atomic_json(tokenizer / ".videoroll-complete.json", {"repo": m.TOKENIZER})


def test_uninstalled_selection_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="download"):
        m.choose(m.MODELS[0], "Vivian", "cuda:0", tmp_path)
    with pytest.raises(ValueError):
        m.download("malicious/third-party-model", "huggingface", tmp_path)


def test_persist_default_model_voice_and_device(tmp_path: Path):
    _make_complete(tmp_path, m.MODELS[1])
    saved = m.choose(m.MODELS[1], "Dylan", "cpu", tmp_path)
    assert saved == m.selection(tmp_path)
    assert m.inventory(tmp_path)["models"][1]["installed"] is True
    with pytest.raises(ValueError):
        m.choose(m.MODELS[1], "Invalid", "cuda:0", tmp_path)


def test_download_is_deduplicated_and_reports_state(tmp_path: Path):
    class FakeProcess:
        pid = 123456789
    with patch.object(m.subprocess, "Popen", return_value=FakeProcess()) as start, patch.object(m.os, "kill"):
        state = m.download(m.MODELS[0], "modelscope", tmp_path, python="/usr/bin/python3")
        assert state["status"] in ("downloading", "interrupted")
        assert start.call_count == 1
        with pytest.raises(ValueError, match="already downloading"):
            m.download(m.MODELS[0], "modelscope", tmp_path, python="/usr/bin/python3")


def test_downloader_marks_model_and_tokenizer_complete(tmp_path: Path, monkeypatch):
    calls = []
    def fake_snapshot(repo, target, source):
        calls.append((repo, source))
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.json").write_text("{}")
        (target / "weights.safetensors").write_bytes(b"0" * 2048)
    monkeypatch.setattr(d, "snapshot", fake_snapshot)
    d.perform(m.MODELS[0], "huggingface", tmp_path)
    assert m.installed(m.MODELS[0], tmp_path)
    assert len(calls) == 2
    assert m.status(m.MODELS[0], tmp_path)["status"] == "installed"
    d.perform(m.MODELS[0], "huggingface", tmp_path)
    assert len(calls) == 2

    # A stale marker must not bypass integrity checks.
    (tmp_path / m.MODELS[0].split("/")[-1] / "weights.safetensors").unlink()
    assert m.status(m.MODELS[0], tmp_path)["status"] == "interrupted"
    d.perform(m.MODELS[0], "huggingface", tmp_path)
    assert len(calls) == 3
    assert m.installed(m.MODELS[0], tmp_path)


def test_downloader_persists_failure_and_can_retry(tmp_path: Path, monkeypatch):
    def failure(repo, target, source):
        raise OSError("network interrupted")
    monkeypatch.setattr(d, "snapshot", failure)
    with pytest.raises(OSError):
        d.perform(m.MODELS[0], "huggingface", tmp_path)
    assert m.status(m.MODELS[0], tmp_path)["status"] == "failed"
    assert "network interrupted" in m.status(m.MODELS[0], tmp_path)["message"]
