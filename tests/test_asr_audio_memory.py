from __future__ import annotations

from array import array
from pathlib import Path
import struct
import sys
import tracemalloc
from types import ModuleType, SimpleNamespace
import wave
import weakref

import pytest

from videoroll.apps.subtitle_service import processing


def _write_wav(path: Path, *, seconds: float, sample: int = 2048) -> None:
    remaining = round(seconds * 16000)
    block = struct.pack("<h", sample) * 4096
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        while remaining:
            count = min(remaining, 4096)
            target.writeframesraw(block[: count * 2])
            remaining -= count


def _write_sparse_wav(path: Path, *, seconds: int, first_sample: int = 2048) -> None:
    """Represent hours of real, readable PCM without allocating hours of audio."""
    data_size = seconds * 16000 * 2
    with path.open("wb") as target:
        target.write(
            struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16,
                1, 1, 16000, 32000, 2, 16, b"data", data_size,
            )
        )
        target.write(struct.pack("<h", first_sample))
        target.seek(44 + data_size - 1)
        target.write(b"\x00")


def _limit_reads(monkeypatch, *, maximum: int) -> list[int]:
    reads: list[int] = []
    original = wave.Wave_read.readframes

    def readframes(source, frame_count: int):
        reads.append(frame_count)
        # Fail before the old implementation can allocate a complete long WAV.
        assert 0 <= frame_count <= maximum, f"unbounded WAV read: {frame_count} frames"
        return original(source, frame_count)

    monkeypatch.setattr(wave.Wave_read, "readframes", readframes)
    return reads


def _small_windows(monkeypatch) -> None:
    monkeypatch.setattr(processing, "_LOCAL_ASR_WINDOW_SECONDS", 2.0, raising=False)
    monkeypatch.setattr(processing, "_LOCAL_ASR_OVERLAP_SECONDS", 0.5, raising=False)


def _chunk(start: float, end: float, text: str):
    return SimpleNamespace(start=start, end=end, text=text, no_speech_prob=0.1, avg_logprob=-0.1)


def test_silence_probe_bounds_reads_on_a_three_hour_wav(monkeypatch, tmp_path) -> None:
    path = tmp_path / "three-hours.wav"
    _write_sparse_wav(path, seconds=3 * 60 * 60)
    reads = _limit_reads(monkeypatch, maximum=65536)

    assert processing._audio_path_is_effectively_silent(path) is False
    assert reads and max(reads) <= 65536
    # A peak above the threshold is conclusive without scanning the remaining hours.
    assert sum(reads) <= 65536


@pytest.mark.parametrize("sample_width", [1, 2, 4])
@pytest.mark.parametrize("kind", ["empty", "silence", "quiet", "rms_only", "diluted_prefix", "late_peak"])
def test_streaming_silence_preserves_complete_signal_statistics(monkeypatch, tmp_path, sample_width, kind) -> None:
    scale = {1: 128, 2: 32768, 4: 2147483648}[sample_width]
    moderate = max(1, round(0.002 * scale))
    values = {
        "empty": [],
        "silence": [0] * 2048,
        "quiet": [round(0.0005 * scale)] * 2048,
        "rms_only": [round(0.001 * scale)] * 2048,
        "diluted_prefix": [moderate] * 64 + [0] * 1984,
        "late_peak": [0] * 2047 + [round(0.02 * scale)],
    }[kind]
    path = tmp_path / "signal.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(sample_width)
        target.setframerate(16000)
        if sample_width == 1:
            target.writeframes(bytes(value + 128 for value in values))
        else:
            target.writeframes(struct.pack("<" + ("h" if sample_width == 2 else "i") * len(values), *values))
    expected = processing._audio_is_effectively_silent(value / scale for value in values)
    monkeypatch.setattr(processing, "_ASR_PCM_READ_FRAMES", 64, raising=False)
    reads = _limit_reads(monkeypatch, maximum=64)

    assert processing._audio_path_is_effectively_silent(path) is expected
    assert not reads or max(reads) <= 64
    if expected:
        assert sum(reads) >= len(values)


def test_wav_window_uses_packed_float32_storage(tmp_path) -> None:
    path = tmp_path / "window.wav"
    _write_wav(path, seconds=1.0, sample=-16384)

    samples = processing._read_wav_window_as_float_mono_16k(path, offset=0.25, duration=0.5)

    assert isinstance(samples, array)
    assert samples.typecode == "f" and samples.itemsize == 4
    assert len(samples) == 8000
    assert samples[0] == samples[-1] == -0.5


def test_openvino_first_window_has_bounded_peak_and_does_not_read_ahead(monkeypatch, tmp_path) -> None:
    path = tmp_path / "three-hours.wav"
    _write_sparse_wav(path, seconds=3 * 60 * 60)
    reads = _limit_reads(monkeypatch, maximum=120 * 16000)
    seen: list[tuple[int, int]] = []

    class StopAfterFirstWindow(Exception):
        pass

    def generate(samples, **kwargs):
        assert isinstance(samples, array) and samples.typecode == "f"
        seen.append((len(samples), samples.itemsize))
        raise StopAfterFirstWindow

    monkeypatch.setattr(processing, "_get_openvino_pipeline", lambda *args: SimpleNamespace(generate=generate))
    tracemalloc.start()
    try:
        with pytest.raises(StopAfterFirstWindow):
            processing.transcribe_openvino_whisper(path, model_name="/models/large-v3", vad_enabled=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert seen == [(120 * 16000, 4)]
    assert sum(reads) <= 120 * 16000 + 65536
    assert peak < 24 * 1024 * 1024


@pytest.mark.parametrize("vad_enabled", [False, True])
def test_openvino_window_offsets_and_overlap_reconciliation(monkeypatch, tmp_path, vad_enabled) -> None:
    _small_windows(monkeypatch)
    path = tmp_path / "overlap.wav"
    _write_wav(path, seconds=4.2)
    reads = _limit_reads(monkeypatch, maximum=32000)
    calls: list[int] = []
    results = [
        [_chunk(1.2, 2.0, "A shared boundary phrase")],
        [_chunk(0.0, 0.8, "A shared boundary phrase"), _chunk(1.1, 1.8, "Later speech")],
        [_chunk(0.0, 0.5, "Later speech"), _chunk(0.7, 1.2, "Tail speech")],
    ]

    def generate(samples, **kwargs):
        assert isinstance(samples, array) and samples.itemsize == 4
        calls.append(len(samples))
        assert len(reads) == len(calls)
        assert kwargs["language"] == "<|ja|>"
        assert kwargs["num_beams"] == 3
        assert kwargs["max_new_tokens"] == 640
        return SimpleNamespace(chunks=results[len(calls) - 1])

    monkeypatch.setattr(processing, "_get_openvino_pipeline", lambda *args: SimpleNamespace(generate=generate))
    # Missing VAD must retain bounded transcription and real speech.
    monkeypatch.setattr(processing, "_detect_silero_speech_spans", lambda *args, **kwargs: None)
    segments = processing.transcribe_openvino_whisper(
        path, model_name="/models/large-v3", language="ja", device="GPU.1",
        num_beams=3, max_new_tokens=640, vad_enabled=vad_enabled,
    )

    assert calls == [32000, 32000, 19200]
    assert [(item.start, item.end) for item in segments] == pytest.approx([(1.2, 2.3), (2.6, 3.5), (3.7, 4.2)])
    assert [item.text for item in segments] == ["A shared boundary phrase", "Later speech", "Tail speech"]


def test_openvino_vad_slices_are_lazy_and_include_window_offsets(monkeypatch, tmp_path) -> None:
    _small_windows(monkeypatch)
    path = tmp_path / "speech.wav"
    _write_wav(path, seconds=4.3)
    reads = _limit_reads(monkeypatch, maximum=32000)
    vad_calls: list[int] = []
    audio_refs: list[weakref.ReferenceType] = []

    def vad(samples, **kwargs):
        assert isinstance(samples, array) and len(samples) <= 32000
        vad_calls.append(len(samples))
        return [
            processing._OpenVinoSpeechSpan(4000, 8000),
            processing._OpenVinoSpeechSpan(16000, 20000),
        ]

    def generate(samples, **kwargs):
        # Do not retain samples in this fake: that would hide eager production allocations.
        assert isinstance(samples, array) and len(samples) == 4000
        assert len(vad_calls) == len(audio_refs) // 2 + 1
        assert len(reads) == len(vad_calls)
        assert sum(ref() is not None for ref in audio_refs) <= 1
        audio_refs.append(weakref.ref(samples))
        return SimpleNamespace(chunks=[_chunk(0, 0.25, "Spoken phrase")])

    monkeypatch.setattr(processing, "_detect_silero_speech_spans", vad)
    monkeypatch.setattr(processing, "_get_openvino_pipeline", lambda *args: SimpleNamespace(generate=generate))
    segments = processing.transcribe_openvino_whisper(path, model_name="/models/large-v3")

    assert vad_calls == [32000, 32000, 20800]
    assert [segment.start for segment in segments] == [0.25, 1.0, 1.75, 2.5, 3.25, 4.0]
    assert all(ref() is None for ref in audio_refs)


def test_openvino_cache_releases_previous_model_before_constructing_replacement(monkeypatch) -> None:
    monkeypatch.setattr(processing, "_OPENVINO_PIPELINE_CACHE", {})
    previous_ref = None
    created: list[tuple[str, str]] = []

    class Pipeline:
        def __init__(self, model_path, *, device):
            if previous_ref is not None:
                assert previous_ref() is None
                assert processing._OPENVINO_PIPELINE_CACHE == {}
            created.append((model_path, device))

    module = ModuleType("openvino_genai")
    module.WhisperPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "openvino_genai", module)
    first = processing._get_openvino_pipeline("/models/first", "GPU")
    assert processing._get_openvino_pipeline("/models/first", "GPU") is first
    previous_ref = weakref.ref(first)
    del first

    second = processing._get_openvino_pipeline("/models/second", "GPU.1")

    assert processing._OPENVINO_PIPELINE_CACHE == {("/models/second", "GPU.1"): second}
    assert created == [("/models/first", "GPU"), ("/models/second", "GPU.1")]


def _fake_faster_whisper(monkeypatch, transcribe):
    constructed: list[tuple[str, dict]] = []

    def model(model_name, **kwargs):
        constructed.append((model_name, kwargs))
        return SimpleNamespace(transcribe=transcribe)

    module = ModuleType("faster_whisper")
    module.WhisperModel = model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return constructed


def test_faster_whisper_long_wav_reuses_model_and_bounds_numpy_decode(monkeypatch, tmp_path) -> None:
    np = pytest.importorskip("numpy")
    _small_windows(monkeypatch)
    path = tmp_path / "overlap.wav"
    _write_wav(path, seconds=4.2)
    _limit_reads(monkeypatch, maximum=65536)
    calls: list[tuple[int, dict]] = []
    results = [
        [_chunk(-0.25, 0.5, "First phrase"), _chunk(1.2, 2.0, "A shared boundary phrase")],
        [_chunk(0.0, 0.8, "A shared boundary phrase"), _chunk(1.1, 1.8, "Later speech")],
        [_chunk(0.0, 0.5, "Later speech"), _chunk(0.7, 8.0, "Tail speech")],
    ]

    def transcribe(audio, language=None, vad_filter=False, vad_parameters=None, condition_on_previous_text=True,
                   word_timestamps=False, no_speech_threshold=None, log_prob_threshold=None,
                   compression_ratio_threshold=None, hallucination_silence_threshold=None):
        assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
        assert audio.ndim == 1 and audio.flags.c_contiguous
        assert audio[0] == 2048 / 32768
        calls.append((len(audio), {
            "language": language, "vad_filter": vad_filter, "vad_parameters": vad_parameters,
            "condition_on_previous_text": condition_on_previous_text, "word_timestamps": word_timestamps,
            "no_speech_threshold": no_speech_threshold, "log_prob_threshold": log_prob_threshold,
            "compression_ratio_threshold": compression_ratio_threshold,
            "hallucination_silence_threshold": hallucination_silence_threshold,
        }))
        return iter(results[len(calls) - 1]), SimpleNamespace()

    constructed = _fake_faster_whisper(monkeypatch, transcribe)
    segments = processing.transcribe_faster_whisper(
        path, model_name="large-v3", language="ja", device="cpu", compute_type="int8",
        cpu_threads=2, num_workers=1,
    )

    assert constructed == [("large-v3", {"device": "cpu", "compute_type": "int8", "cpu_threads": 2, "num_workers": 1})]
    assert [count for count, _ in calls] == [32000, 32000, 19200]
    assert all(kwargs == {
        "language": "ja", "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 2000, "speech_pad_ms": 400},
        "condition_on_previous_text": True, "word_timestamps": True, "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0, "compression_ratio_threshold": 2.4,
        "hallucination_silence_threshold": 1.0,
    } for _, kwargs in calls)
    assert [(item.start, item.end) for item in segments] == pytest.approx([(0.0, 0.5), (1.2, 2.3), (2.6, 3.5), (3.7, 4.2)])
    assert [item.text for item in segments] == ["First phrase", "A shared boundary phrase", "Later speech", "Tail speech"]
    assert all(item.confidence == 0.9 for item in segments)


def test_faster_whisper_regroups_words_across_model_segments(monkeypatch, tmp_path) -> None:
    path = tmp_path / "semantic.wav"
    _write_wav(path, seconds=4.0)
    first = _chunk(0.0, 1.6, "This is only")
    first.words = [
        SimpleNamespace(start=0.0, end=0.4, word=" This", probability=0.95),
        SimpleNamespace(start=0.4, end=0.8, word=" is", probability=0.95),
        SimpleNamespace(start=0.8, end=1.2, word=" only", probability=0.95),
    ]
    second = _chunk(1.2, 3.0, "one complete sentence.")
    second.words = [
        SimpleNamespace(start=1.2, end=1.6, word=" one", probability=0.95),
        SimpleNamespace(start=1.6, end=2.1, word=" complete", probability=0.95),
        SimpleNamespace(start=2.1, end=3.0, word=" sentence.", probability=0.95),
    ]
    seen: dict[str, object] = {}

    def transcribe(audio, word_timestamps=False, condition_on_previous_text=False):
        assert audio == str(path)
        seen["word_timestamps"] = word_timestamps
        seen["condition_on_previous_text"] = condition_on_previous_text
        return iter([first, second]), SimpleNamespace()

    _fake_faster_whisper(monkeypatch, transcribe)
    segments = processing.transcribe_faster_whisper(path, model_name="large-v3")

    assert seen == {"word_timestamps": True, "condition_on_previous_text": True}
    assert [(segment.start, segment.end, segment.text) for segment in segments] == [
        (0.0, 3.0, "This is only one complete sentence.")
    ]


@pytest.mark.parametrize("seconds", [0.5, 2.0])
def test_faster_whisper_short_wav_preserves_path_input(monkeypatch, tmp_path, seconds) -> None:
    _small_windows(monkeypatch)
    path = tmp_path / "short.wav"
    _write_wav(path, seconds=seconds)
    calls: list[str] = []

    def transcribe(audio, language=None):
        assert audio == str(path)
        assert language is None
        calls.append(audio)
        return iter([_chunk(0.1, 0.4, "Short speech")]), SimpleNamespace()

    constructed = _fake_faster_whisper(monkeypatch, transcribe)
    segments = processing.transcribe_faster_whisper(path, model_name="large-v3")

    assert len(constructed) == len(calls) == 1
    assert segments == [processing.Segment(0.1, 0.4, "Short speech", confidence=0.9)]


@pytest.mark.parametrize("during_iteration", [False, True])
def test_faster_whisper_empty_vad_window_does_not_drop_following_speech(monkeypatch, tmp_path, during_iteration) -> None:
    np = pytest.importorskip("numpy")
    _small_windows(monkeypatch)
    path = tmp_path / "empty-vad-window.wav"
    _write_wav(path, seconds=4.2)
    calls: list[int] = []

    def empty_segments():
        yield from ()
        raise ValueError("max() arg is an empty sequence")

    def transcribe(audio):
        assert isinstance(audio, np.ndarray)
        calls.append(len(audio))
        if len(calls) == 2:
            if during_iteration:
                return empty_segments(), SimpleNamespace()
            raise ValueError("max() arg is an empty sequence")
        return iter([_chunk(0.1, 0.5, "Speech survives")]), SimpleNamespace()

    _fake_faster_whisper(monkeypatch, transcribe)
    segments = processing.transcribe_faster_whisper(path, model_name="large-v3")

    assert calls == [32000, 32000, 19200]
    assert [segment.start for segment in segments] == [0.1, 3.1]


def test_external_whisper_plain_text_duration_reads_only_the_wav_header(monkeypatch, tmp_path) -> None:
    path = tmp_path / "three-hours.wav"
    _write_sparse_wav(path, seconds=3 * 60 * 60)
    reads = _limit_reads(monkeypatch, maximum=0)
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"text": "Spoken phrase"})
    monkeypatch.setattr(processing.httpx, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match="without segment/word timestamps"):
        processing.transcribe_external_whisper(
            path, base_url="https://api.example/v1", api_key="offline-test-key", model_name="test-whisper",
        )

    assert reads == []
