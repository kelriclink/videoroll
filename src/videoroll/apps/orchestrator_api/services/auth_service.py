from __future__ import annotations

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.admin_auth_store import (
    DEVICE_COOKIE_NAME,
    device_cookie_max_age_seconds,
    encode_password_hash,
    get_password_hash,
    mint_device_cookie_value,
    set_password_hash,
    validate_new_password,
    verify_device_cookie_value,
    verify_password_hash,
)
from videoroll.apps.orchestrator_api.schemas import (
    AdminAuthLoginRequest,
    AdminAuthSetupRequest,
    AdminAuthStatusRead,
)
from videoroll.db.session import get_sessionmaker


def secure_cookie(request: Request) -> bool:
    proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto == "https"


def set_device_cookie(response: Response, value: str, *, secure: bool) -> None:
    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=value,
        max_age=device_cookie_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=bool(secure),
        path="/",
    )


def get_admin_password_hash(request: Request, db: Session | None = None) -> str:
    cached = str(getattr(request.app.state, "admin_password_hash", "") or "").strip()
    if cached:
        return cached

    password_hash = ""
    if db is not None:
        try:
            password_hash = str(get_password_hash(db) or "").strip()
        except Exception:
            password_hash = ""
    else:
        database_url = str(getattr(request.app.state, "database_url", "") or "").strip()
        if database_url:
            session_local = get_sessionmaker(database_url)
            fallback_db = session_local()
            try:
                password_hash = str(get_password_hash(fallback_db) or "").strip()
            finally:
                fallback_db.close()

    if password_hash:
        request.app.state.admin_password_hash = password_hash
    return password_hash


def auth_status(request: Request, db: Session) -> AdminAuthStatusRead:
    password_hash = get_admin_password_hash(request, db)
    password_set = bool(password_hash)
    trusted = False
    if password_set:
        cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
        cookie_value = str(request.cookies.get(DEVICE_COOKIE_NAME) or "").strip()
        if cookie_secret and cookie_value:
            trusted = verify_device_cookie_value(
                cookie_value,
                internal_secret=cookie_secret,
                password_hash=password_hash,
            )
    return AdminAuthStatusRead(password_set=password_set, trusted=trusted)


def setup_auth(payload: AdminAuthSetupRequest, request: Request, db: Session) -> JSONResponse:
    if get_admin_password_hash(request, db):
        raise HTTPException(status_code=400, detail="admin password already set")

    password = validate_new_password(payload.password)
    encoded = encode_password_hash(password)
    set_password_hash(db, encoded)
    request.app.state.admin_password_hash = encoded

    cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_value = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=encoded)
    body = AdminAuthStatusRead(password_set=True, trusted=True).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=body)
    set_device_cookie(response, cookie_value, secure=secure_cookie(request))
    return response


def login(payload: AdminAuthLoginRequest, request: Request, db: Session) -> JSONResponse:
    password_hash = get_admin_password_hash(request, db)
    if not password_hash:
        raise HTTPException(status_code=400, detail="admin password is not set")
    if not verify_password_hash(str(payload.password or ""), password_hash):
        raise HTTPException(status_code=401, detail="invalid password")

    cookie_secret = str(getattr(request.app.state, "admin_cookie_secret", "") or "").strip()
    cookie_value = mint_device_cookie_value(internal_secret=cookie_secret, password_hash=password_hash)
    body = AdminAuthStatusRead(password_set=True, trusted=True).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=body)
    set_device_cookie(response, cookie_value, secure=secure_cookie(request))
    return response


def logout(request: Request, db: Session) -> JSONResponse:
    password_set = bool(get_admin_password_hash(request, db))
    body = AdminAuthStatusRead(password_set=password_set, trusted=False).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=body)
    response.delete_cookie(key=DEVICE_COOKIE_NAME, path="/")
    return response
