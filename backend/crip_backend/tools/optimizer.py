"""Optimizer tools: where the user can save money, grounded in Azure's own data.

* ``optimizer_advisor_recommendations``: Azure Advisor cost recommendations,
  with the savings figures *Advisor* estimated (CRIP does not estimate).
* ``optimizer_find_idle_resources``: resources that typically cost money while
  doing nothing (unattached disks, unassociated public IPs, orphaned NICs, empty
  App Service plans, VMs stopped but not deallocated), found with Azure Resource
  Graph and then priced with their *actual* month-to-date cost from Cost Management.

Same rules as every CRIP tool: user's OBO token only, real sources, honest
``partial``/``no_data``/``error``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..azure_clients import advisor, resource_graph
from ..azure_clients import cost_management as cm
from ..azure_clients.arm import AzureApiError, InvalidScopeError, split_subscription_scope
from ..contracts import AgentResponse, ResponseStatus, Source
from . import common
from .context import ToolContext

AGENT = "optimizer"
ADVISOR = "optimizer_advisor_recommendations"
IDLE = "optimizer_find_idle_resources"

# Cost Management's In-filter is not meant for thousands of values; cap the enrichment.
_MAX_PRICED_RESOURCES = 200


class AdvisorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    top_n: int = Field(default=10, ge=1, le=50)


class IdleResourcesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    include_cost: bool = True


# --------------------------------------------------------------------------- Advisor


async def advisor_recommendations(args: AdvisorArgs, ctx: ToolContext) -> AgentResponse:
    try:
        subscription_id, resource_group = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{subscription_id}" + (f"/resourceGroups/{resource_group}" if resource_group else "")
    arm = ctx.arm
    query_used = f"GET {advisor.recommendations_url(arm, subscription_id)}"
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        result = await advisor.cost_recommendations(arm, subscription_id=subscription_id, token=token)
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=ADVISOR, scope=scope, query_used=query_used, invoked_at=invoked_at, what="Advisor recommendations"
        )
    src = common.source(result, tool=ADVISOR, scope=scope)

    recs = []
    for item in result.items:
        props = item.get("properties", {})
        resource_id = (props.get("resourceMetadata") or {}).get("resourceId") or ""
        if resource_group and f"/resourcegroups/{resource_group.lower()}/" not in resource_id.lower() + "/":
            continue
        ext = props.get("extendedProperties") or {}
        recs.append(
            {
                "problem": (props.get("shortDescription") or {}).get("problem"),
                "solution": (props.get("shortDescription") or {}).get("solution"),
                "impact": props.get("impact"),
                "resource": props.get("impactedValue"),
                "resource_type": props.get("impactedField"),
                "resource_id": resource_id or None,
                "annual_savings": _float(ext.get("annualSavingsAmount")),
                "monthly_savings": _float(ext.get("savingsAmount")),
                "currency": ext.get("savingsCurrency"),
                "last_updated": props.get("lastUpdated"),
            }
        )

    if not recs:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Azure Advisor has no cost recommendations for {scope}.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=[src],
            caveats=["Advisor refreshes recommendations roughly daily; new resources may not be assessed yet."],
        )

    updated = [d for d in (_parse_dt(r["last_updated"]) for r in recs) if d]
    # Advisor's own assessment time is when this data was true; fall back to the
    # call time only if Advisor omitted it (and say so).
    data_timestamp = max(updated) if updated else result.invoked_at
    savings_by_currency: dict[str, float] = defaultdict(float)
    for r in recs:
        if r["annual_savings"] is not None:
            savings_by_currency[r["currency"] or "unknown"] += r["annual_savings"]
    by_problem: dict[str, dict[str, Any]] = {}
    for r in recs:
        entry = by_problem.setdefault(r["problem"] or "Other", {"problem": r["problem"] or "Other", "count": 0, "annual_savings": 0.0, "currency": r["currency"]})
        entry["count"] += 1
        entry["annual_savings"] += r["annual_savings"] or 0.0
    ranked = sorted(recs, key=lambda r: r["annual_savings"] or 0.0, reverse=True)

    caveats = ["Savings amounts are Azure Advisor's estimates, not CRIP's, and assume the recommendation is applied."]
    if not updated:
        caveats.append("Advisor did not report when these recommendations were last assessed; the timestamp is the call time.")
    if result.truncated:
        caveats.append(f"Recommendations were truncated after {result.pages} pages.")
    missing = sum(1 for r in recs if r["annual_savings"] is None)
    if missing:
        caveats.append(f"{missing} recommendation(s) have no savings estimate from Advisor and are not included in the total.")

    savings_text = ", ".join(f"{common.money(v)} {c}" for c, v in savings_by_currency.items()) or "no quantified savings"
    top_text = "; ".join(
        f"{r['problem']} ({r['resource']}" + (f", {common.money(r['annual_savings'])} {r['currency']}/yr" if r["annual_savings"] else "") + ")"
        for r in ranked[:3]
    )
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if result.truncated else ResponseStatus.OK,
        answer=f"Azure Advisor lists {len(recs)} cost recommendation(s) for {scope}, estimated annual savings {savings_text}. Top: {top_text}.",
        confidence=common.MEDIUM if result.truncated else common.HIGH,
        data={
            "kind": "savings",
            "scope": scope,
            "total_annual_savings_by_currency": {c: round(v, 2) for c, v in savings_by_currency.items()},
            "by_problem": sorted(
                ({**v, "annual_savings": round(v["annual_savings"], 2)} for v in by_problem.values()),
                key=lambda v: v["annual_savings"],
                reverse=True,
            ),
            "recommendations": ranked[: args.top_n],
            "recommendation_count": len(recs),
        },
        query_used=query_used,
        data_timestamp=data_timestamp,
        sources=[src],
        caveats=caveats,
    )


# --------------------------------------------------------------------------- idle resources

IDLE_KQL = """Resources
{rg_filter}| where (type =~ 'microsoft.compute/disks' and tostring(properties.diskState) =~ 'Unattached')
    or (type =~ 'microsoft.network/publicipaddresses' and isnull(properties.ipConfiguration) and isnull(properties.natGateway))
    or (type =~ 'microsoft.network/networkinterfaces' and isnull(properties.virtualMachine) and isnull(properties.privateEndpoint))
    or (type =~ 'microsoft.web/serverfarms' and toint(properties.numberOfSites) == 0)
    or (type =~ 'microsoft.compute/virtualmachines' and tostring(properties.extended.instanceView.powerState.code) =~ 'PowerState/stopped')
| extend category = case(
    type =~ 'microsoft.compute/disks', 'Unattached managed disk',
    type =~ 'microsoft.network/publicipaddresses', 'Unassociated public IP address',
    type =~ 'microsoft.network/networkinterfaces', 'Orphaned network interface',
    type =~ 'microsoft.web/serverfarms', 'Empty App Service plan',
    'VM stopped but not deallocated (compute still billed)')
| project id, name, type, resourceGroup, location, category, sku = tostring(sku.name)
| order by category asc, name asc"""


def build_idle_kql(resource_group: str | None) -> str:
    # resource_group already passed scope validation ([-\w.()] only), so it cannot break out of the string.
    rg_filter = f"| where resourceGroup =~ '{resource_group}'\n" if resource_group else ""
    return IDLE_KQL.format(rg_filter=rg_filter)


def build_resource_cost_body(resource_ids: list[str], *, metric_column: str) -> dict[str, Any]:
    return {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": metric_column, "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
            "filter": {"dimensions": {"name": "ResourceId", "operator": "In", "values": resource_ids}},
        },
    }


async def find_idle_resources(args: IdleResourcesArgs, ctx: ToolContext) -> AgentResponse:
    try:
        subscription_id, resource_group = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{subscription_id}" + (f"/resourceGroups/{resource_group}" if resource_group else "")
    arm = ctx.arm
    kql = build_idle_kql(resource_group)
    rg_api = f"POST {resource_graph.resources_url(arm)}"
    query_used = f"{rg_api}\n{json.dumps({'subscriptions': [subscription_id], 'query': kql})}"
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        graph = await resource_graph.query(arm, subscriptions=[subscription_id], kql=kql, token=token)
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=IDLE, scope=scope, query_used=query_used, invoked_at=invoked_at, what="resource inventory"
        )
    sources: list[Source] = [common.source(graph, tool=IDLE, scope=scope)]
    resources = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "category": r.get("category"),
            "resource_group": r.get("resourceGroup"),
            "location": r.get("location"),
            "sku": r.get("sku") or None,
            "cost_mtd": None,
            "currency": None,
        }
        for r in graph.items
    ]
    if not resources:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"No idle resources of the checked kinds were found in {scope}.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=sources,
            caveats=[_CHECKED_KINDS_CAVEAT],
        )

    caveats = [_CHECKED_KINDS_CAVEAT, "Resource Graph shows only resources your account can read."]
    partial = graph.truncated
    if graph.truncated:
        caveats.append(f"Resource Graph results were truncated after {graph.pages} pages.")
    data_timestamp: datetime = graph.invoked_at  # live control-plane data
    cost_by_currency: dict[str, float] = defaultdict(float)
    full_query_used = query_used

    if args.include_cost:
        ids = [r["id"].lower() for r in resources if r["id"]][:_MAX_PRICED_RESOURCES]
        if len(resources) > _MAX_PRICED_RESOURCES:
            caveats.append(f"Only the first {_MAX_PRICED_RESOURCES} resources were priced.")
            partial = True
        body = build_resource_cost_body(ids, metric_column=arm.metric_column)
        cost_api = f"POST {cm.query_url(arm, f'/subscriptions/{subscription_id}')}"
        full_query_used = f"{query_used}\n\n{cost_api}\n{json.dumps(body, sort_keys=True)}"
        cost_invoked = arm.clock()
        try:
            costs = await cm.query(arm, scope=f"/subscriptions/{subscription_id}", body=body, token=token)
        except AzureApiError as exc:
            partial = True
            caveats.append(f"Month-to-date cost could not be retrieved for these resources ({exc.status_code} {exc.code}); they are listed unpriced.")
            sources.append(
                Source(tool=IDLE, api=exc.api, invoked_at=cost_invoked, scope=f"/subscriptions/{subscription_id}", request_id=exc.request_id, http_status=exc.status_code)
            )
        else:
            sources.append(common.source(costs, tool=IDLE, scope=f"/subscriptions/{subscription_id}"))
            per_resource, latest = _cost_per_resource(costs)
            for r in resources:
                hit = per_resource.get((r["id"] or "").lower())
                if hit:
                    r["cost_mtd"], r["currency"] = round(hit[0], 2), hit[1]
                    cost_by_currency[hit[1]] += hit[0]
            if latest:
                # The answer mixes live inventory with lagged billing data: the
                # older of the two is when the *whole* answer was true.
                data_timestamp = min(data_timestamp, common.day_start(latest))
                caveats.append(f"Costs are month-to-date actual cost through {latest.isoformat()} (UTC). {common.COST_LATENCY_CAVEAT}")
            else:
                caveats.append("Cost Management returned no month-to-date cost for these resources (some idle resources are free, e.g. NICs).")

    resources.sort(key=lambda r: r["cost_mtd"] or 0.0, reverse=True)
    by_category: dict[str, dict[str, Any]] = {}
    for r in resources:
        entry = by_category.setdefault(r["category"], {"category": r["category"], "count": 0, "cost_mtd": 0.0})
        entry["count"] += 1
        entry["cost_mtd"] = round(entry["cost_mtd"] + (r["cost_mtd"] or 0.0), 2)

    cost_text = ", ".join(f"{common.money(v)} {c}" for c, v in cost_by_currency.items())
    summary = "; ".join(f"{v['count']} x {v['category']}" for v in by_category.values())
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if partial else ResponseStatus.OK,
        answer=(
            f"Found {len(resources)} idle resource(s) in {scope}: {summary}."
            + (f" Together they cost {cost_text} month-to-date." if cost_text else "")
        ),
        confidence=common.MEDIUM if partial else common.HIGH,
        data={
            "kind": "idle_resources",
            "scope": scope,
            "resource_count": len(resources),
            "cost_mtd_by_currency": {c: round(v, 2) for c, v in cost_by_currency.items()},
            "by_category": sorted(by_category.values(), key=lambda v: v["cost_mtd"], reverse=True),
            "resources": resources[:50],
        },
        query_used=full_query_used,
        data_timestamp=data_timestamp,
        sources=sources,
        caveats=caveats,
    )


_CHECKED_KINDS_CAVEAT = (
    "Checked kinds: unattached managed disks, unassociated public IPs, orphaned NICs, empty App Service plans, "
    "and VMs stopped without deallocation. Review before deleting: some may be kept on purpose."
)


def _cost_per_resource(result: Any) -> tuple[dict[str, tuple[float, str]], date | None]:
    index = common.column_index(cm.columns(result))
    cost_i, id_i = common.cost_column(index), index.get("resourceid")
    date_i, currency_i = index.get("usagedate"), index.get("currency")
    totals: dict[str, tuple[float, str]] = {}
    latest: date | None = None
    if cost_i is None or id_i is None:
        return totals, None
    for row in result.items:
        rid = str(row[id_i] or "").lower()
        currency = str(row[currency_i]) if currency_i is not None and row[currency_i] else "unknown"
        prev = totals.get(rid, (0.0, currency))
        totals[rid] = (prev[0] + float(row[cost_i] or 0), currency)
        if date_i is not None:
            day = common.parse_usage_date(row[date_i])
            if day and (latest is None or day > latest):
                latest = day
    return totals, latest


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None
