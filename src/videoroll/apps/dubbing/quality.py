"""Bounded, versioned, deterministic manual dubbing settings.

Never silently truncate translated speech to fit a timeline.
"""
from __future__ import annotations
import hashlib
import json
from typing import Any

ALLOWED_MODES = ("replace", "original_low", "background_only")
MAX_BATCH = 4
ACTIVE = frozenset(("queued", "preparing", "synthesizing", "aligning", "mixing",
                    "pause_requested", "cancel_requested", "preview_ready", "needs_review", "paused"))
RUNNING = frozenset(("preparing", "synthesizing", "aligning", "mixing"))

def normalize_quality(*, instruct: str = "", temperature: float = 0.9,
                      top_p: float = 0.9, batch_size: int = 1,
                      mix_mode: str = "replace", model: str = "",
                      cosy_speed: float = 1.0, cosy_mode: str = "cross_lingual",
                      profile_id: str = "", profile_sha256: str = "",
                      debug_timing: bool = False,
                      auto_group_speech: bool = False,
                      auto_rewrite_speech: bool = False) -> dict[str, Any]:
    if len(instruct) > 120 or any(ord(c) < 32 for c in instruct):
        raise ValueError("invalid TTS speaking instruction")
    if not (0.3 <= temperature <= 1.5 and 0.6 <= top_p <= 1.0):
        raise ValueError("TTS sampling parameters are out of range")
    if isinstance(batch_size, bool) or batch_size not in (1, 2, 4):
        raise ValueError("batch size must be 1, 2 or 4")
    if mix_mode not in ALLOWED_MODES:
        raise ValueError("unsupported audio mix mode")
    if instruct.strip() and "0.6B" in model:
        raise ValueError("0.6B CustomVoice does not support speaking instructions")
    if model.startswith('cosyvoice3:'):
        if not 0.8 <= cosy_speed <= 1.25:
            raise ValueError('CosyVoice3 speed out of range')
        if cosy_mode not in ('cross_lingual', 'zero_shot', 'instruct'):
            raise ValueError('invalid CosyVoice3 synthesis mode')
        if batch_size != 1:
            raise ValueError('CosyVoice3 currently supports sequential inference only')
        if not profile_id or not profile_sha256:
            raise ValueError('CosyVoice3 requires an authorized reference voice')
    if not all(isinstance(flag, bool) for flag in (debug_timing, auto_group_speech, auto_rewrite_speech)):
        raise ValueError("debug or natural speech settings must be boolean")
    return {"auto_group_speech": auto_group_speech,
            "auto_rewrite_speech": auto_rewrite_speech,
            "debug_timing": debug_timing,
            "cosy_speed": float(cosy_speed), "cosy_mode": cosy_mode,
            "profile_id": profile_id, "profile_sha256": profile_sha256,
            "instruct": instruct.strip(), "temperature": float(temperature),
            "top_p": float(top_p), "batch_size": int(batch_size), "mix_mode": mix_mode}


def audio_cache_key(*, text: str, voice: str, model: str, quality: dict[str, Any],
                    model_revision: str = "local-managed") -> str:
    relevant = {key: quality.get(key) for key in (
        "instruct", "temperature", "top_p", "cosy_speed", "cosy_mode",
        "profile_id", "profile_sha256", "_sample_nonce") if key in quality}
    payload = {"schema": 2, "text": text, "voice": voice,
               "model": model, "model_revision": quality.get("_model_revision", model_revision), "language": "Chinese",
               "generation": relevant}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
