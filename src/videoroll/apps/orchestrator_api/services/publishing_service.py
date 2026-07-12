from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from videoroll.ai.service import AIService
from videoroll.apps.bilibili_publisher.schemas import BilibiliPublishMeta
from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.apps.orchestrator_api.schemas import PublishActionRequest, PublishAllRequest, RemotePublishResponse
from videoroll.apps.orchestrator_api.services.asset_service import (
    as_dict,
    read_s3_bytes,
    read_s3_json_object,
    write_s3_json,
)
from videoroll.apps.publish_gateway import (
    normalize_publish_platform,
    normalize_social_publish_meta,
    publish_backend_url,
    publish_meta_key,
)
from videoroll.apps.publish_lifecycle import publish_target_key
from videoroll.apps.publish_meta_draft import build_task_publish_meta_draft
from videoroll.apps.publish_platform_settings_store import (
    get_publish_platform_settings,
    is_publish_platform_enabled,
    update_publish_platform_setting,
)
from videoroll.apps.publish_review import review_publish_materials
from videoroll.apps.publish_review_store import (
    get_publish_review_settings,
    get_task_publish_review as get_task_publish_review_record,
    set_task_publish_review,
)
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.bilibili_tags_store import get_task_bilibili_summary, get_task_bilibili_tags
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.config import OrchestratorSettings, get_subtitle_settings
from videoroll.db.models import Asset, AssetKind, PublishBatch, PublishJob, PublishState, Task, TaskStatus
from videoroll.storage.s3 import S3Store


def publish_meta_s3_key(task_id: uuid.UUID) -> str:
    return f"meta/{task_id}/publish_meta.json"


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in (item.strip() for item in value.replace("，", ",").split(",")) if part]
    if isinstance(value, (list, tuple, set)):
        return [text for text in (str(item or "").strip() for item in value) if text]
    text = str(value or "").strip()
    return [text] if text else []


def publish_job_error_message(job: PublishJob) -> str | None:
    data = as_dict(job.response_json)
    if not data:
        return None
    message = str(data.get("error") or data.get("message") or data.get("detail") or "").strip()
    if not message:
        return None
    extras = [
        f"{key}={text}"
        for key in ("code", "status_code", "v_voucher")
        if (text := str(data.get(key) or "").strip())
    ]
    if extras:
        message = f"{message} ({', '.join(extras)})"
    return message[:499] + "…" if len(message) > 500 else message


def published_publish_job_task_ids(db: Session, task_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not task_ids:
        return set()
    rows = (
        db.query(PublishJob.task_id)
        .filter(
            PublishJob.task_id.in_(task_ids),
            PublishJob.batch_id.is_(None),
            PublishJob.state == PublishState.published,
        )
        .all()
    )
    return {row[0] for row in rows}


def reconcile_published_task_state(
    db: Session,
    task: Task,
    *,
    published_task_ids: set[uuid.UUID] | None = None,
) -> bool:
    # Batch-managed tasks are aggregated exclusively by publish_lifecycle.
    # This fallback is retained only for jobs written before batches existed.
    if task.active_publish_batch_id is not None:
        return False
    if task.status in {TaskStatus.published, TaskStatus.canceled}:
        return False
    has_published_job = task.id in published_task_ids if published_task_ids is not None else bool(
        db.query(PublishJob.id)
        .filter(
            PublishJob.task_id == task.id,
            PublishJob.batch_id.is_(None),
            PublishJob.state == PublishState.published,
        )
        .first()
    )
    if not has_published_job:
        return False
    task.status = TaskStatus.published
    task.error_code = None
    task.error_message = None
    db.add(task)
    return True


def task_has_asset_kind(db: Session, task_id: uuid.UUID, kind: AssetKind) -> bool:
    return db.query(Asset.id).filter(Asset.task_id == task_id, Asset.kind == kind).limit(1).first() is not None


def task_status_after_review_pass(db: Session, task: Task) -> TaskStatus:
    if task_has_asset_kind(db, task.id, AssetKind.video_final):
        return TaskStatus.rendered
    if task_has_asset_kind(db, task.id, AssetKind.subtitle_srt) or task_has_asset_kind(
        db, task.id, AssetKind.subtitle_ass
    ):
        return TaskStatus.subtitle_ready
    if task_has_asset_kind(db, task.id, AssetKind.segments_json):
        return TaskStatus.asr_done
    if task_has_asset_kind(db, task.id, AssetKind.video_raw):
        return TaskStatus.downloaded
    return task.status


def latest_task_cover_key(task_id: uuid.UUID, db: Session) -> str | None:
    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.cover_image)
        .order_by(Asset.created_at.desc())
        .first()
    )
    return asset.storage_key if asset else None


def build_auto_publish_after_render(
    task: Task,
    *,
    db: Session,
    s3: S3Store,
    publish_payload_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    auto_profile = get_auto_profile(db)
    stored_meta = read_s3_json_object(s3, publish_meta_s3_key(task.id))
    meta = build_task_publish_meta_draft(task, db=db, s3=s3, mode="source", base_meta=stored_meta)
    write_s3_json(s3, publish_meta_s3_key(task.id), meta)
    cover_key = latest_task_cover_key(task.id, db) if bool(auto_profile.get("publish_use_youtube_cover")) else None
    publish_payload: dict[str, Any] = {
        "account_id": None,
        "video_key": None,
        "cover_key": cover_key,
        "typeid_mode": auto_profile.get("publish_typeid_mode") or "ai_summary",
        "meta": None,
    }
    if isinstance(publish_payload_overrides, dict):
        publish_payload.update(publish_payload_overrides)
    return {"publish": True, "publish_payload": publish_payload}


def apply_task_review_result(db: Session, task: Task, review_result: dict[str, Any]) -> None:
    if bool(review_result.get("ok")):
        if task.error_code == "AI_REVIEW_REJECTED":
            task.error_code = None
            task.error_message = None
            if task.status == TaskStatus.ready_for_review:
                task.status = task_status_after_review_pass(db, task)
    else:
        if task.status not in {TaskStatus.publishing, TaskStatus.published, TaskStatus.canceled}:
            task.status = TaskStatus.ready_for_review
        task.error_code = "AI_REVIEW_REJECTED"
        task.error_message = str(review_result.get("reason") or "").strip() or "AI 审核未通过"
    db.add(task)


def read_latest_task_subtitle_text(task_id: uuid.UUID, db: Session, s3: S3Store) -> str:
    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind.in_([AssetKind.subtitle_srt, AssetKind.subtitle_ass]))
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return ""
    try:
        return read_s3_bytes(s3, asset.storage_key).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def prepare_publish_meta(
    *,
    task: Task,
    payload_meta: dict[str, Any] | None,
    db: Session,
    s3: S3Store,
    allow_auto_draft: bool,
) -> dict[str, Any]:
    if payload_meta is None:
        stored = read_s3_json_object(s3, publish_meta_s3_key(task.id))
        if stored is None:
            if not allow_auto_draft:
                raise HTTPException(status_code=400, detail="meta is missing and publish_meta is not found")
            meta = build_task_publish_meta_draft(task, db=db, s3=s3, mode="auto")
        else:
            meta = dict(stored)
    else:
        meta = dict(payload_meta or {})

    try:
        copyright_value = int(meta.get("copyright") or 1)
    except Exception:
        copyright_value = 1
    if copyright_value == 2 and not str(meta.get("source") or "").strip() and task.source_url:
        meta["source"] = task.source_url

    merged_tags: list[str] = []
    seen: set[str] = set()
    for tag in ["videoroll", *get_task_bilibili_tags(db, str(task.id)), *normalize_tags(meta.get("tags"))]:
        text = str(tag or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        merged_tags.append(text)
        if len(merged_tags) >= 10:
            break
    if merged_tags:
        meta["tags"] = merged_tags

    try:
        return BilibiliPublishMeta.model_validate(meta).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid publish meta: {exc}") from exc


def run_task_publish_review(task: Task, *, meta: dict[str, Any], db: Session, s3: S3Store) -> dict[str, Any]:
    settings = get_publish_review_settings(db)
    current = get_task_publish_review_record(db, str(task.id))
    if not settings["enabled"]:
        if task.error_code == "AI_REVIEW_REJECTED":
            task.error_code = None
            task.error_message = None
            if task.status == TaskStatus.ready_for_review:
                task.status = task_status_after_review_pass(db, task)
            db.add(task)
            db.commit()
        return {"enabled": False, **current}

    translate_settings = get_translate_settings(db, get_subtitle_settings())
    api_key = str(translate_settings.get("openai_api_key") or "").strip()
    ai_service = AIService(lambda: get_translate_settings(db, get_subtitle_settings())) if api_key else None
    result = review_publish_materials(
        title=str(meta.get("title") or "").strip(),
        summary=get_task_bilibili_summary(db, str(task.id)),
        subtitle_text=read_latest_task_subtitle_text(task.id, db, s3),
        blocked_words=settings["blocked_words"],
        reject_rules=settings["ai_rules"],
        ai_service=ai_service,
    )
    stored = set_task_publish_review(
        db,
        str(task.id),
        ok=bool(result.get("ok")),
        reason=str(result.get("reason") or "").strip(),
        matched_blocked_words=list(result.get("matched_blocked_words") or []),
        review_mode=str(result.get("review_mode") or "").strip() or None,
        risk_tags=list(result.get("risk_tags") or []),
        title=result.get("title"),
        summary=result.get("summary"),
        subtitle_chars=int(result.get("subtitle_chars") or 0),
    )
    apply_task_review_result(db, task, stored)
    db.commit()
    return {"enabled": True, **stored}


def get_task_publish_meta(task_id: uuid.UUID, db: Session, s3: S3Store) -> dict[str, Any]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    value = read_s3_json_object(s3, publish_meta_s3_key(task_id))
    if value is None:
        raise HTTPException(status_code=404, detail="publish_meta not found")
    return value


def get_task_publish_meta_draft(task_id: uuid.UUID, db: Session, s3: S3Store) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    stored = read_s3_json_object(s3, publish_meta_s3_key(task_id))
    return build_task_publish_meta_draft(task, db=db, s3=s3, mode="auto", base_meta=stored)


def generate_task_publish_meta_draft(
    task_id: uuid.UUID,
    *,
    mode: str,
    base_meta: dict[str, Any] | None,
    db: Session,
    s3: S3Store,
) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return build_task_publish_meta_draft(task, db=db, s3=s3, mode=mode, base_meta=base_meta)


def put_task_publish_meta(task_id: uuid.UUID, meta: dict[str, Any], db: Session, s3: S3Store) -> dict[str, Any]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="meta must be an object")
    try:
        value = BilibiliPublishMeta.model_validate(meta).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid publish_meta: {exc}") from exc
    key = publish_meta_s3_key(task_id)
    try:
        write_s3_json(s3, key, value)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to write publish_meta: {exc}") from exc
    return {"stored": True, "key": key, "meta": value}


def get_task_publish_review(task_id: uuid.UUID, db: Session) -> dict[str, Any]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    settings = get_publish_review_settings(db)
    return {"enabled": settings["enabled"], **get_task_publish_review_record(db, str(task_id))}


def review_task_publish(task_id: uuid.UUID, meta: dict[str, Any] | None, db: Session, s3: S3Store) -> dict[str, Any]:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    prepared = prepare_publish_meta(task=task, payload_meta=meta, db=db, s3=s3, allow_auto_draft=True)
    return run_task_publish_review(task, meta=prepared, db=db, s3=s3)


def read_publish_platform_settings(db: Session) -> dict[str, bool]:
    return get_publish_platform_settings(db)


def put_publish_platform_setting(platform: str, enabled: bool, db: Session) -> dict[str, bool]:
    try:
        return update_publish_platform_setting(db, platform, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def build_publish_gateway_request(
    *,
    task: Task,
    task_id: uuid.UUID,
    payload: PublishActionRequest,
    video_key: str,
    db: Session,
    s3: S3Store,
) -> dict[str, Any]:
    platform = normalize_publish_platform(payload.platform)
    if platform != "bilibili":
        try:
            uuid.UUID(str(payload.account_id or ""))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="social publish account_id must be a UUID") from exc
    if platform == "bilibili":
        meta = prepare_publish_meta(task=task, payload_meta=payload.meta, db=db, s3=s3, allow_auto_draft=False)
    else:
        meta_source = payload.meta
        if meta_source is None:
            meta_source = read_s3_json_object(s3, publish_meta_key(task_id, platform))
        if meta_source is None:
            meta_source = read_s3_json_object(s3, publish_meta_s3_key(task_id))
        if meta_source is None:
            raise HTTPException(status_code=400, detail="meta is missing and platform publish meta is not found")
        try:
            meta = normalize_social_publish_meta(as_dict(meta_source), platform)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid publish meta: {exc}") from exc
    all_options = payload.platform_options if isinstance(payload.platform_options, dict) else {}
    platform_options = as_dict(all_options.get(platform))
    request: dict[str, Any] = {
        "platform": platform,
        "task_id": str(task_id),
        "account_id": payload.account_id,
        "video": {"type": "s3", "key": video_key},
        "cover": {"type": "s3", "key": payload.cover_key} if payload.cover_key else None,
        "meta": meta,
        "platform_options": platform_options,
    }
    if platform != "bilibili":
        request["force_retry"] = bool(payload.force_retry)
    typeid_mode = str(platform_options.get("typeid_mode") or payload.typeid_mode or "").strip()
    if typeid_mode:
        request["typeid_mode"] = typeid_mode
    return request


def social_publisher_error(exc: httpx.HTTPStatusError) -> HTTPException:
    try:
        body = exc.response.json()
        detail = str(body.get("detail") or body.get("message") or body)
    except Exception:
        detail = exc.response.text
    return HTTPException(status_code=exc.response.status_code, detail=f"social-publisher: {detail}")


def _social_response(request: Any) -> Any:
    try:
        request.raise_for_status()
        return request.json()
    except httpx.HTTPStatusError as exc:
        raise social_publisher_error(exc) from exc


def list_social_publish_accounts(platform: str | None, settings: OrchestratorSettings) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(
                client.get(f"{settings.social_publisher_url}/accounts", params={"platform": platform} if platform else None)
            )
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc


def start_social_login_session(platform: str, payload: dict[str, Any], settings: OrchestratorSettings) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(client.post(f"{settings.social_publisher_url}/login-sessions/{platform}", json=payload))
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc


def get_social_login_session(session_id: uuid.UUID, settings: OrchestratorSettings) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(client.get(f"{settings.social_publisher_url}/login-sessions/{session_id}"))
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc


def cancel_social_login_session(session_id: uuid.UUID, settings: OrchestratorSettings) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(client.delete(f"{settings.social_publisher_url}/login-sessions/{session_id}"))
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc


async def import_social_publish_account(
    platform: str,
    account_name: str,
    file: UploadFile,
    settings: OrchestratorSettings,
) -> Any:
    raw = await file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="storage_state exceeds 1 MiB")
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(
                client.post(
                    f"{settings.social_publisher_url}/accounts/{platform}",
                    data={"account_name": account_name},
                    files={"file": (file.filename or "storage_state.json", raw, "application/json")},
                )
            )
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc
    finally:
        await file.close()


def check_social_publish_account(account_id: uuid.UUID, settings: OrchestratorSettings) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(client.post(f"{settings.social_publisher_url}/accounts/{account_id}/check"))
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc


def delete_social_publish_account(account_id: uuid.UUID, settings: OrchestratorSettings) -> Any:
    try:
        with httpx.Client(timeout=30.0, headers=internal_http_headers(settings)) as client:
            return _social_response(client.delete(f"{settings.social_publisher_url}/accounts/{account_id}"))
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"social-publisher request failed: {exc}") from exc


def list_task_publish_jobs(task_id: uuid.UUID, limit: int, db: Session) -> list[dict[str, Any]]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    jobs = (
        db.query(PublishJob)
        .filter(PublishJob.task_id == task_id)
        .order_by(PublishJob.created_at.desc())
        .limit(limit)
        .all()
    )
    output: list[dict[str, Any]] = []
    for job in jobs:
        response = as_dict(job.response_json)
        typeid = as_dict(response.get("typeid"))
        ai = as_dict(typeid.get("ai"))
        typeid_mode = str(typeid.get("mode") or as_dict(job.meta_json).get("typeid_mode") or "").strip() or None
        selected_by = str(typeid.get("selected_by") or "").strip() or None
        tid_value = typeid.get("selected") if typeid else response.get("tid")
        try:
            tid = int(tid_value) if tid_value is not None else None
        except Exception:
            tid = None
        if tid is not None and tid <= 0:
            tid = None
        ai_ok = bool(ai.get("ok")) if "ok" in ai else None
        output.append(
            {
                "id": job.id,
                "task_id": job.task_id,
                "batch_id": job.batch_id,
                "platform": str(getattr(job.platform, "value", job.platform) or "bilibili"),
                "state": job.state.value,
                "aid": job.aid,
                "bvid": job.bvid,
                "external_id": job.external_id,
                "external_url": job.external_url,
                "account_id": job.account_id,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "tid": tid,
                "typeid_mode": typeid_mode,
                "typeid_selected_by": selected_by,
                "typeid_ai_ok": ai_ok,
                "typeid_ai_reason": str(ai.get("reason") or "").strip() or None,
                "error_message": publish_job_error_message(job),
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
        )
    return output


def list_task_publish_batches(task_id: uuid.UUID, limit: int, db: Session) -> list[dict[str, Any]]:
    if not db.get(Task, task_id):
        raise HTTPException(status_code=404, detail="task not found")
    batches = (
        db.query(PublishBatch)
        .filter(PublishBatch.task_id == task_id)
        .order_by(PublishBatch.created_at.desc())
        .limit(limit)
        .all()
    )
    batch_ids = [batch.id for batch in batches]
    jobs = (
        db.query(PublishJob)
        .filter(PublishJob.batch_id.in_(batch_ids))
        .order_by(PublishJob.updated_at.desc(), PublishJob.created_at.desc())
        .all()
        if batch_ids
        else []
    )
    latest_job_outcomes: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
    for job in jobs:
        if job.batch_id is None:
            continue
        by_target = latest_job_outcomes.setdefault(job.batch_id, {})
        key = publish_target_key(job.platform, job.account_id)
        if key not in by_target:
            by_target[key] = {
                "state": job.state.value,
                "detail": publish_job_error_message(job),
                "job_id": str(job.id),
            }
    return [
        {
            "id": batch.id,
            "task_id": batch.task_id,
            "state": batch.state,
            "expected_targets": list(batch.expected_targets or []),
            "outcomes": {
                **dict(batch.outcomes_json or {}),
                **latest_job_outcomes.get(batch.id, {}),
            },
            "cleanup_enqueued_at": batch.cleanup_enqueued_at,
            "finished_at": batch.finished_at,
            "created_at": batch.created_at,
            "updated_at": batch.updated_at,
        }
        for batch in batches
    ]


def enqueue_publish_job(
    task_id: uuid.UUID,
    payload: PublishActionRequest,
    settings: OrchestratorSettings,
    db: Session,
    s3: S3Store,
) -> RemotePublishResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_license.value == "unknown":
        raise HTTPException(status_code=400, detail="source_license=unknown; add proof before publishing")
    try:
        requested_platform = normalize_publish_platform(payload.platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not is_publish_platform_enabled(db, requested_platform):
        raise HTTPException(
            status_code=409,
            detail=f"publish platform is disabled: {requested_platform}; enable it in 投稿设置 first",
        )

    video_key = payload.video_key
    if not video_key:
        final_asset = (
            db.query(Asset)
            .filter(Asset.task_id == task_id, Asset.kind == AssetKind.video_final)
            .order_by(Asset.created_at.desc())
            .first()
        )
        if not final_asset:
            raise HTTPException(status_code=400, detail="no final video asset found; render first")
        video_key = final_asset.storage_key

    request = build_publish_gateway_request(
        task=task,
        task_id=task_id,
        payload=payload,
        video_key=video_key,
        db=db,
        s3=s3,
    )
    platform = str(request.get("platform") or "bilibili")
    try:
        write_s3_json(s3, publish_meta_key(task_id, platform), as_dict(request.get("meta")))
        if platform == "bilibili":
            write_s3_json(s3, publish_meta_s3_key(task_id), as_dict(request.get("meta")))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"failed to persist publish_meta: {exc}") from exc

    if not bool(payload.skip_review):
        review_result = run_task_publish_review(task, meta=as_dict(request.get("meta")), db=db, s3=s3)
        if not bool(review_result.get("ok")):
            raise HTTPException(status_code=409, detail=str(review_result.get("reason") or "AI 审核未通过"))
    elif task.error_code == "AI_REVIEW_REJECTED":
        task.error_code = None
        task.error_message = None
        if task.status == TaskStatus.ready_for_review:
            task.status = task_status_after_review_pass(db, task)
        db.add(task)
        db.commit()

    from videoroll.apps.publish_service import PublishService

    svc = PublishService(
        db, settings, s3,
        http_headers=lambda: internal_http_headers(settings),
    )
    try:
        data = svc.publish_one(task_id, platform=platform, payload=request)
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
            detail = str(body.get("detail") or body.get("message") or body)
        except Exception:
            detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=f"{platform}-publisher: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{platform}-publisher request failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RemotePublishResponse(**data)


def publish_all(
    task_id: uuid.UUID,
    publish_payload: PublishAllRequest,
    settings: OrchestratorSettings,
    db: Session,
    s3: S3Store,
) -> dict[str, Any]:
    """多平台投稿：读取已启用平台，逐个投稿。"""
    from videoroll.apps.publish_service import PublishService

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.source_license.value == "unknown":
        raise HTTPException(status_code=400, detail="source_license=unknown; add proof before publishing")

    platform_settings = get_publish_platform_settings(db)
    enabled_platforms = [platform for platform, enabled in platform_settings.items() if enabled]
    if not enabled_platforms:
        raise HTTPException(status_code=409, detail="no publish platforms are enabled")

    payload = publish_payload.model_dump()
    platform_meta = payload.get("platform_meta")
    review_meta = as_dict(payload.get("meta"))
    if "bilibili" in enabled_platforms:
        bilibili_meta = platform_meta.get("bilibili") if isinstance(platform_meta, dict) else None
        meta = prepare_publish_meta(
            task=task,
            payload_meta=as_dict(bilibili_meta or payload.get("meta")) or None,
            db=db,
            s3=s3,
            allow_auto_draft=False,
        )
        payload["meta"] = meta
        review_meta = meta
    elif isinstance(platform_meta, dict):
        for platform in enabled_platforms:
            candidate = as_dict(platform_meta.get(platform))
            if candidate:
                review_meta = candidate
                break
    if not review_meta:
        for platform in enabled_platforms:
            stored_meta = read_s3_json_object(s3, publish_meta_key(task_id, platform))
            if stored_meta is None:
                stored_meta = read_s3_json_object(s3, publish_meta_s3_key(task_id))
            if stored_meta:
                review_meta = stored_meta
                break
    if not publish_payload.skip_review:
        review_result = run_task_publish_review(task, meta=review_meta, db=db, s3=s3)
        if not bool(review_result.get("ok")):
            raise HTTPException(
                status_code=409,
                detail=str(review_result.get("reason") or "AI 审核未通过"),
            )

    svc = PublishService(
        db, settings, s3,
        http_headers=lambda: internal_http_headers(settings),
    )
    result = svc.publish(task_id, publish_payload=payload)
    return {
        "results": result.results,
        "batch_id": result.batch_id,
        "all_accepted": result.all_accepted,
        "has_any_accepted": result.has_any_accepted,
        "all_published": result.all_published,
        "all_succeeded": result.all_succeeded,
        "platform_count": result.platform_count,
        "ok_count": result.ok_count,
        "error_count": result.error_count,
        "errors": result.errors,
    }
