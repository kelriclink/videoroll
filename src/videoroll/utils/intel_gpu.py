from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


_INTEL_VENDOR_ID = "0x8086"


def detect_intel_hardware(render_device: str) -> dict[str, Any]:
    device_path = str(render_device or "").strip() or "/dev/dri/renderD128"
    result: dict[str, Any] = {
        "checked": True,
        "available": False,
        "render_device": device_path,
        "model_name": None,
        "driver": None,
        "pci_slot": None,
        "pci_id": None,
        "detail": "",
    }

    node = Path(device_path)
    if not node.exists():
        result["detail"] = f"未找到渲染设备：{device_path}"
        return result

    sysfs_dir = _render_sysfs_device_dir(node)
    if sysfs_dir is None or not sysfs_dir.exists():
        result["detail"] = f"未找到渲染设备的 sysfs 信息：{device_path}"
        return result

    return _probe_intel_sysfs(sysfs_dir, device_path)


def sample_intel_gpu_usage(render_device: str, *, interval_seconds: float = 0.1) -> dict[str, Any]:
    hardware = detect_intel_hardware(render_device)
    out: dict[str, Any] = {
        **hardware,
        "usage_supported": False,
        "usage_percent": None,
        "engines": [],
    }
    if not bool(hardware.get("available")):
        return out

    sysfs_dir = _render_sysfs_device_dir(Path(str(render_device or "").strip() or "/dev/dri/renderD128"))
    if sysfs_dir is None:
        out["detail"] = "未找到渲染设备的 sysfs 信息"
        return out

    first = _read_engine_busy_snapshot(sysfs_dir)
    if not first:
        out["detail"] = f"{hardware.get('detail') or '已检测到 Intel 硬件'}；未找到 GPU busy 计数器"
        return out

    interval = max(0.02, float(interval_seconds))
    time.sleep(interval)
    second = _read_engine_busy_snapshot(sysfs_dir)
    if not second:
        out["detail"] = f"{hardware.get('detail') or '已检测到 Intel 硬件'}；GPU busy 计数器不可读"
        return out

    elapsed_us = interval * 1_000_000.0
    engines: list[dict[str, Any]] = []
    max_percent = 0.0
    for name, start_busy in first.items():
        end_busy = second.get(name)
        if end_busy is None:
            continue
        delta = max(0, end_busy - start_busy)
        percent = max(0.0, min(100.0, (delta / elapsed_us) * 100.0))
        max_percent = max(max_percent, percent)
        engines.append({"name": name, "percent": percent})

    if not engines:
        out["detail"] = f"{hardware.get('detail') or '已检测到 Intel 硬件'}；GPU busy 计数器无有效数据"
        return out

    out["usage_supported"] = True
    out["usage_percent"] = max_percent
    out["engines"] = sorted(engines, key=lambda item: str(item.get("name") or ""))
    return out


def _probe_intel_sysfs(sysfs_dir: Path, render_device: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "checked": True,
        "available": False,
        "render_device": render_device,
        "model_name": None,
        "driver": _read_driver_name(sysfs_dir),
        "pci_slot": None,
        "pci_id": None,
        "detail": "",
    }

    vendor = (_read_text(sysfs_dir / "vendor") or "").lower()
    device = (_read_text(sysfs_dir / "device") or "").lower()
    uevent = _read_kv_file(sysfs_dir / "uevent")
    pci_slot = (uevent.get("PCI_SLOT_NAME") or "").strip() or _guess_pci_slot(sysfs_dir)

    result["pci_slot"] = pci_slot or None
    result["pci_id"] = _format_pci_id(vendor, device)
    if not result["driver"]:
        result["driver"] = (uevent.get("DRIVER") or "").strip() or None

    if vendor != _INTEL_VENDOR_ID:
        result["detail"] = "检测到的设备不是 Intel GPU"
        return result

    model_name = _read_lspci_name(pci_slot) if pci_slot else None
    if not model_name:
        model_name = _fallback_model_name(result["pci_id"], result["driver"])

    result["available"] = True
    result["model_name"] = model_name
    result["detail"] = "已检测到 Intel 硬件"
    return result


def _read_engine_busy_snapshot(sysfs_dir: Path) -> dict[str, int]:
    engine_root = sysfs_dir / "engine"
    out: dict[str, int] = {}
    if engine_root.exists():
        for busy_path in engine_root.glob("*/busy"):
            raw = _read_text(busy_path)
            try:
                value = int(str(raw or "").strip())
            except Exception:
                continue
            name = busy_path.parent.name.strip() or "engine"
            out[name] = value
    if out:
        return out

    # Some kernels expose aggregate busy counters under the device directory.
    for name in ["busy", "gt/gt0/rps_busy", "gt/gt0/busy"]:
        path = sysfs_dir / name
        raw = _read_text(path)
        try:
            value = int(str(raw or "").strip())
        except Exception:
            continue
        out[name.replace("/", ".")] = value
    return out


def _render_sysfs_device_dir(node: Path) -> Path | None:
    name = node.name
    if name:
        direct = Path("/sys/class/drm") / name / "device"
        if direct.exists():
            return direct

    try:
        st = node.stat()
    except Exception:
        return None

    dev_char = Path("/sys/dev/char") / f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    if not dev_char.exists():
        return None

    resolved = dev_char.resolve()
    candidate = resolved / "device"
    if candidate.exists():
        return candidate
    return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _read_kv_file(path: Path) -> dict[str, str]:
    raw = _read_text(path) or ""
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _read_driver_name(sysfs_dir: Path) -> str | None:
    driver_link = sysfs_dir / "driver"
    if not driver_link.exists() and not driver_link.is_symlink():
        return None
    try:
        return driver_link.resolve().name or None
    except Exception:
        return None


def _guess_pci_slot(sysfs_dir: Path) -> str | None:
    name = sysfs_dir.resolve().name
    return name if ":" in name else None


def _format_pci_id(vendor: str, device: str) -> str | None:
    vendor_clean = vendor.removeprefix("0x").strip()
    device_clean = device.removeprefix("0x").strip()
    if not vendor_clean or not device_clean:
        return None
    return f"{vendor_clean}:{device_clean}"


def _read_lspci_name(pci_slot: str | None) -> str | None:
    if not pci_slot:
        return None
    if not shutil.which("lspci"):
        return None
    try:
        proc = subprocess.run(
            ["lspci", "-s", pci_slot],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None

    line = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
    if not line:
        return None

    prefix = f"{pci_slot} "
    if line.startswith(prefix):
        line = line[len(prefix) :].strip()
    if ": " in line:
        _, line = line.split(": ", 1)
    return line.strip() or None


def _fallback_model_name(pci_id: str | None, driver: str | None) -> str:
    parts = ["Intel GPU"]
    if pci_id:
        parts.append(f"(PCI {pci_id})")
    if driver:
        parts.append(f"[driver: {driver}]")
    return " ".join(parts)
