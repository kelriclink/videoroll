"""Choose voice references from the original ASR, never translated subtitles.

ASR timing and text are useful alignment hints, not speaker diarization.
The API exposes ranked options and requires explicit voice-use authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from videoroll.db.models import Asset, AssetKind, SubtitleJob
from videoroll.storage.filesystem import FileStore


@dataclass(frozen=True)
class SpeechCue:
    start: float
    end: float
    text: str
    confidence: float | None
    speaker: str | None


def _original_cues(raw: Any) -> list[SpeechCue]:
    if not isinstance(raw, list) or len(raw) > 25_000:
        return []
    result: list[SpeechCue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start, end = float(item['start']), float(item['end'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        text = str(item.get('text') or '').strip()
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or
            not 0.25 <= end - start <= 24 or end > 48 * 3600 or
            not 2 <= len(text) <= 400):
            continue
        c = item.get('confidence')
        try:confidence = float(c) if c is not None else None
        except (ValueError, TypeError):confidence = None
        if confidence is not None and not (0 <= confidence <= 1):
            confidence = None
        speaker = item.get('speaker_id', item.get('speaker'))
        result.append(SpeechCue(start, end, text, confidence,
                                str(speaker) if speaker is not None else None))
    return sorted(result, key=lambda c: (c.start, c.end))


def rank_reference_candidates(raw: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    """Find clear-ish, single contiguous ASR utterances, target 6–15 seconds.

    No unsupported claim of speaker identity or background music isolation:
    those require separate diarization/separation analysis.
    """
    cues = _original_cues(raw)
    options: list[dict[str, Any]] = []
    for begin, first in enumerate(cues):
        if first.confidence is not None and first.confidence < 0.38:
            continue
        end, parts = first.end, [first]
        for next_cue in cues[begin+1:]:
            gap = next_cue.start-end
            if gap < -0.12 or gap > 0.75:
                break
            if first.speaker is not None and next_cue.speaker is not None and first.speaker != next_cue.speaker:
                break
            if next_cue.confidence is not None and next_cue.confidence < 0.38:
                break
            if next_cue.end-first.start > 16.0:
                break
            parts.append(next_cue)
            end = max(end,next_cue.end)
            if len(parts) >= 20:
                break
        # Generate both a natural single utterance and 1..N contiguous joins.
        for end_index in range(1,len(parts)+1):
            subset=parts[:end_index]
            duration=subset[-1].end-first.start
            if duration < 3 or duration > 16:
                continue
            actual_speech = sum(c.end-c.start for c in subset)
            coverage = min(1.0,actual_speech/duration)
            if coverage < 0.69:
                continue
            transcript = ' '.join(c.text for c in subset).strip()
            if len(transcript) < 12 or len(transcript) > 1000:
                continue
            confidences=[c.confidence for c in subset if c.confidence is not None]
            avg_conf=sum(confidences)/len(confidences) if confidences else None
            # Prefer a natural 8–12s sample, sustained speech and ASR confidence.
            score=(100 - 6.0*abs(duration-9.0) + 30*coverage
                   + (15*avg_conf if avg_conf is not None else 0)
                   - (8 if first.start < 8 else 0)
                   - (3 if len(subset)>7 else 0))
            options.append({
                'start_seconds':round(first.start,3),
                'duration_seconds':round(duration,3),
                'reference_text':transcript,
                'speech_coverage':round(coverage,3),
                'confidence':round(avg_conf,3) if avg_conf is not None else None,
                'score':round(score,3),
                'speaker_identified':first.speaker is not None,
            })
    # Do not offer three heavily overlapping recordings of the same sentence.
    selected: list[dict[str,Any]]=[]
    for option in sorted(options,key=lambda v: v['score'],reverse=True):
        start,end=option['start_seconds'],option['start_seconds']+option['duration_seconds']
        if any(max(0,min(end,other['start_seconds']+other['duration_seconds'])-
                   max(start,other['start_seconds']))>min(end-start,other['duration_seconds'])*0.35
               for other in selected):
            continue
        selected.append(option)
        if len(selected)>=limit:
            break
    for index,item in enumerate(selected):
        item['index']=index
    return selected


def source_asr_for_job(db: Session, store: FileStore, job: SubtitleJob) -> tuple[list, str]:
    """Resolve original ASR for one subtitle job without mixing job revisions.

    Recent jobs pin ASR key. Legacy jobs only use unambiguous unique segment
    asset. Multiple legacy source artifacts are rejected instead of guessing.
    """
    artifacts=(job.request_json or {}).get('artifacts') or {}
    pinned=artifacts.get('source_segments_key') if isinstance(artifacts,dict) else None
    assets=db.query(Asset).filter(Asset.task_id==job.task_id,
                                  Asset.kind==AssetKind.segments_json).all()
    if pinned:
        if not any(a.storage_key==pinned for a in assets):
            raise ValueError('original ASR asset for this subtitle job was not found')
        key=pinned
    elif len(assets)==1:
        key=assets[0].storage_key
    elif not assets:
        raise ValueError('no original-language ASR segments available')
    else:
        raise ValueError('multiple ASR versions found; re-run subtitles to associate the correct transcript')
    try:
        raw=json.loads(store.path_for(key).read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        raise ValueError('original ASR segment data is missing or unreadable') from exc
    if not isinstance(raw,list):
        raise ValueError('original ASR data is not a segment list')
    return raw,key
