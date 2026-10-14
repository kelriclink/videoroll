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
        cache_identity: str | None = None,
        instruct: str = "",
        temperature: float = 0.9,
        top_p: float = 0.9,
    ):
        if voice not in CHINESE_SPEAKERS:
            raise ValueError(f"unsupported Chinese voice: {voice}")
        # Explicitly avoid BF16/FlashAttention2: Tesla V100 is SM 7.0.
        import torch
        from qwen_tts import Qwen3TTSModel

        self.model_path = cache_identity or model_path
        self.voice = voice
        self.generation = ({"instruct": instruct} if instruct else {})
        if temperature != 0.9:
            self.generation["temperature"] = temperature
        if top_p != 0.9:
            self.generation["top_p"] = top_p
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
            text=text.strip(), language="Chinese", speaker=speaker, **self.generation,
        )
        if not wavs or sr <= 0:
            raise RuntimeError("Qwen3-TTS returned an empty audio response")
        output.parent.mkdir(parents=True, exist_ok=True)
        sf.write(output, wavs[0], sr, subtype="PCM_16")

    def synthesize_batch(self, texts: list[str], outputs: list[Path], *,
                         voices: list[str]) -> None:
        """Qwen official list-valued CustomVoice inference, at most 4 items."""
        import soundfile as sf
        if not 2 <= len(texts) <= 4 or len(texts) != len(outputs) or len(voices) != len(texts):
            raise ValueError("invalid Qwen3-TTS batch dimensions")
        if any(not t.strip() for t in texts):
            raise ValueError("empty batch text")
        wavs, rate = self.model.generate_custom_voice(
            text=[t.strip() for t in texts], language=["Chinese"] * len(texts),
            speaker=voices, **self.generation,
        )
        if len(wavs) != len(texts) or rate <= 0:
            raise RuntimeError("Qwen TTS batch returned incomplete audio")
        for wav, target in zip(wavs, outputs):
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".pending.wav")
            try:
                sf.write(temp, wav, rate, subtype="PCM_16")
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
