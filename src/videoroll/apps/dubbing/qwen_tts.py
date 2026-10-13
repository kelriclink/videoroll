"""Lazy Qwen3-TTS CustomVoice synthesizer.

The package and GPU weights are deliberately loaded only inside a dedicated
dubbing worker, not during imports by the Orchestrator or RenderWorkers.
"""
from __future__ import annotations

from pathlib import Path


CHINESE_SPEAKERS = frozenset({"Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric"})


class QwenCustomVoice:
    def __init__(
        self,
        model_path: str = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        *,
        device: str = "cuda:0",
        voice: str = "Vivian",
    ):
        if voice not in CHINESE_SPEAKERS:
            raise ValueError(f"unsupported Chinese voice: {voice}")
        # Explicitly avoid BF16/FlashAttention2: Tesla V100 is SM 7.0.
        import torch
        from qwen_tts import Qwen3TTSModel

        self.model_path = model_path
        self.voice = voice
        self.model = Qwen3TTSModel.from_pretrained(
            model_path,
            device_map=device,
            dtype=torch.float16 if device.startswith("cuda") else torch.float32,
            attn_implementation="sdpa",
        )

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        import soundfile as sf

        speaker = voice or self.voice
        if speaker not in CHINESE_SPEAKERS:
            raise ValueError(f"unsupported Chinese voice: {speaker}")
        if not text.strip():
            raise ValueError("TTS text is empty")
        wavs, sr = self.model.generate_custom_voice(
            text=text.strip(), language="Chinese", speaker=speaker,
        )
        if not wavs or sr <= 0:
            raise RuntimeError("Qwen3-TTS returned an empty audio response")
        output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(output, wavs[0], sr, subtype="PCM_16")
