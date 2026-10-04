"""Optimizer tools: Advisor savings and idle resources priced with real cost."""

import json
from datetime import UTC, datetime

import httpx
import respx

from crip_backend.contracts import ResponseStatus
from crip_backend.tools import optimizer

from .conftest import SUBSCRIPTION

ADVISOR_URL = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Advisor/recommendations"
    "?api-version=2023-01-01&$filter=Category%20eq%20%27Cost%27"
)
GRAPH_URL = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
QUERY_URL = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CostManagement/query"
    "?api-version=2023-11-01"
)


def rec(problem, resource, annual, rg="rg-app", updated="2026-09-21T06:00:00Z"):
    return {
        "properties": {
            "category": "Cost",
            "impact": "High",
            "impactedField": "Microsoft.Compute/virtualMachines",
            "impactedValue": resource,
            "shortDescription": {"problem": problem, "solution": "Right-size or shut down"},
            "extendedProperties": {"annualSavingsAmount": str(annual), "savingsAmount": str(annual / 12), "savingsCurrency": "USD"},
            "resourceMetadata": {"resourceId": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{rg}/providers/x/{resource}"},
            "lastUpdated": updated,
        }
    }


@respx.mock
async def test_advisor_savings_are_advisors_own_estimates(tool_ctx, token_source):
    route = respx.get(ADVISOR_URL).mock(
        return_value=httpx.Response(
            200,
            json={"value": [rec("Right-size underused VMs", "vm-big", 1200), rec("Buy reserved instances", "vm-db", 3000, updated="2026-09-20T00:00:00Z")]},
        )
    )
    result = await optimizer.advisor_recommendations(optimizer.AdvisorArgs(scope=SUBSCRIPTION), tool_ctx)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {token_source.token}"
    assert result.status is ResponseStatus.OK
    assert result.data["total_annual_savings_by_currency"] == {"USD": 4200.0}
    assert result.data["recommendations"][0]["resource"] == "vm-db"  # ranked by savings
    assert result.data_timestamp == datetime(2026, 9, 21, 6, tzinfo=UTC)  # Advisor's assessment time
    assert any("Advisor's estimates" in c for c in result.caveats)


@respx.mock
async def test_advisor_filters_to_resource_group_scope(tool_ctx):
    respx.get(ADVISOR_URL).mock(
        return_value=httpx.Response(200, json={"value": [rec("A", "vm-1", 100, rg="rg-app"), rec("B", "vm-2", 900, rg="rg-other")]})
    )
    result = await optimizer.advisor_recommendations(
        optimizer.AdvisorArgs(scope=f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-app"), tool_ctx
    )
    assert [r["resource"] for r in result.data["recommendations"]] == ["vm-1"]


@respx.mock
async def test_no_advisor_recommendations_is_no_data(tool_ctx):
    respx.get(ADVISOR_URL).mock(return_value=httpx.Response(200, json={"value": []}))
    result = await optimizer.advisor_recommendations(optimizer.AdvisorArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.NO_DATA and result.data is None


def idle_resource(name, category, rg="rg-app"):
    return {
        "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{rg}/providers/Microsoft.Compute/disks/{name}",
        "name": name,
        "category": category,
        "resourceGroup": rg,
        "location": "westeurope",
        "sku": "Premium_LRS",
    }


@respx.mock
async def test_idle_resources_are_priced_with_actual_cost(tool_ctx):
    disk = idle_resource("old-disk", "Unattached managed disk")
    ip = idle_resource("old-ip", "Unassociated public IP address")
    graph = respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={"data": [disk, ip], "count": 2}))
    cost = respx.post(QUERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "columns": [{"name": "Cost"}, {"name": "UsageDate"}, {"name": "ResourceId"}, {"name": "Currency"}],
                    "rows": [[20.0, 20260920, disk["id"].lower(), "USD"], [22.5, 20260921, disk["id"].lower(), "USD"], [3.0, 20260921, ip["id"].lower(), "USD"]],
                }
            },
        )
    )
    result = await optimizer.find_idle_resources(optimizer.IdleResourcesArgs(scope=SUBSCRIPTION), tool_ctx)

    kql = json.loads(graph.calls.last.request.content)["query"]
    assert "Unattached" in kql and "PowerState/stopped" in kql
    cost_body = json.loads(cost.calls.last.request.content)
    assert cost_body["dataset"]["filter"]["dimensions"]["values"] == [disk["id"].lower(), ip["id"].lower()]
    assert result.status is ResponseStatus.OK
    assert result.data["cost_mtd_by_currency"] == {"USD": 45.5}
    assert result.data["resources"][0]["name"] == "old-disk" and result.data["resources"][0]["cost_mtd"] == 42.5
    assert len(result.sources) == 2  # Resource Graph + Cost Management
    # Mixed live inventory + lagged billing: the answer is only as fresh as the billing data.
    assert result.data_timestamp == datetime(2026, 9, 21, tzinfo=UTC)


@respx.mock
async def test_idle_resources_cost_failure_is_partial_not_hidden(tool_ctx):
    respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={"data": [idle_resource("d", "Unattached managed disk")]}))
    respx.post(QUERY_URL).mock(return_value=httpx.Response(403, json={"error": {"code": "AuthorizationFailed", "message": "no"}}))
    result = await optimizer.find_idle_resources(optimizer.IdleResourcesArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.PARTIAL
    assert result.data["resources"][0]["cost_mtd"] is None
    assert any("could not be retrieved" in c for c in result.caveats)
    assert result.sources[1].http_status == 403


@respx.mock
async def test_no_idle_resources_is_no_data(tool_ctx):
    respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    result = await optimizer.find_idle_resources(optimizer.IdleResourcesArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.NO_DATA


def test_idle_kql_scopes_to_resource_group():
    assert "| where resourceGroup =~ 'rg-app'" in optimizer.build_idle_kql("rg-app")
    assert "resourceGroup =~" not in optimizer.build_idle_kql(None)


async def test_management_group_scope_is_rejected_for_resource_tools(tool_ctx):
    result = await optimizer.find_idle_resources(
        optimizer.IdleResourcesArgs(scope="/providers/Microsoft.Management/managementGroups/mg1"), tool_ctx
    )
    assert result.status is ResponseStatus.ERROR
