from __future__ import annotations

import os
import shutil
import subprocess
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
