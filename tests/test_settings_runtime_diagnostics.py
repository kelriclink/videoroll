from __future__ import annotations

import sys
from types import SimpleNamespace


def test_embedding_runtime_status_degrades_cleanly_without_postgres(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/15")
    from videoroll.apps.subtitle_service import main as subtitle_main

    monkeypatch.setattr(
        subtitle_main,
        "get_translate_settings",
        lambda *_args, **_kwargs: {
            "rag_embedding_provider": "openai",
            "rag_embedding_model": "Qwen3-Embedding-8B",
            "rag_embedding_dimensions": 4096,
            "rag_embedding_device": "openvino:GPU",
        },
    )

    class _Db:
        @staticmethod
        def get_bind():
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    result = subtitle_main.get_embedding_runtime_status(
        settings=SimpleNamespace(),
        db=_Db(),
    )

    assert result.supported is False
    assert result.search_mode == "unavailable"
    assert result.configured_dimensions == 4096
    assert result.model == "Qwen3-Embedding-8B"
    assert "PostgreSQL/pgvector" in result.detail


def test_intel_hardware_probe_reports_openvino_gpu_visibility(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/15")
    from videoroll.apps.subtitle_service import main as subtitle_main

    monkeypatch.setattr(
        subtitle_main,
        "detect_intel_hardware",
        lambda _device: {
            "checked": True,
            "available": True,
            "render_device": "/dev/dri/renderD128",
            "model_name": "Intel Arc A380",
            "driver": "i915",
            "pci_slot": "0000:01:00.0",
            "pci_id": "8086:56a5",
            "detail": "",
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "openvino",
        SimpleNamespace(Core=lambda: SimpleNamespace(available_devices=["CPU", "GPU"])),
    )

    result = subtitle_main.get_intel_hardware_view(
        settings=SimpleNamespace(intel_gpu_render_device="/dev/dri/renderD128"),
    )

    assert result.available is True
    assert result.openvino_devices == ["CPU", "GPU"]
    assert result.openvino_gpu_available is True
    assert result.openvino_error == ""


def test_embedding_runtime_diagnostics_is_browser_proxy_allowed() -> None:
    from videoroll.apps.orchestrator_api.services.subtitle_service import _is_browser_proxy_path_allowed

    assert _is_browser_proxy_path_allowed("GET", "subtitle/embedding/runtime") is True
