"""Governance (posture) and Platform (admin) tools."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from crip_backend.access.graph import GraphError
from crip_backend.access.model import AccessLevel
from crip_backend.contracts import ResponseStatus
from crip_backend.persistence.repository import AccessEvent
from crip_backend.tools import governance, platform

from .conftest import SUBSCRIPTION, InMemoryRepository, make_access

GRAPH_URL = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
ADVISOR_ALL = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01"


def by_query(mapping):
    def handler(request):
        query = json.loads(request.content)["query"]
        for needle, data in mapping.items():
            if needle in query:
                return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"data": []})
    return handler


@respx.mock
async def test_network_posture_reports_each_check(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=by_query({
        "networksecuritygroups": [{"name": "nsg-jump / allow-rdp", "resourceGroup": "rg-ops", "detail": "from * to port 3389"}],
        "isnull(subnet.properties.networkSecurityGroup)": [{"name": "vnet-a / app", "resourceGroup": "rg-net", "detail": "10.0.1.0/24"}],
        "publicipaddresses": [{"name": "pip-1", "resourceGroup": "rg-net", "detail": "20.1.2.3"}],
    }))
    result = await governance.network_posture(governance.ScopeArgs(scope=SUBSCRIPTION), tool_ctx)
    findings = {f["check"]: f for f in result.data["findings"]}
    assert result.status is ResponseStatus.OK
    assert findings["open_management_ports"]["count"] == 1 and findings["open_management_ports"]["severity"] == "High"
    assert findings["subnets_without_nsg"]["count"] == 1
    assert result.data["issue_count"] == 2  # public IPs are informational
    assert len(result.sources) == len(governance.NETWORK_CHECKS)


@respx.mock
async def test_security_posture_score_and_findings(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=by_query({
        "securescores": [{"subscriptionId": SUBSCRIPTION, "current": 31.5, "max": 50, "percentage": 0.63}],
        "assessments": [{"recommendation": "MFA should be enabled", "severity": "High", "resources": 4}],
    }))
    result = await governance.security_posture(governance.ScopeArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.data["secure_score_pct"] == 63.0
    assert result.data["findings"][0]["recommendation"] == "MFA should be enabled"


@respx.mock
async def test_advisor_posture_excludes_cost(tool_ctx):
    def rec(category, impact, problem):
        return {"properties": {"category": category, "impact": impact, "shortDescription": {"problem": problem, "solution": "fix"},
                               "impactedValue": "res", "lastUpdated": "2026-09-21T00:00:00Z", "resourceMetadata": {"resourceId": "/x"}}}
    respx.get(ADVISOR_ALL).mock(return_value=httpx.Response(200, json={"value": [
        rec("Cost", "High", "Right-size VMs"), rec("Security", "High", "Enable MFA"), rec("HighAvailability", "Medium", "Use zones"),
    ]}))
    result = await governance.advisor_recommendations(governance.AdvisorArgs(scope=SUBSCRIPTION), tool_ctx)
    problems = [r["problem"] for r in result.data["recommendations"]]
    assert problems == ["Enable MFA", "Use zones"]
    assert {c["category"] for c in result.data["by_category"]} == {"security", "reliability"}


@respx.mock
async def test_policy_compliance_maps_assignment_names(tool_ctx):
    pa = f"/subscriptions/{SUBSCRIPTION}/providers/microsoft.authorization/policyassignments/pa1"
    respx.post(url__regex=r".*policyStates/latest/summarize.*").mock(return_value=httpx.Response(200, json={"value": [
        {"results": {"nonCompliantResources": 7}, "policyAssignments": [{"policyAssignmentId": pa, "results": {"nonCompliantResources": 7}}]}]}))
    respx.get(url__regex=r".*policyAssignments\?.*").mock(return_value=httpx.Response(200, json={"value": [
        {"id": pa, "name": "pa1", "properties": {"displayName": "Require owner tag"}}]}))
    result = await governance.policy_compliance(governance.ScopeArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.data["non_compliant_resources"] == 7
    assert result.data["by_assignment"] == [{"assignment": "Require owner tag", "non_compliant_resources": 7}]


OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"


def ra(pid, role_guid, scope=f"/subscriptions/{SUBSCRIPTION}", ptype="User"):
    return {"properties": {"principalId": pid, "principalType": ptype, "scope": scope,
                           "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{role_guid}"}}


class FakeGraph:
    def __init__(self, known=None, error=None):
        self.known, self.error = known or {}, error

    async def principals(self, ids):
        if self.error:
            raise self.error
        return {i: self.known[i] for i in ids if i in self.known}


@respx.mock
async def test_access_review_names_and_findings(tool_ctx):
    respx.get(url__regex=r".*roleAssignments\?.*atScope.*").mock(return_value=httpx.Response(200, json={"value": [
        ra("u1", OWNER), ra("u2", OWNER), ra("gone", OWNER), ra("g1", OWNER, ptype="Group", scope="/providers/Microsoft.Management/managementGroups/root"),
    ]}))
    respx.get(url__regex=r".*roleDefinitions\?.*").mock(return_value=httpx.Response(200, json={"value": [{"name": OWNER, "properties": {"roleName": "Owner"}}]}))
    tool_ctx.graph = FakeGraph({
        "u1": {"name": "Asha", "type": "user", "upn": "asha@contoso.com", "user_type": "Member"},
        "u2": {"name": "Guest Gary", "type": "user", "upn": "gary_ext#EXT#@contoso.com", "user_type": "Guest"},
        "g1": {"name": "Platform Owners", "type": "group", "upn": None, "user_type": None},
    })
    result = await platform.access_review(platform.AccessReviewArgs(scope=SUBSCRIPTION), tool_ctx)
    findings = {f["finding"]: f["count"] for f in result.data["findings"]}
    assert result.status is ResponseStatus.OK
    assert findings["Owners on the subscription"] == 4
    assert findings["Guest users with privileged roles"] == 1
    assert findings["Assignments to deleted identities"] == 1
    assert any(r["inherited"] and r["name"] == "Platform Owners" for r in result.data["assignments"])


@respx.mock
async def test_access_review_without_graph_permission_is_partial(tool_ctx):
    respx.get(url__regex=r".*roleAssignments\?.*").mock(return_value=httpx.Response(200, json={"value": [ra("u1", OWNER)]}))
    respx.get(url__regex=r".*roleDefinitions\?.*").mock(return_value=httpx.Response(200, json={"value": []}))
    tool_ctx.graph = FakeGraph(error=GraphError(403, "Insufficient privileges"))
    result = await platform.access_review(platform.AccessReviewArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.PARTIAL
    assert any("Directory.Read.All" in c for c in result.caveats)
    assert result.data["assignments"][0]["role"] == "Owner"  # built-in name still known


@respx.mock
async def test_estate_overview_rows(tool_ctx):
    tool_ctx.access = make_access(AccessLevel.COST, admin=True)
    tool_ctx.management_group_id = "platform"
    respx.post(GRAPH_URL).mock(side_effect=by_query({
        "advisorresources": [{"subscriptionId": SUBSCRIPTION, "category": "Security", "impact": "High", "n": 3}],
        "securescores": [{"subscriptionId": SUBSCRIPTION, "percentage": 0.72}],
    }))
    respx.post(url__regex=r".*managementGroups/platform/providers/Microsoft.CostManagement/query.*").mock(return_value=httpx.Response(200, json={
        "properties": {"columns": [{"name": "Cost"}, {"name": "SubscriptionId"}, {"name": "Currency"}], "rows": [[1234.5, SUBSCRIPTION, "AUD"]]}}))
    result = await platform.estate_overview(platform.EstateArgs(), tool_ctx)
    [row] = result.data["subscriptions"]
    assert row["cost_mtd"] == 1234.5 and row["secure_score_pct"] == 72.0 and row["advisor_high_impact"] == 3


async def test_crip_usage_from_audit_log(tool_ctx):
    repo = InMemoryRepository()
    tool_ctx.repository = repo
    await repo.log_access(AccessEvent(user_oid="a", user_name="Asha", action="chat", scope=None, outcome="ok"))
    await repo.log_access(AccessEvent(user_oid="b", user_name="Ben", action="tool:costpulse_query_costs", scope=SUBSCRIPTION, outcome="denied"))
    result = await platform.crip_usage(platform.UsageArgs(days=7), tool_ctx)
    assert result.data["distinct_users"] == 2 and result.data["denied"] == 1
    assert {a["name"] for a in result.data["by_action"]} == {"chat", "dashboard"}
    old = repo.access_log[-1]
    old["at"] = datetime.now(UTC) - timedelta(days=30)
    assert (await platform.crip_usage(platform.UsageArgs(days=7), tool_ctx)).data["total_events"] == 1
