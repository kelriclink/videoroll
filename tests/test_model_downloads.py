from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from videoroll.apps.subtitle_service.model_downloads import (
    default_model_dir_name,
    download_model_snapshot,
    resolve_model_repo_id,
)


class ModelDownloadsTests(unittest.TestCase):
    def test_default_dir_names_do_not_collide_between_engines(self) -> None:
        self.assertEqual(default_model_dir_name("faster-whisper", "small"), "small")
        self.assertEqual(default_model_dir_name("openvino", "small"), "whisper-small-fp16-ov")

    def test_openvino_known_size_maps_to_official_repo(self) -> None:
        self.assertEqual(resolve_model_repo_id("openvino", "large-v3"), "OpenVINO/whisper-large-v3-fp16-ov")

    def test_download_model_snapshot_uses_repo_mapping_and_writes_dest(self) -> None:
        fake_hf = types.ModuleType("huggingface_hub")
        seen: dict[str, object] = {}

        def snapshot_download(*, repo_id: str, revision: str | None = None, local_dir: str) -> None:
            seen["repo_id"] = repo_id
            seen["revision"] = revision
            seen["local_dir"] = local_dir
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "openvino_encoder_model.xml").write_text("<xml />", encoding="utf-8")

        fake_hf.snapshot_download = snapshot_download  # type: ignore[attr-defined]
        prev = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = fake_hf
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                model_dir = Path(tmpdir)
                name = default_model_dir_name("openvino", "small")
                dest = download_model_snapshot(
                    engine="openvino",
                    model="small",
                    model_dir=model_dir,
                    name=name,
                    revision="main",
                )
                self.assertEqual(dest, model_dir / "whisper-small-fp16-ov")
                self.assertTrue((dest / "openvino_encoder_model.xml").exists())
                self.assertEqual(seen["repo_id"], "OpenVINO/whisper-small-fp16-ov")
                self.assertEqual(seen["revision"], "main")
        finally:
            if prev is None:
                sys.modules.pop("huggingface_hub", None)
            else:
                sys.modules["huggingface_hub"] = prev


if __name__ == "__main__":
    unittest.main()
