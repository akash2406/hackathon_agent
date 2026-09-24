"""FastAPI application factory.

Run with ``uvicorn --factory crip_backend.main:create_app``. In production the
lifespan builds real services (``services.build_services``); tests pass a
pre-built ``Services`` of fakes instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import chat, health, tools
from .config import Settings, get_settings
from .errors import install_error_handlers
from .secrets import SecretStore
from .services import Services, build_services
from .telemetry import configure_logging, configure_telemetry


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    if services is None:
        settings = settings or get_settings()
        configure_logging(settings.log_level)
        configure_telemetry(SecretStore(settings.secrets_dir), settings.appinsights_secret_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            yield
            return
        assert settings is not None
        app.state.services = await build_services(settings)
        try:
            yield
        finally:
            await app.state.services.aclose()

    app = FastAPI(title="CRIP backend", version="0.1.0", lifespan=lifespan)
    if services is not None:
        app.state.services = services
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(tools.router)
    return app
