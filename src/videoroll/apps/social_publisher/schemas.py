from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SocialPlatform = Literal["douyin", "xiaohongshu", "kuaishou"]


class SocialAccountRead(BaseModel):
    id: uuid.UUID
    platform: str
    name: str
    is_active: bool
    check_state: str
    last_checked_at: datetime | None = None
    last_check_message: str | None = None
    rotated_at: datetime | None = None


class SocialAccountImportResponse(BaseModel):
    account: SocialAccountRead
    check_job_id: str


class SocialLoginStartRequest(BaseModel):
    account_name: str


class SocialLoginSessionRead(BaseModel):
    id: uuid.UUID
    platform: str
    account_name: str
    state: str
    message: str | None = None
    browser_url: str
    created_at: datetime
    finished_at: datetime | None = None


class InputRef(BaseModel):
    type: Literal["s3"] = "s3"
    key: str


class SocialPublishRequest(BaseModel):
    platform: SocialPlatform
    task_id: uuid.UUID
    account_id: uuid.UUID
    video: InputRef
    cover: InputRef | None = None
    meta: dict[str, Any]
    platform_options: dict[str, Any] = Field(default_factory=dict)
    force_retry: bool = False


class SocialPublishResponse(BaseModel):
    job_id: uuid.UUID
    platform: str
    state: str
    external_id: str | None = None
    external_url: str | None = None
    response: dict[str, Any] | None = None
