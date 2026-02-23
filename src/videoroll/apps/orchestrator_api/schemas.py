from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from videoroll.db.models import SourceLicense, SourceType, TaskStatus


class TaskCreate(BaseModel):
    source_type: SourceType
    source_url: Optional[str] = None
    source_license: SourceLicense = SourceLicense.own
    source_proof_url: Optional[str] = None
    priority: int = 0
    created_by: Optional[str] = None


class TaskRead(BaseModel):
    id: uuid.UUID
    source_type: SourceType
    source_url: Optional[str]
    source_license: SourceLicense
    source_proof_url: Optional[str]
    status: TaskStatus
    priority: int
    created_by: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    retry_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssetRead(BaseModel):
    id: uuid.UUID
    kind: str
    storage_key: str
    sha256: Optional[str]
    size_bytes: Optional[int]
    duration_ms: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class SubtitleActionRequest(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["srt"])
    burn_in: bool = False
    soft_sub: bool = False
    ass_style: str = "clean_white"
    video_codec: str = "av1"

    asr_engine: str = "auto"
    asr_language: str = "auto"
    asr_model: Optional[str] = None

    translate_enabled: bool = False
    translate_provider: str = "mock"
    target_lang: str = "zh"
    translate_style: str = "口语自然"
    translate_batch_size: Optional[int] = None
    translate_enable_summary: Optional[bool] = None
    bilingual: bool = False


class PublishActionRequest(BaseModel):
    account_id: Optional[str] = None
    video_key: Optional[str] = None
    cover_key: Optional[str] = None
    typeid_mode: Optional[str] = None
    meta: dict[str, Any]


class RemoteJobResponse(BaseModel):
    job_id: uuid.UUID
    status: str


class RemotePublishResponse(BaseModel):
    state: str
    aid: Optional[str] = None
    bvid: Optional[str] = None
    response: Optional[dict[str, Any]] = None


class SubtitleJobSummary(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    status: str
    progress: int
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PublishJobSummary(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    state: str
    aid: Optional[str]
    bvid: Optional[str]
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class YouTubeMetaRead(BaseModel):
    title: str
    description: str = ""
    webpage_url: str
    uploader: Optional[str] = None
    upload_date: Optional[str] = None
    duration: Optional[int] = None


class YouTubeMetaActionResponse(BaseModel):
    metadata: YouTubeMetaRead
    metadata_asset: AssetRead


class YouTubeDownloadActionResponse(BaseModel):
    metadata: YouTubeMetaRead
    metadata_asset: AssetRead
    video_asset: AssetRead
    cover_asset: Optional[AssetRead] = None


class AutoYouTubeRequest(BaseModel):
    url: str
    license: SourceLicense = SourceLicense.authorized
    proof_url: Optional[str] = None


class AutoYouTubeResponse(BaseModel):
    task_id: uuid.UUID
    pipeline_job_id: str
    deduped: bool = False
    source_id: Optional[str] = None


class ConvertedVideoItem(BaseModel):
    task: TaskRead
    final_asset: AssetRead
    cover_asset: Optional[AssetRead] = None
    display_title: Optional[str] = None


class StorageRetentionSettingsRead(BaseModel):
    asset_ttl_days: int = 0


class StorageRetentionSettingsUpdate(BaseModel):
    asset_ttl_days: Optional[int] = None


class YouTubeSettingsRead(BaseModel):
    proxy: str = ""


class YouTubeSettingsUpdate(BaseModel):
    proxy: Optional[str] = None


class YouTubeProxyTestRequest(BaseModel):
    proxy: Optional[str] = None
    url: Optional[str] = None


class YouTubeProxyTestResponse(BaseModel):
    ok: bool
    url: str
    used_proxy: Optional[str] = None
    status_code: Optional[int] = None
    elapsed_ms: int
    error: Optional[str] = None
