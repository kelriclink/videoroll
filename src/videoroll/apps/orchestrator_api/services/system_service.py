from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.schemas import SystemCPURead, SystemIntelGPURead, SystemMemoryRead, SystemResourcesRead
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.config import get_subtitle_settings
from videoroll.utils.intel_gpu import sample_intel_gpu_usage
from videoroll.utils.resources import process_cpu_summary, read_cgroup_memory_stats, read_memory_stats


def _memory_read(data: dict[str, Any] | None) -> SystemMemoryRead:
    data = data or {}
    return SystemMemoryRead(
        total_bytes=int(data.get("total_bytes") or 0),
        used_bytes=int(data.get("used_bytes") or 0),
        available_bytes=int(data.get("available_bytes") or 0),
        percent=float(data["percent"]) if data.get("percent") is not None else None,
    )


def collect_system_resources(db: Session) -> SystemResourcesRead:
    cpu_data = process_cpu_summary()
    memory = read_memory_stats()
    cgroup_memory = read_cgroup_memory_stats()

    intel_gpu: SystemIntelGPURead | None = None
    try:
        profile = get_auto_profile(db)
        intel_enabled = bool(profile.get("use_intel_gpu"))
    except Exception:
        intel_enabled = False

    if intel_enabled:
        subtitle_settings = get_subtitle_settings()
        render_device = str(subtitle_settings.intel_gpu_render_device or "").strip() or "/dev/dri/renderD128"
        try:
            gpu_info = sample_intel_gpu_usage(render_device)
        except Exception as exc:
            gpu_info = {
                "enabled": True,
                "checked": True,
                "available": False,
                "render_device": render_device,
                "detail": str(exc),
            }
        intel_gpu = SystemIntelGPURead(
            enabled=True,
            checked=bool(gpu_info.get("checked", True)),
            available=bool(gpu_info.get("available", False)),
            render_device=str(gpu_info.get("render_device") or render_device),
            model_name=gpu_info.get("model_name"),
            driver=gpu_info.get("driver"),
            pci_slot=gpu_info.get("pci_slot"),
            pci_id=gpu_info.get("pci_id"),
            usage_supported=bool(gpu_info.get("usage_supported", False)),
            usage_percent=float(gpu_info["usage_percent"]) if gpu_info.get("usage_percent") is not None else None,
            engines=[
                {
                    "name": str(item.get("name") or ""),
                    "percent": float(item["percent"]) if item.get("percent") is not None else None,
                }
                for item in list(gpu_info.get("engines") or [])
                if isinstance(item, dict)
            ],
            detail=str(gpu_info.get("detail") or ""),
        )

    return SystemResourcesRead(
        sampled_at=datetime.now(tz=timezone.utc).isoformat(),
        cpu=SystemCPURead(
            percent=float(cpu_data["percent"]) if cpu_data.get("percent") is not None else None,
            cores=int(cpu_data.get("cores") or 0),
            load_average=cpu_data.get("load_average"),
        ),
        memory=_memory_read(memory),
        cgroup_memory=_memory_read(cgroup_memory) if cgroup_memory else None,
        intel_gpu=intel_gpu,
    )
