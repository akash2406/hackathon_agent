"""Registry of executable tools: Foundry function-tool name -> backend handler + required access.

Flow position: the hand-off point between "the model (or a dashboard) asked for
a tool" and "a tool call reaching Azure". ``execute_tool``:

1. validates the arguments against the handler's pydantic model;
2. **authorises**: the user's ``UserAccess`` must meet the tool's requirement
   for the requested scope (resources / cost / platform admin). A refusal is
   an honest ``error`` answer and no Azure call is made;
3. runs the handler;
4. **stamps provenance** on every ``Source``: which identity made the call
   (``user_obo`` / ``app_identity``) and why this user was allowed
   (``authorized_via``). Done here, centrally, so no tool can forget it;
5. records the result as a contribution.

Tool names and argument schemas must match ``foundry/definitions/*.json``
(``tests/test_foundry_definitions.py`` enforces it).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ..access.model import Requirement
from ..contracts import AgentResponse, Confidence, ResponseStatus
from . import costpulse, governance, inventory, netdiag, optimizer, platform
from .context import ToolContext, ToolContribution

log = logging.getLogger(__name__)

Handler = Callable[[Any, ToolContext], Awaitable[AgentResponse]]
RequirementFor = Requirement | Callable[[Any], Requirement]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    agent: str
    args_model: type[BaseModel]
    handler: Handler
    requires: RequirementFor = Requirement.COST

    def requirement(self, args: Any) -> Requirement:
        return self.requires(args) if callable(self.requires) else self.requires


R, C, A = Requirement.RESOURCES, Requirement.COST, Requirement.ADMIN

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        # CostPulse: spend, trend, forecast (cost level)
        ToolSpec(costpulse.QUERY_COSTS, costpulse.AGENT, costpulse.QueryCostsArgs, costpulse.query_costs, C),
        ToolSpec(costpulse.COST_TREND, costpulse.AGENT, costpulse.CostTrendArgs, costpulse.cost_trend, C),
        ToolSpec(costpulse.FORECAST, costpulse.AGENT, costpulse.ForecastArgs, costpulse.forecast_month_end, C),
        ToolSpec(costpulse.LIST_SUBSCRIPTIONS, costpulse.AGENT, costpulse.ListSubscriptionsArgs, costpulse.list_subscriptions, R),
        # Optimizer: savings (cost level; idle resources without prices only needs resources level)
        ToolSpec(optimizer.ADVISOR, optimizer.AGENT, optimizer.AdvisorArgs, optimizer.advisor_recommendations, C),
        ToolSpec(optimizer.IDLE, optimizer.AGENT, optimizer.IdleResourcesArgs, optimizer.find_idle_resources,
                 lambda a: C if a.include_cost else R),
        # Inventory: what exists, tag coverage
        ToolSpec(inventory.SUMMARY, inventory.AGENT, inventory.ResourceSummaryArgs, inventory.resource_summary, R),
        # Governance: security, reliability, network, policy (resources level)
        ToolSpec(governance.ADVISOR, governance.AGENT, governance.AdvisorArgs, governance.advisor_recommendations, R),
        ToolSpec(governance.SECURITY, governance.AGENT, governance.ScopeArgs, governance.security_posture, R),
        ToolSpec(governance.NETWORK, governance.AGENT, governance.ScopeArgs, governance.network_posture, R),
        ToolSpec(governance.POLICY, governance.AGENT, governance.ScopeArgs, governance.policy_compliance, R),
        # Network Doctor: connectivity troubleshooting for application teams (resources level)
        ToolSpec(netdiag.CHECK, netdiag.AGENT, netdiag.CheckArgs, netdiag.check_connectivity, R),
        ToolSpec(netdiag.FIND, netdiag.AGENT, netdiag.FindArgs, netdiag.find_endpoint, R),
        ToolSpec(netdiag.EFFECTIVE, netdiag.AGENT, netdiag.ResourceArgs, netdiag.effective_rules, R),
        ToolSpec(netdiag.DNS, netdiag.AGENT, netdiag.ResourceArgs, netdiag.private_endpoint_dns, R),
        ToolSpec(netdiag.SUBNETS, netdiag.AGENT, netdiag.SubnetArgs, netdiag.subnet_health, R),
        # Platform: admin only
        ToolSpec(platform.ACCESS_REVIEW, platform.AGENT, platform.AccessReviewArgs, platform.access_review, A),
        ToolSpec(platform.ESTATE, platform.AGENT, platform.EstateArgs, platform.estate_overview, A),
        ToolSpec(platform.USAGE, platform.AGENT, platform.UsageArgs, platform.crip_usage, A),
    )
}


class UnknownToolError(KeyError):
    pass


def _denied(spec: ToolSpec, reason: str) -> AgentResponse:
    return AgentResponse(
        agent=spec.agent,
        status=ResponseStatus.ERROR,
        answer=f"Access denied: {reason}",
        confidence=Confidence.from_score(0.0),
        caveats=["No Azure call was made."],
    )


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

    via: str | None = None
    if ctx.access is not None:
        decision = ctx.access.check(getattr(args, "scope", None), spec.requirement(args))
        if not decision.allowed:
            log.info("tool denied", extra={"tool": name, "user": ctx.user.object_id, "reason": decision.reason})
            return _denied(spec, decision.reason)
        via = decision.via

    started = time.perf_counter()
    response = await spec.handler(args, ctx)
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.sources:
        response = response.model_copy(
            update={"sources": [s.model_copy(update={"auth": ctx.auth_kind, "authorized_via": via}) for s in response.sources]}
        )
    ctx.contributions.append(ToolContribution(agent=spec.agent, tool=name, response=response, latency_ms=latency_ms))
    log.info(
        "tool executed",
        extra={"tool": name, "status": response.status.value, "latency_ms": latency_ms, "session_id": str(ctx.session_id)},
    )
    return response
