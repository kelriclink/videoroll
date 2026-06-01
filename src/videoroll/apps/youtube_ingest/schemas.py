from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from videoroll.db.models import SourceLicense, YouTubeSourceType


class YouTubeSourceCreate(BaseModel):
    source_type: Optional[YouTubeSourceType] = None
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    license: SourceLicense = SourceLicense.own
    proof_url: Optional[str] = None
    enabled: bool = True
    scan_interval_minutes: int = Field(default=60, ge=1, le=1440)
    scan_limit: int = Field(default=20, ge=1, le=200)
    auto_process: bool = True


class YouTubeSourceRead(BaseModel):
    id: uuid.UUID
    source_type: YouTubeSourceType
    source_id: str
    source_url: str
    display_name: Optional[str] = None
    license: SourceLicense
    proof_url: Optional[str]
    enabled: bool
    scan_interval_minutes: int = 60
    scan_limit: int = 20
    auto_process: bool = True
    last_scan_started_at: Optional[datetime] = None
    last_scan_finished_at: Optional[datetime] = None
    last_scan_discovered_count: int = 0
    last_scan_created_count: int = 0
    last_scan_started_pipeline_count: int = 0
    last_scan_skipped_duplicates: int = 0
    last_scan_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class YouTubeSourceUpdate(BaseModel):
    license: Optional[SourceLicense] = None
    proof_url: Optional[str] = None
    enabled: Optional[bool] = None
    scan_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    scan_limit: Optional[int] = Field(default=None, ge=1, le=200)
    auto_process: Optional[bool] = None
    display_name: Optional[str] = None


class YouTubeIngestRequest(BaseModel):
    url: str
    license: SourceLicense = SourceLicense.own
    proof_url: Optional[str] = None


class YouTubeIngestResponse(BaseModel):
    task_id: uuid.UUID
    deduped: bool = False
    source_id: Optional[str] = None


class YouTubeScanRequest(BaseModel):
    source_type: YouTubeSourceType
    source_id: str
    since: Optional[datetime] = None
    limit: int = Field(default=20, ge=1, le=200)
    auto_process: bool = True


class YouTubeScanResponse(BaseModel):
    discovered_count: int
    created_task_ids: list[uuid.UUID]
    skipped_duplicates: int
    started_pipeline_job_ids: list[str] = Field(default_factory=list)


class YouTubeSourceScanRequest(BaseModel):
    limit: Optional[int] = Field(default=None, ge=1, le=200)
    auto_process: Optional[bool] = None
