from __future__ import annotations

from fastapi import FastAPI


_DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def create_app(*, install_lifecycle: bool = True) -> FastAPI:
    """Create an orchestrator app while routes are migrated out of the legacy module.

    The legacy bridge is intentionally isolated here and will be removed after all
    domain routers and lifecycle hooks have moved to their owning modules.
    """
    from videoroll.apps.orchestrator_api.main import app as legacy_app

    application = FastAPI(title="videoroll-orchestrator", version="0.1.0")
    application.router.routes.extend(route for route in legacy_app.routes if route.path not in _DOC_PATHS)
    application.exception_handlers.update(legacy_app.exception_handlers)
    application.user_middleware = list(legacy_app.user_middleware)
    application.middleware_stack = None

    if install_lifecycle:
        application.router.on_startup.extend(legacy_app.router.on_startup)
        application.router.on_shutdown.extend(legacy_app.router.on_shutdown)

    return application
