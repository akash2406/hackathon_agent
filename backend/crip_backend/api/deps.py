"""Shared API dependencies: the signed-in user's resolved access, tool contexts, and the usage audit."""

from __future__ import annotations

import logging
import time
from uuid import UUID

from fastapi import Depends, Request

from ..access.model import Requirement, UserAccess
from ..auth.entra import AuthenticatedUser, require_user
from ..contracts import ErrorCode
from ..errors import ApiError, correlation_id
from ..persistence.repository import AccessEvent
from ..services import Services
from ..tools.context import ToolContext
from . import get_services

log = logging.getLogger(__name__)


async def require_access(
    user: AuthenticatedUser = Depends(require_user),
    services: Services = Depends(get_services),
) -> UserAccess:
    return await services.access.resolve(user)


def tool_context(services: Services, user: AuthenticatedUser, access: UserAccess, session_id: UUID | None) -> ToolContext:
    return ToolContext(
        user=user,
        session_id=session_id,
        tokens=services.tokens,
        arm=services.arm,
        access=access,
        repository=services.repository,
        graph=services.graph,
        management_group_id=services.settings.management_group_id,
    )


async def audit(
    request: Request,
    services: Services,
    user: AuthenticatedUser,
    *,
    action: str,
    scope: str | None,
    outcome: str,
    started: float,
    detail: str | None = None,
) -> None:
    """Record one request in the usage log. Never fails the request it describes."""
    try:
        await services.repository.log_access(
            AccessEvent(
                user_oid=user.object_id,
                user_name=user.display_name or user.username,
                action=action,
                scope=scope,
                outcome=outcome,
                latency_ms=int((time.perf_counter() - started) * 1000),
                correlation_id=correlation_id(request),
                detail=detail,
            )
        )
    except Exception:
        log.exception("could not write usage log entry")


async def require_admin(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
    access: UserAccess = Depends(require_access),
    services: Services = Depends(get_services),
) -> UserAccess:
    decision = access.check(None, Requirement.ADMIN)
    if not decision.allowed:
        await audit(request, services, user, action=f"admin:{request.url.path}", scope=None, outcome="denied", started=time.perf_counter())
        raise ApiError(ErrorCode.FORBIDDEN, decision.reason, status_code=403)
    return access
