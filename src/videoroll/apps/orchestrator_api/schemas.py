from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from videoroll.db.models import SourceLicense, SourceType, TaskStatus


class TaskCreate(BaseModel):
    source_type: SourceType
    source_url: Optional[str] = None
    source_license: SourceLicense = SourceLicense.own
    source_proof_url: Optional[str] = None
    priority: int = 0
    created_by: Optional[str] = None


class BilibiliUploadProgressRead(BaseModel):
    job_id: uuid.UUID
    progress: int = Field(ge=0, le=100)


class TaskRead(BaseModel):
    id: uuid.UUID
    source_type: SourceType
    source_url: Optional[str]
    source_license: SourceLicense
    source_proof_url: Optional[str]
    status: TaskStatus
    stopped_status: Optional[TaskStatus] = None
    priority: int
    created_by: Optional[str]
    display_title: Optional[str] = None
    error_code: Optional[str]
    error_message: Optional[str]
    retry_count: int
    bilibili_upload: Optional[BilibiliUploadProgressRead] = None
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


class AdminAuthStatusRead(BaseModel):
    password_set: bool
    trusted: bool


class AdminAuthSetupRequest(BaseModel):
    password: str


class AdminAuthLoginRequest(BaseModel):
    password: str


class SubtitleActionRequest(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["srt"])
    resume: bool = False
    burn_in: bool = False
    soft_sub: bool = False
    ass_style: str = "clean_white"
    video_codec: str = "av1"
    use_intel_gpu: bool = False
    video_preset: Optional[str] = None
    video_crf: Optional[int] = Field(default=None, ge=0, le=63)

    asr_engine: str = "auto"
    asr_language: str = "auto"
    asr_model: Optional[str] = None

    prefer_youtube_subtitles: bool = True
    youtube_subtitle_mode: Literal["off", "target", "auto_source"] = "target"
    translate_enabled: bool = False
    translate_provider: str = "mock"
    target_lang: str = "zh"
    translate_style: str = "口语自然"
    translate_batch_size: Optional[int] = None
    translate_enable_summary: Optional[bool] = None
    bilingual: bool = False
    auto_publish: bool = False
    publish_payload: Optional[dict[str, Any]] = None


class PublishActionRequest(BaseModel):
    platform: str = "bilibili"
    account_id: Optional[str] = None
    video_key: Optional[str] = None
    cover_key: Optional[str] = None
    typeid_mode: Optional[str] = None
    meta: Optional[dict[str, Any]] = None
    platform_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    skip_review: bool = False
    force_retry: bool = False


class PublishAllRequest(BaseModel):
    """Validated multi-platform publish input.

    ``meta`` remains the shared/Bilibili fallback.  ``platform_meta`` takes
    precedence for an individual platform and is normalized before dispatch.
    """

    account_id: Optional[str] = None
    account_ids: dict[str, str] = Field(default_factory=dict)
    video_key: Optional[str] = None
    cover_key: Optional[str] = None
    typeid_mode: Optional[str] = None
    meta: Optional[dict[str, Any]] = None
    platform_meta: dict[str, dict[str, Any]] = Field(default_factory=dict)
    platform_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    skip_review: bool = False
    force_retry: bool = False


class PublishPlatformSettingsRead(BaseModel):
    platforms: dict[str, bool] = Field(default_factory=dict)


class PublishAllResultResponse(BaseModel):
    batch_id: Optional[uuid.UUID] = None
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    all_accepted: bool = False
    has_any_accepted: bool = False
    all_published: bool = False
    all_succeeded: bool = False
    platform_count: int = 0
    ok_count: int = 0
    error_count: int = 0
    errors: dict[str, str] = Field(default_factory=dict)


class PublishPlatformSettingUpdate(BaseModel):
    enabled: bool


class PublishMetaDraftRequest(BaseModel):
    mode: Literal["auto", "default", "source"] = "auto"
    meta: Optional[dict[str, Any]] = None


class PublishMetaDraftResponse(BaseModel):
    meta: dict[str, Any] = Field(default_factory=dict)


class PublishReviewActionRequest(BaseModel):
    meta: Optional[dict[str, Any]] = None


class PublishReviewSettingsRead(BaseModel):
    enabled: bool = True
    blocked_words: list[str] = Field(default_factory=list)
    ai_rules: str = ""


class PublishReviewSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    blocked_words: Optional[list[str]] = None
    ai_rules: Optional[str] = None


class TaskPublishReviewRead(BaseModel):
    enabled: bool = True
    checked: bool = False
    ok: Optional[bool] = None
    reason: Optional[str] = None
    matched_blocked_words: list[str] = Field(default_factory=list)
    review_mode: Optional[str] = None
    risk_tags: list[str] = Field(default_factory=list)
    title: Optional[str] = None
    summary: Optional[str] = None
    subtitle_chars: int = 0
    checked_at: Optional[str] = None


class RemoteJobResponse(BaseModel):
    job_id: uuid.UUID
    status: str


class TaskBulkControlResponse(BaseModel):
    matched_count: int
    changed_count: int


class RecentFailedResumeItem(BaseModel):
    task_id: uuid.UUID
    job_id: Optional[uuid.UUID] = None
    status: str
    detail: Optional[str] = None


class RecentFailedResumeResponse(BaseModel):
    window_hours: int
    matched_count: int
    resumed_count: int
    skipped_count: int
    failed_count: int
    results: list[RecentFailedResumeItem] = Field(default_factory=list)


class RemotePublishResponse(BaseModel):
    state: str
    platform: str = "bilibili"
    job_id: Optional[uuid.UUID] = None
    aid: Optional[str] = None
    bvid: Optional[str] = None
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    response: Optional[dict[str, Any]] = None


class SystemCPURead(BaseModel):
    percent: Optional[float] = None
    cores: int = 0
    load_average: Optional[list[float]] = None


class SystemMemoryRead(BaseModel):
    total_bytes: int = 0
    used_bytes: int = 0
    available_bytes: int = 0
    percent: Optional[float] = None


class SystemIntelGPUEngineRead(BaseModel):
    name: str
    percent: Optional[float] = None


class SystemIntelGPURead(BaseModel):
    enabled: bool = False
    checked: bool = False
    available: bool = False
    render_device: str = ""
    model_name: Optional[str] = None
    driver: Optional[str] = None
    pci_slot: Optional[str] = None
    pci_id: Optional[str] = None
    usage_supported: bool = False
    usage_percent: Optional[float] = None
    engines: list[SystemIntelGPUEngineRead] = Field(default_factory=list)
    detail: str = ""


class SystemResourcesRead(BaseModel):
    sampled_at: str
    cpu: SystemCPURead
    memory: SystemMemoryRead
    cgroup_memory: Optional[SystemMemoryRead] = None
    intel_gpu: Optional[SystemIntelGPURead] = None


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
    batch_id: Optional[uuid.UUID] = None
    platform: str = "bilibili"
    state: str
    aid: Optional[str]
    bvid: Optional[str]
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    account_id: Optional[uuid.UUID] = None
    upload_progress: int = 0
    upload_active: bool = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    tid: Optional[int] = None
    typeid_mode: Optional[str] = None
    typeid_selected_by: Optional[str] = None
    typeid_ai_ok: Optional[bool] = None
    typeid_ai_reason: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PublishBatchSummary(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    state: str
    expected_targets: list[dict[str, Any]] = Field(default_factory=list)
    outcomes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cleanup_enqueued_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
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
    metadata_asset: Optional[AssetRead] = None


class YouTubeDownloadActionResponse(BaseModel):
    metadata: YouTubeMetaRead
    metadata_asset: AssetRead
    video_asset: AssetRead
    cover_asset: Optional[AssetRead] = None


class AutoYouTubeRequest(BaseModel):
    url: str
    license: SourceLicense = SourceLicense.authorized
    proof_url: Optional[str] = None
    auto_publish: Optional[bool] = None


class RemoteAutoYouTubeRequest(BaseModel):
    """JSON contract for the public, token-authenticated YouTube intake."""

    url: str
    license: SourceLicense = SourceLicense.authorized
    proof_url: Optional[str] = None
    auto_publish: Optional[bool] = None


class AutoYouTubeResponse(BaseModel):
    task_id: uuid.UUID
    pipeline_job_id: Optional[str] = None
    deduped: bool = False
    source_id: Optional[str] = None


class AutoYouTubeTaskStartResponse(BaseModel):
    task_id: uuid.UUID
    pipeline_job_id: str


class ConvertedVideoItem(BaseModel):
    task: TaskRead
    final_asset: AssetRead
    cover_asset: Optional[AssetRead] = None
    display_title: Optional[str] = None


class StorageRetentionSettingsRead(BaseModel):
    asset_ttl_days: int = 0


class StorageRetentionSettingsUpdate(BaseModel):
    asset_ttl_days: Optional[int] = None


class StorageResourceCleanupRead(BaseModel):
    matched_tasks: int = 0
    matched_assets: int = 0
    matched_subtitles: int = 0
    deleted_assets: int = 0
    deleted_subtitles: int = 0
    deleted_objects: int = 0
    pending_objects: int = 0


class LiveStreamSettingsRead(BaseModel):
    rtmp_url: str = ""
    stream_key_set: bool = False
    video_bitrate_kbps: int = Field(default=4500, ge=500, le=20000)
    audio_bitrate_kbps: int = Field(default=160, ge=32, le=512)
    fps: int = Field(default=30, ge=15, le=60)
    keyframe_interval_seconds: int = Field(default=2, ge=1, le=10)


class LiveStreamSettingsUpdate(BaseModel):
    rtmp_url: Optional[str] = None
    # Never returned by the API. An empty string deliberately clears it.
    stream_key: Optional[str] = Field(default=None, max_length=1024)
    video_bitrate_kbps: Optional[int] = Field(default=None, ge=500, le=20000)
    audio_bitrate_kbps: Optional[int] = Field(default=None, ge=32, le=512)
    fps: Optional[int] = Field(default=None, ge=15, le=60)
    keyframe_interval_seconds: Optional[int] = Field(default=None, ge=1, le=10)


class LivePlaylistItem(BaseModel):
    source: Literal["library", "task_asset"]
    id: uuid.UUID


class LivePlaylistRead(BaseModel):
    video_items: list[LivePlaylistItem] = Field(default_factory=list)
    audio_items: list[LivePlaylistItem] = Field(default_factory=list)
    playback_mode: Literal["sequential", "shuffle"] = "sequential"
    loop_playlist: bool = True


class LivePlaylistUpdate(BaseModel):
    video_items: Optional[list[LivePlaylistItem]] = None
    audio_items: Optional[list[LivePlaylistItem]] = None
    playback_mode: Optional[Literal["sequential", "shuffle"]] = None
    loop_playlist: Optional[bool] = None


class LiveMediaRead(BaseModel):
    id: uuid.UUID
    media_type: Literal["video", "audio"]
    origin: Literal["upload", "completed_video"] = "upload"
    source_task_id: Optional[uuid.UUID] = None
    source_asset_id: Optional[uuid.UUID] = None
    display_name: str
    storage_key: str
    content_type: str
    size_bytes: int = 0
    sha256: Optional[str] = None
    created_at: Optional[str] = None


class CompletedLiveVideoRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    display_name: str
    storage_key: str
    size_bytes: Optional[int] = None
    duration_ms: Optional[int] = None
    created_at: datetime


class LiveCurrentMediaRead(BaseModel):
    source: Literal["library", "task_asset"]
    id: uuid.UUID
    display_name: str


class LiveSessionRead(BaseModel):
    status: Literal["idle", "starting", "running", "paused", "stopped", "failed"] = "idle"
    session_id: Optional[str] = None
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    stopped_at: Optional[str] = None
    current_video: Optional[LiveCurrentMediaRead] = None
    current_audio: Optional[LiveCurrentMediaRead] = None
    last_error: Optional[str] = None


class LiveDashboardRead(BaseModel):
    settings: LiveStreamSettingsRead
    session: LiveSessionRead
    playlist: LivePlaylistRead
    library_media: list[LiveMediaRead] = Field(default_factory=list)
    completed_videos: list[CompletedLiveVideoRead] = Field(default_factory=list)


class RemoteAPISettingsRead(BaseModel):
    token_set: bool = False
    token_updated_at: Optional[str] = None
    endpoint_path: str = "/remote/auto/youtube"
    http_method: str = "POST"
    authorization_header: str = "Authorization"
    idempotency_header: str = "Idempotency-Key"


class RemoteAPISettingsUpdate(BaseModel):
    token: Optional[str] = None


class YouTubeSettingsRead(BaseModel):
    proxy: str = ""
    cookies_set: bool = False
    cookies_enabled: bool = False
    cookies_updated_at: Optional[str] = None
    cookies_count: int = 0
    cookies_domains_count: int = 0
    cookies_has_auth: bool = False
    cookies_has_bot_check_bypass: bool = False
    cookies_has_visitor_info: bool = False
    cookie_file_configured: bool = False
    cookie_file_exists: bool = False
    home_scan_enabled: bool = False
    home_scan_interval_minutes: int = 60
    home_scan_limit: int = 10
    home_scan_long_videos_only: bool = False
    home_scan_min_duration_seconds: int = 180
    home_scan_running: bool = False
    home_scan_last_started_at: Optional[str] = None
    home_scan_last_finished_at: Optional[str] = None
    home_scan_last_discovered_count: int = 0
    home_scan_last_started_count: int = 0
    home_scan_last_skipped_duplicates: int = 0
    home_scan_last_failed_count: int = 0
    home_scan_last_candidate_count: int = 0
    home_scan_last_explicit_shorts_count: int = 0
    home_scan_last_known_duration_count: int = 0
    home_scan_last_unknown_duration_count: int = 0
    home_scan_last_below_min_duration_count: int = 0
    home_scan_last_kept_unknown_duration_count: int = 0
    home_scan_last_eligible_count: int = 0
    home_scan_last_log_lines: list[str] = Field(default_factory=list)
    home_scan_last_error: Optional[str] = None
    home_scan_last_sample_urls: list[str] = Field(default_factory=list)


class YouTubeSettingsUpdate(BaseModel):
    proxy: Optional[str] = None
    cookies_txt: Optional[str] = None
    cookies_enabled: Optional[bool] = None
    home_scan_enabled: Optional[bool] = None
    home_scan_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    home_scan_limit: Optional[int] = Field(default=None, ge=1, le=100)
    home_scan_long_videos_only: Optional[bool] = None
    home_scan_min_duration_seconds: Optional[int] = Field(default=None, ge=0, le=86400)


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


class YouTubeHomeScanRunResponse(BaseModel):
    discovered_count: int
    created_task_ids: list[uuid.UUID]
    skipped_duplicates: int
    failed_count: int = 0
    candidate_count: int = 0
    explicit_shorts_count: int = 0
    known_duration_count: int = 0
    unknown_duration_count: int = 0
    below_min_duration_count: int = 0
    kept_unknown_duration_count: int = 0
    eligible_count: int = 0
    min_duration_seconds: int = 0
    log_lines: list[str] = Field(default_factory=list)
    started_pipeline_job_ids: list[str] = Field(default_factory=list)
    sample_urls: list[str] = Field(default_factory=list)


class WorkdirMaintenanceEntryRead(BaseModel):
    kind: Literal["subtitle", "render", "youtube"]
    owner_id: str
    rel_path: str
    size_bytes: int = 0
    modified_at: datetime
    reclaimable: bool = False
    reason: str = ""
    task_id: Optional[uuid.UUID] = None


class WorkdirMaintenanceRead(BaseModel):
    work_dir: str
    scanned_dirs: int = 0
    reclaimable_dirs: int = 0
    total_bytes: int = 0
    reclaimable_bytes: int = 0
    deleted_dirs: int = 0
    deleted_bytes: int = 0
    deleted_paths: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    entries: list[WorkdirMaintenanceEntryRead] = Field(default_factory=list)
