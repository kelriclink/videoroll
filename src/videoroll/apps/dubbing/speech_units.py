"""Plan natural TTS utterances separately from immutable display subtitles.

The source subtitles stay untouched; only internal TTS speech units are joined.
Never infer speakers from punctuation or silently rewrite arbitrary content.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from videoroll.apps.dubbing.timing import SpokenSegment


@dataclass(frozen=True)
class SpeechUnit:
    part: SpokenSegment
    source_indices: tuple[int, ...]


def normalize_spoken_text(text: str) -> str:
    """Remove subtitle layout line breaks without changing spoken numbers."""
    text = text.strip()
    text = re.sub(r"(?<=\d)\.\s*\n\s*(?=\d)", ".", text)
    text = re.sub(r"(?<=[\u3400-\u9fff])\s*\n\s*(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"(?<=[，。、；：！？])\s*\n\s*(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def _join(left: str, right: str) -> str:
    """Preserve original words, including potential intentional repetition."""
    a, b = normalize_spoken_text(left), normalize_spoken_text(right)
    if a.endswith(("，", ",", "、", "：", ":", "；", ";")):
        return a + b
    return a + (" " if a and b and a[-1].isascii() and b[0].isascii() else "") + b


def build_speech_units(
    parts: Sequence[SpokenSegment], *, enabled: bool,
    max_window: float = 15.0, max_characters: int = 240,
    join_gap: float = 0.35, min_fragment_duration: float = 2.3,
) -> list[SpeechUnit]:
    """Merge fragmented speech across contiguous captions, preserving order.

    No VAD or speaker diarization: voice here is the *chosen TTS voice*,
    not a verified identity. This experimental grouping must be reviewed for
    multi-speaker source videos. Never join gaps >350ms, source overlaps,
    different TTS voices or spans >15s. Non-merged text is unmodified except
    harmless display line-break normalization in this opt-in mode.
    """
    if not enabled:
        return [SpeechUnit(p, (p.index,)) for p in parts]
    if max_window <= 0 or max_characters <= 0 or join_gap < 0 or min_fragment_duration <= 0:
        raise ValueError("invalid speech grouping limits")
    result: list[SpeechUnit] = []
    n = len(parts)
    i = 0
    while i < n:
        first = parts[i]
        text = normalize_spoken_text(first.speech_text)
        source = [first.index]
        end = first.end
        j = i + 1
        while j < n:
            nxt = parts[j]
            gap = nxt.start - end
            if gap < -0.001 or gap > join_gap or nxt.voice != first.voice:
                break
            if nxt.end - first.start > max_window:
                break
            if len(text) + len(nxt.speech_text) > max_characters:
                break
            # A finished sentence stands alone. Otherwise merge short orphan
            # captions and comma/colon continuations into one coherent speech.
            current_span = end - first.start
            short_next = nxt.end - nxt.start < min_fragment_duration
            unfinished = text.rstrip().endswith(("，", ",", "、", "：", ":", "；", ";"))
            too_short = current_span < min_fragment_duration
            if text.rstrip().endswith(("。", "！", "？", "!", "?")):
                break
            if not (short_next or unfinished or too_short):
                break
            text = _join(text, nxt.speech_text)
            source.append(nxt.index)
            end = nxt.end
            j += 1
        part = SpokenSegment(
            index=first.index, start=first.start, end=end,
            subtitle_text="\n".join(p.subtitle_text for p in parts[i:j]),
            speech_text=text, voice=first.voice,
        )
        result.append(SpeechUnit(part, tuple(source)))
        i = j
    return result
