"""``POST /api/chat`` - the entry point of "a user asking a question".

Sequence per request:

1. ``require_user`` validates the Entra token (401/403 envelope on failure).
2. Load the caller's session (404 if it is not theirs) or create one.
3. Persist the user's message (explicit per-session seq).
4. Run the Orchestrator via ``ConversationService``; tools it triggers obtain
   the user's OBO token through the ``ToolContext`` built here.
5. Persist the composed answer and one ``agent_invocations`` row per contribution.
6. Return a ``ChatResponse`` whose citations are the tools' own AgentResponses.

If the Orchestrator run fails, the failure is persisted and the client gets a
typed ``ErrorEnvelope`` (502/504) - never a made-up answer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..agent_gateway.foundry_gateway import FoundryRunError, FoundryRunTimeout
from ..auth.entra import AuthenticatedUser, require_user
from ..contracts import ChatRequest, ChatResponse, ErrorCode, ErrorEnvelope, ResponseStatus, compose_status
from ..errors import ApiError
from ..services import Services
from ..tools.context import ToolContext
from . import get_services

router = APIRouter(prefix="/api", tags=["chat"])

NOT_GROUNDED_CAVEAT = "No Azure data was successfully retrieved for this reply, so it is not grounded in cost data."

_ERRORS = {
    code: {"model": ErrorEnvelope}
    for code in (401, 403, 404, 422, 500, 502, 503, 504)
}


@router.post("/chat", response_model=ChatResponse, responses=_ERRORS)
async def chat(
    body: ChatRequest,
    user: AuthenticatedUser = Depends(require_user),
    services: Services = Depends(get_services),
) -> ChatResponse:
    repo = services.repository

    if body.session_id is not None:
        session = await repo.get_session(body.session_id, owner_oid=user.object_id)
        if session is None:
            raise ApiError(ErrorCode.SESSION_NOT_FOUND, "Session not found.", status_code=404)
    else:
        session = await repo.create_session(owner_oid=user.object_id, tenant_id=user.tenant_id, title=body.message[:80])

    await repo.append_message(session.id, owner_oid=user.object_id, role="user", content=body.message)

    ctx = ToolContext(user=user, session_id=session.id, tokens=services.tokens, cost_client=services.cost_client)
    try:
        result = await services.conversation.ask(thread_id=session.foundry_thread_id, message=body.message, ctx=ctx)
    except FoundryRunError as exc:
        timed_out = isinstance(exc, FoundryRunTimeout)
        code = ErrorCode.UPSTREAM_TIMEOUT if timed_out else ErrorCode.ORCHESTRATOR_UNAVAILABLE
        # Persist the failure (and any tool calls that did happen) for the audit trail.
        await repo.append_message(
            session.id,
            owner_oid=user.object_id,
            role="assistant",
            content=f"No answer was produced: {exc}",
            status=ResponseStatus.ERROR,
            error_code=code.value,
            contributions=ctx.contributions,
        )
        raise ApiError(
            code,
            "The Orchestrator could not produce an answer, so none is shown. Please retry.",
            status_code=504 if timed_out else 502,
            retryable=True,
        ) from exc

    if session.foundry_thread_id != result.thread_id:
        await repo.set_thread(session.id, owner_oid=user.object_id, thread_id=result.thread_id)

    contributions = [c.response for c in ctx.contributions]
    status = compose_status(contributions)
    caveats = list(dict.fromkeys(caveat for c in contributions for caveat in c.caveats))
    if not any(c.grounded for c in contributions):
        caveats.insert(0, NOT_GROUNDED_CAVEAT)

    message = await repo.append_message(
        session.id,
        owner_oid=user.object_id,
        role="assistant",
        content=result.answer,
        status=status,
        contributions=ctx.contributions,
    )
    return ChatResponse(
        session_id=session.id,
        message_id=message.id,
        answer=result.answer,
        status=status,
        contributions=contributions,
        caveats=caveats,
        created_at=message.created_at,
    )
