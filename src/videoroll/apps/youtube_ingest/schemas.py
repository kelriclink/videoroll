from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from videoroll.db.models import SourceLicense, YouTubeSourceType


class YouTubeSourceCreate(BaseModel):
    source_type: YouTubeSourceType
    source_id: str
    license: SourceLicense = SourceLicense.own
    proof_url: Optional[str] = None
    enabled: bool = True


class YouTubeSourceRead(BaseModel):
    id: uuid.UUID
    source_type: YouTubeSourceType
    source_id: str
    license: SourceLicense
    proof_url: Optional[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
