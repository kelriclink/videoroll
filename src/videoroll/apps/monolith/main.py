from __future__ import annotations

from fastapi import FastAPI

from videoroll.apps.bilibili_publisher.main import app as bilibili_publisher_app
from videoroll.apps.orchestrator_api.main import app as orchestrator_app
from videoroll.apps.subtitle_service.main import app as subtitle_service_app
from videoroll.apps.youtube_ingest.main import app as youtube_ingest_app


app: FastAPI = orchestrator_app

# Mount module apps for "single process" mode.
app.mount("/subtitle-service", subtitle_service_app)
app.mount("/youtube-ingest", youtube_ingest_app)
app.mount("/bilibili-publisher", bilibili_publisher_app)

