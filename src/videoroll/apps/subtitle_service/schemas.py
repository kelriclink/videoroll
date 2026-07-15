from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class InputRef(BaseModel):
    type: Literal["s3"]
    key: str


class ASROptions(BaseModel):
    engine: str = "auto"
    language: str = "auto"
    model: Optional[str] = None


class TranslateOptions(BaseModel):
    enabled: bool = False
    target_lang: str = "zh"
    provider: str = "mock"
    style: str = "口语自然"
    batch_size: Optional[int] = None
    enable_summary: Optional[bool] = None
    glossary_id: Optional[str] = None
    bilingual: bool = False


class RenderOptions(BaseModel):
    burn_in: bool = False
    soft_sub: bool = False
    ass_style: str = "clean_white"
    video_codec: str = "av1"
    use_intel_gpu: bool = False
    # Optional encoder preset (codec-dependent). If omitted, codec-specific defaults are used.
    video_preset: Optional[str] = None
    # Optional encoder quality control. If omitted, codec-specific defaults are used.
    video_crf: Optional[int] = Field(default=None, ge=0, le=63)
    primary_font_scale_percent: int = Field(default=100, ge=25, le=300)
    secondary_font_scale_percent: int = Field(default=100, ge=25, le=300)


class OutputOptions(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["srt"])
    render: RenderOptions = Field(default_factory=RenderOptions)


class SubtitleJobCreate(BaseModel):
    task_id: uuid.UUID
    resume: bool = False
    prefer_youtube_subtitles: bool = True
    youtube_subtitle_mode: Literal["off", "target", "auto_source"] = "target"
    input: InputRef
    asr: ASROptions = Field(default_factory=ASROptions)
    translate: TranslateOptions = Field(default_factory=TranslateOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)
    output_prefix: str = ""
    after_render: Optional[dict[str, Any]] = None


class SubtitleJobRead(BaseModel):
    job_id: uuid.UUID
    task_id: uuid.UUID
    status: str
    progress: int
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    logs_key: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WhisperSettingsRead(BaseModel):
    asr_engine: str
    whisper_model: str
    whisper_model_dir: str
    whisper_device: str
    whisper_compute_type: str
    openvino_model: str
    openvino_device: str
    openvino_num_beams: int = 1
    openvino_max_new_tokens: int = 448
    whisper_cpu_threads: int = 0
    whisper_num_workers: int = 1
    whisper_cpu_threads_effective: int = 0
    whisper_num_workers_effective: int = 1
    faster_whisper_installed: bool = False
    openvino_installed: bool = False


class IntelHardwareProbeRead(BaseModel):
    checked: bool = True
    available: bool = False
    render_device: str
    model_name: Optional[str] = None
    driver: Optional[str] = None
    pci_slot: Optional[str] = None
    pci_id: Optional[str] = None
    detail: str = ""


class ASRDefaultsRead(BaseModel):
    default_engine: str
    default_language: str
    default_model: str
    openvino_device: str = "GPU"
    openvino_num_beams: int = 1
    openvino_max_new_tokens: int = 448
    model_download_proxy: str = ""


class ASRDefaultsUpdate(BaseModel):
    default_engine: Optional[str] = None
    default_language: Optional[str] = None
    default_model: Optional[str] = None
    openvino_device: Optional[str] = None
    openvino_num_beams: Optional[int] = Field(default=None, ge=1, le=16)
    openvino_max_new_tokens: Optional[int] = Field(default=None, ge=1, le=4096)
    model_download_proxy: Optional[str] = None


class SubtitleAutoProfileRead(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["srt", "ass"])
    burn_in: bool = True
    soft_sub: bool = False
    ass_style: str = "clean_white"
    video_codec: str = "av1"
    use_intel_gpu: bool = False
    video_preset: Optional[str] = None
    video_crf: Optional[int] = None
    primary_font_scale_percent: int = Field(default=100, ge=25, le=300)
    secondary_font_scale_percent: int = Field(default=100, ge=25, le=300)

    asr_engine: str = "auto"
    asr_language: str = "auto"
    asr_model: Optional[str] = None

    prefer_youtube_subtitles: bool = True
    youtube_subtitle_mode: Literal["off", "target", "auto_source"] = "target"
    translate_enabled: bool = True
    translate_provider: str = "openai"
    target_lang: str = "zh"
    translate_style: str = "口语自然"
    translate_enable_summary: bool = True
    bilingual: bool = False

    auto_publish: bool = True
    publish_typeid_mode: str = "ai_summary"
    publish_title_prefix: str = "【熟肉】"
    publish_translate_title: bool = True
    publish_use_youtube_cover: bool = True
    publish_enable_reprint: bool = True


class SubtitleAutoProfileUpdate(BaseModel):
    formats: Optional[list[str]] = None
    burn_in: Optional[bool] = None
    soft_sub: Optional[bool] = None
    ass_style: Optional[str] = None
    video_codec: Optional[str] = None
    use_intel_gpu: Optional[bool] = None
    video_preset: Optional[str] = None
    video_crf: Optional[int] = Field(default=None, ge=0, le=63)
    primary_font_scale_percent: Optional[int] = Field(default=None, ge=25, le=300)
    secondary_font_scale_percent: Optional[int] = Field(default=None, ge=25, le=300)

    asr_engine: Optional[str] = None
    asr_language: Optional[str] = None
    asr_model: Optional[str] = None

    prefer_youtube_subtitles: Optional[bool] = None
    youtube_subtitle_mode: Optional[Literal["off", "target", "auto_source"]] = None
    translate_enabled: Optional[bool] = None
    translate_provider: Optional[str] = None
    target_lang: Optional[str] = None
    translate_style: Optional[str] = None
    translate_enable_summary: Optional[bool] = None
    bilingual: Optional[bool] = None

    auto_publish: Optional[bool] = None
    publish_typeid_mode: Optional[str] = None
    publish_title_prefix: Optional[str] = None
    publish_translate_title: Optional[bool] = None
    publish_use_youtube_cover: Optional[bool] = None
    publish_enable_reprint: Optional[bool] = None


class TranslateSettingsRead(BaseModel):
    default_provider: str
    default_target_lang: str
    default_style: str
    default_batch_size: int
    default_max_retries: int
    default_enable_summary: bool

    openai_api_key_set: bool
    openai_base_url: str
    openai_model: str
    openai_temperature: float
    openai_timeout_seconds: float
    openai_max_retries: int = 3

    rag_enabled: bool = False
    rag_top_k: int = 8
    rag_min_score: float = 0.68
    rag_embedding_provider: str = "openai"
    rag_embedding_model: str = "text-embedding-3-small"
    rag_embedding_dimensions: int = 1536
    rag_embedding_model_dir: str = "/models/embeddings"
    rag_embedding_device: str = "cpu"
    rag_embedding_api_key_set: bool = False
    rag_embedding_base_url: str = ""
    rag_embedding_timeout_seconds: float = 60.0
    rag_auto_discover_terms: bool = False
    rag_auto_learn_terms: bool = False
    rag_dictionary_enabled: bool = True
    rag_dictionary_top_k: int = 8
    rag_dictionary_min_quality: float = 0.0
    rag_dictionary_auto_promote: bool = False
    rag_wiki_enabled: bool = False
    rag_search_enabled: bool = False
    rag_search_url: str = ""
    rag_search_categories: str = "general"
    rag_search_engines: str = ""
    rag_search_fallback_engines: str = "bing,baidu"
    rag_search_language: str = "all"
    rag_search_safesearch: int = 0
    rag_search_time_range: str = ""
    rag_search_pageno: int = 1
    rag_domain: str = ""
    rag_agent_parallelism: int = 1
    rag_agent_timeout_seconds: float = 120.0
    rag_agent_skills_enabled: bool = False
    rag_agent_builtin_skills_enabled: bool = True
    rag_agent_user_skills_enabled: bool = True


class TranslateSettingsUpdate(BaseModel):
    default_provider: Optional[str] = None
    default_target_lang: Optional[str] = None
    default_style: Optional[str] = None
    default_batch_size: Optional[int] = None
    default_max_retries: Optional[int] = None
    default_enable_summary: Optional[bool] = None

    # Secrets are never returned from the API. If set to "" it clears the stored key.
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: Optional[str] = None
    openai_temperature: Optional[float] = None
    openai_timeout_seconds: Optional[float] = None
    openai_max_retries: Optional[int] = Field(default=None, ge=1, le=10)

    rag_enabled: Optional[bool] = None
    rag_top_k: Optional[int] = Field(default=None, ge=0, le=30)
    rag_min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rag_embedding_provider: Optional[str] = None
    rag_embedding_model: Optional[str] = None
    rag_embedding_dimensions: Optional[int] = Field(default=None, ge=1, le=4096)
    rag_embedding_model_dir: Optional[str] = None
    rag_embedding_device: Optional[str] = None
    rag_embedding_api_key: Optional[str] = None
    rag_embedding_base_url: Optional[str] = None
    rag_embedding_timeout_seconds: Optional[float] = None
    rag_auto_discover_terms: Optional[bool] = None
    rag_auto_learn_terms: Optional[bool] = None
    rag_dictionary_enabled: Optional[bool] = None
    rag_dictionary_top_k: Optional[int] = Field(default=None, ge=0, le=30)
    rag_dictionary_min_quality: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rag_dictionary_auto_promote: Optional[bool] = None
    rag_wiki_enabled: Optional[bool] = None
    rag_search_enabled: Optional[bool] = None
    rag_search_url: Optional[str] = None
    rag_search_categories: Optional[str] = None
    rag_search_engines: Optional[str] = None
    rag_search_fallback_engines: Optional[str] = None
    rag_search_language: Optional[str] = None
    rag_search_safesearch: Optional[int] = Field(default=None, ge=0, le=2)
    rag_search_time_range: Optional[str] = None
    rag_search_pageno: Optional[int] = Field(default=None, ge=1, le=100)
    rag_domain: Optional[str] = None
    rag_agent_parallelism: Optional[int] = Field(default=None, ge=1, le=8)
    rag_agent_timeout_seconds: Optional[float] = Field(default=None, ge=10.0, le=900.0)
    rag_agent_skills_enabled: Optional[bool] = None
    rag_agent_builtin_skills_enabled: Optional[bool] = None
    rag_agent_user_skills_enabled: Optional[bool] = None


class TranslateTestRequest(BaseModel):
    text: str = "Hello world."
    target_lang: str = "zh"
    style: str = "口语自然"


class TranslateTestResponse(BaseModel):
    translated_text: str


class KnowledgeItemRead(BaseModel):
    id: uuid.UUID
    item_type: str
    term: str = ""
    translation: str = ""
    target_lang: str = "zh"
    domain: str = ""
    aliases: list[Any] = Field(default_factory=list)
    title: str = ""
    content: str = ""
    description: str = ""
    sources: list[Any] = Field(default_factory=list)
    confidence: float = 0.0
    status: str = "approved"
    created_by: str = "manual"
    usage_count: int = 0
    embedding_model: str = ""
    last_verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KnowledgeItemUpsertRequest(BaseModel):
    item_type: Literal["term", "document"] = "term"
    target_lang: str = "zh"
    term: str = ""
    translation: str = ""
    domain: str = ""
    aliases: list[str] = Field(default_factory=list)
    title: str = ""
    content: str = ""
    description: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: str = "approved"
    created_by: str = "manual"


class KnowledgeItemUpsertResponse(BaseModel):
    id: uuid.UUID


class KnowledgeEmbeddingRebuildRequest(BaseModel):
    item_type: Optional[Literal["term", "document"]] = None
    status: Optional[str] = None
    limit: int = Field(default=1000, ge=1, le=10000)


class KnowledgeEmbeddingRebuildResponse(BaseModel):
    total: int
    updated: int
    failed: int
    skipped: int
    embedding_model: str
    dimensions: int
    errors: list[dict[str, str]] = Field(default_factory=list)


class DictionarySourceRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str = ""
    source_lang: str = ""
    target_lang: str = "zh"
    format: str = "csv"
    license: str = ""
    license_url: str = ""
    source_url: str = ""
    version: str = ""
    attribution: str = ""
    domain: str = ""
    priority: int = 0
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    entry_count: int = 0
    created_at: datetime
    updated_at: datetime


class DictionarySourceUpdate(BaseModel):
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=-1000, le=1000)
    domain: Optional[str] = None
    description: Optional[str] = None


class DictionaryEntryRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    source_name: str = ""
    source_slug: str = ""
    source_lang: str = ""
    target_lang: str = "zh"
    term: str
    normalized_term: str = ""
    translations: list[str] = Field(default_factory=list)
    translation: str = ""
    translation_text: str = ""
    pos: str = ""
    definition: str = ""
    domain: str = ""
    tags: list[Any] = Field(default_factory=list)
    aliases: list[Any] = Field(default_factory=list)
    examples: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    quality: float = 0.0
    enabled: bool = True
    usage_count: int = 0
    license: str = ""
    license_url: str = ""
    source_url: str = ""
    attribution: str = ""
    created_at: datetime
    updated_at: datetime


class DictionaryEntryUpdate(BaseModel):
    enabled: bool


class DictionaryImportResponse(BaseModel):
    source_id: uuid.UUID
    batch_id: uuid.UUID
    status: str
    parsed: int = 0
    upserted: int = 0
    skipped: int = 0
    max_entries: int = 0
    full_import: bool = False
    sha256: str = ""


class DictionaryLookupRequest(BaseModel):
    term: str
    source_lang: str = ""
    target_lang: str = "zh"
    domain: str = ""
    exact: bool = True
    min_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=8, ge=1, le=50)


class DictionaryLookupResponse(BaseModel):
    count: int
    results: list[DictionaryEntryRead] = Field(default_factory=list)


class DictionaryPromoteRequest(BaseModel):
    entry_id: uuid.UUID
    status: str = "approved"
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class DictionaryPromoteResponse(BaseModel):
    knowledge_item_id: uuid.UUID


class AgentRunRead(BaseModel):
    id: uuid.UUID
    agent_type: str
    status: str
    term: str = ""
    domain: str = ""
    target_lang: str = "zh"
    task_id: Optional[uuid.UUID] = None
    subtitle_job_id: Optional[uuid.UUID] = None
    query: str = ""
    steps: list[Any] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    knowledge_item_id: Optional[uuid.UUID] = None
    parent_agent_run_id: Optional[uuid.UUID] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AgentSkillRead(BaseModel):
    name: str
    description: str = ""
    domain: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    runnable: bool = True
    run_mode: str = "agent_guidance"
    source: str = "user"
    path: str = ""
    resource_count: int = 0


class EmbeddingModelInfo(BaseModel):
    name: str
    path: str
    size_bytes: Optional[int] = None


class EmbeddingModelListRequest(BaseModel):
    model_dir: Optional[str] = None


class EmbeddingModelDownloadRequest(BaseModel):
    model: str = "BAAI/bge-small-zh-v1.5"
    name: Optional[str] = None
    model_dir: Optional[str] = None
    revision: Optional[str] = None
    force: bool = False


class EmbeddingTestRequest(BaseModel):
    text: str = "hello world"
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout_seconds: Optional[float] = None
    model_dir: Optional[str] = None
    dimensions: Optional[int] = None
    device: Optional[str] = None


class EmbeddingTestResponse(BaseModel):
    provider: str
    model: str
    dimensions: int
    expected_dimensions: int
    ok: bool


class WhisperModelInfo(BaseModel):
    name: str
    path: str
    size_bytes: Optional[int] = None


class WhisperModelDownloadRequest(BaseModel):
    engine: str = "faster-whisper"
    model: str
    name: Optional[str] = None
    revision: Optional[str] = None
    force: bool = False


class ModelDownloadProxyTestRequest(BaseModel):
    proxy: Optional[str] = None
    url: Optional[str] = None


class ModelDownloadProxyTestResponse(BaseModel):
    ok: bool
    url: str
    used_proxy: Optional[str] = None
    status_code: Optional[int] = None
    elapsed_ms: int
    error: Optional[str] = None


class TaskQueueSettingsRead(BaseModel):
    max_concurrency: int = Field(1, description="0=暂停调度；>0 表示最多同时运行多少个任务（Task pipeline）")
    runtime_worker_concurrency: Optional[int] = Field(default=None, description="运行中 subtitle worker 的目标并发；最小为 1")
    runtime_sync_ok: Optional[bool] = None
    runtime_sync_detail: Optional[str] = None
    runtime_sync_workers: list[dict[str, Any]] = Field(default_factory=list)


class TaskQueueSettingsUpdate(BaseModel):
    max_concurrency: Optional[int] = Field(default=None, ge=0, le=32)


class TaskQueueItemRead(BaseModel):
    task_id: uuid.UUID
    state: str
    stage: str
    subtitle_job_id: Optional[uuid.UUID] = None
    render_job_id: Optional[uuid.UUID] = None
    progress: int = 0
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class TaskQueueRead(BaseModel):
    settings: TaskQueueSettingsRead
    running_count: int = 0
    queued_count: int = 0
    tasks: list[TaskQueueItemRead] = Field(default_factory=list)


class RenderQueueSettingsRead(BaseModel):
    max_concurrency: int = Field(1, description="(legacy) use TaskQueueSettingsRead instead")


class RenderQueueSettingsUpdate(BaseModel):
    max_concurrency: Optional[int] = Field(default=None, ge=0, le=32)


class RenderJobRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    subtitle_job_id: Optional[uuid.UUID] = None
    status: str
    progress: int
    retry_count: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class RenderQueueRead(BaseModel):
    settings: RenderQueueSettingsRead
    running_count: int = 0
    queued_count: int = 0
    jobs: list[RenderJobRead] = Field(default_factory=list)
    history: list[RenderJobRead] = Field(default_factory=list, description="最近已结束（succeeded/failed/canceled）的压制任务列表（用于排查消失/失败原因）")
