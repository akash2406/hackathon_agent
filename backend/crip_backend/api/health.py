"""Health probes used by the Helm chart.

``GET /health`` (readiness) checks the database, because without it the service
cannot record provenance and so should not receive chat traffic.
``GET /health/live`` (liveness) checks only that the process is serving; a
database outage must not make Kubernetes restart-loop healthy pods.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..contracts import ErrorCode
from ..errors import PersistenceError, envelope
from ..services import Services
from . import get_services

router = APIRouter(tags=["health"])


@router.get("/health")
async def readiness(request: Request, services: Services = Depends(get_services)) -> JSONResponse:
    try:
        await asyncio.wait_for(services.repository.ping(), timeout=2.0)
    except (PersistenceError, TimeoutError):
        return envelope(request, ErrorCode.PERSISTENCE_UNAVAILABLE, "Database unreachable.", 503, retryable=True)
    return JSONResponse({"status": "ok"})


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}
