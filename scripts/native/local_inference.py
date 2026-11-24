#!/usr/bin/env python3
"""Offline, one-request model adapter. No DB, API, queue, network or task logic."""
import contextlib
import json
import sys
from pathlib import Path

def transcribe(req):
    import av
    import numpy as np
    audio = req["audio"]
    engine = req["engine"]
    model = req["model_path"]
    language = req.get("language", "auto")
    if language == "auto": language = None
    if engine == "faster-whisper":
        from faster_whisper import WhisperModel
        kwargs = {"device":req["device"],"compute_type":req["compute_type"],"local_files_only":True,
                  "cpu_threads":req["cpu_threads"],"num_workers":req["num_workers"]}
        pipeline = WhisperModel(model, **kwargs)
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
    from openvino_genai import WhisperPipeline
    pipeline=WhisperPipeline(model,device=req["device"])
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

def embedding(req):
    import numpy as np
    path=Path(req["model_path"])
    device=req["device"]
    text=req["text"]
    if device.lower().startswith("openvino"):
        # Existing exported IR is required; never export/download or modify a
        # production model directory implicitly during a knowledge write.
        import openvino as ov
        from tokenizers import Tokenizer
        ir=path/"openvino_model.xml"
        if not ir.is_file():ir=path/"openvino"/"openvino_model.xml"
        if not ir.is_file():raise RuntimeError("OpenVINO embedding export missing")
        tokenizer=Tokenizer.from_file(str(path/"tokenizer.json"))
        max_len=512
        config=path/"sentence_bert_config.json"
        if config.is_file():max_len=min(512,max(1,int(json.loads(config.read_text()).get("max_seq_length",512))))
        tokenizer.enable_truncation(max_length=max_len)
        encoded=tokenizer.encode(text)
        ids=np.asarray([encoded.ids],dtype=np.int64)
        mask=np.asarray([encoded.attention_mask],dtype=np.int64)
        types=np.asarray([encoded.type_ids],dtype=np.int64)
        compiled=ov.Core().compile_model(str(ir),device.partition(":")[2] or "CPU")
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
        from sentence_transformers import SentenceTransformer
        model=SentenceTransformer(str(path),device=device,local_files_only=True,trust_remote_code=False)
        vector=np.asarray(model.encode(text,normalize_embeddings=True),dtype=np.float32)
    norm=float(np.linalg.norm(vector))
    if vector.ndim!=1 or not np.isfinite(vector).all() or norm<=0:raise RuntimeError("invalid embedding")
    return {"embedding":(vector/norm).tolist()}

def main():
    req=json.load(sys.stdin)
    # Third-party progress output must not corrupt the JSON protocol.
    with contextlib.redirect_stdout(sys.stderr):
        if req["kind"]=="asr":out=transcribe(req)
        elif req["kind"]=="embedding":out=embedding(req)
        elif req["kind"]=="devices":
            import openvino as ov
            out={"devices":ov.Core().available_devices}
        else:raise ValueError("unsupported operation")
    print(json.dumps(out,ensure_ascii=False,allow_nan=False))

if __name__=="__main__":
    main()
