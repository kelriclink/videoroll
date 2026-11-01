from __future__ import annotations
import hashlib
import json
import logging
import time
import uuid
from sqlalchemy import select
from videoroll.apps.bilibili_publisher.auth_settings_store import get_bilibili_cookie_header, get_bilibili_csrf_token
from videoroll.apps.bilibili_publisher.bilibili_web_client import BilibiliWebClient, BilibiliWebError, _bili_code_ok, _json
from videoroll.config import get_bilibili_publisher_settings
from videoroll.db.models import AppSetting, Asset, AssetKind, PublishJob, PublishState
from videoroll.db.session import get_sessionmaker
from videoroll.storage.filesystem import FileStore

logger = logging.getLogger(__name__)
# The generated SRT remains Simplified Chinese (zh-Hans). Bilibili's
# subtitle-draft API expects its own language identifier, "zh", not BCP-47.
LANGUAGE = "zh"

def _cids(data: dict) -> list[int]:
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        raise BilibiliWebError("archive pages are not ready")
    cids = [int(p.get("cid") or 0) for p in pages if isinstance(p, dict)]
    if len(cids) != len(pages) or any(c <= 0 for c in cids) or len(set(cids)) != len(cids):
        raise BilibiliWebError("invalid Bilibili pages")
    return cids

def _upload(client: BilibiliWebClient, path, csrf: str, aid: int, cid: int) -> str:
    with path.open("rb") as f:
        resp = client._bili.post("https://api.bilibili.com/x/upload/web/image",
            params={"csrf": csrf, "t": int(time.time() * 1000)},
            data={"bucket": "subtitle", "csrf": csrf, "content_type": "application/x-subrip"},
            files={"file": (path.name, f, "application/x-subrip")})
    payload = _json(resp)
    _bili_code_ok(payload, status_code=resp.status_code)
    location = str((payload.get("data") or {}).get("location") or "")
    if not location:
        raise BilibiliWebError("subtitle upload missing location")
    resp = client._bili.post("https://api.bilibili.com/x/v2/dm/subtitle/draft/preSave",
        params={"csrf": csrf, "t": int(time.time() * 1000)},
        data={"oid": str(cid), "aid": str(aid), "type": "1", "csrf": csrf,
              "files": json.dumps([{"url": location, "lan": LANGUAGE, "subtitle_id": 0}])})
    _bili_code_ok(_json(resp), status_code=resp.status_code)
    return location

def register_subtitle_sync_task(app):
    @app.task(name="bilibili_publisher.sync_subtitles", bind=True, max_retries=8)
    def sync_subtitles(self, job_id: str):
        settings = get_bilibili_publisher_settings()
        db = get_sessionmaker(settings.database_url)()
        key = "bilibili.subtitle_sync." + job_id
        def save(value):
            row = db.get(AppSetting, key)
            if row is None:
                row = AppSetting(key=key, value_json={})
            row.value_json = dict(value)
            db.add(row)
            db.commit()
        try:
            job = db.get(PublishJob, uuid.UUID(job_id))
            if job is None or job.state != PublishState.published or not job.aid:
                return {"status": "skipped"}
            existing = db.get(AppSetting, key)
            state = dict(existing.value_json or {}) if existing else {}
            if state.get("enabled", True) is False:
                return {"status": "disabled"}
            asset = db.execute(select(Asset).where(
                Asset.task_id == job.task_id, Asset.kind == AssetKind.subtitle_srt)
                .order_by(Asset.created_at.desc(), Asset.id.desc()).limit(1)).scalar_one_or_none()
            if asset is None:
                return {"status": "skipped", "reason": "no SRT"}
            path = FileStore(settings).path_for(asset.storage_key)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            cookie, csrf = get_bilibili_cookie_header(db), get_bilibili_csrf_token(db)
            if not cookie or not csrf:
                raise BilibiliWebError("missing Bilibili credentials")
            with BilibiliWebClient(cookie) as client:
                response = client._bili.get("https://api.bilibili.com/x/web-interface/view",
                                            params={"aid": int(job.aid)})
                data = _json(response)
                _bili_code_ok(data, status_code=response.status_code)
                cids = _cids(data.get("data") or {})
                expected = len((job.response_json or {}).get("video_parts") or [])
                if expected and expected != len(cids):
                    raise BilibiliWebError("Bilibili pages not ready")
                done = dict(state.get("completed") or {})
                for cid in cids:
                    token = f"{cid}:{LANGUAGE}:{checksum}"
                    if token not in done:
                        location = _upload(client, path, csrf, int(job.aid), cid)
                        done[token] = {"cid": cid, "lan": LANGUAGE, "location": location}
                        state.update({"status": "submitted", "completed": done})
                        save(state)
                state.update({"status": "submitted", "aid": job.aid, "bvid": job.bvid})
                state.pop("error", None)
                save(state)
                return {"status": "submitted", "parts": len(cids)}
        except Exception as exc:
            db.rollback()
            row = db.get(AppSetting, key)
            state = dict(row.value_json or {}) if row else {}
            state.update({"status": "retrying" if self.request.retries < self.max_retries else "failed",
                          "error": (type(exc).__name__ + ": " + str(exc))[:400]})
            save(state)
            logger.warning("subtitle sync failed for %s: %s", job_id, exc)
            if self.request.retries >= self.max_retries:
                return {"status": "failed"}
            raise self.retry(exc=exc, countdown=min(1800, 60 * (2 ** self.request.retries)))
        finally:
            db.close()
