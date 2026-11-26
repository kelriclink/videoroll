import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BRIDGE = Path(__file__).resolve().parents[1] / "scripts/native/local_inference.py"
SPEC = importlib.util.spec_from_file_location("videoroll_local_inference_bridge", BRIDGE)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class LocalInferenceGpuLockTests(unittest.TestCase):
    def test_openvino_devices_are_normalized_and_cpu_is_not_locked(self):
        self.assertEqual(bridge._gpu_lock_device({"kind": "asr", "engine": "openvino", "device": "GPU"}), "GPU.0")
        self.assertEqual(bridge._gpu_lock_device({"kind": "asr", "engine": "openvino", "device": "AUTO"}), "GPU.0")
        self.assertEqual(bridge._gpu_lock_device({"kind": "asr", "engine": "faster-whisper", "device": "cuda"}), "CUDA")
        self.assertIsNone(bridge.lock_gpu({"kind": "asr", "engine": "openvino", "device": "CPU"}))

    def test_gpu_lock_is_device_scoped_and_uses_secure_slot_files(self):
        with tempfile.TemporaryDirectory() as root:
            base = str(Path(root) / ".local-inference-gpu.lock")
            env = {"VIDEOROLL_LOCAL_GPU_LOCK": base, "VIDEOROLL_LOCAL_GPU_SLOTS": "2"}
            fds = []
            try:
                with patch.dict(os.environ, env, clear=False):
                    fds.append(bridge.lock_gpu({"kind": "asr", "engine": "openvino", "device": "GPU"}))
                    fds.append(bridge.lock_gpu({"kind": "asr", "engine": "openvino", "device": "GPU.1"}))
                files = sorted(Path(root).iterdir())
                self.assertEqual(len(files), 2)
                for path in files:
                    info = path.stat()
                    self.assertTrue(stat.S_ISREG(info.st_mode))
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    self.assertEqual(info.st_nlink, 1)
                self.assertTrue(all(fd is not None for fd in fds))
            finally:
                for fd in fds:
                    if fd is not None:
                        os.close(fd)

    def test_gpu_slot_count_is_bounded(self):
        with patch.dict(os.environ, {"VIDEOROLL_LOCAL_GPU_SLOTS": "99"}, clear=False):
            self.assertEqual(bridge._gpu_lock_slots(), 8)
        with patch.dict(os.environ, {"VIDEOROLL_LOCAL_GPU_SLOTS": "0"}, clear=False):
            self.assertEqual(bridge._gpu_lock_slots(), 1)


if __name__ == "__main__":
    unittest.main()
