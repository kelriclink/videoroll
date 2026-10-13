"""Authenticated TTS settings, model inventory and explicit downloads."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from videoroll.apps.dubbing.model_management import choose, download, inventory

router = APIRouter()


class DubbingSelection(BaseModel):
    model: Literal["Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]
    voice: Literal["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric"]
    device: Literal["cuda:0", "cuda:1", "cpu"]


class ModelDownload(BaseModel):
    model: Literal["Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]
    source: Literal["huggingface", "modelscope"] = "huggingface"


@router.get("/settings/dubbing")
def get_dubbing_config() -> dict:
    return inventory()


@router.put("/settings/dubbing")
def save_dubbing_config(payload: DubbingSelection) -> dict:
    try:
        choose(payload.model, payload.voice, payload.device)
        return inventory()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/settings/dubbing/models/download", status_code=202)
def download_dubbing_model(payload: ModelDownload) -> dict:
    try:
        return download(payload.model, payload.source)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
