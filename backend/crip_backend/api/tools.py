"""``POST /api/tools/{tool_name}`` - invoke a tool directly, bypassing the agents.

Useful for demos and debugging: it returns the raw grounded ``AgentResponse``
(query, source, data timestamp) exactly as a Foundry agent would receive it,
using the caller's own OBO token. Same auth as the chat endpoint; no session,
so the OBO token is not cached.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth.entra import AuthenticatedUser, require_user
from ..contracts import AgentResponse, ErrorCode, ErrorEnvelope
from ..errors import ApiError
from ..services import Services
from ..tools.context import ToolContext
from ..tools.registry import UnknownToolError, execute_tool
from . import get_services

router = APIRouter(prefix="/api/tools", tags=["tools"])


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
    ctx = ToolContext(user=user, session_id=None, tokens=services.tokens, cost_client=services.cost_client)
    arguments = (await request.body()).decode("utf-8") or "{}"
    try:
        return await execute_tool(tool_name, arguments, ctx)
    except UnknownToolError as exc:
        raise ApiError(ErrorCode.UNKNOWN_TOOL, f"Unknown tool '{tool_name}'.", status_code=404) from exc
