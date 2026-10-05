"""FastAPI application factory: the API **and** the web UI, from one container.

Run with ``uvicorn --factory crip_backend.main:create_app``. In App Service the
single image serves:

* ``/api/*``       the chat API (Entra-authenticated)
* ``/health*``     probes (App Service health check)
* ``/config.js``   the SPA's runtime config, built from App Settings (public IDs only)
* everything else  the built React app (``CRIP_STATIC_DIR``), with SPA fallback

One origin means no CORS, one URL to register as the Entra redirect URI, and
one App Service to deploy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import admin, capabilities, chat, health, me, tools
from .config import Settings, get_settings
from .contracts import ErrorCode
from .errors import ApiError, install_error_handlers
from .secrets import SecretStore
from .services import Services, build_services
from .telemetry import configure_logging, configure_telemetry

log = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    if services is not None:
        settings = services.settings
    else:
        settings = settings or get_settings()
        configure_logging(settings.log_level)
        configure_telemetry(SecretStore(settings.secrets_dir), settings.appinsights_secret_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            yield
            return
        app.state.services = await build_services(settings)
        try:
            yield
        finally:
            await app.state.services.aclose()

    app = FastAPI(title="CRIP", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    if services is not None:
        app.state.services = services
    install_error_handlers(app)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(me.router)
    app.include_router(admin.router)
    app.include_router(chat.router)
    app.include_router(tools.router)
    _mount_ui(app, settings)
    return app


def _mount_ui(app: FastAPI, settings: Settings) -> None:
    @app.get("/config.js", include_in_schema=False)
    async def config_js() -> Response:
        # Public identifiers only: an SPA's client ID, tenant ID and API scope
        # are visible to every browser that loads the app anyway.
        body = "window.CRIP_CONFIG = " + json.dumps(
            {
                "tenantId": settings.tenant_id,
                "spaClientId": settings.effective_spa_client_id,
                "apiScope": settings.api_scope,
                "apiBaseUrl": "",
                "demoMode": settings.ui_demo_mode,
            }
        ) + ";\n"
        return Response(body, media_type="application/javascript", headers={"Cache-Control": "no-store"})

    static = settings.static_dir.resolve()
    index = static / "index.html"
    if not index.is_file():
        log.warning("Web UI not found at %s (build the frontend or set CRIP_STATIC_DIR); serving the API only", static)
        return
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        # Unknown API paths get the typed error envelope, never the HTML shell.
        if path.startswith("api/") or path == "api" or path.startswith("health"):
            raise ApiError(ErrorCode.NOT_FOUND, f"No route /{path}.", status_code=404)
        candidate = (static / path).resolve()
        if path and candidate.is_file() and static in candidate.parents:
            return FileResponse(candidate)
        # SPA fallback: client-side routes render index.html. no-store so a
        # redeploy is picked up immediately (assets are content-hashed).
        return FileResponse(index, headers={"Cache-Control": "no-store"})
