"""Deterministic test double for the separately managed YouDub native bridge.
No TTS model is loaded. Copies a valid test video so that Rust can verify
workdir isolation, task identity, P2 rendering receipt and source coupling.
"""
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def main(argv: list[str]) -> int:
    runtime = Path(os.environ["VIDEOROLL_YOUDUB_RUNTIME"]).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    if not str(runtime).endswith("mock-youdub-v2"):
        raise ValueError("test bridge cannot use a production runtime")
    mode = argv[0] if argv else ""
    if mode == "--serve":
        import socketserver
        sock = runtime / "voxcpm-resident.sock"
        sock.unlink(missing_ok=True)
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request=json.loads(self.rfile.readline())
                if request.get("command") == "ping":
                    self.wfile.write(b'{"result":{"gpu_loaded":true}}\n')
                else:
                    self.wfile.write(b'{"error":"unsupported command"}\n')
        class Server(socketserver.UnixStreamServer):
            allow_reuse_address=True
        with Server(str(sock), Handler) as server:
            server.serve_forever(poll_interval=0.1)
        return 0
    if mode == "--prepare":
        print(json.dumps({"status": "ready"}))
        return 0
    def get_arg(name):
        index = argv.index(name)
        return argv[index + 1]
    job_id = str(uuid.UUID(get_arg("--task-id")))
    manifest = runtime / f"{job_id}.json"
    if mode == "--status":
        if not manifest.is_file():
            print("VIDEOROLL_YOUDUB_TASK_NOT_FOUND", file=sys.stderr)
            return 44
        print(manifest.read_text(encoding="utf-8"))
        return 0
    if mode == "--create":
        video = Path(get_arg("--video")).resolve()
        subtitles = Path(get_arg("--srt")).resolve()
        if not video.is_file() or not subtitles.is_file():
            raise ValueError("original media and subtitle must exist")
        if "--direction" not in argv or get_arg("--direction") != "en-zh":
            raise ValueError("mock bridge only accepts en-zh")
        record = {"status": "queued", "video": str(video), "srt": str(subtitles), "audio_only": "--audio-only" in argv}
        manifest.write_text(json.dumps(record), encoding="utf-8")
        print(json.dumps(record))
        return 0
    if mode == "--resume":
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["status"] = "queued"
        manifest.write_text(json.dumps(record), encoding="utf-8")
        print(json.dumps(record))
        return 0
    if mode == "--run":
        record = json.loads(manifest.read_text(encoding="utf-8"))
        output_dir = runtime / "outputs"
        output_dir.mkdir(exist_ok=True)
        output = output_dir / f"{job_id}.mp4"
        if record["audio_only"]:
            session=output_dir/job_id
            session.mkdir(exist_ok=True)
            mixed=session/"mixed.m4a"
            voice=session/"voice.wav"
            aligned=session/"aligned.srt"
            for path,codec in ((mixed,"aac"),(voice,"pcm_s16le")):
                subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","sine=frequency=880:duration=1","-c:a",codec,"-y",str(path)],check=True)
            shutil.copyfile(record["srt"],aligned)
            record.update({"status":"succeeded","output_video":str(mixed),"mixed_audio":str(mixed),"voice_audio":str(voice),"aligned_srt":str(aligned)})
        else:
            shutil.copyfile(record["video"], output)
            record.update({"status": "succeeded", "output_video": str(output)})
        manifest.write_text(json.dumps(record), encoding="utf-8")
        print(json.dumps(record))
        return 0
    raise ValueError(f"unsupported bridge mode: {mode}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (IndexError, ValueError, KeyError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
