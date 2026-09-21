from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

class WorkerRegisterRequest(BaseModel):
    worker_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=32)
    architecture: str | None = Field(default=None, max_length=32)
    version: str = Field(default="", max_length=64)
    protocol_version: int = Field(default=1, ge=1, le=100)
    render_spec_versions: list[int] = Field(default_factory=lambda: [1], min_length=1, max_length=16)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int = Field(default=1, ge=1, le=32)

class WorkerEnrollRequest(WorkerRegisterRequest):
    enrollment_token: str = Field(min_length=32, max_length=256)

class LocalWorkerEnrollRequest(WorkerRegisterRequest):
    pass

class EnrollmentCreateRequest(BaseModel):
    label: str = Field(default="", max_length=128)
    ttl_minutes: int = Field(default=30, ge=5, le=1440)

class EnrollmentCreateResponse(BaseModel):
    id: uuid.UUID
    token: str
    server_url: str
    expires_at: datetime

class RenderConnectionUpdate(BaseModel):
    server_url: str = Field(min_length=1, max_length=512)

class EnrollmentAdminRead(BaseModel):
    id: uuid.UUID
    label: str
    status: str
    expires_at: datetime
    consumed_at: datetime | None = None
    worker_id: uuid.UUID | None = None
    created_at: datetime
    model_config = {"from_attributes": True}

class WorkerHeartbeatRequest(BaseModel):
    status: Literal["online", "busy", "paused", "draining"] = "online"
    resources: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] | None = None
    active_execution_ids: list[uuid.UUID] = Field(default_factory=list, max_length=64)

class WorkerRead(BaseModel):
    id: uuid.UUID
    worker_key: str
    name: str
    platform: str
    architecture: str | None = None
    version: str
    protocol_version: int
    render_spec_versions: list[int]
    capabilities: dict[str, Any]
    resources: dict[str, Any]
    labels: dict[str, Any]
    status: str
    enabled: bool
    draining: bool
    max_concurrency: int
    active_jobs: int
    last_seen_at: datetime
    model_config = {"from_attributes": True}

class WorkerEnrollResponse(BaseModel):
    worker: WorkerRead
    credential: str

class WorkerAdminRead(WorkerRead):
    stale: bool = False
    seconds_since_heartbeat: int = 0
    credential_active: bool = False

class WorkerControlRequest(BaseModel):
    enabled: bool | None = None
    draining: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=32)

class ClaimRequest(BaseModel):
    available_slots: int = Field(default=1, ge=0, le=32)
    accepted_transfer_modes: list[Literal["http", "mapped"]] = Field(default_factory=lambda: ["http"])
    available_encoders: list[str] = Field(default_factory=list, max_length=128)

class ArtifactSpec(BaseModel):
    role: str
    storage_key: str
    size_bytes: int | None = None
    sha256: str | None = None
    download_url: str

class RenderSpec(BaseModel):
    schema_version: int = 1
    render_job_id: uuid.UUID
    task_id: uuid.UUID
    mode: Literal["burn_in", "soft_sub", "noop"]
    request: dict[str, Any]
    artifacts: list[ArtifactSpec] = Field(default_factory=list)

class ExecutionRead(BaseModel):
    id: uuid.UUID
    render_job_id: uuid.UUID
    worker_id: uuid.UUID
    attempt: int
    fence_token: str
    state: str
    transfer_mode: str
    progress: int
    lease_until: datetime | None = None
    render_spec: dict[str, Any]
    model_config = {"from_attributes": True}

class ExecutionAdminRead(BaseModel):
    id: uuid.UUID
    render_job_id: uuid.UUID
    worker_id: uuid.UUID
    attempt: int
    state: str
    transfer_mode: str
    progress: int
    lease_until: datetime | None = None
    render_spec: dict[str, Any]
    worker_name: str | None = None
    task_id: uuid.UUID | None = None
    job_status: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_tail: str | None = None
    error_message: str | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime
    finished_at: datetime | None = None
    model_config = {"from_attributes": True}

class ExecutionAdminActionRequest(BaseModel):
    reason: str = Field(default="canceled by coordinator", min_length=1, max_length=1024)

class ClaimResponse(BaseModel):
    execution: ExecutionRead | None = None
    render_spec: RenderSpec | None = None
    retry_after_seconds: int = 5

class ExecutionHeartbeatRequest(BaseModel):
    fence_token: str = Field(min_length=16, max_length=64)
    progress: int | None = Field(default=None, ge=0, le=100)
    metrics: dict[str, Any] = Field(default_factory=dict)

class ExecutionProgressRequest(ExecutionHeartbeatRequest):
    stage: str | None = Field(default=None, max_length=64)

class ExecutionLogRequest(BaseModel):
    fence_token: str = Field(min_length=16, max_length=64)
    text: str = Field(max_length=65536)

class ExecutionCompleteRequest(BaseModel):
    fence_token: str = Field(min_length=16, max_length=64)
    output_asset_id: uuid.UUID | None = None
    output: dict[str, Any] = Field(default_factory=dict)

class ExecutionFailRequest(BaseModel):
    fence_token: str = Field(min_length=16, max_length=64)
    error: str = Field(min_length=1, max_length=8192)
    retryable: bool = True

class ExecutionCancelRead(BaseModel):
    cancel_requested: bool
    reason: str | None = None
