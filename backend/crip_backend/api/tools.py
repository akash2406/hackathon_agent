"""``POST /api/tools/{tool_name}`` - invoke a tool directly, bypassing the agents.

Powers the dashboard and admin pages: fast, deterministic, no LLM, and it
returns the same grounded ``AgentResponse`` a Foundry agent would receive.
Access is checked per tool and scope in ``execute_tool``; every call (including
denials) is written to the usage log.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends, Request

from ..access.model import UserAccess
from ..auth.entra import AuthenticatedUser, require_user
from ..contracts import AgentResponse, ErrorCode, ErrorEnvelope
from ..errors import ApiError
from ..services import Services
from ..tools.registry import UnknownToolError, execute_tool
from . import get_services
from .deps import audit, require_access, tool_context

router = APIRouter(prefix="/api/tools", tags=["tools"])
_DASHBOARD_NS = uuid.UUID("5f0c8a1e-7c2b-4f7a-9a51-3c1f0d4b6e21")


@router.post(
    "/{tool_name}",
    response_model=AgentResponse,
    responses={code: {"model": ErrorEnvelope} for code in (401, 403, 404)},
)
async def run_tool(
    tool_name: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
    access: UserAccess = Depends(require_access),
    services: Services = Depends(get_services),
) -> AgentResponse:
    started = time.perf_counter()
    # A stable per-user key lets parallel dashboard calls share one OBO exchange
    # in user_obo mode (the cache is keyed by (session, oid), so it stays per user).
    ctx = tool_context(services, user, access, uuid.uuid5(_DASHBOARD_NS, user.object_id))
    arguments = (await request.body()).decode("utf-8") or "{}"
    try:
        scope = json.loads(arguments).get("scope")
    except (ValueError, AttributeError):
        scope = None
    try:
        response = await execute_tool(tool_name, arguments, ctx)
    except UnknownToolError as exc:
        raise ApiError(ErrorCode.UNKNOWN_TOOL, f"Unknown tool '{tool_name}'.", status_code=404) from exc
    outcome = "denied" if response.answer.startswith("Access denied:") else response.status.value
    await audit(request, services, user, action=f"tool:{tool_name}", scope=scope, outcome=outcome, started=started)
    return response
