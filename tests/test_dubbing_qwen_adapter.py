from __future__ import annotations

import sys
import types
import wave

import pytest

from videoroll.apps.dubbing.qwen_tts import QwenCustomVoice


def test_qwen_adapter_uses_fp16_sdpa_and_official_customvoice_api(tmp_path, monkeypatch):
    configs = []
    calls = []

    class FakeQwenModel:
        @classmethod
        def from_pretrained(cls, model_path, **kwargs):
            configs.append((model_path, kwargs))
            return cls()

        def generate_custom_voice(self, **kwargs):
            calls.append(kwargs)
            return [[0.1, -0.1, 0.0] * 500], 24000

    fake_torch = types.ModuleType("torch")
    fake_torch.float16 = "fp16"
    fake_torch.float32 = "fp32"
    fake_qwen = types.ModuleType("qwen_tts")
    fake_qwen.Qwen3TTSModel = FakeQwenModel
    fake_sf = types.ModuleType("soundfile")

    def write(path, _samples, sample_rate, subtype="PCM_16"):
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"\x00\x00" * 1500)

    fake_sf.write = write
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "qwen_tts", fake_qwen)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    adapter = QwenCustomVoice(model_path="local-model", voice="Serena")
    output = tmp_path / "spoken.wav"
    adapter.synthesize("这里是中文配音", output)
    assert output.is_file()
    assert configs == [("local-model", {
        "device_map": "cuda:0", "dtype": "fp16", "attn_implementation": "sdpa",
    })]
    assert calls == [{"text": "这里是中文配音", "language": "Chinese", "speaker": "Serena"}]


def test_qwen_adapter_rejects_unsupported_speaker_before_loading_weights():
    with pytest.raises(ValueError, match="unsupported"):
        QwenCustomVoice(voice="Unknown")
