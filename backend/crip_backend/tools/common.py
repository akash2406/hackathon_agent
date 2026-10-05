"""Helpers shared by every domain agent's tools.

These keep the honesty rules in one place: how a delegated token is obtained,
how an Azure call becomes a ``Source``, and how a failed call becomes an
``error`` contract with the real HTTP status (never a guess).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from azure.core.exceptions import AzureError

from ..auth.obo import NotDelegatedTokenError, OboExchangeError
from ..azure_clients.arm import ArmResult, AzureApiError
from ..contracts import AgentResponse, Confidence, ResponseStatus, Source
from .context import ToolContext

HIGH = Confidence.from_score(0.9)
MEDIUM = Confidence.from_score(0.6)
LOW = Confidence.from_score(0.1)

COST_LATENCY_CAVEAT = "Azure Cost Management data typically lags 8-24 hours behind actual usage."


async def user_token(ctx: ToolContext, *, agent: str, query_used: str | None = None) -> str | AgentResponse:
    """The token for Azure data calls, or an ``error`` response explaining why there is none (no Azure call is made)."""
    try:
        return await ctx.arm_token()
    except (OboExchangeError, NotDelegatedTokenError) as exc:
        reason = f"I could not obtain a delegated Azure token for you, so no data was retrieved ({exc})."
    except AzureError as exc:  # app_identity mode: managed identity unavailable / not authorised
        reason = f"CRIP's identity could not obtain an Azure token, so no data was retrieved ({type(exc).__name__}: {exc})."
    return AgentResponse(
        agent=agent,
        status=ResponseStatus.ERROR,
        answer=reason,
        confidence=LOW,
        query_used=query_used,
        caveats=["No Azure call was made; nothing in this answer comes from Azure data."],
    )


def source(result: ArmResult, *, tool: str, scope: str) -> Source:
    return Source(
        tool=tool,
        api=result.api,
        invoked_at=result.invoked_at,
        scope=scope,
        request_id=result.request_id,
        http_status=result.http_status,
    )


def failure(
    exc: AzureApiError,
    *,
    agent: str,
    tool: str,
    scope: str,
    query_used: str,
    invoked_at: datetime,
    what: str = "data",
    role_hint: str = "Reader",
) -> AgentResponse:
    if exc.status_code == 403:
        reason = f"Azure denied access at {scope} (403 {exc.code}). Your account needs {role_hint} on that scope."
    elif exc.status_code == 401:
        reason = f"Azure rejected the delegated token (401 {exc.code})."
    elif exc.status_code == 404:
        reason = f"The scope {scope} was not found or is not visible to your account (404 {exc.code})."
    elif exc.status_code == 429:
        reason = "Azure is throttling requests and the retries were exhausted (429). Please try again shortly."
    elif exc.status_code is None:
        reason = f"The call to Azure failed before a response was received ({exc.message})."
    else:
        reason = f"Azure returned {exc.status_code} {exc.code}: {exc.message}"
    return AgentResponse(
        agent=agent,
        status=ResponseStatus.ERROR,
        answer=f"The {what} could not be retrieved. {reason}",
        confidence=LOW,
        query_used=query_used,
        sources=[
            Source(
                tool=tool, api=exc.api, invoked_at=invoked_at, scope=scope, request_id=exc.request_id, http_status=exc.status_code
            )
        ],
        caveats=[f"No {what} is available for this request."],
    )


def invalid_scope(agent: str, message: str) -> AgentResponse:
    return AgentResponse(agent=agent, status=ResponseStatus.ERROR, answer=message, confidence=LOW)


def parse_usage_date(value: Any) -> date | None:
    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():  # 20260921
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def money(value: float) -> str:
    return f"{value:,.2f}"


def column_index(columns: list[dict[str, Any]]) -> dict[str, int]:
    return {str(c.get("name", "")).lower(): i for i, c in enumerate(columns)}


COST_COLUMNS = ("totalcost", "cost", "pretaxcost", "costusd")


def cost_column(index: dict[str, int]) -> int | None:
    return next((index[n] for n in COST_COLUMNS if n in index), None)
