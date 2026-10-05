"""Platform-admin tools (``CRIP.PlatformAdmin`` only, enforced in tools.registry).

* ``platform_access_review``: who holds which Azure role on a subscription
  (including inherited from management groups), with names from Microsoft Graph,
  and the findings a platform team cares about: owner count, direct user grants
  of privileged roles, guests with privileged roles, grants to deleted identities.
* ``platform_estate_overview``: one row per subscription in scope:
  month-to-date cost, secure score, Advisor recommendation counts.
* ``platform_crip_usage``: who used CRIP, how, and what was denied (CRIP's own audit log).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..access.graph import GraphError
from ..access.model import BUILTIN_ROLES
from ..azure_clients import cost_management as cm
from ..azure_clients import resource_graph
from ..azure_clients.arm import AzureApiError, InvalidScopeError, split_subscription_scope
from ..contracts import AgentResponse, ResponseStatus, Source
from . import common
from .context import ToolContext

AGENT = "platform"
ACCESS_REVIEW = "platform_access_review"
ESTATE = "platform_estate_overview"
USAGE = "platform_crip_usage"

PRIVILEGED = frozenset({"Owner", "Contributor", "User Access Administrator", "Role Based Access Control Administrator"})
_RA_API = "2022-04-01"


class AccessReviewArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)


class EstateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UsageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=7, ge=1, le=90)


# --------------------------------------------------------------------------- access review


async def access_review(args: AccessReviewArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, _rg = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{sub}"
    arm = ctx.arm
    ra_url = arm.url(f"{scope}/providers/Microsoft.Authorization/roleAssignments", _RA_API, "&$filter=atScope()")
    rd_url = arm.url(f"{scope}/providers/Microsoft.Authorization/roleDefinitions", _RA_API)
    query_used = f"GET {ra_url}\n\nGET {rd_url}\n\nPOST https://graph.microsoft.com/v1.0/directoryObjects/getByIds"
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        assignments, definitions = await asyncio.gather(arm.request("GET", ra_url, token), arm.request("GET", rd_url, token))
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=ACCESS_REVIEW, scope=scope, query_used=query_used, invoked_at=invoked_at, what="role assignments")
    sources: list[Source] = [common.source(assignments, tool=ACCESS_REVIEW, scope=scope), common.source(definitions, tool=ACCESS_REVIEW, scope=scope)]
    role_names = {str(d.get("name", "")).lower(): (d.get("properties") or {}).get("roleName") for d in definitions.items}

    principal_ids = [str((a.get("properties") or {}).get("principalId", "")) for a in assignments.items]
    principals: dict[str, dict[str, Any]] = {}
    caveats = ["Includes assignments inherited from management groups and the root; resource-group-level grants are not listed."]
    status = ResponseStatus.OK
    if ctx.graph is None:
        status = ResponseStatus.PARTIAL
        caveats.append("Names unavailable: Microsoft Graph is not configured for CRIP.")
    else:
        graph_at = datetime.now(UTC)
        try:
            principals = await ctx.graph.principals(principal_ids)
            sources.append(Source(tool=ACCESS_REVIEW, api="POST https://graph.microsoft.com/v1.0/directoryObjects/getByIds", invoked_at=graph_at, scope="tenant directory", http_status=200))
        except GraphError as exc:
            status = ResponseStatus.PARTIAL
            caveats.append(f"Names unavailable: Microsoft Graph returned {exc.status} (grant Directory.Read.All to CRIP's identity; see docs/access-model.md).")

    rows = []
    for a in assignments.items:
        props = a.get("properties") or {}
        pid = str(props.get("principalId", ""))
        role_id = str(props.get("roleDefinitionId", "")).rsplit("/", 1)[-1].lower()
        role = role_names.get(role_id) or BUILTIN_ROLES.get(role_id) or role_id
        assigned_scope = str(props.get("scope", ""))
        info = principals.get(pid)
        rows.append(
            {
                "principal_id": pid,
                "name": info["name"] if info else None,
                "upn": info["upn"] if info else None,
                "principal_type": (info or {}).get("type") or props.get("principalType"),
                "guest": bool(info and info.get("user_type") == "Guest"),
                "role": role,
                "scope": assigned_scope,
                "inherited": assigned_scope.lower() != scope.lower(),
                # Graph was reachable but did not find the principal: the identity was deleted.
                "orphaned": bool(principals) and info is None,
            }
        )
    rows.sort(key=lambda r: (r["role"] not in PRIVILEGED, r["role"], r["name"] or r["principal_id"]))

    owners = [r for r in rows if r["role"] == "Owner"]
    direct_user_priv = [r for r in rows if r["role"] in PRIVILEGED and str(r["principal_type"]).lower() == "user"]
    guest_priv = [r for r in rows if r["role"] in PRIVILEGED and r["guest"]]
    orphaned = [r for r in rows if r["orphaned"]]
    findings = [
        {"finding": "Owners on the subscription", "severity": "High" if len(owners) > 3 else "Info", "count": len(owners),
         "advice": "Keep Owners to a small, named set (Microsoft recommends no more than 3)."},
        {"finding": "Privileged roles granted directly to users", "severity": "Medium" if direct_user_priv else "Info", "count": len(direct_user_priv),
         "advice": "Grant privileged roles to groups (ideally via PIM), not individual users."},
        {"finding": "Guest users with privileged roles", "severity": "High" if guest_priv else "Info", "count": len(guest_priv),
         "advice": "Review external accounts with Owner/Contributor access."},
        {"finding": "Assignments to deleted identities", "severity": "Low" if orphaned else "Info", "count": len(orphaned),
         "advice": "Remove role assignments whose principal no longer exists."},
    ]
    return AgentResponse(
        agent=AGENT,
        status=status,
        answer=f"{len(rows)} role assignment(s) apply to {scope}: {len(owners)} Owner(s), {len(direct_user_priv)} privileged direct user grant(s), "
        f"{len(guest_priv)} guest(s) with privileged roles, {len(orphaned)} assignment(s) to deleted identities.",
        confidence=common.MEDIUM if status is ResponseStatus.PARTIAL else common.HIGH,
        data={"kind": "access_review", "scope": scope, "assignment_count": len(rows), "findings": findings, "assignments": rows[:200]},
        query_used=query_used,
        data_timestamp=assignments.invoked_at,  # live control-plane data
        sources=sources,
        caveats=caveats,
    )


# --------------------------------------------------------------------------- estate overview

ADVISOR_COUNTS_KQL = """advisorresources
| where type == 'microsoft.advisor/recommendations'
| summarize n = count() by subscriptionId, category = tostring(properties.category), impact = tostring(properties.impact)"""


async def estate_overview(args: EstateArgs, ctx: ToolContext) -> AgentResponse:
    subs = list((ctx.access.subscriptions if ctx.access else {}).values())
    if not subs:
        return common.invalid_scope(AGENT, "No subscriptions are in CRIP's scope.")
    sub_ids = [s.subscription_id for s in subs]
    arm = ctx.arm
    mg = ctx.management_group_id
    cost_scope = f"/providers/Microsoft.Management/managementGroups/{mg}" if mg else None
    cost_body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": arm.metric_column, "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "SubscriptionId"}],
        },
    }
    api = f"POST {resource_graph.resources_url(arm)}"
    query_used = "\n\n".join(
        [f"{api}\n{json.dumps({'subscriptions': sub_ids, 'query': q})}" for q in (ADVISOR_COUNTS_KQL, _ESTATE_SCORE_KQL)]
        + ([f"POST {cm.query_url(arm, cost_scope)}\n{json.dumps(cost_body, sort_keys=True)}"] if cost_scope else [])
    )
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        advisor_counts, scores = await asyncio.gather(
            resource_graph.query(arm, subscriptions=sub_ids, kql=ADVISOR_COUNTS_KQL, token=token),
            resource_graph.query(arm, subscriptions=sub_ids, kql=_ESTATE_SCORE_KQL, token=token),
        )
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=ESTATE, scope="estate", query_used=query_used, invoked_at=invoked_at, what="estate overview")
    sources = [common.source(advisor_counts, tool=ESTATE, scope="estate"), common.source(scores, tool=ESTATE, scope="estate")]
    caveats = ["Secure score and Advisor counts are live; cost is month-to-date actual (lags 8-24 hours)."]
    status = ResponseStatus.OK

    costs: dict[str, tuple[float, str]] = {}
    if cost_scope:
        try:
            cost = await cm.query(arm, scope=cost_scope, body=cost_body, token=token)
            sources.append(common.source(cost, tool=ESTATE, scope=cost_scope))
            index = common.column_index(cm.columns(cost))
            ci, si, cur = common.cost_column(index), index.get("subscriptionid"), index.get("currency")
            for row in cost.items:
                if ci is not None and si is not None:
                    costs[str(row[si]).lower()] = (round(float(row[ci] or 0), 2), str(row[cur]) if cur is not None else "")
        except AzureApiError as exc:
            status = ResponseStatus.PARTIAL
            caveats.append(f"Cost by subscription unavailable ({exc.status_code} {exc.code}).")
    else:
        caveats.append("Set CRIP_MANAGEMENT_GROUP_ID to include month-to-date cost per subscription.")

    advisor: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in advisor_counts.items:
        sid = str(r.get("subscriptionId", "")).lower()
        advisor[sid][str(r.get("category"))] += int(r.get("n", 0))
        if r.get("impact") == "High":
            advisor[sid]["high_impact"] += int(r.get("n", 0))
    score_by_sub = {str(r.get("subscriptionId", "")).lower(): r.get("percentage") for r in scores.items}

    rows = []
    for s in subs:
        sid = s.subscription_id
        a = advisor.get(sid, {})
        pct = score_by_sub.get(sid)
        rows.append(
            {
                "subscription_id": sid,
                "name": s.display_name,
                "cost_mtd": costs.get(sid, (None, None))[0],
                "currency": costs.get(sid, (None, None))[1],
                "secure_score_pct": round(float(pct) * 100, 1) if pct is not None else None,
                "advisor_cost": a.get("Cost", 0),
                "advisor_security": a.get("Security", 0),
                "advisor_reliability": a.get("HighAvailability", 0),
                "advisor_high_impact": a.get("high_impact", 0),
            }
        )
    rows.sort(key=lambda r: r["cost_mtd"] or 0, reverse=True)
    total_by_currency: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["cost_mtd"] is not None:
            total_by_currency[r["currency"]] += r["cost_mtd"]
    timestamps = [x.invoked_at for x in (advisor_counts, scores)]
    return AgentResponse(
        agent=AGENT,
        status=status,
        answer=f"{len(rows)} subscription(s) in scope; "
        + (", ".join(f"{common.money(v)} {c}" for c, v in total_by_currency.items()) + " month-to-date; " if total_by_currency else "")
        + f"{sum(r['advisor_high_impact'] for r in rows)} high-impact Advisor recommendation(s) across the estate.",
        confidence=common.MEDIUM if status is ResponseStatus.PARTIAL else common.HIGH,
        data={"kind": "estate", "subscription_count": len(rows), "total_cost_mtd_by_currency": {c: round(v, 2) for c, v in total_by_currency.items()}, "subscriptions": rows},
        query_used=query_used,
        data_timestamp=min(timestamps),
        sources=sources,
        caveats=caveats,
    )


_ESTATE_SCORE_KQL = """securityresources
| where type == 'microsoft.security/securescores' and name == 'ascScore'
| project subscriptionId, percentage = todouble(properties.score.percentage)"""


# --------------------------------------------------------------------------- CRIP usage


async def crip_usage(args: UsageArgs, ctx: ToolContext) -> AgentResponse:
    if ctx.repository is None:
        return common.invalid_scope(AGENT, "CRIP's audit store is not available to this tool.")
    since = datetime.now(UTC) - timedelta(days=args.days)
    invoked_at = datetime.now(UTC)
    report = await ctx.repository.usage(since=since, limit=100)
    src = Source(tool=USAGE, api="CRIP audit store: access_log, sessions, messages", invoked_at=invoked_at, scope="crip")
    if not report["events"]:
        return AgentResponse(
            agent=AGENT, status=ResponseStatus.NO_DATA, answer=f"No CRIP usage was recorded in the last {args.days} day(s).",
            confidence=common.HIGH, query_used=f"usage since {since.isoformat()}", sources=[src],
        )
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.OK,
        answer=f"In the last {args.days} day(s): {report['distinct_users']} user(s), {report['total_events']} request(s), "
        f"{report['denied']} denied. Most active: " + ", ".join(f"{u['user']} ({u['events']})" for u in report["top_users"][:3]) + ".",
        confidence=common.HIGH,
        data={"kind": "usage", "days": args.days, **report},
        query_used=f"usage since {since.isoformat()}",
        data_timestamp=invoked_at,
        sources=[src],
    )
