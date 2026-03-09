from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError

from videoroll.config import OrchestratorSettings


@dataclass(frozen=True)
class YouTubeMeta:
    title: str
    description: str
    webpage_url: str
    uploader: Optional[str] = None
    upload_date: Optional[str] = None
    duration: Optional[int] = None


class YtDlpRuntimeError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: list[str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_str(v: Any) -> str:
    return str(v or "").strip()


def _pick_video_info(info: Any) -> dict[str, Any]:
    d = _as_dict(info)
    if not d:
        return {}
    if d.get("_type") in {"playlist", "multi_video"} and isinstance(d.get("entries"), list):
        for entry in d["entries"]:
            ed = _as_dict(entry)
            if ed:
                return ed
        return {}
    return d


def _extractor_args(settings: OrchestratorSettings) -> dict[str, Any] | None:
    raw = settings.youtube_extractor_args_json
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception as e:
        raise ValueError(f"invalid YOUTUBE_EXTRACTOR_ARGS_JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("YOUTUBE_EXTRACTOR_ARGS_JSON must be a JSON object")
    return parsed


def _redact_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except Exception:
        return raw
    hostname = parts.hostname or ""
    if not hostname:
        return raw
    auth = ""
    if parts.username:
        auth = parts.username
        if parts.password is not None:
            auth += ":***"
        auth += "@"
    netloc = auth + hostname
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _sanitize_diagnostic_line(line: str, *, proxy: str | None) -> str:
    text = str(line or "").rstrip()
    if proxy:
        text = text.replace(proxy, _redact_url(proxy))
    return text


def _yt_dlp_diagnostics_header(
    *,
    url: str,
    settings: OrchestratorSettings,
    outtmpl: str | None,
) -> list[str]:
    extractor_args = _extractor_args(settings)
    return [
        "videoroll yt-dlp diagnostics",
        f"url={url}",
        f"proxy={_redact_url(str(settings.youtube_proxy or '').strip()) or '(none)'}",
        f"cookiefile={(str(settings.youtube_cookie_file or '').strip() or '(none)')}",
        f"ffmpeg_location={(str(settings.ffmpeg_path or '').strip() or '(default PATH)')}",
        f"outtmpl={outtmpl or '(none)'}",
        f"extractor_args={json.dumps(extractor_args, ensure_ascii=False, sort_keys=True) if extractor_args else '(none)'}",
        "---- yt-dlp verbose ----",
    ]


class _DiagnosticsLogger:
    def __init__(self, diagnostics: list[str], *, proxy: str | None) -> None:
        self._diagnostics = diagnostics
        self._proxy = str(proxy or "").strip() or None

    def debug(self, msg: str) -> None:
        self._diagnostics.append(_sanitize_diagnostic_line(f"[debug] {msg}", proxy=self._proxy))

    def info(self, msg: str) -> None:
        self._diagnostics.append(_sanitize_diagnostic_line(str(msg), proxy=self._proxy))

    def warning(self, msg: str) -> None:
        self._diagnostics.append(_sanitize_diagnostic_line(f"WARNING: {msg}", proxy=self._proxy))

    def error(self, msg: str) -> None:
        self._diagnostics.append(_sanitize_diagnostic_line(f"ERROR: {msg}", proxy=self._proxy))


def build_ydl_opts(
    settings: OrchestratorSettings,
    *,
    outtmpl: str | None = None,
    for_download: bool,
    logger: Any | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "http_headers": {"User-Agent": settings.youtube_user_agent},
    }

    cookiefile = (settings.youtube_cookie_file or "").strip()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    proxy = (settings.youtube_proxy or "").strip()
    if proxy:
        opts["proxy"] = proxy

    # Let yt-dlp locate ffmpeg/ffprobe automatically via PATH by default.
    # If user provided a custom FFMPEG_PATH, resolve it to an actual path.
    ffmpeg_location = (settings.ffmpeg_path or "").strip()
    if ffmpeg_location:
        p = Path(ffmpeg_location)
        if p.exists():
            opts["ffmpeg_location"] = str(p)
        else:
            resolved = shutil.which(ffmpeg_location)
            if resolved:
                opts["ffmpeg_location"] = resolved

    extractor_args = _extractor_args(settings)
    if extractor_args:
        opts["extractor_args"] = extractor_args

    if outtmpl:
        opts["outtmpl"] = outtmpl
    if logger is not None:
        opts["logger"] = logger
    if verbose:
        opts["verbose"] = True

    return opts


def summarize_info(info: dict[str, Any], *, fallback_url: str) -> YouTubeMeta:
    title = _as_str(info.get("title") or info.get("fulltitle") or info.get("alt_title"))
    description = _as_str(info.get("description") or "")
    webpage_url = _as_str(info.get("webpage_url") or info.get("original_url") or fallback_url) or fallback_url

    uploader = _as_str(info.get("uploader") or info.get("channel") or info.get("uploader_id")) or None
    upload_date = _as_str(info.get("upload_date") or info.get("release_date") or "") or None

    duration: Optional[int]
    try:
        duration_val = info.get("duration")
        duration = int(duration_val) if duration_val is not None else None
    except Exception:
        duration = None

    return YouTubeMeta(
        title=title,
        description=description,
        webpage_url=webpage_url,
        uploader=uploader,
        upload_date=upload_date,
        duration=duration,
    )


def pick_thumbnail_url(info: dict[str, Any]) -> Optional[str]:
    thumbs = info.get("thumbnails")
    best_url: str | None = None
    best_score = -1
    if isinstance(thumbs, list):
        for item in thumbs:
            d = _as_dict(item)
            url = d.get("url") or d.get("src")
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            try:
                width = int(d.get("width") or 0)
                height = int(d.get("height") or 0)
            except Exception:
                width = 0
                height = 0
            try:
                pref = int(d.get("preference") or 0)
            except Exception:
                pref = 0

            score = width * height + pref * 10
            if score > best_score:
                best_score = score
                best_url = url

    if best_url:
        return best_url

    thumb = info.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        return thumb.strip()
    return None


def download_thumbnail_jpg(info: dict[str, Any], settings: OrchestratorSettings, *, work_dir: Path) -> Optional[Path]:
    url = pick_thumbnail_url(info)
    if not url:
        return None

    work_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    suffix = Path(parsed.path or "").suffix.lower()
    if not suffix or len(suffix) > 6:
        suffix = ".img"

    in_path = work_dir / f"thumbnail{suffix}"
    out_path = work_dir / "thumbnail.jpg"

    headers = {"User-Agent": settings.youtube_user_agent}
    proxy = (settings.youtube_proxy or "").strip() or None

    client_kwargs: dict[str, Any] = {"timeout": 30.0, "follow_redirects": True, "headers": headers}
    if proxy:
        try:
            client_kwargs["proxy"] = proxy
        except Exception:
            pass

    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url)
            resp.raise_for_status()
            in_path.write_bytes(resp.content)
    except TypeError:
        # Older httpx versions may not support the "proxy" kwarg.
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            in_path.write_bytes(resp.content)

    # Convert to JPG so bilibili cover upload is more likely to accept it.
    # Also normalize to a common Bilibili-friendly cover ratio (16:10).
    # Many sources recommend >=960x600 and 16:10; use 1146x717 as a safe default.
    cover_w, cover_h = 1146, 717
    cmd = [
        (settings.ffmpeg_path or "ffmpeg").strip() or "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(in_path),
        "-vf",
        f"scale={cover_w}:{cover_h}:force_original_aspect_ratio=increase,crop={cover_w}:{cover_h}",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    _run(cmd)
    if not out_path.exists() or out_path.stat().st_size <= 0:
        return None
    return out_path


def extract_youtube_metadata(url: str, settings: OrchestratorSettings) -> tuple[dict[str, Any], YouTubeMeta]:
    """
    Returns:
      - sanitized info dict (safe for json.dumps)
      - meta summary
    """
    try:
        with yt_dlp.YoutubeDL(build_ydl_opts(settings, for_download=False)) as ydl:
            info_raw = ydl.extract_info(url, download=False)
            info = _pick_video_info(info_raw)
            if not info:
                raise ValueError("yt-dlp returned empty info")
            sanitized = ydl.sanitize_info(info)
    except DownloadError as e:
        raise RuntimeError(str(e)) from e
    meta = summarize_info(_as_dict(sanitized), fallback_url=url)
    return _as_dict(sanitized), meta


def _resolve_downloaded_file(info: dict[str, Any], *, work_dir: Path) -> Path:
    info_d = _as_dict(info)
    candidates: list[Path] = []
    for key in ("filepath", "_filename"):
        p = info_d.get(key)
        if isinstance(p, str) and p.strip():
            candidates.append(Path(p))

    # requested_downloads may contain the final filepath (and/or individual streams).
    req = info_d.get("requested_downloads")
    if isinstance(req, list):
        for item in req:
            d = _as_dict(item)
            p = d.get("filepath") or d.get("_filename")
            if isinstance(p, str) and p.strip():
                candidates.append(Path(p))

    for p in candidates:
        if p.is_file():
            return p

    # Fallback: pick the largest plausible media file in work_dir.
    media_exts = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v"}
    files = [p for p in work_dir.iterdir() if p.is_file()]
    media_files = [p for p in files if p.suffix.lower() in media_exts and not p.name.endswith(".part")]
    if media_files:
        media_files.sort(key=lambda p: p.stat().st_size, reverse=True)
        return media_files[0]

    if files:
        files.sort(key=lambda p: p.stat().st_size, reverse=True)
        return files[0]

    raise RuntimeError("yt-dlp download succeeded but no output file was found")


def _download_once(
    url: str,
    settings: OrchestratorSettings,
    *,
    work_dir: Path,
    diagnostics: list[str] | None = None,
) -> tuple[Path, dict[str, Any], YouTubeMeta]:
    work_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(work_dir / "%(id)s.%(ext)s")
    ydl_logger = None
    if diagnostics is not None:
        diagnostics.extend(_yt_dlp_diagnostics_header(url=url, settings=settings, outtmpl=outtmpl))
        ydl_logger = _DiagnosticsLogger(diagnostics, proxy=settings.youtube_proxy)
    with yt_dlp.YoutubeDL(build_ydl_opts(settings, outtmpl=outtmpl, for_download=True, logger=ydl_logger, verbose=diagnostics is not None)) as ydl:
        info_raw = ydl.extract_info(url, download=True)
        info = _pick_video_info(info_raw)
        if not info:
            raise ValueError("yt-dlp returned empty info after download")
        sanitized = ydl.sanitize_info(info)

    meta = summarize_info(_as_dict(sanitized), fallback_url=url)
    video_path = _resolve_downloaded_file(_as_dict(info), work_dir=work_dir)
    return video_path, _as_dict(sanitized), meta


def download_youtube_video(url: str, settings: OrchestratorSettings, *, work_dir: Path) -> tuple[Path, dict[str, Any], YouTubeMeta]:
    """
    Downloads the best quality video to work_dir and returns:
      - output video file path
      - sanitized info dict
      - meta summary
    """
    diagnostics: list[str] = []
    try:
        return _download_once(url, settings, work_dir=work_dir, diagnostics=diagnostics)
    except DownloadError as e:
        raise YtDlpRuntimeError(str(e), diagnostics=diagnostics) from e
    except Exception as e:
        raise YtDlpRuntimeError(str(e), diagnostics=diagnostics) from e
