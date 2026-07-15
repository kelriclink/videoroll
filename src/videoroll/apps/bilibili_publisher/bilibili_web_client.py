from __future__ import annotations

import base64
import hashlib
import math
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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
        scope: str | None = None,
    ) -> None:
        self.code = int(code)
        self.message = str(message or "").strip()
        self.status_code = int(status_code) if status_code is not None else None
        self.v_voucher = str(v_voucher or "").strip() or None
        self.raw = raw or {}
        self.scope = str(scope or "").strip() or None
        super().__init__(f"rate limited (code={self.code} status={self.status_code} message={self.message})")


class BilibiliDescTooLongError(BilibiliWebError):
    def __init__(
        self,
        *,
        code: int,
        message: str,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.code = int(code)
        self.message = str(message or "").strip()
        self.raw = raw or {}
        super().__init__(f"bilibili api error (code={self.code} message={self.message})")


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


def _bili_code_ok(
    data: dict[str, Any],
    *,
    status_code: int | None = None,
    rate_limit_scope: str | None = None,
) -> None:
    code = data.get("code")
    if code == 0:
        return
    message = str(data.get("message") or "").strip()
    try:
        code_int = int(code)
    except Exception:
        code_int = 0
    if code_int == 21010 and "简介" in message and "过长" in message:
        raise BilibiliDescTooLongError(code=code_int, message=message, raw=_sanitize_error_json(data))
    _raise_rate_limit_if_needed(data, status_code=status_code, scope=rate_limit_scope)
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


@dataclass(frozen=True)
class PublishedVideoInfo:
    aid: int
    bvid: str
    title: str
    cid: int
    cover: str


@dataclass(frozen=True)
class SeasonRef:
    season_id: int
    section_id: int
    title: str


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


def _normalize_bili_url(url: str) -> str:
    raw = str(url or "").strip()
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw


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


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _looks_like_rate_limited(code: int | None, message: str) -> bool:
    if code in {601, 406, -702}:
        return True
    text = str(message or "").strip()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "请求频率过高",
            "投稿过于频繁",
            "上传过快",
            "上传视频过快",
        )
    )


def _raise_rate_limit_if_needed(
    data: dict[str, Any],
    *,
    status_code: int | None = None,
    scope: str | None = None,
) -> None:
    safe = _sanitize_error_json(data)
    code = _as_int(safe.get("code"))
    message = _err_msg(safe)
    if not _looks_like_rate_limited(code, message):
        return
    raise BilibiliRateLimitError(
        code=code if code is not None else -1,
        message=message or "请求频率过高，请稍后再试",
        status_code=status_code,
        v_voucher=_extract_v_voucher(safe),
        raw=safe,
        scope=scope,
    )


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
        _raise_rate_limit_if_needed(safe, status_code=resp.status_code, scope="upload")

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
        on_progress: Callable[[int, int], None] | None = None,
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
                if on_progress is not None:
                    on_progress(end, filesize)

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

    def get_video_info(
        self,
        *,
        aid: int | None = None,
        bvid: str | None = None,
        retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> PublishedVideoInfo:
        params = _drop_none(
            {
                "aid": int(aid) if aid is not None else None,
                "bvid": str(bvid or "").strip() or None,
            }
        )
        _require(bool(params), "aid or bvid is required")

        last_error: str = "unknown error"
        for attempt in range(max(1, int(retries))):
            resp = self._bili.get("https://api.bilibili.com/x/web-interface/view", params=params)
            data = _json(resp)
            code = data.get("code")
            if code == 0:
                obj = _as_dict(data.get("data"))
                try:
                    aid_val = int(obj.get("aid") or aid or 0)
                    cid_val = int(obj.get("cid") or 0)
                except Exception as e:
                    raise BilibiliWebError("video info returned invalid numeric fields") from e
                title = str(obj.get("title") or "").strip()
                bvid_val = str(obj.get("bvid") or bvid or "").strip()
                cover = _normalize_bili_url(str(obj.get("pic") or "").strip())
                _require(aid_val > 0, "video info returned empty aid")
                _require(cid_val > 0, "video info returned empty cid")
                _require(bool(title), "video info returned empty title")
                return PublishedVideoInfo(
                    aid=aid_val,
                    bvid=bvid_val,
                    title=title,
                    cid=cid_val,
                    cover=cover,
                )

            last_error = f"code={code} message={data.get('message')}"
            if attempt + 1 < max(1, int(retries)):
                time.sleep(max(0.0, float(retry_delay_seconds)))

        raise BilibiliWebError(f"failed to fetch video info ({last_error})")

    def list_seasons(self, *, pn: int = 1, ps: int = 30) -> dict[str, Any]:
        resp = self._bili.get(
            "https://member.bilibili.com/x2/creative/web/seasons",
            params={
                "pn": max(1, int(pn)),
                "ps": max(1, int(ps)),
                "order": "mtime",
                "sort": "desc",
                "draft": 1,
            },
        )
        data = _json(resp)
        _bili_code_ok(data)
        return _as_dict(data.get("data"))

    @staticmethod
    def _season_ref_from_item(item: dict[str, Any]) -> SeasonRef | None:
        season = _as_dict(item.get("season"))
        sections_root = _as_dict(item.get("sections"))
        sections = sections_root.get("sections")
        if not isinstance(sections, list):
            return None

        try:
            season_id = int(season.get("id") or 0)
        except Exception:
            season_id = 0
        title = str(season.get("title") or "").strip()
        if season_id <= 0 or not title:
            return None

        for section in sections:
            if not isinstance(section, dict):
                continue
            try:
                section_id = int(section.get("id") or 0)
            except Exception:
                section_id = 0
            if section_id > 0:
                return SeasonRef(season_id=season_id, section_id=section_id, title=title)
        return None

    def find_season_by_title(self, title: str, *, ps: int = 30) -> SeasonRef | None:
        expected = str(title or "").strip()
        _require(bool(expected), "season title is empty")

        page = 1
        while True:
            data = self.list_seasons(pn=page, ps=ps)
            items = data.get("seasons")
            if not isinstance(items, list) or not items:
                return None

            for item in items:
                if not isinstance(item, dict):
                    continue
                ref = self._season_ref_from_item(item)
                if ref and ref.title == expected:
                    return ref

            try:
                total = int(data.get("total") or 0)
            except Exception:
                total = 0
            if len(items) < ps or total <= page * ps:
                return None
            page += 1

    def get_season_by_id(self, season_id: int, *, ps: int = 30) -> SeasonRef | None:
        target = int(season_id or 0)
        _require(target > 0, "season_id must be > 0")

        page = 1
        while True:
            data = self.list_seasons(pn=page, ps=ps)
            items = data.get("seasons")
            if not isinstance(items, list) or not items:
                return None

            for item in items:
                if not isinstance(item, dict):
                    continue
                ref = self._season_ref_from_item(item)
                if ref and ref.season_id == target:
                    return ref

            try:
                total = int(data.get("total") or 0)
            except Exception:
                total = 0
            if len(items) < ps or total <= page * ps:
                return None
            page += 1

    def create_season(
        self,
        *,
        title: str,
        cover: str,
        csrf: str,
        desc: str = "",
        season_price: int = 0,
    ) -> int:
        csrf = str(csrf or "").strip()
        title = str(title or "").strip()
        cover = _normalize_bili_url(str(cover or "").strip())
        _require(bool(csrf), "csrf (bili_jct) is empty")
        _require(bool(title), "season title is empty")
        _require(bool(cover), "season cover is empty")

        resp = self._bili.post(
            "https://member.bilibili.com/x2/creative/web/season/add",
            data={
                "title": title,
                "desc": str(desc or "").strip(),
                "cover": cover,
                "season_price": int(season_price),
                "csrf": csrf,
            },
        )
        data = _json(resp)
        _bili_code_ok(data)
        try:
            season_id = int(data.get("data") or 0)
        except Exception as e:
            raise BilibiliWebError("season add returned invalid id") from e
        _require(season_id > 0, "season add returned empty id")
        return season_id

    def ensure_season(self, *, title: str, cover: str, csrf: str) -> tuple[SeasonRef, bool]:
        existing = self.find_season_by_title(title)
        if existing is not None:
            return existing, False

        season_id = self.create_season(title=title, cover=cover, csrf=csrf)
        for _ in range(3):
            created = self.get_season_by_id(season_id)
            if created is not None:
                return created, True
            time.sleep(1.0)
        raise BilibiliWebError(f"season created but not visible yet (season_id={season_id})")

    def add_to_season(self, *, section_id: int, episodes: list[dict[str, Any]], csrf: str) -> dict[str, Any]:
        csrf = str(csrf or "").strip()
        _require(bool(csrf), "csrf (bili_jct) is empty")
        _require(int(section_id or 0) > 0, "section_id must be > 0")
        _require(bool(episodes), "episodes is empty")

        resp = self._bili.post(
            "https://member.bilibili.com/x2/creative/web/season/section/episodes/add",
            params={"csrf": csrf},
            json={
                "sectionId": int(section_id),
                "episodes": episodes,
                "csrf": csrf,
            },
            headers={"Content-Type": "application/json; charset=UTF-8"},
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
        _bili_code_ok(data, status_code=resp.status_code, rate_limit_scope="submit")
        return data
