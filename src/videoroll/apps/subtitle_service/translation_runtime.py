from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.model_provider_store import LEGACY_ID, resolve_provider_settings


@dataclass(frozen=True)
class TranslationRuntime:
    provider: str
    model_provider_id: str
    model_id: str
    settings: dict[str, Any]

    @property
    def uses_custom_model(self) -> bool:
        return self.provider == "openai" and (
            self.model_provider_id != LEGACY_ID or bool(self.model_id)
        )


def resolve_translation_runtime(
    db: Session,
    legacy_settings: dict[str, Any],
    *,
    provider: object,
    model_provider_id: object = LEGACY_ID,
    model_id: object = "",
) -> TranslationRuntime:
    """Resolve a task's translation model once for every downstream AI call."""
    provider_name = str(provider or "openai").strip() or "openai"
    selected_provider = str(model_provider_id or LEGACY_ID).strip() or LEGACY_ID
    selected_model = str(model_id or "").strip()
    settings = dict(legacy_settings)

    if provider_name == "openai" and (
        selected_provider != LEGACY_ID or selected_model
    ):
        settings = resolve_provider_settings(
            db,
            settings,
            selected_provider,
            selected_model,
        )

    return TranslationRuntime(
        provider=provider_name,
        model_provider_id=selected_provider,
        model_id=selected_model,
        settings=settings,
    )
