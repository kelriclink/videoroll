from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Generator

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from videoroll.config import SubtitleServiceSettings, get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.models import Asset, AssetKind, SubtitleJob
from videoroll.db.session import db_session, get_engine
from videoroll.storage.s3 import S3Store
from videoroll.apps.subtitle_service.schemas import (
    ASRDefaultsRead,
    ASRDefaultsUpdate,
    SubtitleJobCreate,
    SubtitleJobRead,
    SubtitleAutoProfileRead,
    SubtitleAutoProfileUpdate,
    TranslateSettingsRead,
    TranslateSettingsUpdate,
    TranslateTestRequest,
    TranslateTestResponse,
    WhisperModelDownloadRequest,
    WhisperModelInfo,
    WhisperSettingsRead,
    ModelDownloadProxyTestRequest,
    ModelDownloadProxyTestResponse,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings, update_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile, update_auto_profile
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings, update_translate_settings
from videoroll.apps.subtitle_service.worker import celery_app
from videoroll.utils.hf_hub import configure_hf_hub_proxy


def get_settings() -> SubtitleServiceSettings:
    return get_subtitle_settings()


def get_db(settings: SubtitleServiceSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    yield from db_session(settings.database_url)


def _models_dir(settings: SubtitleServiceSettings) -> Path:
    return Path(settings.whisper_model_dir)


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_model_name(name: str) -> str:
    name = (name or "").strip()
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid model name (allowed: [A-Za-z0-9._-], max 64 chars)")
    return name


def _dir_size_bytes(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            total += p.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                raise HTTPException(status_code=400, detail=f"unsafe zip entry: {name}")
        zf.extractall(dest_dir)


app = FastAPI(title="videoroll-subtitle-service", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    settings = get_subtitle_settings()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    S3Store(settings).ensure_bucket()
    _models_dir(settings).mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/subtitle/settings", response_model=WhisperSettingsRead)
def get_subtitle_settings_view(settings: SubtitleServiceSettings = Depends(get_settings)) -> WhisperSettingsRead:
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        fw_installed = True
    except Exception:
        fw_installed = False
    return WhisperSettingsRead(
        asr_engine=settings.asr_engine,
        whisper_model=settings.whisper_model,
        whisper_model_dir=settings.whisper_model_dir,
        whisper_device=settings.whisper_device,
        whisper_compute_type=settings.whisper_compute_type,
        faster_whisper_installed=fw_installed,
    )


@app.get("/subtitle/asr/settings", response_model=ASRDefaultsRead)
def get_asr_settings_view(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ASRDefaultsRead:
    cfg = get_asr_settings(db, settings)
    return ASRDefaultsRead(**cfg)


@app.put("/subtitle/asr/settings", response_model=ASRDefaultsRead)
def put_asr_settings_view(
    payload: ASRDefaultsUpdate,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ASRDefaultsRead:
    try:
        cfg = update_asr_settings(db, settings, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ASRDefaultsRead(**cfg)


@app.get("/subtitle/auto/profile", response_model=SubtitleAutoProfileRead)
def get_subtitle_auto_profile(db: Session = Depends(get_db)) -> SubtitleAutoProfileRead:
    cfg = get_auto_profile(db)
    return SubtitleAutoProfileRead(**cfg)


@app.put("/subtitle/auto/profile", response_model=SubtitleAutoProfileRead)
def put_subtitle_auto_profile(payload: SubtitleAutoProfileUpdate, db: Session = Depends(get_db)) -> SubtitleAutoProfileRead:
    try:
        cfg = update_auto_profile(db, payload.model_dump(exclude_unset=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SubtitleAutoProfileRead(**cfg)


@app.get("/subtitle/translate/settings", response_model=TranslateSettingsRead)
def get_translate_settings_view(
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TranslateSettingsRead:
    cfg = get_translate_settings(db, settings)
    return TranslateSettingsRead(
        default_provider=cfg["default_provider"],
        default_target_lang=cfg["default_target_lang"],
        default_style=cfg["default_style"],
        default_batch_size=cfg["default_batch_size"],
        default_enable_summary=cfg["default_enable_summary"],
        openai_api_key_set=cfg["openai_api_key_set"],
        openai_base_url=cfg["openai_base_url"],
        openai_model=cfg["openai_model"],
        openai_temperature=cfg["openai_temperature"],
        openai_timeout_seconds=cfg["openai_timeout_seconds"],
    )


@app.put("/subtitle/translate/settings", response_model=TranslateSettingsRead)
def put_translate_settings_view(
    payload: TranslateSettingsUpdate,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TranslateSettingsRead:
    cfg = update_translate_settings(db, settings, payload.model_dump(exclude_unset=True))
    return TranslateSettingsRead(
        default_provider=cfg["default_provider"],
        default_target_lang=cfg["default_target_lang"],
        default_style=cfg["default_style"],
        default_batch_size=cfg["default_batch_size"],
        default_enable_summary=cfg["default_enable_summary"],
        openai_api_key_set=cfg["openai_api_key_set"],
        openai_base_url=cfg["openai_base_url"],
        openai_model=cfg["openai_model"],
        openai_temperature=cfg["openai_temperature"],
        openai_timeout_seconds=cfg["openai_timeout_seconds"],
    )


@app.post("/subtitle/translate/test", response_model=TranslateTestResponse)
def translate_test(
    payload: TranslateTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> TranslateTestResponse:
    from videoroll.apps.subtitle_service.processing import Segment, translate_segments_openai

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long (max 2000 chars)")

    try:
        cfg = get_translate_settings(db, settings)
        translated = translate_segments_openai(
            [Segment(start=0.0, end=1.0, text=text)],
            target_lang=payload.target_lang,
            style=payload.style,
            api_key=cfg["openai_api_key"],
            base_url=cfg["openai_base_url"],
            model=cfg["openai_model"],
            temperature=cfg["openai_temperature"],
            timeout_seconds=cfg["openai_timeout_seconds"],
            batch_size=1,
            enable_summary=cfg["default_enable_summary"],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return TranslateTestResponse(translated_text=translated[0].text if translated else "")


@app.get("/subtitle/models", response_model=list[WhisperModelInfo])
def list_whisper_models(settings: SubtitleServiceSettings = Depends(get_settings)) -> list[WhisperModelInfo]:
    root = _models_dir(settings)
    root.mkdir(parents=True, exist_ok=True)

    out: list[WhisperModelInfo] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        if p.name.startswith("."):
            continue
        out.append(WhisperModelInfo(name=p.name, path=str(p), size_bytes=_dir_size_bytes(p)))
    return out


@app.post("/subtitle/models/download", response_model=WhisperModelInfo)
def download_whisper_model(
    payload: WhisperModelDownloadRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> WhisperModelInfo:
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=400, detail="huggingface_hub not installed. Rebuild with INSTALL_ASR=1.") from e

    size_to_repo = {
        "tiny": "Systran/faster-whisper-tiny",
        "base": "Systran/faster-whisper-base",
        "small": "Systran/faster-whisper-small",
        "medium": "Systran/faster-whisper-medium",
        "large-v1": "Systran/faster-whisper-large-v1",
        "large-v2": "Systran/faster-whisper-large-v2",
        "large-v3": "Systran/faster-whisper-large-v3",
    }

    model = (payload.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    repo_id = size_to_repo.get(model, model)

    name = payload.name
    if not name:
        name = model.replace("/", "--")
    name = _validate_model_name(name)

    dest = _models_dir(settings) / name
    tmp = dest.with_name(dest.name + ".downloading")
    if dest.exists():
        if not payload.force:
            raise HTTPException(status_code=400, detail="model already exists; set force=true to overwrite")
        shutil.rmtree(dest, ignore_errors=True)

    # Use the dedicated model-download proxy (stored in DB via Settings · ASR).
    asr_cfg = get_asr_settings(db, settings)
    proxy = str(asr_cfg.get("model_download_proxy") or "").strip() or None
    configure_hf_hub_proxy(proxy)

    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        try:
            snapshot_download(repo_id=repo_id, revision=payload.revision, local_dir=str(tmp))
        except TypeError:
            # Backward-compatible with older huggingface_hub versions (no proxies/local_dir_use_symlinks kwarg).
            snapshot_download(repo_id=repo_id, revision=payload.revision, local_dir=str(tmp))

        shutil.rmtree(dest, ignore_errors=True)
        tmp.replace(dest)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"download failed: {type(e).__name__}: {e}") from e

    return WhisperModelInfo(name=name, path=str(dest), size_bytes=_dir_size_bytes(dest))


@app.post("/subtitle/models/upload", response_model=WhisperModelInfo)
async def upload_whisper_model(
    name: str,
    file: UploadFile = File(...),
    settings: SubtitleServiceSettings = Depends(get_settings),
) -> WhisperModelInfo:
    name = _validate_model_name(name)
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="only .zip is supported")

    dest = _models_dir(settings) / name
    if dest.exists():
        raise HTTPException(status_code=400, detail="model already exists; delete it first")

    with tempfile.NamedTemporaryFile(prefix="whisper_model_", suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    try:
        _safe_extract_zip(tmp_path, dest)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    return WhisperModelInfo(name=name, path=str(dest), size_bytes=_dir_size_bytes(dest))


@app.delete("/subtitle/models/{name}")
def delete_whisper_model(name: str, settings: SubtitleServiceSettings = Depends(get_settings)) -> dict[str, bool]:
    name = _validate_model_name(name)
    dest = _models_dir(settings) / name
    if not dest.exists():
        raise HTTPException(status_code=404, detail="model not found")
    shutil.rmtree(dest, ignore_errors=True)
    return {"deleted": True}


@app.post("/subtitle/models/proxy/test", response_model=ModelDownloadProxyTestResponse)
def test_model_download_proxy(
    payload: ModelDownloadProxyTestRequest,
    settings: SubtitleServiceSettings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ModelDownloadProxyTestResponse:
    url = str(payload.url or "").strip() or "https://huggingface.co/robots.txt"

    if payload.proxy is not None:
        proxy = str(payload.proxy or "").strip()
    else:
        cfg = get_asr_settings(db, settings)
        proxy = str(cfg.get("model_download_proxy") or "").strip()

    start = time.perf_counter()
    client_kwargs: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True}
    if proxy:
        try:
            client_kwargs["proxy"] = proxy
        except Exception:
            pass

    try:
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.get(url)
                ok = resp.status_code < 400
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return ModelDownloadProxyTestResponse(
                    ok=ok,
                    url=url,
                    used_proxy=proxy or None,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed_ms,
                )
        except TypeError:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                resp = client.get(url)
                ok = resp.status_code < 400
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return ModelDownloadProxyTestResponse(
                    ok=ok,
                    url=url,
                    used_proxy=None,
                    status_code=resp.status_code,
                    elapsed_ms=elapsed_ms,
                    error="httpx does not support proxy kwarg in this environment",
                )
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ModelDownloadProxyTestResponse(
            ok=False,
            url=url,
            used_proxy=proxy or None,
            status_code=None,
            elapsed_ms=elapsed_ms,
            error=str(e),
        )


@app.post("/subtitle/jobs")
def create_job(payload: SubtitleJobCreate, db: Session = Depends(get_db)) -> dict[str, str]:
    job = SubtitleJob(task_id=payload.task_id, request_json=payload.model_dump(mode="json"))
    db.add(job)
    db.commit()
    db.refresh(job)
    celery_app.send_task("subtitle_service.process_job", args=[str(job.id)], queue="subtitle")
    return {"job_id": str(job.id), "status": job.status.value}


@app.get("/subtitle/jobs/{job_id}", response_model=SubtitleJobRead)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> SubtitleJobRead:
    job = db.get(SubtitleJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    artifacts = (
        db.query(Asset)
        .filter(
            Asset.task_id == job.task_id,
            Asset.kind.in_(
                [
                    AssetKind.audio_wav,
                    AssetKind.segments_json,
                    AssetKind.subtitle_srt,
                    AssetKind.subtitle_ass,
                    AssetKind.video_final,
                    AssetKind.log,
                ]
            ),
        )
        .order_by(Asset.created_at.asc())
        .all()
    )

    return SubtitleJobRead(
        job_id=job.id,
        task_id=job.task_id,
        status=job.status.value,
        progress=job.progress,
        artifacts=[{"kind": a.kind.value, "key": a.storage_key} for a in artifacts],
        logs_key=job.logs_key,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
