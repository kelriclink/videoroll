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


class OutputOptions(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["srt"])
    render: RenderOptions = Field(default_factory=RenderOptions)


class SubtitleJobCreate(BaseModel):
    task_id: uuid.UUID
    input: InputRef
    asr: ASROptions = Field(default_factory=ASROptions)
    translate: TranslateOptions = Field(default_factory=TranslateOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)
    output_prefix: str = ""


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
    faster_whisper_installed: bool = False


class ASRDefaultsRead(BaseModel):
    default_engine: str
    default_language: str
    default_model: str
    model_download_proxy: str = ""


class ASRDefaultsUpdate(BaseModel):
    default_engine: Optional[str] = None
    default_language: Optional[str] = None
    default_model: Optional[str] = None
    model_download_proxy: Optional[str] = None


class SubtitleAutoProfileRead(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["srt", "ass"])
    burn_in: bool = True
    soft_sub: bool = False
    ass_style: str = "clean_white"

    asr_engine: str = "auto"
    asr_language: str = "auto"
    asr_model: Optional[str] = None

    translate_enabled: bool = True
    translate_provider: str = "openai"
    target_lang: str = "zh"
    translate_style: str = "口语自然"
    translate_enable_summary: bool = True
    bilingual: bool = False

    auto_publish: bool = True
    publish_title_prefix: str = "【熟肉】"
    publish_translate_title: bool = True
    publish_use_youtube_cover: bool = True


class SubtitleAutoProfileUpdate(BaseModel):
    formats: Optional[list[str]] = None
    burn_in: Optional[bool] = None
    soft_sub: Optional[bool] = None
    ass_style: Optional[str] = None

    asr_engine: Optional[str] = None
    asr_language: Optional[str] = None
    asr_model: Optional[str] = None

    translate_enabled: Optional[bool] = None
    translate_provider: Optional[str] = None
    target_lang: Optional[str] = None
    translate_style: Optional[str] = None
    translate_enable_summary: Optional[bool] = None
    bilingual: Optional[bool] = None

    auto_publish: Optional[bool] = None
    publish_title_prefix: Optional[str] = None
    publish_translate_title: Optional[bool] = None
    publish_use_youtube_cover: Optional[bool] = None


class TranslateSettingsRead(BaseModel):
    default_provider: str
    default_target_lang: str
    default_style: str
    default_batch_size: int
    default_enable_summary: bool

    openai_api_key_set: bool
    openai_base_url: str
    openai_model: str
    openai_temperature: float
    openai_timeout_seconds: float


class TranslateSettingsUpdate(BaseModel):
    default_provider: Optional[str] = None
    default_target_lang: Optional[str] = None
    default_style: Optional[str] = None
    default_batch_size: Optional[int] = None
    default_enable_summary: Optional[bool] = None

    # Secrets are never returned from the API. If set to "" it clears the stored key.
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: Optional[str] = None
    openai_temperature: Optional[float] = None
    openai_timeout_seconds: Optional[float] = None


class TranslateTestRequest(BaseModel):
    text: str = "Hello world."
    target_lang: str = "zh"
    style: str = "口语自然"


class TranslateTestResponse(BaseModel):
    translated_text: str


class WhisperModelInfo(BaseModel):
    name: str
    path: str
    size_bytes: Optional[int] = None


class WhisperModelDownloadRequest(BaseModel):
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
