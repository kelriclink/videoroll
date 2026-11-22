"""Deterministic test double for the separately managed YouDub native bridge.
No TTS model is loaded. Copies a valid test video so that Rust can verify
workdir isolation, task identity, P2 rendering receipt and source coupling.
"""
import json
import os
import shutil
import sys
import uuid
from pathlib import Path


def main(argv: list[str]) -> int:
    runtime = Path(os.environ["VIDEOROLL_YOUDUB_RUNTIME"]).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    if not str(runtime).endswith("mock-youdub-v2"):
        raise ValueError("test bridge cannot use a production runtime")
    mode = argv[0] if argv else ""
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
            print("not found", file=sys.stderr)
            return 2
        print(manifest.read_text(encoding="utf-8"))
        return 0
    if mode == "--create":
        video = Path(get_arg("--video")).resolve()
        subtitles = Path(get_arg("--srt")).resolve()
        if not video.is_file() or not subtitles.is_file():
            raise ValueError("original media and subtitle must exist")
        if "--direction" not in argv or get_arg("--direction") != "en-zh":
            raise ValueError("mock bridge only accepts en-zh")
        record = {"status": "queued", "video": str(video), "srt": str(subtitles)}
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
