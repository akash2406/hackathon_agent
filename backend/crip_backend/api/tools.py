"""``POST /api/tools/{tool_name}`` - invoke a tool directly, bypassing the agents.

Powers the dashboard pages (Overview, Spend, Trends, Savings, Inventory): fast,
deterministic, no LLM, and it returns the same grounded ``AgentResponse``
(query, source, data timestamp) a Foundry agent would receive, using the
caller's own OBO token. Same auth as the chat endpoint.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from ..auth.entra import AuthenticatedUser, require_user
from ..contracts import AgentResponse, ErrorCode, ErrorEnvelope
from ..errors import ApiError
from ..services import Services
from ..tools.context import ToolContext
from ..tools.registry import UnknownToolError, execute_tool
from . import get_services

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
    services: Services = Depends(get_services),
) -> AgentResponse:
    # A stable per-user key lets the dashboard's parallel tool calls share one
    # OBO exchange (the token cache is keyed by (session, oid), so it stays per user).
    ctx = ToolContext(user=user, session_id=uuid.uuid5(_DASHBOARD_NS, user.object_id), tokens=services.tokens, arm=services.arm)
    arguments = (await request.body()).decode("utf-8") or "{}"
    try:
        return await execute_tool(tool_name, arguments, ctx)
    except UnknownToolError as exc:
        raise ApiError(ErrorCode.UNKNOWN_TOOL, f"Unknown tool '{tool_name}'.", status_code=404) from exc
