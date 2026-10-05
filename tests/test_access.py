"""Access model and resolver: levels from Azure RBAC, app roles and groups; enforcement in execute_tool."""

import json
import time

import httpx
import pytest
import respx

from crip_backend.access.model import AccessLevel, Requirement, level_for_role
from crip_backend.access.resolver import AccessResolver
from crip_backend.auth.entra import AuthenticatedUser
from crip_backend.contracts import ResponseStatus
from crip_backend.tools.registry import execute_tool

from .conftest import SUBSCRIPTION, TENANT, USER_OID, make_access

OTHER_SUB = "77777777-7777-7777-7777-777777777777"
SUBS_URL = "https://management.azure.com/subscriptions?api-version=2022-12-01"
GRAPH_URL = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"


def ra_url(sub: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments"
        f"?api-version=2022-04-01&$filter=assignedTo('{USER_OID}')"
    )


def assignment(role_guid: str, scope: str) -> dict:
    return {"properties": {"roleDefinitionId": f"/providers/Microsoft.Authorization/roleDefinitions/{role_guid}", "scope": scope}}


READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
COST_READER = "72fafb9e-0641-4937-9268-a91bfd8191a3"


def user(roles=(), groups=(), overage=False) -> AuthenticatedUser:
    return AuthenticatedUser(object_id=USER_OID, tenant_id=TENANT, scopes=frozenset({"access_as_user"}), display_name="Test User",
                             roles=frozenset(roles), groups=frozenset(groups), groups_overage=overage)


async def static_token(_user):
    return "app-identity-token"


# --------------------------------------------------------------------------- model


def test_role_levels():
    assert level_for_role("Reader") is AccessLevel.RESOURCES
    assert level_for_role("Cost Management Reader") is AccessLevel.COST
    assert level_for_role("Owner") is AccessLevel.COST
    assert level_for_role("Storage Blob Data Reader") is AccessLevel.NONE


def test_reader_gets_resources_not_cost():
    access = make_access(AccessLevel.RESOURCES, via="rbac:Reader")
    assert access.check(SUBSCRIPTION, Requirement.RESOURCES).allowed
    denied = access.check(SUBSCRIPTION, Requirement.COST)
    assert not denied.allowed and "Cost Management Reader" in denied.reason
    assert not access.check(SUBSCRIPTION, Requirement.ADMIN).allowed
    assert not access.check(OTHER_SUB, Requirement.RESOURCES).allowed
    assert not access.check("/providers/Microsoft.Management/managementGroups/mg1", Requirement.RESOURCES).allowed


def test_admin_can_use_admin_and_management_group_scope():
    access = make_access(AccessLevel.COST, admin=True)
    assert access.check(None, Requirement.ADMIN).allowed
    assert access.check("/providers/Microsoft.Management/managementGroups/mg1", Requirement.COST).allowed


# --------------------------------------------------------------------------- resolver


@pytest.fixture
def subs_route():
    return respx.get(SUBS_URL).mock(return_value=httpx.Response(200, json={"value": [
        {"subscriptionId": SUBSCRIPTION, "displayName": "Prod", "state": "Enabled"},
        {"subscriptionId": OTHER_SUB, "displayName": "Dev", "state": "Enabled"},
    ]}))


@respx.mock
async def test_rbac_reader_and_cost_reader_per_subscription(settings, arm_client, subs_route):
    respx.get(ra_url(SUBSCRIPTION)).mock(return_value=httpx.Response(200, json={"value": [assignment(COST_READER, f"/subscriptions/{SUBSCRIPTION}")]}))
    respx.get(ra_url(OTHER_SUB)).mock(return_value=httpx.Response(200, json={"value": [
        assignment(READER, "/providers/Microsoft.Management/managementGroups/platform"),  # inherited from MG: counts
        assignment(COST_READER, f"/subscriptions/{OTHER_SUB}/resourceGroups/rg-app"),   # RG-level: ignored
    ]}))
    access = await AccessResolver(settings, arm_client, static_token).resolve(user())
    assert access.subscriptions[SUBSCRIPTION].level is AccessLevel.COST
    assert access.subscriptions[SUBSCRIPTION].via[0] == "rbac:Cost Management Reader"
    assert access.subscriptions[OTHER_SUB].level is AccessLevel.RESOURCES
    assert not access.is_platform_admin


@respx.mock
async def test_no_role_means_no_subscription(settings, arm_client, subs_route):
    respx.get(ra_url(SUBSCRIPTION)).mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get(ra_url(OTHER_SUB)).mock(return_value=httpx.Response(200, json={"value": []}))
    access = await AccessResolver(settings, arm_client, static_token).resolve(user())
    assert access.subscriptions == {}
    assert not access.check(None, Requirement.RESOURCES).allowed


@respx.mock
async def test_platform_admin_app_role_skips_rbac_and_sees_everything(settings, arm_client, subs_route):
    rbac = respx.get(ra_url(SUBSCRIPTION))
    access = await AccessResolver(settings, arm_client, static_token).resolve(user(roles=["CRIP.PlatformAdmin"]))
    assert access.is_platform_admin
    assert {s.level for s in access.subscriptions.values()} == {AccessLevel.COST}
    assert not rbac.called


@respx.mock
async def test_group_mapping_and_overage_via_graph(settings, arm_client, subs_route):
    settings.reader_group_ids = "aaaaaaaa-0000-0000-0000-000000000001"
    settings.rbac_access_check = False

    class Graph:
        async def transitive_group_ids(self, oid):
            return {"AAAAAAAA-0000-0000-0000-000000000001"}

    access = await AccessResolver(settings, arm_client, static_token, graph=Graph()).resolve(user(overage=True))
    assert access.subscriptions[SUBSCRIPTION].level is AccessLevel.RESOURCES
    assert access.subscriptions[SUBSCRIPTION].via == ("group:reader",)


@respx.mock
async def test_management_group_limits_the_catalog(settings, arm_client, subs_route):
    settings.management_group_id = "platform"
    graph = respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={"data": [{"subscriptionId": SUBSCRIPTION}]}))
    access = await AccessResolver(settings, arm_client, static_token).resolve(user(roles=["CRIP.CostReader"]))
    assert list(access.subscriptions) == [SUBSCRIPTION]
    assert "managementGroupAncestorsChain" in json.loads(graph.calls.last.request.content)["query"]


@respx.mock
async def test_results_are_cached_per_user(settings, arm_client, subs_route):
    route = respx.get(ra_url(SUBSCRIPTION)).mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get(ra_url(OTHER_SUB)).mock(return_value=httpx.Response(200, json={"value": []}))
    resolver = AccessResolver(settings, arm_client, static_token, clock=time.monotonic)
    await resolver.resolve(user())
    await resolver.resolve(user())
    assert route.call_count == 1


# --------------------------------------------------------------------------- enforcement in execute_tool


@respx.mock
async def test_reader_is_refused_cost_tool_without_any_azure_call(tool_ctx):
    tool_ctx.access = make_access(AccessLevel.RESOURCES, via="rbac:Reader")
    route = respx.post(url__regex=r".*CostManagement/query.*")
    result = await execute_tool("costpulse_query_costs", json.dumps({"scope": SUBSCRIPTION}), tool_ctx)
    assert result.status is ResponseStatus.ERROR and result.answer.startswith("Access denied")
    assert not route.called and tool_ctx.contributions == []


@respx.mock
async def test_allowed_call_is_stamped_with_identity_and_reason(tool_ctx):
    tool_ctx.access = make_access(AccessLevel.RESOURCES, via="rbac:Reader")
    respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    result = await execute_tool("governance_network_posture", json.dumps({"scope": SUBSCRIPTION}), tool_ctx)
    assert result.status is ResponseStatus.OK
    assert {(s.auth, s.authorized_via) for s in result.sources} == {("user_obo", "rbac:Reader")}


async def test_idle_resources_need_cost_only_when_priced(tool_ctx):
    tool_ctx.access = make_access(AccessLevel.RESOURCES, via="rbac:Reader")
    priced = await execute_tool("optimizer_find_idle_resources", json.dumps({"scope": SUBSCRIPTION}), tool_ctx)
    assert priced.answer.startswith("Access denied")


async def test_platform_tools_require_admin(tool_ctx):
    tool_ctx.access = make_access(AccessLevel.COST)
    result = await execute_tool("platform_estate_overview", "{}", tool_ctx)
    assert result.answer.startswith("Access denied") and "Platform admin" in result.answer
