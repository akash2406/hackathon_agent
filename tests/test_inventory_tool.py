"""Inventory tool: resource counts and tag coverage from Resource Graph."""

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from crip_backend.contracts import ResponseStatus
from crip_backend.tools import inventory

from .conftest import SUBSCRIPTION

GRAPH_URL = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"


def graph_router(request):
    query = json.loads(request.content)["query"]
    if "by type" in query:
        data = [{"type": "microsoft.compute/virtualmachines", "resources": 6}, {"type": "microsoft.storage/storageaccounts", "resources": 4}]
    elif "by location" in query:
        data = [{"location": "westeurope", "resources": 8}, {"location": "eastus", "resources": 2}]
    else:
        data = [{"resourceGroup": "rg-app", "total": 7, "tagged": 6}, {"resourceGroup": "rg-legacy", "total": 3, "tagged": 0}]
    return httpx.Response(200, json={"data": data})


@respx.mock
async def test_summary_with_tag_coverage(tool_ctx):
    route = respx.post(GRAPH_URL).mock(side_effect=graph_router)
    result = await inventory.resource_summary(inventory.ResourceSummaryArgs(scope=SUBSCRIPTION, tag_key="owner"), tool_ctx)

    assert route.call_count == 3
    assert result.status is ResponseStatus.OK
    assert result.data["total_resources"] == 10
    coverage = result.data["tag_coverage"]
    assert coverage["coverage_pct"] == 60.0 and coverage["untagged"] == 4
    assert coverage["worst_resource_groups"][0] == {"resource_group": "rg-legacy", "total": 3, "untagged": 3}
    assert len(result.sources) == 3


@respx.mock
async def test_empty_subscription_is_no_data(tool_ctx):
    respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    result = await inventory.resource_summary(inventory.ResourceSummaryArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.NO_DATA


def test_tag_key_cannot_inject_kql():
    with pytest.raises(ValidationError):
        inventory.ResourceSummaryArgs(scope=SUBSCRIPTION, tag_key="owner'] | project secrets //")
