from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CommonSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    redis_url: str = Field(..., alias="REDIS_URL")

    s3_endpoint_url: str = Field(..., alias="S3_ENDPOINT_URL")
    s3_access_key_id: str = Field(..., alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(..., alias="S3_SECRET_ACCESS_KEY")
    s3_bucket: str = Field(..., alias="S3_BUCKET")
    s3_region_name: str = Field("us-east-1", alias="S3_REGION_NAME")
    s3_use_ssl: bool = Field(False, alias="S3_USE_SSL")


class OrchestratorSettings(CommonSettings):
    subtitle_service_url: str = Field("http://subtitle-service:8001", alias="SUBTITLE_SERVICE_URL")
    youtube_ingest_url: str = Field("http://youtube-ingest:8002", alias="YOUTUBE_INGEST_URL")
    bilibili_publisher_url: str = Field("http://bilibili-publisher:8003", alias="BILIBILI_PUBLISHER_URL")

    # Shared runtime settings (used by orchestrator actions).
    work_dir: str = Field("/tmp/videoroll", alias="WORK_DIR")
    ffmpeg_path: str = Field("ffmpeg", alias="FFMPEG_PATH")

    # YouTube downloader (yt-dlp) settings.
    youtube_user_agent: str = Field("videoroll/0.1", alias="YOUTUBE_USER_AGENT")
    youtube_cookie_file: str | None = Field(None, alias="YOUTUBE_COOKIE_FILE")
    youtube_proxy: str | None = Field(None, alias="YOUTUBE_PROXY")
    youtube_ytdlp_format: str = Field(
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        alias="YOUTUBE_YTDLP_FORMAT",
    )
    youtube_extractor_args_json: str | None = Field(None, alias="YOUTUBE_EXTRACTOR_ARGS_JSON")


class SubtitleServiceSettings(CommonSettings):
    asr_engine: str = Field("faster-whisper", alias="SUBTITLE_ASR_ENGINE")
    whisper_model: str = Field("tiny", alias="SUBTITLE_WHISPER_MODEL")
    whisper_device: str = Field("cpu", alias="SUBTITLE_WHISPER_DEVICE")
    whisper_compute_type: str = Field("int8", alias="SUBTITLE_WHISPER_COMPUTE_TYPE")
    whisper_model_dir: str = Field("/models/whisper", alias="SUBTITLE_WHISPER_MODEL_DIR")
    ffmpeg_path: str = Field("ffmpeg", alias="FFMPEG_PATH")
    work_dir: str = Field("/tmp/videoroll", alias="WORK_DIR")

    translate_default_provider: str = Field("openai", alias="SUBTITLE_TRANSLATE_DEFAULT_PROVIDER")
    translate_default_target_lang: str = Field("zh", alias="SUBTITLE_TRANSLATE_DEFAULT_TARGET_LANG")
    translate_default_style: str = Field("口语自然", alias="SUBTITLE_TRANSLATE_DEFAULT_STYLE")
    translate_batch_size: int = Field(50, alias="SUBTITLE_TRANSLATE_BATCH_SIZE")
    translate_enable_summary: bool = Field(True, alias="SUBTITLE_TRANSLATE_ENABLE_SUMMARY")
    translate_max_retries: int = Field(2, alias="SUBTITLE_TRANSLATE_MAX_RETRIES")

    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field("https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    openai_temperature: float = Field(0.2, alias="OPENAI_TEMPERATURE")
    openai_timeout_seconds: float = Field(180.0, alias="OPENAI_TIMEOUT_SECONDS")


class YouTubeIngestSettings(CommonSettings):
    user_agent: str = Field("videoroll/0.1", alias="YOUTUBE_USER_AGENT")
    youtube_proxy: str | None = Field(None, alias="YOUTUBE_PROXY")


class BilibiliPublisherSettings(CommonSettings):
    publish_mode: str = Field("mock", alias="BILIBILI_PUBLISH_MODE")


@lru_cache
def get_orchestrator_settings() -> OrchestratorSettings:
    return OrchestratorSettings()


@lru_cache
def get_subtitle_settings() -> SubtitleServiceSettings:
    return SubtitleServiceSettings()


@lru_cache
def get_youtube_ingest_settings() -> YouTubeIngestSettings:
    return YouTubeIngestSettings()


@lru_cache
def get_bilibili_publisher_settings() -> BilibiliPublisherSettings:
    return BilibiliPublisherSettings()
