"""``GET /api/me``: who you are in CRIP and what you may see (drives navigation in the UI)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from ..access.model import UserAccess
from ..access.resolver import describe
from ..auth.entra import AuthenticatedUser, require_user
from ..services import Services
from . import get_services
from .deps import audit, require_access

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
async def me(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
    access: UserAccess = Depends(require_access),
    services: Services = Depends(get_services),
) -> dict:
    started = time.perf_counter()
    await audit(request, services, user, action="me", scope=None, outcome="ok", started=started)
    return {
        "user": {"object_id": user.object_id, "name": user.display_name, "username": user.username},
        "access_mode": services.settings.azure_access_mode.value,
        **describe(access),
    }
