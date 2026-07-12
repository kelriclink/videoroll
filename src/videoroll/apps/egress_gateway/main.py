from __future__ import annotations

import base64

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from videoroll.apps.egress_gateway.client import EgressDenied, fetch_public
from videoroll.apps.security.service_auth import require_internal_service, service_token


class EgressGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    internal_api_secret: str = Field("", alias="INTERNAL_API_SECRET")


class FetchRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    timeout: float = Field(20.0, ge=0.1, le=60.0)
    max_bytes: int = Field(500_000, ge=1, le=2_000_000)
    redirects: int = Field(5, ge=0, le=5)


class FetchResponse(BaseModel):
    status_code: int
    headers: dict[str, str]
    body_base64: str
    url: str
    truncated: bool


def get_settings() -> EgressGatewaySettings:
    return EgressGatewaySettings()


def internal_service_token(settings: EgressGatewaySettings | None = None) -> str:
    return service_token(settings or get_settings())


app = FastAPI(title="videoroll-egress-gateway", version="0.1.0")
app.state.internal_service_token = internal_service_token()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/fetch", response_model=FetchResponse)
def fetch(
    payload: FetchRequest,
    _: None = Depends(require_internal_service),
) -> FetchResponse:
    try:
        response = fetch_public(
            payload.url,
            timeout=payload.timeout,
            max_bytes=payload.max_bytes,
            redirects=payload.redirects,
        )
    except EgressDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="egress fetch failed") from exc
    return FetchResponse(
        status_code=response.status_code,
        headers=response.headers,
        body_base64=base64.b64encode(response.content).decode("ascii"),
        url=response.url,
        truncated=response.truncated,
    )
