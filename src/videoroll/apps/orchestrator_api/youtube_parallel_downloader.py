"""Bounded aria2c Range downloader for large progressive HTTP YouTube assets.

yt-dlp remains responsible for extraction, auth, muxing and format selection.
Only direct HTTP(S) objects above 128 MiB are eligible. All output is parsed
locally; sanitized progress is sent to the existing yt-dlp progress hooks.
"""
from __future__ import annotations

import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from yt_dlp.downloader import external

MIN_PARALLEL_BYTES = 128 * 1024 * 1024
_GIB_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
              "KB": 1000, "MB": 1000**2, "GB": 1000**3}
_AMOUNT = re.compile(r"(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<unit>GiB|MiB|KiB|GB|MB|KB|B)")
_PROGRESS = re.compile(
    r"(?P<done>[0-9.]+(?:GiB|MiB|KiB|GB|MB|KB|B))/"
    r"(?P<total>[0-9.]+(?:GiB|MiB|KiB|GB|MB|KB|B))"
    r"\((?P<percent>\d+)%\).*?CN:(?P<connections>\d+)"
    r".*?DL:(?P<speed>[0-9.]+(?:GiB|MiB|KiB|GB|MB|KB|B))"
    r"(?:.*?ETA:(?P<eta>[0-9a-z]+))?",
    re.IGNORECASE,
)


def _bytes(value: str) -> int | None:
    match = _AMOUNT.fullmatch(value.strip())
    if match is None:
        return None
    return int(float(match["number"]) * _GIB_UNITS[match["unit"]])


def parse_aria2_progress(line: str) -> dict[str, Any] | None:
    # aria2 writes CR-refreshing display lines, even when piped.
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    match = _PROGRESS.search(clean)
    if not match:
        return None
    downloaded = _bytes(match["done"])
    total = _bytes(match["total"])
    speed = _bytes(match["speed"])
    if downloaded is None or total is None:
        return None
    eta = None
    value = match["eta"]
    if value:
        parts = re.findall(r"(\d+)([hms])", value.lower())
        if parts and "".join(f"{n}{u}" for n, u in parts) == value.lower():
            eta = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[unit] for n, unit in parts)
    return {
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "speed": speed,
        "eta": eta,
        "connections": int(match["connections"]),
    }


class VideoRollAria2cFD(external.Aria2cFD):
    EXE_NAME = "aria2c"

    @classmethod
    def can_download(cls, info_dict, path=None):
        protocol = str(info_dict.get("protocol") or "")
        size = info_dict.get("filesize") or info_dict.get("filesize_approx") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            return False
        return (
            protocol in ("http", "https")
            and not info_dict.get("is_live")
            and size >= MIN_PARALLEL_BYTES
            and super().can_download(info_dict, "aria2c")
        )

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = super()._make_cmd(tmpfilename, info_dict)
        count = max(1, min(16, int(self.params.get("videoroll_parallel_connections") or 8)))
        idx = cmd.index("--")
        cmd[idx:idx] = [
            f"--max-connection-per-server={count}",
            f"--split={count}",
            "--min-split-size=8M",
            "--summary-interval=1",
            "--show-console-readout=true",
            "--console-log-level=notice",
            "--enable-color=false",
            "--continue=true",
        ]
        return cmd

    def _call_process(self, cmd, info_dict):
        # With many Range connections, file size itself can equal the final
        # size long before all ranges are written. st_blocks instead measures
        # physically allocated sectors, and --file-allocation=none prevents
        # preallocation. Sample from this process's own thread because the
        # upstream progress hook persists via a SQLAlchemy Session.
        output = cmd[cmd.index("--out") + 1]
        directory = cmd[cmd.index("--dir") + 1] if "--dir" in cmd else "."
        target = Path(directory) / output
        total = int(info_dict.get("filesize") or info_dict.get("filesize_approx") or 0)
        filename = str(target)
        last_bytes = 0
        last_time = time.monotonic()
        sent_at = 0.0
        logs: list[str] = []
        fragments = bytearray()

        def report(values: dict[str, Any], *, force: bool = False):
            nonlocal last_bytes, last_time, sent_at
            now = time.monotonic()
            downloaded = max(0, int(values.get("downloaded_bytes") or 0))
            if not force and (downloaded <= last_bytes or now - sent_at < 0.5):
                return
            speed = values.get("speed")
            if speed is None:
                speed = max(0.0, (downloaded - last_bytes) / max(0.1, now - last_time))
            count = max(1, min(16, int(self.params.get("videoroll_parallel_connections") or 8)))
            progress = {
                "status": "downloading",
                "downloaded_bytes": downloaded,
                "total_bytes": int(values.get("total_bytes") or total),
                "speed": speed,
                "eta": values.get("eta") if values.get("eta") is not None else (
                    int(max(0, total - downloaded) / speed) if speed and total else None
                ),
                "filename": filename, "download_backend": "aria2c",
                "configured_connections": count,
                "connections": values.get("connections"),
            }
            self._hook_progress(progress, info_dict)
            last_bytes = downloaded
            last_time = now
            sent_at = now

        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, start_new_session=True,
        ) as proc:
            try:
                assert proc.stdout is not None
                with selectors.DefaultSelector() as poller:
                    poller.register(proc.stdout, selectors.EVENT_READ)
                    finished = False
                    while not finished:
                        for _key, _events in poller.select(timeout=0.5):
                            chunk = os.read(proc.stdout.fileno(), 8192)
                            if not chunk:
                                finished = True
                                break
                            for byte in chunk:
                                if byte in (10, 13):
                                    if not fragments:
                                        continue
                                    line = fragments.decode("utf-8", errors="replace")
                                    fragments.clear()
                                    state = parse_aria2_progress(line)
                                    if state:
                                        report(state)
                                    elif len(logs) < 16:
                                        # Avoid leaking tokenized URLs in logs.
                                        if "http" not in line.lower() and "cookie" not in line.lower():
                                            logs.append(line[:200])
                                elif len(fragments) < 4096:
                                    fragments.append(byte)
                        if target.exists():
                            try:
                                allocated = min(total, target.stat().st_blocks * 512) if total else 0
                                if allocated:
                                    report({"downloaded_bytes": allocated, "total_bytes": total})
                            except OSError:
                                pass
                returncode = proc.wait()
            except BaseException:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise
        # yt-dlp's native ExternalFD emits the final 100% event after the
        # process succeeds, which remains the source of truth for completion.
        return "", f"aria2c exited with status {returncode}", returncode


def register_parallel_downloader() -> bool:
    if shutil.which("aria2c") is None:
        return False
    external._BY_NAME["videoroll-aria2c"] = VideoRollAria2cFD
    return True
