"""Qwen3-TTS subprocess for the unified, persistent VideoRoll dubbing service."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--model-path', required=True)
    parser.add_argument('--voice', required=True)
    parser.add_argument('--device', default='cuda:0')
    parser.add_argument('--instruct', default='')
    parser.add_argument('--temperature', type=float, default=0.9)
    parser.add_argument('--top-p', type=float, default=0.9)
    args = parser.parse_args()
    protocol = os.fdopen(os.dup(1), 'w', buffering=1, encoding='utf-8')
    sys.stdout = sys.stderr

    def emit(payload: dict) -> None:
        protocol.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n')
        protocol.flush()

    try:
        from videoroll.apps.dubbing.qwen_tts import QwenCustomVoice
        synth = QwenCustomVoice(model_path=args.model_path, cache_identity=args.model,
                                device=args.device, voice=args.voice,
                                instruct=args.instruct, temperature=args.temperature,
                                top_p=args.top_p)
        emit({'ready': True, 'engine': 'qwen3'})
        for line in sys.stdin:
            if not line.strip(): continue
            try:
                request = json.loads(line)
                op = request.get('op')
                if op == 'close': break
                if op not in ('synthesize', 'batch'): raise ValueError('invalid operation')
                if op == 'synthesize':
                    text = str(request.get('text') or '')
                    output = Path(str(request.get('output') or ''))
                    if not text or len(text) > 1200 or not output.is_absolute():
                        raise ValueError('invalid text or output')
                    tmp = output.with_suffix('.writing.wav')
                    try:
                        synth.synthesize(text, tmp, voice=str(request.get('voice') or args.voice))
                        tmp.replace(output)
                    finally:
                        tmp.unlink(missing_ok=True)
                    if output.stat().st_size < 44:raise RuntimeError('empty WAV output')
                    emit({'ok': True, 'outputs': [str(output)]})
                else:
                    texts = request.get('texts')
                    outputs = request.get('outputs')
                    voices = request.get('voices')
                    if (not isinstance(texts, list) or not isinstance(outputs, list)
                        or not isinstance(voices, list) or not 2 <= len(texts) <= 4
                        or len(texts) != len(outputs) or len(texts) != len(voices)
                        or any(not isinstance(t, str) or not 0 < len(t) <= 1200 for t in texts)
                        or any(not isinstance(p, str) or not Path(p).is_absolute() for p in outputs)):
                        raise ValueError('invalid batch payload')
                    synth.synthesize_batch(texts, list(map(Path, outputs)), voices=voices)
                    emit({'ok': True, 'outputs': outputs})
            except Exception as exc:
                emit({'ok': False, 'error': f'{type(exc).__name__}: {str(exc)[:450]}'})
    except Exception as exc:
        emit({'ready': False, 'error': f'{type(exc).__name__}: {str(exc)[:450]}'})
        raise
    finally:
        protocol.close()

if __name__ == '__main__': main()
