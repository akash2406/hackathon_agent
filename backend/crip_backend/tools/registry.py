"""Registry of executable tools: Foundry function-tool name -> backend handler.

Flow position: the hand-off point between "the model decided to call a tool"
and "a tool call reaching Azure". The agent gateway receives a function call
from a Foundry run (name + JSON arguments), and ``execute_tool`` validates the
arguments against the handler's pydantic model, runs it with the user's
``ToolContext``, records the resulting ``AgentResponse`` as a contribution, and
returns it.

The tool *names* and argument *schemas* here must match
``foundry/definitions/*.json``; ``tests/test_foundry_definitions.py`` enforces
that, and the app refuses to start if a registered agent references a tool that
has no handler.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ..contracts import AgentResponse, Confidence, ResponseStatus
from . import costpulse
from .context import ToolContext, ToolContribution

log = logging.getLogger(__name__)

Handler = Callable[[Any, ToolContext], Awaitable[AgentResponse]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    agent: str
    args_model: type[BaseModel]
    handler: Handler


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(costpulse.QUERY_COSTS, costpulse.AGENT, costpulse.QueryCostsArgs, costpulse.query_costs),
        ToolSpec(costpulse.LIST_SUBSCRIPTIONS, costpulse.AGENT, costpulse.ListSubscriptionsArgs, costpulse.list_subscriptions),
        # Next agent's tools go here, e.g.:
        # ToolSpec(inventory.LIST_RESOURCES, inventory.AGENT, inventory.ListResourcesArgs, inventory.list_resources),
    )
}


class UnknownToolError(KeyError):
    pass


async def execute_tool(name: str, arguments_json: str, ctx: ToolContext, tools: dict[str, ToolSpec] = TOOLS) -> AgentResponse:
    spec = tools.get(name)
    if spec is None:
        raise UnknownToolError(name)
    try:
        args = spec.args_model.model_validate_json(arguments_json or "{}")
    except ValidationError as exc:
        # Returned to the model so it can correct its call; not recorded as a
        # contribution because no Azure call was attempted.
        return AgentResponse(
            agent=spec.agent,
            status=ResponseStatus.ERROR,
            answer=f"Invalid arguments for {name}: {exc.errors(include_url=False)}",
            confidence=Confidence.from_score(0.0),
        )

    started = time.perf_counter()
    response = await spec.handler(args, ctx)
    latency_ms = int((time.perf_counter() - started) * 1000)
    ctx.contributions.append(ToolContribution(agent=spec.agent, tool=name, response=response, latency_ms=latency_ms))
    log.info(
        "tool executed",
        extra={"tool": name, "status": response.status.value, "latency_ms": latency_ms, "session_id": str(ctx.session_id)},
    )
    return response
