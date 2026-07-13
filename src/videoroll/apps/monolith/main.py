from __future__ import annotations

from fastapi import FastAPI

from videoroll.apps.orchestrator_api.main import app as orchestrator_app


app: FastAPI = orchestrator_app
