"""Inventory tool: what exists in a subscription and how well it is tagged.

``inventory_resource_summary`` runs Azure Resource Graph queries with the user's
OBO token (Resource Graph only returns what the user can read): resource counts
by type and by location and, optionally, tag coverage for one tag key per
resource group. That last part answers the classic FinOps question "how much of
our estate can we even attribute to a cost owner?"
"""

from __future__ import annotations

import asyncio
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..azure_clients import resource_graph
from ..azure_clients.arm import ArmResult, AzureApiError, InvalidScopeError, split_subscription_scope
from ..contracts import AgentResponse, ResponseStatus
from . import common
from .context import ToolContext

AGENT = "inventory"
SUMMARY = "inventory_resource_summary"

# Tag keys are interpolated into KQL string literals: allow common tag-key
# characters only (no quotes or backslashes), so they cannot break out.
_TAG_KEY = re.compile(r"^[\w .:/@+=-]{1,128}$")


class ResourceSummaryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    tag_key: str | None = None
    top_n: int = Field(default=10, ge=1, le=50)

    @field_validator("tag_key")
    @classmethod
    def _safe_tag_key(cls, v: str | None) -> str | None:
        if v is not None and not _TAG_KEY.match(v):
            raise ValueError("tag_key may contain letters, digits, spaces and . : / @ + = - _ only")
        return v


def build_queries(resource_group: str | None, tag_key: str | None) -> dict[str, str]:
    base = "Resources" + (f"\n| where resourceGroup =~ '{resource_group}'" if resource_group else "")
    queries = {
        "by_type": f"{base}\n| summarize resources = count() by type\n| order by resources desc",
        "by_location": f"{base}\n| summarize resources = count() by location\n| order by resources desc",
    }
    if tag_key:
        queries["tag_coverage"] = (
            f"{base}\n| extend hasTag = isnotempty(tostring(tags['{tag_key}']))"
            "\n| summarize total = count(), tagged = countif(hasTag) by resourceGroup\n| order by total desc"
        )
    return queries


async def resource_summary(args: ResourceSummaryArgs, ctx: ToolContext) -> AgentResponse:
    try:
        subscription_id, resource_group = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{subscription_id}" + (f"/resourceGroups/{resource_group}" if resource_group else "")
    arm = ctx.arm
    queries = build_queries(resource_group, args.tag_key)
    api = f"POST {resource_graph.resources_url(arm)}"
    query_used = "\n\n".join(f"{api}\n{json.dumps({'subscriptions': [subscription_id], 'query': q})}" for q in queries.values())
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        # Independent queries: run them concurrently.
        results: list[ArmResult] = list(
            await asyncio.gather(
                *(resource_graph.query(arm, subscriptions=[subscription_id], kql=q, token=token) for q in queries.values())
            )
        )
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=SUMMARY, scope=scope, query_used=query_used, invoked_at=invoked_at, what="resource inventory"
        )
    named = dict(zip(queries, results, strict=True))
    sources = [common.source(r, tool=SUMMARY, scope=scope) for r in results]
    by_type = [{"group": r.get("type"), "count": int(r.get("resources", 0))} for r in named["by_type"].items]
    by_location = [{"group": r.get("location"), "count": int(r.get("resources", 0))} for r in named["by_location"].items]
    total = sum(r["count"] for r in by_type)

    if total == 0:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Resource Graph found no resources you can read in {scope}.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=sources,
        )

    data: dict = {
        "kind": "inventory",
        "scope": scope,
        "total_resources": total,
        "by_type": by_type[: args.top_n],
        "by_location": by_location[: args.top_n],
        "resource_type_count": len(by_type),
    }
    answer = (
        f"{scope} contains {total} resources across {len(by_type)} types and {len(by_location)} locations. "
        f"Most common: " + ", ".join(f"{t['group']} ({t['count']})" for t in by_type[:3]) + "."
    )
    caveats = ["Resource Graph shows only resources your account can read; counts are live."]
    if "tag_coverage" in named:
        rows = named["tag_coverage"].items
        tagged = sum(int(r.get("tagged", 0)) for r in rows)
        coverage = round(tagged / total * 100, 1) if total else 0.0
        worst = sorted(
            (
                {
                    "resource_group": r.get("resourceGroup"),
                    "total": int(r.get("total", 0)),
                    "untagged": int(r.get("total", 0)) - int(r.get("tagged", 0)),
                }
                for r in rows
            ),
            key=lambda x: x["untagged"],
            reverse=True,
        )
        data["tag_coverage"] = {
            "tag_key": args.tag_key,
            "tagged": tagged,
            "untagged": total - tagged,
            "coverage_pct": coverage,
            "worst_resource_groups": [w for w in worst if w["untagged"] > 0][: args.top_n],
        }
        answer += f" {coverage}% of resources have the '{args.tag_key}' tag ({total - tagged} untagged)."
        caveats.append(f"Tag keys are matched exactly as written ('{args.tag_key}'), case-sensitively.")
    truncated = any(r.truncated for r in results)
    if truncated:
        caveats.append("Some Resource Graph results were truncated.")

    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if truncated else ResponseStatus.OK,
        answer=answer,
        confidence=common.MEDIUM if truncated else common.HIGH,
        data=data,
        query_used=query_used,
        # Resource Graph is live control-plane data: when Azure answered is when it was true.
        data_timestamp=min(r.invoked_at for r in results),
        sources=sources,
        caveats=caveats,
    )
