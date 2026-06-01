from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from videoroll.utils.intel_gpu import _probe_intel_sysfs, detect_intel_hardware


class IntelGpuDetectionTests(unittest.TestCase):
    def test_probe_intel_sysfs_uses_lspci_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "vendor").write_text("0x8086\n", encoding="utf-8")
            (root / "device").write_text("0x46a6\n", encoding="utf-8")
            (root / "uevent").write_text("PCI_SLOT_NAME=0000:00:02.0\nDRIVER=i915\n", encoding="utf-8")

            with patch("videoroll.utils.intel_gpu._read_lspci_name", return_value="Intel Corporation Iris Xe Graphics"):
                info = _probe_intel_sysfs(root, "/dev/dri/renderD128")

        self.assertTrue(info["available"])
        self.assertEqual(info["model_name"], "Intel Corporation Iris Xe Graphics")
        self.assertEqual(info["driver"], "i915")
        self.assertEqual(info["pci_slot"], "0000:00:02.0")
        self.assertEqual(info["pci_id"], "8086:46a6")

    def test_probe_intel_sysfs_falls_back_when_lspci_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "vendor").write_text("0x8086\n", encoding="utf-8")
            (root / "device").write_text("0x9a49\n", encoding="utf-8")
            (root / "uevent").write_text("PCI_SLOT_NAME=0000:00:02.0\nDRIVER=xe\n", encoding="utf-8")

            with patch("videoroll.utils.intel_gpu._read_lspci_name", return_value=None):
                info = _probe_intel_sysfs(root, "/dev/dri/renderD128")

        self.assertTrue(info["available"])
        self.assertEqual(info["model_name"], "Intel GPU (PCI 8086:9a49) [driver: xe]")

    def test_probe_intel_sysfs_rejects_non_intel_vendor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "vendor").write_text("0x1002\n", encoding="utf-8")
            (root / "device").write_text("0x164e\n", encoding="utf-8")
            (root / "uevent").write_text("PCI_SLOT_NAME=0000:03:00.0\nDRIVER=amdgpu\n", encoding="utf-8")

            info = _probe_intel_sysfs(root, "/dev/dri/renderD128")

        self.assertFalse(info["available"])
        self.assertEqual(info["detail"], "检测到的设备不是 Intel GPU")

    def test_detect_intel_hardware_reports_missing_render_node(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "renderD999")
            info = detect_intel_hardware(missing)

        self.assertFalse(info["available"])
        self.assertIn("未找到渲染设备", info["detail"])


if __name__ == "__main__":
    unittest.main()
