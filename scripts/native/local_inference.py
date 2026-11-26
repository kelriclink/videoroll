#!/usr/bin/env python3
"""Offline model adapter, one-shot or bounded persistent subprocess. No business state."""
import contextlib
import hashlib
import json
import functools
import fcntl
import os
import stat
import struct
import sys
import time
from pathlib import Path

@functools.lru_cache(maxsize=1)
def faster_model(model, device, compute_type, cpu_threads, num_workers):
    from faster_whisper import WhisperModel
    return WhisperModel(model, device=device, compute_type=compute_type,
                        local_files_only=True, cpu_threads=cpu_threads, num_workers=num_workers)

@functools.lru_cache(maxsize=1)
def ov_whisper(model, device):
    from openvino_genai import WhisperPipeline
    return WhisperPipeline(model, device=device)

@functools.lru_cache(maxsize=1)
def ov_embedding(model, device):
    import openvino as ov
    from tokenizers import Tokenizer
    path=Path(model)
    ir=path/"openvino_model.xml"
    if not ir.is_file():ir=path/"openvino"/"openvino_model.xml"
    if not ir.is_file():raise RuntimeError("OpenVINO embedding export missing")
    tokenizer=Tokenizer.from_file(str(path/"tokenizer.json"))
    max_len=512
    config=path/"sentence_bert_config.json"
    if config.is_file():max_len=min(512,max(1,int(json.loads(config.read_text()).get("max_seq_length",512))))
    tokenizer.enable_truncation(max_length=max_len)
    return tokenizer,ov.Core().compile_model(str(ir),device.partition(":")[2] or "CPU")

@functools.lru_cache(maxsize=1)
def st_embedding(model, device):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model,device=device,local_files_only=True,trust_remote_code=False)

def transcribe(req):
    import av
    import numpy as np
    audio = req["audio"]
    engine = req["engine"]
    model = req["model_path"]
    language = req.get("language", "auto")
    if language == "auto": language = None
    if engine == "faster-whisper":
        pipeline = faster_model(model,req["device"],req["compute_type"],req["cpu_threads"],req["num_workers"])
        segments, _ = pipeline.transcribe(audio, language=language, vad_filter=True,
            vad_parameters={"min_silence_duration_ms":2000,"speech_pad_ms":400})
        return {"segments":[{"start":float(s.start),"end":float(s.end),"text":s.text,
            "no_speech_prob":float(s.no_speech_prob),"avg_logprob":float(s.avg_logprob),
            "compression_ratio":float(s.compression_ratio)} for s in segments]}
    if engine != "openvino": raise ValueError("unsupported ASR engine")
    from faster_whisper.audio import decode_audio
    samples = decode_audio(audio, sampling_rate=16000)
    if samples.size==0 or float(np.max(np.abs(samples))) < 0.0001:return {"segments":[]}
    if req["vad_enabled"]:
        from faster_whisper.vad import get_speech_timestamps,VadOptions
        spans=get_speech_timestamps(samples,VadOptions(threshold=req["vad_threshold"],min_silence_duration_ms=2000,speech_pad_ms=400))
    else: spans=[{"start":0,"end":len(samples)}]
    if not spans:return {"segments":[]}
    pipeline=ov_whisper(model,req["device"])
    cfg=pipeline.get_generation_config()
    cfg.return_timestamps=True
    cfg.task="transcribe"
    cfg.num_beams=req["num_beams"]
    cfg.max_new_tokens=req["max_new_tokens"]
    if language:cfg.language=language if language.startswith("<|") else f"<|{language.lower()}|>"
    out=[]
    for span in spans:
        start,end=span["start"],span["end"]
        chunk=samples[start:end]
        result=pipeline.generate(chunk.tolist(),generation_config=cfg)
        duration=len(chunk)/16000
        for seg in getattr(result,"chunks",[]):
            text=str(seg.text).strip()
            a=max(0.,min(duration,float(seg.start_ts)))
            b=max(a,min(duration,float(seg.end_ts)))
            if not text or b<=a:continue
            if (b-a>=8 and len(text.split())<=3) or (b-a>=4 and len(text.split())<=1):continue
            out.append({"start":start/16000+a,"end":start/16000+b,"text":text})
        if not getattr(result,"chunks",[]) and any(str(x).strip() for x in getattr(result,"texts",[])):
            raise RuntimeError("OpenVINO returned text without timestamps")
    return {"segments":out}

def speech_spans(req):
    """CPU Silero VAD only; return uncertainty instead of suppressing speech
    when the optional dependency or audio decoding is unavailable."""
    try:
        import numpy as np
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        audio=Path(req["audio"])
        if not audio.is_file() or audio.stat().st_size>24*1024*1024:
            return {"available":False}
        samples=decode_audio(str(audio),sampling_rate=16000)
        if len(samples)>60*16000 or not np.isfinite(samples).all():
            return {"available":False}
        if len(samples)==0:
            return {"available":False}
        if float(np.max(np.abs(samples)))<0.0001:
            return {"available":True,"spans":[]}
        refine=req.get("purpose")=="timing"
        spans=get_speech_timestamps(samples,VadOptions(
            threshold=max(0.01,min(0.99,float(req.get("threshold",0.5)))),
            min_speech_duration_ms=120 if refine else 250,
            min_silence_duration_ms=160 if refine else 500,
            speech_pad_ms=80 if refine else 180,max_speech_duration_s=60 if refine else 30))
        return {"available":True,"spans":[
            {"start_ms":round(max(0,int(span["start"]))/16),
             "end_ms":round(min(len(samples),int(span["end"]))/16)}
            for span in spans if int(span["end"])>int(span["start"])]}
    except Exception:
        return {"available":False}

def embedding(req):
    import numpy as np
    path=Path(req["model_path"])
    device=req["device"]
    text=req["text"]
    if device.lower().startswith("openvino"):
        # Existing exported IR is required; never export/download or modify a
        # production model directory implicitly during a knowledge write.
        tokenizer,compiled=ov_embedding(str(path),device)
        encoded=tokenizer.encode(text)
        ids=np.asarray([encoded.ids],dtype=np.int64)
        mask=np.asarray([encoded.attention_mask],dtype=np.int64)
        types=np.asarray([encoded.type_ids],dtype=np.int64)
        values={"input_ids":ids,"attention_mask":mask,"token_type_ids":types}
        inputs={port.any_name:values[port.any_name] for port in compiled.inputs}
        outputs=compiled(inputs)
        hidden=next((v for k,v in outputs.items() if "last_hidden_state" in k.names),None)
        if hidden is None:
            candidates=[v for v in outputs.values() if v.ndim==3]
            if len(candidates)!=1:raise RuntimeError("embedding hidden state missing")
            hidden=candidates[0]
        weighted=mask[...,None].astype(np.float32)
        vector=(hidden*weighted).sum(axis=1)/np.maximum(weighted.sum(axis=1),1e-9)
        vector=vector[0].astype(np.float32)
    else:
        model=st_embedding(str(path),device)
        vector=np.asarray(model.encode(text,normalize_embeddings=True),dtype=np.float32)
    norm=float(np.linalg.norm(vector))
    if vector.ndim!=1 or not np.isfinite(vector).all() or norm<=0:raise RuntimeError("invalid embedding")
    return {"embedding":(vector/norm).tolist()}

MAX_REQUEST=256*1024
MAX_RESPONSE=12*1024*1024

def dispatch(req):
    if req["kind"]=="asr":return transcribe(req)
    if req["kind"]=="embedding":return embedding(req)
    if req["kind"]=="vad":return speech_spans(req)
    if req["kind"]=="devices":
        import openvino as ov
        return {"devices":ov.Core().available_devices}
    raise ValueError("unsupported operation")

def identity(req):
    return tuple(req.get(k) for k in ("kind","engine","model_path","device","compute_type","cpu_threads","num_workers"))

def _gpu_lock_device(req):
    device=str(req.get("device","")).strip().upper()
    engine=str(req.get("engine","")).strip().lower()
    if device in {"OPENVINO", "OPENVINO:"}: return "CPU"
    if device.startswith("OPENVINO:"):
        device=device.split(":",1)[1]
        engine="openvino"
    if engine=="openvino" and device in {"GPU","GPU.0","AUTO"}:
        return "GPU.0"
    if device in {"CUDA","CUDA:0"}: return "CUDA"
    return device or "GPU"

def _gpu_lock_slots():
    try:return max(1,min(8,int(os.environ.get("VIDEOROLL_LOCAL_GPU_SLOTS","1"))))
    except (TypeError,ValueError):return 1

def lock_gpu(req):
    device=_gpu_lock_device(req)
    if req["kind"]=="devices" or device in {"CPU","OPENVINO:CPU"}:
        return None
    base=os.environ["VIDEOROLL_LOCAL_GPU_LOCK"]
    digest=hashlib.sha256(device.encode("utf-8")).hexdigest()[:16]
    slots=_gpu_lock_slots()
    waiting_logged=False
    deadline=time.monotonic()+3600.0
    while time.monotonic()<deadline:
        for slot in range(slots):
            path=f"{base}.{digest}.slot-{slot}.lock"
            fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode&0o077:
                    raise RuntimeError("unsafe GPU admission lock")
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    os.close(fd)
            except BaseException:
                try:os.close(fd)
                except OSError:pass
                raise
        if not waiting_logged:
            sys.stderr.write(f"local inference waiting for GPU admission: device={device} slots={slots}\n")
            sys.stderr.flush()
            waiting_logged=True
        time.sleep(0.25)
    raise TimeoutError("local inference GPU admission timeout")

def serve():
    # Reserve a private protocol stream; native runtime writes to fd 1
    # are redirected too, not only Python print().
    output=os.fdopen(os.dup(sys.stdout.fileno()),"wb")
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    stdin=sys.stdin.buffer
    current=None
    gpu_lock=None
    try:
        while True:
            header=stdin.read(4)
            if not header:return
            if len(header)!=4:raise ValueError("truncated frame")
            length=struct.unpack(">I",header)[0]
            if not 0<length<=MAX_REQUEST:raise ValueError("request frame too large")
            raw=stdin.read(length)
            if len(raw)!=length:raise ValueError("truncated request")
            try:
                req=json.loads(raw)
                key=identity(req)
                if current is None:
                    gpu_lock=lock_gpu(req)
                    current=key
                elif current!=key:
                    raise ValueError("model identity changed within session")
                result={"ok":True,"result":dispatch(req)}
                encoded=json.dumps(result,ensure_ascii=False,allow_nan=False).encode()
                if len(encoded)>MAX_RESPONSE:raise ValueError("response too large")
            except Exception as exc:
                # Never include request text, model secrets or traceback in the response.
                encoded=json.dumps({"ok":False,"error_type":type(exc).__name__}).encode()
                output.write(struct.pack(">I",len(encoded))+encoded)
                output.flush()
                return
            output.write(struct.pack(">I",len(encoded))+encoded)
            output.flush()
    finally:
        # Keep the lock held until process exit (including runtime teardown).
        output.close()

def main():
    if sys.argv[1:]==["--serve"]:
        return serve()
    if sys.argv[1:]:raise ValueError("unsupported arguments")
    req=json.load(sys.stdin)
    with contextlib.redirect_stdout(sys.stderr):
        out=dispatch(req)
    print(json.dumps(out,ensure_ascii=False,allow_nan=False))

if __name__=="__main__":
    main()
