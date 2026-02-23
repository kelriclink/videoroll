from __future__ import annotations

import base64
import hashlib
import math
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta


class BilibiliWebError(RuntimeError):
    pass


class BilibiliRateLimitError(BilibiliWebError):
    def __init__(
        self,
        *,
        code: int,
        message: str,
        status_code: int | None = None,
        v_voucher: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.code = int(code)
        self.message = str(message or "").strip()
        self.status_code = int(status_code) if status_code is not None else None
        self.v_voucher = str(v_voucher or "").strip() or None
        self.raw = raw or {}
        super().__init__(f"rate limited (code={self.code} status={self.status_code} message={self.message})")


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise BilibiliWebError(msg)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception as e:
        raise BilibiliWebError(f"invalid json response (status={resp.status_code})") from e
    if not isinstance(data, dict):
        raise BilibiliWebError(f"unexpected json response type: {type(data).__name__}")
    return data


def _bili_code_ok(data: dict[str, Any]) -> None:
    code = data.get("code")
    if code == 0:
        return
    raise BilibiliWebError(f"bilibili api error (code={code} message={data.get('message')})")


@dataclass(frozen=True)
class PreuploadInfo:
    auth: str
    biz_id: int
    chunk_size: int
    endpoint: str
    upos_uri: str


@dataclass(frozen=True)
class UploadMeta:
    upload_id: str
    bucket: str
    key: str


@dataclass(frozen=True)
class UploadedVideo:
    filename_no_suffix: str
    cid: int
    upload_id: str
    upos_uri: str


def _upload_url(pre: PreuploadInfo) -> str:
    endpoint = str(pre.endpoint or "").strip()
    upos_uri = str(pre.upos_uri or "").strip()
    _require(endpoint.startswith("//"), "preupload.endpoint is invalid")
    _require(upos_uri.startswith("upos://"), "preupload.upos_uri is invalid")
    path = upos_uri.replace("upos:/", "", 1)  # -> "/bucket/filename.ext"
    _require(path.startswith("/"), "preupload.upos_uri path is invalid")
    return f"https:{endpoint}{path}"


def _filename_no_suffix_from_upos_uri(upos_uri: str) -> str:
    name = Path((upos_uri or "").replace("upos://", "")).name
    if "." not in name:
        return name
    return name.rsplit(".", 1)[0]


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _sanitize_error_json(data: dict[str, Any]) -> dict[str, Any]:
    safe = dict(data)
    # preupload auth may appear in some responses; never return or log it.
    safe.pop("auth", None)
    safe.pop("fetch_headers", None)
    safe.pop("post_auth", None)
    safe.pop("put_auth", None)
    return safe


def _err_msg(data: dict[str, Any]) -> str:
    for key in ("message", "msg", "error", "err", "info"):
        v = data.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _extract_v_voucher(data: dict[str, Any]) -> str | None:
    vv = data.get("v_voucher")
    if isinstance(vv, str) and vv.strip():
        return vv.strip()

    detail = data.get("detail")
    if isinstance(detail, dict):
        vv = detail.get("v_voucher")
        if isinstance(vv, str) and vv.strip():
            return vv.strip()

    d = data.get("data")
    if isinstance(d, dict):
        vv = d.get("v_voucher")
        if isinstance(vv, str) and vv.strip():
            return vv.strip()
        detail = d.get("detail")
        if isinstance(detail, dict):
            vv = detail.get("v_voucher")
            if isinstance(vv, str) and vv.strip():
                return vv.strip()

    return None


class BilibiliWebClient:
    def __init__(
        self,
        cookie: str,
        *,
        user_agent: str = "Mozilla/5.0 (X11; Linux x86_64; rv:60.1) Gecko/20100101 Firefox/60.1",
    ) -> None:
        cookie = (cookie or "").strip()
        _require(bool(cookie), "cookie is empty")

        common_headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        bili_headers = {
            **common_headers,
            "Cookie": cookie,
            "Origin": "https://member.bilibili.com",
            "Referer": "https://member.bilibili.com/",
        }

        self._bili = httpx.Client(timeout=30.0, headers=bili_headers, follow_redirects=True)
        # IMPORTANT: Do NOT send bilibili cookies to upload CDN domains.
        self._upos = httpx.Client(timeout=120.0, headers=common_headers, follow_redirects=True)
        self._preupload_probe_query: str | None = None

    def close(self) -> None:
        self._bili.close()
        self._upos.close()

    def __enter__(self) -> "BilibiliWebClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upload_cover(self, image_path: Path, *, csrf: str) -> str:
        csrf = (csrf or "").strip()
        _require(bool(csrf), "csrf (bili_jct) is empty")
        _require(image_path.exists(), f"cover file not found: {image_path}")

        mime, _ = mimetypes.guess_type(str(image_path))
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"

        resp = self._bili.post(
            "https://member.bilibili.com/x/vu/web/cover/up",
            params={"ts": int(time.time() * 1000)},
            data={"csrf": csrf, "cover": data_uri},
        )
        data = _json(resp)
        _bili_code_ok(data)
        url = _as_dict(data.get("data")).get("url")
        url = str(url or "").strip()
        _require(bool(url), "cover upload succeeded but returned empty url")
        return url

    def _get_preupload_probe_query(self) -> str:
        cached = self._preupload_probe_query
        if cached is not None:
            return cached

        query = ""
        try:
            resp = self._upos.get("https://member.bilibili.com/preupload", params={"r": "probe"})
            if resp.status_code == 200:
                data = _json(resp)
                if data.get("OK") == 1 and isinstance(data.get("lines"), list):
                    lines = [x for x in data["lines"] if isinstance(x, dict)]
                    # Prefer UPOS lines.
                    for line in lines:
                        if str(line.get("os") or "") == "upos":
                            q = str(line.get("query") or "").strip()
                            if q:
                                query = q
                                break
                    if not query:
                        for line in lines:
                            q = str(line.get("query") or "").strip()
                            if q:
                                query = q
                                break
        except Exception:
            query = ""

        self._preupload_probe_query = query
        return query

    def preupload_video(self, *, filename: str, filesize: int, profile: str = "ugcupos/bup") -> PreuploadInfo:
        filename = (filename or "").strip()
        _require(bool(filename), "filename is empty")
        _require(filesize > 0, "filesize must be > 0")
        probe_query = self._get_preupload_probe_query()
        url = "https://member.bilibili.com/preupload"
        if probe_query:
            url = f"{url}?{probe_query}"
        resp = self._bili.get(
            url,
            params={
                "name": filename,
                "r": "upos",
                "profile": profile,
                # Match web uploader params (biliup-master).
                "ssl": 0,
                "version": "2.14.0",
                "build": 2140000,
                "size": int(filesize),
            },
        )
        try:
            data = _json(resp)
        except BilibiliWebError:
            raise BilibiliWebError(f"preupload http error (status={resp.status_code} body={resp.text[:200]})")

        safe = _sanitize_error_json(data)
        code = safe.get("code")
        if code == 601:
            raise BilibiliRateLimitError(
                code=601,
                message=_err_msg(safe) or "上传过快",
                status_code=resp.status_code,
                v_voucher=_extract_v_voucher(safe),
                raw=safe,
            )

        if resp.status_code != 200:
            msg = _err_msg(safe)
            raise BilibiliWebError(f"preupload http error (status={resp.status_code} message={msg or safe})")
        if data.get("OK") != 1:
            msg = _err_msg(safe)
            raise BilibiliWebError(f"preupload failed (OK={data.get('OK')} message={msg or safe})")
        auth = str(data.get("auth") or "").strip()
        endpoint = str(data.get("endpoint") or "").strip()
        upos_uri = str(data.get("upos_uri") or "").strip()
        try:
            biz_id = int(data.get("biz_id") or 0)
            chunk_size = int(data.get("chunk_size") or 0)
        except Exception as e:
            raise BilibiliWebError("preupload returned invalid numeric fields") from e
        _require(bool(auth), "preupload.auth is empty")
        _require(biz_id > 0, "preupload.biz_id is invalid")
        _require(chunk_size > 0, "preupload.chunk_size is invalid")
        _require(bool(endpoint), "preupload.endpoint is empty")
        _require(bool(upos_uri), "preupload.upos_uri is empty")
        return PreuploadInfo(auth=auth, biz_id=biz_id, chunk_size=chunk_size, endpoint=endpoint, upos_uri=upos_uri)

    def post_video_meta(self, pre: PreuploadInfo, *, filesize: int, profile: str = "ugcupos/bup") -> UploadMeta:
        url = _upload_url(pre)
        resp = self._upos.post(
            url,
            params={
                "uploads": "",
                "output": "json",
                "profile": profile,
                "filesize": str(int(filesize)),
                "partsize": str(int(pre.chunk_size)),
                "biz_id": str(int(pre.biz_id)),
            },
            headers={"X-Upos-Auth": pre.auth},
        )
        try:
            data = _json(resp)
        except BilibiliWebError:
            raise BilibiliWebError(f"post video meta http error (status={resp.status_code} body={resp.text[:200]})")
        if resp.status_code != 200:
            msg = _err_msg(data)
            raise BilibiliWebError(f"post video meta http error (status={resp.status_code} message={msg or data})")
        if data.get("OK") != 1:
            msg = _err_msg(data)
            raise BilibiliWebError(f"post video meta failed (OK={data.get('OK')} message={msg or data})")
        upload_id = str(data.get("upload_id") or "").strip()
        bucket = str(data.get("bucket") or "").strip()
        key = str(data.get("key") or "").strip()
        _require(bool(upload_id), "upload_id is empty")
        return UploadMeta(upload_id=upload_id, bucket=bucket, key=key)

    def upload_video_file(
        self,
        video_path: Path,
        *,
        profile: str = "ugcupos/bup",
    ) -> tuple[UploadedVideo, dict[str, Any]]:
        _require(video_path.exists(), f"video file not found: {video_path}")
        filesize = video_path.stat().st_size
        _require(filesize > 0, "video file is empty")

        pre = self.preupload_video(filename=video_path.name, filesize=filesize, profile=profile)
        meta = self.post_video_meta(pre, filesize=filesize, profile=profile)

        url = _upload_url(pre)
        chunk_size = int(pre.chunk_size)
        chunks = int(math.ceil(filesize / float(chunk_size)))
        parts: list[dict[str, Any]] = []

        with video_path.open("rb") as f:
            for chunk in range(chunks):
                start = chunk * chunk_size
                buf = f.read(chunk_size)
                if not buf:
                    break
                end = start + len(buf)
                resp = self._upos.put(
                    url,
                    params={
                        "partNumber": str(chunk + 1),
                        "uploadId": meta.upload_id,
                        "chunk": str(chunk),
                        "chunks": str(chunks),
                        "size": str(len(buf)),
                        "start": str(start),
                        "end": str(end),
                        "total": str(filesize),
                    },
                    headers={"X-Upos-Auth": pre.auth, "Content-Type": "application/octet-stream"},
                    content=buf,
                )
                if resp.status_code != 200:
                    raise BilibiliWebError(f"chunk upload failed (status={resp.status_code} body={resp.text[:200]})")
                # Some servers respond with "MULTIPART_PUT_SUCCESS" plain text. Prefer ETag header if present;
                # otherwise fall back to MD5 of the uploaded chunk (common ETag semantics).
                etag = (resp.headers.get("ETag") or resp.headers.get("Etag") or resp.headers.get("etag") or "").strip()
                if etag:
                    etag = etag.strip('"')
                else:
                    etag = hashlib.md5(buf).hexdigest()  # noqa: S324
                parts.append({"partNumber": chunk + 1, "eTag": etag or "etag"})

        resp = self._upos.post(
            url,
            params={
                "output": "json",
                "name": video_path.name,
                "profile": profile,
                "uploadId": meta.upload_id,
                "biz_id": str(int(pre.biz_id)),
            },
            headers={"X-Upos-Auth": pre.auth},
            json={"parts": parts},
        )
        try:
            end_data = _json(resp)
        except BilibiliWebError:
            raise BilibiliWebError(f"end upload http error (status={resp.status_code} body={resp.text[:200]})")
        if resp.status_code != 200:
            msg = _err_msg(end_data)
            raise BilibiliWebError(f"end upload http error (status={resp.status_code} message={msg or end_data})")
        if end_data.get("OK") != 1:
            msg = _err_msg(end_data)
            raise BilibiliWebError(f"end upload failed (OK={end_data.get('OK')} message={msg or end_data})")

        filename_no_suffix = _filename_no_suffix_from_upos_uri(pre.upos_uri)
        _require(bool(filename_no_suffix), "failed to derive filename from upos_uri")

        uploaded = UploadedVideo(filename_no_suffix=filename_no_suffix, cid=pre.biz_id, upload_id=meta.upload_id, upos_uri=pre.upos_uri)
        # Return minimal debug info (NO auth).
        return uploaded, {
            "biz_id": pre.biz_id,
            "chunk_size": pre.chunk_size,
            "endpoint": pre.endpoint,
            "upos_uri": pre.upos_uri,
            "upload_id": meta.upload_id,
            "upload_url": url,
            "chunks": len(parts),
        }

    def predict_type(self, *, csrf: str, filename: str, title: str = "", upload_id: str = "") -> Optional[int]:
        csrf = (csrf or "").strip()
        _require(bool(csrf), "csrf (bili_jct) is empty")
        files = {
            "filename": (None, str(filename or "")),
            "title": (None, str(title or "")),
            "upload_id": (None, str(upload_id or "")),
        }
        resp = self._bili.post(
            "https://member.bilibili.com/x/vupre/web/archive/types/predict",
            params={"csrf": csrf, "ts": int(time.time() * 1000)},
            files=files,
        )
        data = _json(resp)
        _bili_code_ok(data)
        arr = data.get("data")
        if not isinstance(arr, list) or not arr:
            return None
        first = arr[0] if isinstance(arr[0], dict) else {}
        try:
            tid = int(first.get("id") or 0)
        except Exception:
            tid = 0
        return tid if tid > 0 else None

    def archive_pre(self) -> dict[str, Any]:
        resp = self._bili.get(
            "https://member.bilibili.com/x/vupre/web/archive/pre",
            params={"ts": int(time.time() * 1000)},
        )
        data = _json(resp)
        _bili_code_ok(data)
        return data

    def add_archive(
        self,
        meta: BilibiliPublishMeta,
        *,
        csrf: str,
        tid: int,
        uploaded: UploadedVideo,
        cover_url: str = "",
    ) -> dict[str, Any]:
        csrf = (csrf or "").strip()
        _require(bool(csrf), "csrf (bili_jct) is empty")
        _require(tid > 0, "tid must be > 0")
        tags = list(meta.tags or [])
        _require(bool(tags), "meta.tags is required for bilibili publish")
        cover_url = str(cover_url or "").strip()

        body: dict[str, Any] = {
            "videos": [
                {
                    "filename": uploaded.filename_no_suffix,
                    "title": meta.title,
                    "desc": "",
                    "cid": uploaded.cid,
                }
            ],
            # If cover is omitted, Bilibili will auto-pick one.
            "cover": cover_url or None,
            "cover43": "",
            "title": meta.title,
            "copyright": int(meta.copyright),
            # Only include when reprint.
            "source": meta.source if int(meta.copyright) == 2 else None,
            "tid": int(tid),
            "human_type2": meta.human_type2,
            "tag": ",".join(tags),
            "desc_format_id": int(meta.desc_format_id),
            "desc": meta.desc,
            "desc_v2": meta.desc_v2,
            "recreate": int(meta.recreate),
            "dynamic": meta.dynamic,
            "interactive": int(meta.interactive),
            "act_reserve_create": int(meta.act_reserve_create),
            "no_disturbance": int(meta.no_disturbance),
            "no_reprint": int(meta.no_reprint),
            "subtitle": {"open": int(meta.subtitle.open), "lan": str(meta.subtitle.lan or "")},
            "dolby": int(meta.dolby),
            "lossless_music": int(meta.lossless_music),
            "up_selection_reply": bool(meta.up_selection_reply),
            "up_close_reply": bool(meta.up_close_reply),
            "up_close_danmu": bool(meta.up_close_danmu),
            "web_os": int(meta.web_os),
            "is_only_self": meta.is_only_self,
            "topic_id": meta.topic_id,
            "mission_id": meta.mission_id,
            "is_360": meta.is_360,
            "neutral_mark": meta.neutral_mark,
            "dtime": meta.dtime,
            "csrf": csrf,
        }

        resp = self._bili.post(
            "https://member.bilibili.com/x/vu/web/add/v3",
            params={"csrf": csrf, "ts": int(time.time() * 1000)},
            json=_drop_none(body),
        )
        data = _json(resp)
        _bili_code_ok(data)
        return data
