"""CosyVoice3 Python 3.10 inference process, newline JSON stdin/stdout protocol.

Started only by the trusted VideoRoll dubbing worker, never as a public API.
The process loads weights once per job, and owns its own Python dependency tree.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time


def _rl_view(base: Path) -> Path:
    """Share base weights without copying or overwriting llm.pt."""
    view = base / '.videoroll-rl-view'
    view.mkdir(exist_ok=True)
    for item in base.iterdir():
        if item == view or item.name.startswith('.'): continue
        target = view / item.name
        if item.name == 'llm.pt':
            source = base / 'llm.rl.pt'
        elif item.name == 'llm.rl.pt':
            continue
        else:
            source = item
        if not source.exists():continue
        if target.is_symlink():
            if target.resolve() != source.resolve(): raise RuntimeError(f'RL view mismatch: {target}')
            continue
        if target.exists(): raise RuntimeError(f'non-link file in RL view: {target}')
        target.symlink_to(source)
    if not (view / 'llm.pt').is_file(): raise RuntimeError('RL checkpoint missing')
    return view


def _stream_output(protocol, data: dict) -> None:
    protocol.write(json.dumps(data, ensure_ascii=False, allow_nan=False) + '\n')
    protocol.flush()


def serve(*, model_root: Path, variant: str, reference: Path, prompt_text: str,
          mode: str, instruct: str, fp16: bool) -> None:
    protocol = os.fdopen(os.dup(1), 'w', buffering=1, encoding='utf-8')
    # Third-party libraries use stdout for progress and tokenizer output.
    # Reserve only duplicated original FD1 for machine-readable messages.
    sys.stdout = sys.stderr
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from cosyvoice.cli.cosyvoice import CosyVoice3
        if not torch.cuda.is_available():
            raise RuntimeError('CosyVoice3 requires an available CUDA GPU')
        model_path = _rl_view(model_root) if variant == 'rl' else model_root
        model = CosyVoice3(str(model_path), load_trt=False, load_vllm=False, fp16=fp16)
        if not reference.is_file(): raise RuntimeError('reference WAV missing')
        if model.sample_rate != 24000: raise RuntimeError('unexpected CosyVoice3 sample rate')
        _stream_output(protocol, {'ready': True, 'sample_rate': model.sample_rate,
                                  'variant': variant, 'device': torch.cuda.get_device_name(0)})
        for line in sys.stdin:
            if not line.strip(): continue
            try:
                request = json.loads(line)
                if request.get('op') == 'close':break
                if request.get('op') != 'synthesize': raise ValueError('unknown operation')
                text = str(request.get('text') or '').strip()
                output = Path(str(request.get('output') or ''))
                speed = float(request.get('speed', 1.0))
                if not text or len(text) > 1000 or not 0.8 <= speed <= 1.25:
                    raise ValueError('invalid text or speed')
                if not output.is_absolute() or output.suffix.lower() != '.wav':
                    raise ValueError('invalid output WAV path')
                # Explicit language markers are per the CosyVoice3 example.
                tts_text = 'You are a helpful assistant.<|endofprompt|>' + text
                if mode == 'cross_lingual':
                    generator = model.inference_cross_lingual(tts_text, str(reference), stream=False, speed=speed)
                elif mode == 'zero_shot':
                    if not prompt_text.strip(): raise ValueError('zero_shot requires a reference transcript')
                    prefixed_prompt = 'You are a helpful assistant.<|endofprompt|>' + prompt_text.strip()
                    generator = model.inference_zero_shot(tts_text, prefixed_prompt, str(reference), stream=False, speed=speed)
                elif mode == 'instruct':
                    if not instruct.strip(): raise ValueError('instruct mode requires an instruction')
                    safe_instruct = 'You are a helpful assistant. ' + instruct.strip() + '<|endofprompt|>'
                    generator = model.inference_instruct2(tts_text, safe_instruct, str(reference), stream=False, speed=speed)
                else:
                    raise ValueError('unsupported CosyVoice3 mode')
                started = time.perf_counter()
                chunks = [item['tts_speech'].detach().float().cpu().numpy().reshape(-1)
                          for item in generator]
                if not chunks:raise RuntimeError('model yielded no audio')
                wave = np.concatenate(chunks)
                if wave.size < 2400 or not np.isfinite(wave).all():
                    raise RuntimeError('CosyVoice3 generated empty or nonfinite audio')
                rms = float(np.sqrt(np.mean(np.square(wave.astype(np.float64)))))
                if rms < 0.0002:raise RuntimeError('CosyVoice3 generated nearly silent audio')
                peak = float(np.abs(wave).max())
                if peak > 1.05:raise RuntimeError('CosyVoice3 output amplitude out of range')
                output.parent.mkdir(parents=True, exist_ok=True)
                tmp = output.with_name(output.name + '.writing.wav')
                try:
                    sf.write(str(tmp), wave, 24000, subtype='PCM_16')
                    tmp.replace(output)
                finally:
                    tmp.unlink(missing_ok=True)
                _stream_output(protocol, {'ok': True, 'seconds': round(wave.size / 24000, 3),
                                          'rtf': round((time.perf_counter() - started) / (wave.size / 24000), 3)})
            except Exception as exc:
                _stream_output(protocol, {'ok': False, 'error': f'{type(exc).__name__}: {str(exc)[:450]}'})
    except Exception as exc:
        _stream_output(protocol, {'ready': False, 'error': f'{type(exc).__name__}: {str(exc)[:450]}'})
        raise
    finally:
        protocol.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-root', type=Path, required=True)
    parser.add_argument('--variant', choices=['base', 'rl'], required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--mode', choices=['cross_lingual','zero_shot','instruct'], default='cross_lingual')
    parser.add_argument('--prompt-text', default='')
    parser.add_argument('--instruct', default='')
    parser.add_argument('--fp16', action='store_true')
    opts = parser.parse_args()
    serve(**vars(opts))

if __name__ == '__main__':main()
