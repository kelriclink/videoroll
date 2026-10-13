from pathlib import Path

import pytest

from videoroll.apps.dubbing.mux import build_dubbed_part_command


def _command(**kwargs):
    return build_dubbed_part_command(
        "ffmpeg", Path("subtitle.mp4"), Path("speech.wav"),
        Path("dubbed.mp4"), video_duration=120.0,
        original_audio_present=True, **kwargs,
    )


def test_p2_reuses_encoded_p1_video_and_replaces_audio() -> None:
    cmd = _command()
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "[out]" in cmd
    assert cmd[cmd.index("-t") + 1] == "120.000000"
    assert "0:a:0" not in cmd[cmd.index("-filter_complex") + 1]
    assert cmd[-1] == "dubbed.mp4"


def test_optional_original_audio_low_volume_is_explicit() -> None:
    cmd = _command(retain_original_audio=True, original_volume=0.08)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=0.0800" in graph
    assert "amix=inputs=2" in graph
    assert "alimiter=" in graph


@pytest.mark.parametrize("opts", [
    {"video_duration": 0},
    {"original_volume": 2},
    {"original_audio_present": False, "retain_original_audio": True},
])
def test_invalid_mux_parameters_are_rejected(opts) -> None:
    params = {
        "video_duration": 120.0,
        "original_audio_present": True,
        "retain_original_audio": False,
        "original_volume": 0.1,
    }
    params.update(opts)
    with pytest.raises(ValueError):
        build_dubbed_part_command(
            "ffmpeg", Path("p1.mp4"), Path("voice.wav"), Path("p2.mp4"), **params,
        )
