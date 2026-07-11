from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_YOUTUBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


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
    social_publisher_url: str = Field("http://social-publisher-api:8010", alias="SOCIAL_PUBLISHER_URL")

    # Shared runtime settings (used by orchestrator actions).
    work_dir: str = Field("/tmp/videoroll", alias="WORK_DIR")
    ffmpeg_path: str = Field("ffmpeg", alias="FFMPEG_PATH")

    # YouTube downloader (yt-dlp) settings.
    youtube_user_agent: str = Field(DEFAULT_YOUTUBE_USER_AGENT, alias="YOUTUBE_USER_AGENT")
    youtube_cookie_file: str | None = Field(None, alias="YOUTUBE_COOKIE_FILE")
    youtube_proxy: str | None = Field(None, alias="YOUTUBE_PROXY")
    youtube_extractor_args_json: str | None = Field(None, alias="YOUTUBE_EXTRACTOR_ARGS_JSON")


class SubtitleServiceSettings(CommonSettings):
    asr_engine: str = Field("faster-whisper", alias="SUBTITLE_ASR_ENGINE")
    whisper_model: str = Field("tiny", alias="SUBTITLE_WHISPER_MODEL")
    whisper_device: str = Field("cpu", alias="SUBTITLE_WHISPER_DEVICE")
    whisper_compute_type: str = Field("int8", alias="SUBTITLE_WHISPER_COMPUTE_TYPE")
    whisper_model_dir: str = Field("/models/whisper", alias="SUBTITLE_WHISPER_MODEL_DIR")
    openvino_model: str = Field("", alias="SUBTITLE_OPENVINO_MODEL")
    openvino_device: str = Field("GPU", alias="SUBTITLE_OPENVINO_DEVICE")
    openvino_num_beams: int = Field(1, alias="SUBTITLE_OPENVINO_NUM_BEAMS")
    openvino_max_new_tokens: int = Field(448, alias="SUBTITLE_OPENVINO_MAX_NEW_TOKENS")
    # faster-whisper runtime parallelism (CPU only):
    # - cpu_threads=0 means "auto" (use available CPUs).
    # - num_workers defaults to 1 to avoid memory spikes.
    whisper_cpu_threads: int = Field(0, alias="SUBTITLE_WHISPER_CPU_THREADS")
    whisper_num_workers: int = Field(1, alias="SUBTITLE_WHISPER_NUM_WORKERS")
    ffmpeg_path: str = Field("ffmpeg", alias="FFMPEG_PATH")
    work_dir: str = Field("/tmp/videoroll", alias="WORK_DIR")
    intel_gpu_render_device: str = Field("/dev/dri/renderD128", alias="INTEL_GPU_RENDER_DEVICE")

    # Shared YouTube downloader settings so the subtitle worker can reuse
    # cookies/proxy/extractor args when fetching subtitles directly.
    youtube_user_agent: str = Field(DEFAULT_YOUTUBE_USER_AGENT, alias="YOUTUBE_USER_AGENT")
    youtube_cookie_file: str | None = Field(None, alias="YOUTUBE_COOKIE_FILE")
    youtube_proxy: str | None = Field(None, alias="YOUTUBE_PROXY")
    youtube_extractor_args_json: str | None = Field(None, alias="YOUTUBE_EXTRACTOR_ARGS_JSON")

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

    rag_enabled: bool = Field(False, alias="SUBTITLE_RAG_ENABLED")
    rag_top_k: int = Field(8, alias="SUBTITLE_RAG_TOP_K")
    rag_min_score: float = Field(0.68, alias="SUBTITLE_RAG_MIN_SCORE")
    rag_embedding_provider: str = Field("openai", alias="SUBTITLE_RAG_EMBEDDING_PROVIDER")
    rag_embedding_model: str = Field("text-embedding-3-small", alias="SUBTITLE_RAG_EMBEDDING_MODEL")
    rag_embedding_dimensions: int = Field(1536, alias="SUBTITLE_RAG_EMBEDDING_DIMENSIONS")
    rag_embedding_model_dir: str = Field("/models/embeddings", alias="SUBTITLE_RAG_EMBEDDING_MODEL_DIR")
    rag_embedding_device: str = Field("cpu", alias="SUBTITLE_RAG_EMBEDDING_DEVICE")
    rag_embedding_api_key: str = Field("", alias="SUBTITLE_RAG_EMBEDDING_API_KEY")
    rag_embedding_base_url: str = Field("", alias="SUBTITLE_RAG_EMBEDDING_BASE_URL")
    rag_embedding_timeout_seconds: float = Field(60.0, alias="SUBTITLE_RAG_EMBEDDING_TIMEOUT_SECONDS")
    rag_auto_discover_terms: bool = Field(False, alias="SUBTITLE_RAG_AUTO_DISCOVER_TERMS")
    rag_auto_learn_terms: bool = Field(False, alias="SUBTITLE_RAG_AUTO_LEARN_TERMS")
    rag_dictionary_enabled: bool = Field(True, alias="SUBTITLE_RAG_DICTIONARY_ENABLED")
    rag_dictionary_top_k: int = Field(8, alias="SUBTITLE_RAG_DICTIONARY_TOP_K")
    rag_dictionary_min_quality: float = Field(0.0, alias="SUBTITLE_RAG_DICTIONARY_MIN_QUALITY")
    rag_dictionary_auto_promote: bool = Field(False, alias="SUBTITLE_RAG_DICTIONARY_AUTO_PROMOTE")
    dictionary_data_dir: str = Field("data/dictionaries", alias="SUBTITLE_DICTIONARY_DATA_DIR")
    rag_search_enabled: bool = Field(False, alias="SUBTITLE_RAG_SEARCH_ENABLED")
    rag_search_url: str = Field("", alias="SUBTITLE_RAG_SEARCH_URL")
    rag_search_categories: str = Field("general", alias="SUBTITLE_RAG_SEARCH_CATEGORIES")
    rag_search_engines: str = Field("", alias="SUBTITLE_RAG_SEARCH_ENGINES")
    rag_search_fallback_engines: str = Field("bing,baidu", alias="SUBTITLE_RAG_SEARCH_FALLBACK_ENGINES")
    rag_search_language: str = Field("all", alias="SUBTITLE_RAG_SEARCH_LANGUAGE")
    rag_search_safesearch: int = Field(0, alias="SUBTITLE_RAG_SEARCH_SAFESEARCH")
    rag_search_time_range: str = Field("", alias="SUBTITLE_RAG_SEARCH_TIME_RANGE")
    rag_search_pageno: int = Field(1, alias="SUBTITLE_RAG_SEARCH_PAGENO")
    rag_domain: str = Field("", alias="SUBTITLE_RAG_DOMAIN")
    rag_agent_skills_enabled: bool = Field(False, alias="SUBTITLE_RAG_AGENT_SKILLS_ENABLED")
    rag_agent_builtin_skills_enabled: bool = Field(True, alias="SUBTITLE_RAG_AGENT_BUILTIN_SKILLS_ENABLED")
    rag_agent_user_skills_enabled: bool = Field(True, alias="SUBTITLE_RAG_AGENT_USER_SKILLS_ENABLED")

    # Orchestrator API (used by subtitle worker for auto pipelines).
    orchestrator_url: str = Field("http://localhost:8000", alias="ORCHESTRATOR_URL")
    orchestrator_timeout_seconds: float = Field(1800.0, alias="ORCHESTRATOR_TIMEOUT_SECONDS")


class YouTubeIngestSettings(CommonSettings):
    user_agent: str = Field(DEFAULT_YOUTUBE_USER_AGENT, alias="YOUTUBE_USER_AGENT")
    youtube_proxy: str | None = Field(None, alias="YOUTUBE_PROXY")


class BilibiliPublisherSettings(CommonSettings):
    publish_mode: str = Field("mock", alias="BILIBILI_PUBLISH_MODE")


class SocialPublisherSettings(CommonSettings):
    work_dir: str = Field("/work/social-publisher", alias="SOCIAL_PUBLISH_WORK_DIR")
    sau_executable: str = Field("sau", alias="SAU_EXECUTABLE")
    sau_runtime_dir: str = Field("/opt/social-auto-upload", alias="SAU_RUNTIME_DIR")
    sau_cookies_dir: str = Field("/opt/social-auto-upload/cookies", alias="SAU_COOKIES_DIR")
    sau_headless: bool = Field(True, alias="SAU_HEADLESS")
    account_check_timeout_seconds: float = Field(120.0, alias="SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS")
    upload_timeout_seconds: float = Field(3600.0, alias="SAU_UPLOAD_TIMEOUT_SECONDS")
    output_max_bytes: int = Field(65536, alias="SAU_OUTPUT_MAX_BYTES")
    lock_margin_seconds: int = Field(600, alias="SAU_LOCK_MARGIN_SECONDS")
    login_timeout_seconds: float = Field(900.0, alias="SOCIAL_LOGIN_TIMEOUT_SECONDS")
    login_display: str = Field(":99", alias="SOCIAL_LOGIN_DISPLAY")
    login_browser_url: str = Field(
        "/social-login/vnc.html?autoconnect=1&resize=scale&path=social-login/websockify",
        alias="SOCIAL_LOGIN_BROWSER_URL",
    )


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


@lru_cache
def get_social_publisher_settings() -> SocialPublisherSettings:
    return SocialPublisherSettings()
