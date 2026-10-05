"""Governance tools: security, reliability, network and policy posture of a subscription.

Available at the RESOURCES access level (no cost data), so Readers get real
value too. All checks are deterministic queries against Azure's own data:

* ``governance_advisor_recommendations``: Advisor beyond cost (security,
  reliability, operational excellence, performance).
* ``governance_security_posture``: Defender for Cloud secure score and the
  unhealthy assessments affecting the most resources.
* ``governance_network_posture``: Resource Graph checks: management ports open
  to the internet, subnets without an NSG, storage reachable from all networks,
  VNet peerings not connected, public IPs.
* ``governance_policy_compliance``: Azure Policy non-compliance by assignment.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..azure_clients import advisor, resource_graph
from ..azure_clients.arm import AzureApiError, InvalidScopeError, split_subscription_scope
from ..contracts import AgentResponse, ResponseStatus
from . import common
from .context import ToolContext

AGENT = "governance"
ADVISOR = "governance_advisor_recommendations"
SECURITY = "governance_security_posture"
NETWORK = "governance_network_posture"
POLICY = "governance_policy_compliance"

_POLICY_API = "2019-10-01"
_POLICY_ASSIGNMENTS_API = "2022-06-01"


class AdvisorCategory(StrEnum):
    ALL = "all"  # every non-cost category
    SECURITY = "security"
    RELIABILITY = "reliability"
    OPERATIONAL_EXCELLENCE = "operational_excellence"
    PERFORMANCE = "performance"


class AdvisorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    category: AdvisorCategory = AdvisorCategory.ALL
    top_n: int = Field(default=15, ge=1, le=50)


class ScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)


def _scope(args_scope: str) -> tuple[str, str | None, str]:
    sub, rg = split_subscription_scope(args_scope)
    return sub, rg, f"/subscriptions/{sub}" + (f"/resourceGroups/{rg}" if rg else "")


# --------------------------------------------------------------------------- Advisor (non-cost)

_IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}


async def advisor_recommendations(args: AdvisorArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, rg, scope = _scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    query_used = f"GET {advisor.recommendations_url(arm, sub, None)}"
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        result = await advisor.recommendations(arm, subscription_id=sub, token=token, category=None)
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=ADVISOR, scope=scope, query_used=query_used, invoked_at=invoked_at, what="Advisor recommendations")
    src = common.source(result, tool=ADVISOR, scope=scope)

    recs = []
    for item in result.items:
        props = item.get("properties", {})
        category = advisor.CATEGORIES.get(str(props.get("category")), "other")
        if category == "cost":
            continue  # cost lives in the Optimizer agent (needs the cost access level)
        if args.category is not AdvisorCategory.ALL and category != args.category.value:
            continue
        resource_id = (props.get("resourceMetadata") or {}).get("resourceId") or ""
        if rg and f"/resourcegroups/{rg.lower()}/" not in resource_id.lower() + "/":
            continue
        recs.append(
            {
                "category": category,
                "impact": props.get("impact"),
                "problem": (props.get("shortDescription") or {}).get("problem"),
                "solution": (props.get("shortDescription") or {}).get("solution"),
                "resource": props.get("impactedValue"),
                "resource_type": props.get("impactedField"),
                "last_updated": props.get("lastUpdated"),
            }
        )
    if not recs:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Azure Advisor has no {args.category.value.replace('_', ' ')} recommendations for {scope}.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=[src],
        )
    recs.sort(key=lambda r: (_IMPACT_ORDER.get(r["impact"], 3), r["category"]))
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"High": 0, "Medium": 0, "Low": 0})
    for r in recs:
        by_category[r["category"]][r["impact"] or "Low"] = by_category[r["category"]].get(r["impact"] or "Low", 0) + 1
    updated = [d for d in (_dt(r["last_updated"]) for r in recs) if d]
    high = sum(1 for r in recs if r["impact"] == "High")
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if result.truncated else ResponseStatus.OK,
        answer=f"Azure Advisor lists {len(recs)} non-cost recommendation(s) for {scope}, {high} of them high impact. "
        + "Top: " + "; ".join(f"[{r['impact']}] {r['problem']} ({r['resource']})" for r in recs[:3]) + ".",
        confidence=common.MEDIUM if result.truncated else common.HIGH,
        data={
            "kind": "advisor_posture",
            "scope": scope,
            "total": len(recs),
            "high_impact": high,
            "by_category": [{"category": c, **counts, "total": sum(counts.values())} for c, counts in sorted(by_category.items())],
            "recommendations": recs[: args.top_n],
        },
        query_used=query_used,
        data_timestamp=max(updated) if updated else result.invoked_at,
        sources=[src],
        caveats=[] if updated else ["Advisor did not report assessment times; the timestamp is the call time."],
    )


# --------------------------------------------------------------------------- security posture

SECURE_SCORE_KQL = """securityresources
| where type == 'microsoft.security/securescores' and name == 'ascScore'
| project subscriptionId, current = todouble(properties.score.current), max = todouble(properties.score.max), percentage = todouble(properties.score.percentage)"""

UNHEALTHY_KQL = """securityresources
| where type == 'microsoft.security/assessments' and tostring(properties.status.code) == 'Unhealthy'
{rg_filter}| summarize resources = count() by recommendation = tostring(properties.displayName), severity = tostring(properties.metadata.severity)
| order by resources desc"""


async def security_posture(args: ScopeArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, rg, scope = _scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    rg_filter = f"| where resourceGroup =~ '{rg}'\n" if rg else ""
    unhealthy_kql = UNHEALTHY_KQL.format(rg_filter=rg_filter)
    api = f"POST {resource_graph.resources_url(arm)}"
    query_used = "\n\n".join(f"{api}\n{json.dumps({'subscriptions': [sub], 'query': q})}" for q in (SECURE_SCORE_KQL, unhealthy_kql))
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        score, unhealthy = await asyncio.gather(
            resource_graph.query(arm, subscriptions=[sub], kql=SECURE_SCORE_KQL, token=token),
            resource_graph.query(arm, subscriptions=[sub], kql=unhealthy_kql, token=token),
        )
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=SECURITY, scope=scope, query_used=query_used, invoked_at=invoked_at, what="security posture")
    sources = [common.source(score, tool=SECURITY, scope=scope), common.source(unhealthy, tool=SECURITY, scope=scope)]
    score_row = score.items[0] if score.items else None
    findings = [
        {"recommendation": r.get("recommendation"), "severity": r.get("severity") or "Unknown", "resources": int(r.get("resources", 0))}
        for r in unhealthy.items
    ]
    if score_row is None and not findings:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Defender for Cloud returned no secure score or assessments for {scope} (it may not be enabled).",
            confidence=common.HIGH,
            query_used=query_used,
            sources=sources,
        )
    by_severity: dict[str, int] = defaultdict(int)
    for f in findings:
        by_severity[f["severity"]] += f["resources"]
    caveats = ["Defender for Cloud recalculates the secure score periodically (typically several times a day)."]
    if score_row is None:
        caveats.append("No secure score was returned (Defender for Cloud's foundational CSPM may be off); assessments are shown.")
    pct = round(float(score_row["percentage"]) * 100, 1) if score_row and score_row.get("percentage") is not None else None
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if score_row is None else ResponseStatus.OK,
        answer=(
            (f"Secure score for {scope}: {pct}% ({score_row['current']} of {score_row['max']}). " if pct is not None else "")
            + f"{len(findings)} unhealthy recommendation(s); top: "
            + "; ".join(f"[{f['severity']}] {f['recommendation']} ({f['resources']} resources)" for f in findings[:3])
            + "."
        ),
        confidence=common.MEDIUM if score_row is None else common.HIGH,
        data={
            "kind": "security_posture",
            "scope": scope,
            "secure_score_pct": pct,
            "secure_score_current": score_row.get("current") if score_row else None,
            "secure_score_max": score_row.get("max") if score_row else None,
            "unhealthy_by_severity": dict(by_severity),
            "findings": findings[:25],
        },
        query_used=query_used,
        data_timestamp=min(s.invoked_at for s in (score, unhealthy)),
        sources=sources,
        caveats=caveats,
    )


# --------------------------------------------------------------------------- network posture

_MGMT_PORTS = "dynamic(['22','3389','*'])"
NETWORK_CHECKS: dict[str, tuple[str, str, str]] = {
    # key: (title, severity, KQL)
    "open_management_ports": (
        "Management ports (SSH/RDP) open to the internet",
        "High",
        f"""Resources
| where type =~ 'microsoft.network/networksecuritygroups'
{{rg}}| mv-expand rule = properties.securityRules
| where tostring(rule.properties.direction) =~ 'Inbound' and tostring(rule.properties.access) =~ 'Allow'
| extend src = tostring(rule.properties.sourceAddressPrefix), port = tostring(rule.properties.destinationPortRange), ports = rule.properties.destinationPortRanges
| where src in~ ('*', '0.0.0.0/0', 'Internet', 'Any')
| where port in ({_MGMT_PORTS}) or ports has '22' or ports has '3389'
| project name = strcat(name, ' / ', tostring(rule.name)), resourceGroup, detail = strcat('from ', src, ' to port ', iff(isempty(port), tostring(ports), port))""",
    ),
    "subnets_without_nsg": (
        "Subnets without a network security group",
        "Medium",
        """Resources
| where type =~ 'microsoft.network/virtualnetworks'
{rg}| mv-expand subnet = properties.subnets
| where isnull(subnet.properties.networkSecurityGroup)
| where tostring(subnet.name) !in~ ('GatewaySubnet', 'AzureFirewallSubnet', 'AzureFirewallManagementSubnet', 'AzureBastionSubnet', 'RouteServerSubnet')
| project name = strcat(name, ' / ', tostring(subnet.name)), resourceGroup, detail = tostring(subnet.properties.addressPrefix)""",
    ),
    "storage_open_to_all_networks": (
        "Storage accounts reachable from all networks",
        "Medium",
        """Resources
| where type =~ 'microsoft.storage/storageaccounts'
{rg}| where tostring(properties.publicNetworkAccess) !~ 'Disabled' and tostring(properties.networkAcls.defaultAction) =~ 'Allow'
| project name, resourceGroup, detail = strcat('allowBlobPublicAccess=', tostring(properties.allowBlobPublicAccess))""",
    ),
    "peerings_not_connected": (
        "VNet peerings not in Connected state",
        "Medium",
        """Resources
| where type =~ 'microsoft.network/virtualnetworks'
{rg}| mv-expand peering = properties.virtualNetworkPeerings
| where isnotempty(peering) and tostring(peering.properties.peeringState) !~ 'Connected'
| project name = strcat(name, ' / ', tostring(peering.name)), resourceGroup, detail = tostring(peering.properties.peeringState)""",
    ),
    "public_ip_addresses": (
        "Public IP addresses (review exposure)",
        "Info",
        """Resources
| where type =~ 'microsoft.network/publicipaddresses'
{rg}| project name, resourceGroup, detail = strcat(tostring(properties.ipAddress), iff(isnull(properties.ipConfiguration), ' (unassociated)', ''))""",
    ),
}


async def network_posture(args: ScopeArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, rg, scope = _scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    rg_filter = f"| where resourceGroup =~ '{rg}'\n" if rg else ""
    queries = {key: kql.format(rg=rg_filter) for key, (_, _, kql) in NETWORK_CHECKS.items()}
    api = f"POST {resource_graph.resources_url(arm)}"
    query_used = "\n\n".join(f"{api}\n{json.dumps({'subscriptions': [sub], 'query': q})}" for q in queries.values())
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        results = await asyncio.gather(*(resource_graph.query(arm, subscriptions=[sub], kql=q, token=token) for q in queries.values()))
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=NETWORK, scope=scope, query_used=query_used, invoked_at=invoked_at, what="network posture")
    findings = []
    for (key, (title, severity, _)), result in zip(NETWORK_CHECKS.items(), results, strict=True):
        findings.append(
            {
                "check": key,
                "title": title,
                "severity": severity,
                "count": len(result.items),
                "items": [{"name": r.get("name"), "resource_group": r.get("resourceGroup"), "detail": r.get("detail")} for r in result.items[:20]],
            }
        )
    issues = [f for f in findings if f["count"] and f["severity"] != "Info"]
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if any(r.truncated for r in results) else ResponseStatus.OK,
        answer=(
            f"Network posture for {scope}: "
            + ("; ".join(f"{f['count']} x {f['title'].lower()}" for f in issues) if issues else "no issues found by the checks")
            + f". {next(f['count'] for f in findings if f['check'] == 'public_ip_addresses')} public IP address(es)."
        ),
        confidence=common.HIGH,
        data={"kind": "network_posture", "scope": scope, "issue_count": sum(f["count"] for f in issues), "findings": findings},
        query_used=query_used,
        data_timestamp=min(r.invoked_at for r in results),
        sources=[common.source(r, tool=NETWORK, scope=scope) for r in results],
        caveats=["Checks are heuristics on configuration; some findings may be intentional (e.g. a bastion-less jump host)."],
    )


# --------------------------------------------------------------------------- policy

async def policy_compliance(args: ScopeArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, rg, scope = _scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    summarize_url = arm.url(f"{scope}/providers/Microsoft.PolicyInsights/policyStates/latest/summarize", _POLICY_API)
    assignments_url = arm.url(f"/subscriptions/{sub}/providers/Microsoft.Authorization/policyAssignments", _POLICY_ASSIGNMENTS_API)
    query_used = f"POST {summarize_url}\n\nGET {assignments_url}"
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        summary, assignments = await asyncio.gather(
            arm.request("POST", summarize_url, token, page_items=lambda p: list(p.get("value", []))),
            arm.request("GET", assignments_url, token),
        )
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=POLICY, scope=scope, query_used=query_used, invoked_at=invoked_at, what="policy compliance")
    sources = [common.source(summary, tool=POLICY, scope=scope), common.source(assignments, tool=POLICY, scope=f"/subscriptions/{sub}")]
    names = {str(a.get("id", "")).lower(): (a.get("properties") or {}).get("displayName") or a.get("name") for a in assignments.items}
    first = summary.items[0] if summary.items else {}
    total_nc = int((first.get("results") or {}).get("nonCompliantResources", 0))
    rows = []
    for pa in first.get("policyAssignments", []) or []:
        nc = int((pa.get("results") or {}).get("nonCompliantResources", 0))
        if nc:
            pid = str(pa.get("policyAssignmentId", "")).lower()
            rows.append({"assignment": names.get(pid) or pid.rsplit("/", 1)[-1], "non_compliant_resources": nc})
    rows.sort(key=lambda r: r["non_compliant_resources"], reverse=True)
    if not first:
        return AgentResponse(
            agent=AGENT, status=ResponseStatus.NO_DATA, answer=f"Azure Policy returned no compliance summary for {scope}.",
            confidence=common.HIGH, query_used=query_used, sources=sources,
        )
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.OK,
        answer=f"{total_nc} resource(s) in {scope} are non-compliant with Azure Policy"
        + (": " + "; ".join(f"{r['assignment']} ({r['non_compliant_resources']})" for r in rows[:3]) if rows else "") + ".",
        confidence=common.HIGH,
        data={"kind": "policy_compliance", "scope": scope, "non_compliant_resources": total_nc, "assignment_count": len(assignments.items), "by_assignment": rows[:20]},
        query_used=query_used,
        data_timestamp=summary.invoked_at,
        sources=sources,
        caveats=["Azure Policy evaluates compliance periodically (about every 24 hours) and after changes; very recent changes may not be reflected."],
    )


def _dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    return parsed if parsed and parsed.tzinfo else None

