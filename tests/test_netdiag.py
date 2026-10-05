"""Network Doctor: the path evaluator (pure) and the tools against a fake Resource Graph / ARM."""

import json
import re

import httpx
import respx

from crip_backend.access.model import AccessLevel
from crip_backend.contracts import ResponseStatus
from crip_backend.tools import netdiag
from crip_backend.tools import netpath as np
from crip_backend.tools.registry import execute_tool

from .conftest import SUBSCRIPTION, make_access

ARM = "https://management.azure.com"
GRAPH_URL = f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
HUB = "11111111-2222-4333-8444-555555555555"


# --------------------------------------------------------------------------- evaluator


def rule(name, priority, direction, access, *, src="*", dst="*", port="*", protocol="*", **extra):
    return {"name": name, "properties": {"priority": priority, "direction": direction, "access": access, "protocol": protocol,
                                         "sourceAddressPrefix": src, "destinationAddressPrefix": dst, "sourcePortRange": "*",
                                         "destinationPortRange": port, **extra}}


SPACE = np.networks(["10.1.0.0/16", "10.2.0.0/16"])


def test_nsg_first_matching_rule_by_priority_wins():
    nsg = {"properties": {"securityRules": [
        rule("AllowHttps", 100, "Inbound", "Allow", src="10.1.0.0/16", port="443", protocol="Tcp"),
        rule("DenyAppToSql", 200, "Inbound", "Deny", src="10.1.0.0/16", port="1433", protocol="Tcp"),
    ]}}
    d = np.evaluate_nsg(nsg, direction="Inbound", protocol="TCP", src=np.Side("10.1.1.4"), dst=np.Side("10.2.1.5"), port=1433, vnet_space=SPACE)
    assert (d.verdict, d.rule, d.priority) == ("deny", "DenyAppToSql", 200)
    d = np.evaluate_nsg(nsg, direction="Inbound", protocol="TCP", src=np.Side("10.1.1.4"), dst=np.Side("10.2.1.5"), port=443, vnet_space=SPACE)
    assert (d.verdict, d.rule) == ("allow", "AllowHttps")


def test_nsg_default_rules_allow_vnet_and_deny_internet():
    nsg = {"properties": {"securityRules": []}}
    vnet = np.evaluate_nsg(nsg, direction="Inbound", protocol="TCP", src=np.Side("10.1.1.4"), dst=np.Side("10.2.1.5"), port=22, vnet_space=SPACE)
    internet = np.evaluate_nsg(nsg, direction="Inbound", protocol="TCP", src=np.Side("20.30.40.50"), dst=np.Side("10.2.1.5"), port=22, vnet_space=SPACE)
    assert (vnet.verdict, vnet.rule) == ("allow", "AllowVnetInBound")
    assert (internet.verdict, internet.rule) == ("deny", "DenyAllInBound")


def test_nsg_application_security_groups():
    asg = "/subscriptions/x/resourcegroups/rg/providers/microsoft.network/applicationsecuritygroups/asg-web"
    nsg = {"properties": {"securityRules": [rule("DenyWeb", 100, "Outbound", "Deny", port="5432", src="", sourceApplicationSecurityGroups=[{"id": asg}])]}}
    member = np.evaluate_nsg(nsg, direction="Outbound", protocol="TCP", src=np.Side("10.1.1.4", frozenset({asg})), dst=np.Side("10.2.1.5"), port=5432, vnet_space=SPACE)
    other = np.evaluate_nsg(nsg, direction="Outbound", protocol="TCP", src=np.Side("10.1.1.9"), dst=np.Side("10.2.1.5"), port=5432, vnet_space=SPACE)
    assert member.verdict == "deny" and other.verdict == "allow"


def test_nsg_service_tag_it_cannot_expand_makes_the_hop_unknown():
    nsg = {"properties": {"securityRules": [rule("DenyToSql", 100, "Outbound", "Deny", dst="Sql.WestEurope", port="1433")]}}
    d = np.evaluate_nsg(nsg, direction="Outbound", protocol="TCP", src=np.Side("10.1.1.4"), dst=np.Side(None), port=1433, vnet_space=SPACE)
    assert d.verdict == "unknown" and "DenyToSql (priority 100)" in d.undecidable[0]
    tagged = np.evaluate_nsg(nsg, direction="Outbound", protocol="TCP", src=np.Side("10.1.1.4"), dst=np.Side(None, service_tags=frozenset({"sql"})), port=1433, vnet_space=SPACE)
    assert tagged.verdict == "deny"


def test_routes_longest_prefix_and_unpeered_private_ranges():
    udr = [{"name": "to-fw", "properties": {"addressPrefix": "0.0.0.0/0", "nextHopType": "VirtualAppliance", "nextHopIpAddress": "10.0.0.4"}}]
    local = np.networks(["10.1.0.0/16"])
    peers = {"vnet-data": np.networks(["10.2.0.0/16"])}
    assert np.effective_route("10.2.1.5", user_routes=udr, vnet_space=local, peers=peers).next_hop_type == "VNetPeering"
    assert np.effective_route("10.9.0.1", user_routes=[], vnet_space=local, peers=peers).next_hop_type == "None"
    fw = np.effective_route("10.9.0.1", user_routes=udr + [{"name": "spokes", "properties": {"addressPrefix": "10.0.0.0/8", "nextHopType": "VirtualAppliance", "nextHopIpAddress": "10.0.0.4"}}],
                            vnet_space=local, peers=peers)
    assert (fw.next_hop_type, fw.route_name, fw.origin) == ("VirtualAppliance", "spokes", "User")
    assert np.effective_route(None, user_routes=udr, vnet_space=local, peers=peers).next_hop_type == "VirtualAppliance"


def fw_policy(*rules, app=False):
    coll = {"ruleCollectionType": "FirewallFilterRuleCollection", "name": "spokes", "priority": 200, "action": {"type": "Allow"},
            "rules": [{"ruleType": "NetworkRule", **r} for r in rules] + ([{"ruleType": "ApplicationRule", "name": "web"}] if app else [])}
    return [{"properties": {"priority": 100, "ruleCollections": [coll]}}]


def test_firewall_network_rules_and_default_deny():
    allow_sql = {"name": "app-to-sql", "ipProtocols": ["TCP"], "sourceAddresses": ["10.1.0.0/16"], "destinationAddresses": ["10.2.0.0/16"], "destinationPorts": ["1433"]}
    args = dict(protocol="TCP", src=np.Side("10.1.1.4"), dst=np.Side("10.2.1.5"))
    assert np.evaluate_firewall({}, fw_policy(allow_sql), port=1433, **args).verdict == "allow"
    denied = np.evaluate_firewall({}, fw_policy(allow_sql), port=5432, **args)
    assert denied.verdict == "deny" and "default" in denied.rule
    assert np.evaluate_firewall({}, fw_policy(allow_sql, app=True), port=443, **args).verdict == "unknown"


def test_service_firewall_rules():
    subnet_id = "/subscriptions/x/resourcegroups/rg/providers/microsoft.network/virtualnetworks/vnet-app/subnets/snet-app"
    st = {"type": "Microsoft.Storage/storageAccounts", "name": "st1",
          "properties": {"networkAcls": {"defaultAction": "Deny", "virtualNetworkRules": [{"id": subnet_id}]}}}
    with_endpoint = {"properties": {"serviceEndpoints": [{"service": "Microsoft.Storage"}]}}
    assert np.service_public_access(st, src_subnet=with_endpoint, src_subnet_id=subnet_id).verdict == "allow"
    no_endpoint = np.service_public_access(st, src_subnet={"properties": {}}, src_subnet_id=subnet_id)
    assert no_endpoint.verdict == "deny" and "service endpoint" in no_endpoint.fix
    off = {"type": "Microsoft.Sql/servers", "name": "sql1", "properties": {"publicNetworkAccess": "Disabled"}}
    assert np.service_public_access(off, src_subnet=None, src_subnet_id=None).verdict == "deny"


def test_subnet_capacity_and_overlap_helpers():
    assert np.usable_addresses("10.0.0.0/24") == 251
    assert np.overlapping({"a": np.networks(["10.1.0.0/16"]), "b": np.networks(["10.1.128.0/17"]), "c": np.networks(["10.3.0.0/16"])}) == [("a", "b", "10.1.0.0/16 overlaps 10.1.128.0/17")]


# --------------------------------------------------------------------------- a small fake estate


def rid(sub, rg, provider, name):
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{name}"


VNET_APP = rid(SUBSCRIPTION, "rg-net", "Microsoft.Network/virtualNetworks", "vnet-app")
VNET_DATA = rid(SUBSCRIPTION, "rg-net", "Microsoft.Network/virtualNetworks", "vnet-data")
SNET_APP, SNET_DATA = f"{VNET_APP}/subnets/snet-app", f"{VNET_DATA}/subnets/snet-data"
NSG_DATA = rid(SUBSCRIPTION, "rg-net", "Microsoft.Network/networkSecurityGroups", "nsg-data")
NW = rid(SUBSCRIPTION, "NetworkWatcherRG", "Microsoft.Network/networkWatchers", "NetworkWatcher_australiaeast")
PE = rid(SUBSCRIPTION, "rg-data", "Microsoft.Network/privateEndpoints", "pe-stappdata")
ZONE = rid(HUB, "rg-dns", "Microsoft.Network/privateDnsZones", "privatelink.blob.core.windows.net")


def res(rid_, type_, props, *, sub=SUBSCRIPTION, location="australiaeast"):
    return {"id": rid_, "name": rid_.rsplit("/", 1)[-1], "type": type_.lower(), "subscriptionId": sub, "location": location,
            "resourceGroup": rid_.split("/")[4], "properties": props}


def peering(name, remote, prefixes, state="Connected", **extra):
    return {"name": name, "properties": {"peeringState": state, "remoteVirtualNetwork": {"id": remote}, "remoteAddressSpace": {"addressPrefixes": prefixes}, **extra}}


def vm(name, ip, subnet):
    vm_id = rid(SUBSCRIPTION, "rg-app", "Microsoft.Compute/virtualMachines", name)
    nic_id = rid(SUBSCRIPTION, "rg-app", "Microsoft.Network/networkInterfaces", f"{name}-nic")
    return [
        res(vm_id, "Microsoft.Compute/virtualMachines", {"networkProfile": {"networkInterfaces": [{"id": nic_id, "properties": {"primary": True}}]}}),
        res(nic_id, "Microsoft.Network/networkInterfaces", {"virtualMachine": {"id": vm_id}, "ipConfigurations": [
            {"name": "ipconfig1", "properties": {"primary": True, "privateIPAddress": ip, "subnet": {"id": subnet}}}]}),
    ]


def estate():
    return [
        *vm("app-vm-01", "10.1.1.4", SNET_APP),
        *vm("db-vm", "10.2.1.5", SNET_DATA),
        res(VNET_APP, "Microsoft.Network/virtualNetworks", {"addressSpace": {"addressPrefixes": ["10.1.0.0/16"]},
            "subnets": [{"id": SNET_APP, "name": "snet-app", "properties": {"addressPrefix": "10.1.1.0/24"}}],
            "virtualNetworkPeerings": [peering("app-to-data", VNET_DATA, ["10.2.0.0/16"])]}),
        res(VNET_DATA, "Microsoft.Network/virtualNetworks", {"addressSpace": {"addressPrefixes": ["10.2.0.0/16"]},
            "subnets": [{"id": SNET_DATA, "name": "snet-data", "properties": {"addressPrefix": "10.2.1.0/24", "networkSecurityGroup": {"id": NSG_DATA}}}],
            "virtualNetworkPeerings": [peering("data-to-app", VNET_APP, ["10.1.0.0/16"])]}),
        res(NSG_DATA, "Microsoft.Network/networkSecurityGroups", {"securityRules": [
            rule("DenyAppToSql", 200, "Inbound", "Deny", src="10.1.0.0/16", port="1433", protocol="Tcp")]}),
        res(rid(SUBSCRIPTION, "rg-data", "Microsoft.Storage/storageAccounts", "stappdata"), "Microsoft.Storage/storageAccounts", {
            "publicNetworkAccess": "Disabled", "primaryEndpoints": {"blob": "https://stappdata.blob.core.windows.net/"},
            "privateEndpointConnections": [{"properties": {"privateEndpoint": {"id": PE}, "privateLinkServiceConnectionState": {"status": "Approved"}}}]}),
        res(PE, "Microsoft.Network/privateEndpoints", {"networkInterfaces": [{"id": f"{PE}-nic"}],
            "privateLinkServiceConnections": [{"properties": {"groupIds": ["blob"]}}]}),
        res(f"{PE}-nic", "Microsoft.Network/networkInterfaces", {"privateEndpoint": {"id": PE}, "ipConfigurations": [
            {"properties": {"privateIPAddress": "10.2.1.10", "subnet": {"id": SNET_DATA}}}]}),
        res(ZONE, "Microsoft.Network/privateDnsZones", {}, sub=HUB, location="global"),
    ]


LINKS = [{"id": f"{ZONE}/virtualNetworkLinks/data", "name": "data", "vnet": VNET_DATA, "state": "Completed"}]


def fake_graph(resources, *, watchers=()):
    def handler(request):
        q = json.loads(request.content)["query"]
        ql = q.lower()
        if "virtualnetworklinks" in ql:
            data = [link for link in LINKS if any(link["id"].lower().startswith(z.lower() + "/") for z in re.findall(r"startswith '([^']+)/'", q))]
        elif "networkwatchers" in ql:
            data = list(watchers)
        elif "id in~ (" in ql:
            data = [r for r in resources if f"'{r['id'].lower()}'" in ql]
        elif "privateipaddress" in ql:
            ip = re.search(r"== '([^']+)'", q).group(1)
            data = [r for r in resources if r["type"] == "microsoft.network/networkinterfaces"
                    and any(c["properties"].get("privateIPAddress") == ip for c in r["properties"].get("ipConfigurations", []))]
        elif "name =~ '" in ql:
            name = re.search(r"name =~ '([^']+)'", q).group(1).lower()
            data = [r for r in resources if r["name"].lower() == name and r["type"] in ql]
        elif "type =~ 'microsoft.network/virtualnetworks'" in ql:
            data = [r for r in resources if r["type"] == "microsoft.network/virtualnetworks"]
        else:
            data = []
        return httpx.Response(200, json={"data": data})
    return handler


def check(source, destination, port):
    return netdiag.CheckArgs(scope=SUBSCRIPTION, source=source, destination=destination, port=port)


# --------------------------------------------------------------------------- connectivity


@respx.mock
async def test_blocked_by_destination_subnet_nsg(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    result = await netdiag.check_connectivity(check("app-vm-01", "db-vm", 1433), tool_ctx)
    d = result.data
    assert d["verdict"] == "blocked" and d["blocked_at"] == "Destination subnet NSG (inbound)"
    blocking = next(h for h in d["hops"] if h["verdict"] == "deny")
    assert blocking["rule"] == "DenyAppToSql" and blocking["evidence"] == "crip" and "nsg-data" in blocking["fix"]
    assert [h["step"] for h in d["hops"]] == ["source", "nsg", "route", "peering", "nsg", "destination"]
    # Network Watcher is not enabled in the region: CRIP says so and the result is partial, not a guess.
    assert result.status is ResponseStatus.PARTIAL and any("Network Watcher" in c for c in result.caveats)
    assert result.sources and result.query_used and result.data_timestamp


@respx.mock
async def test_same_path_on_another_port_is_reachable(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    result = await netdiag.check_connectivity(check("app-vm-01", "10.2.1.5", 443), tool_ctx)
    assert result.data["verdict"] == "reachable"
    assert any(h["rule"] == "AllowVnetInBound" for h in result.data["hops"])


@respx.mock
async def test_private_endpoint_zone_not_linked_to_source_vnet(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    respx.get(f"{ARM}{ZONE}/A?api-version=2018-09-01").mock(return_value=httpx.Response(200, json={"value": [
        {"name": "stappdata", "properties": {"aRecords": [{"ipv4Address": "10.2.1.10"}]}}]}))
    result = await netdiag.check_connectivity(check("app-vm-01", "stappdata.blob.core.windows.net", 443), tool_ctx)
    dns = next(h for h in result.data["hops"] if h["step"] == "dns")
    assert result.data["verdict"] == "blocked" and result.data["blocked_at"] == "Name resolution"
    assert dns["verdict"] == "deny" and "Link the private DNS zone privatelink.blob.core.windows.net to vnet-app" in dns["fix"]


@respx.mock
async def test_network_watcher_verdicts_are_used_when_available(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate(), watchers=[{"id": NW, "location": "australiaeast", "subscriptionId": SUBSCRIPTION}]))
    poll = f"{ARM}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Network/locations/australiaeast/operationResults/1?api-version=2023-09-01"

    def flow(request):
        body = json.loads(request.content)
        if body["direction"] == "Outbound":
            return httpx.Response(200, json={"access": "Allow", "ruleName": "defaultSecurityRules/AllowVnetOutBound"})
        return httpx.Response(202, headers={"Location": poll, "Retry-After": "1"})

    respx.post(url__regex=r".*/ipFlowVerify\?.*").mock(side_effect=flow)
    respx.get(poll).mock(return_value=httpx.Response(200, json={"access": "Deny", "ruleName": "securityRules/DenyAppToSql"}))
    respx.post(url__regex=r".*/nextHop\?.*").mock(return_value=httpx.Response(200, json={"nextHopType": "VnetLocal", "routeTableId": "System Route"}))
    result = await netdiag.check_connectivity(check("app-vm-01", "db-vm", 1433), tool_ctx)
    blocking = next(h for h in result.data["hops"] if h["verdict"] == "deny")
    assert blocking["evidence"] == "azure" and blocking["rule"] == "DenyAppToSql"
    assert result.data["network_watcher_used"] and result.status is ResponseStatus.OK
    assert any("ipFlowVerify" in s.api for s in result.sources)


@respx.mock
async def test_unknown_source_is_no_data(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    result = await netdiag.check_connectivity(check("nope-vm", "db-vm", 22), tool_ctx)
    assert result.status is ResponseStatus.NO_DATA and "nope-vm" in result.answer


def test_targets_reject_kql_injection():
    import pydantic
    import pytest
    with pytest.raises(pydantic.ValidationError):
        netdiag.CheckArgs(scope=SUBSCRIPTION, source="x' or 1==1 //", destination="db", port=1)


# --------------------------------------------------------------------------- other tools


@respx.mock
async def test_effective_rules_without_permission_names_the_role(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    respx.post(url__regex=r".*/effective.*").mock(return_value=httpx.Response(403, json={"error": {"code": "AuthorizationFailed", "message": "no"}}))
    result = await netdiag.effective_rules(netdiag.ResourceArgs(scope=SUBSCRIPTION, resource="app-vm-01"), tool_ctx)
    assert result.status is ResponseStatus.ERROR and "CRIP Network Diagnostics" in result.answer


@respx.mock
async def test_effective_rules_polls_the_long_running_operation(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    poll = f"{ARM}/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Network/locations/australiaeast/operationResults/2"
    respx.post(url__regex=r".*/effectiveNetworkSecurityGroups\?.*").mock(return_value=httpx.Response(202, headers={"Location": poll}))
    respx.get(poll).mock(return_value=httpx.Response(200, json={"value": [{"networkSecurityGroup": {"id": NSG_DATA}, "association": {"subnet": {"id": SNET_APP}},
        "effectiveSecurityRules": [{"name": "securityRules/DenyAppToSql", "direction": "Inbound", "access": "Deny", "priority": 200, "protocol": "Tcp",
                                    "sourceAddressPrefix": "10.1.0.0/16", "destinationAddressPrefix": "*", "destinationPortRange": "1433-1433"}]}]}))
    respx.post(url__regex=r".*/effectiveRouteTable\?.*").mock(return_value=httpx.Response(200, json={"value": [
        {"source": "Default", "state": "Active", "addressPrefix": ["0.0.0.0/0"], "nextHopType": "Internet", "nextHopIpAddress": []}]}))
    result = await netdiag.effective_rules(netdiag.ResourceArgs(scope=SUBSCRIPTION, resource="app-vm-01"), tool_ctx)
    assert result.status is ResponseStatus.OK
    assert result.data["nsgs"][0]["rules"][0]["name"] == "DenyAppToSql"
    assert "internet-bound traffic goes to Internet" in result.answer


@respx.mock
async def test_private_endpoint_dns_per_vnet(tool_ctx):
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    respx.get(f"{ARM}{ZONE}/A?api-version=2018-09-01").mock(return_value=httpx.Response(200, json={"value": [
        {"name": "stappdata", "properties": {"aRecords": [{"ipv4Address": "10.2.1.10"}]}}]}))
    result = await netdiag.private_endpoint_dns(netdiag.ResourceArgs(scope=SUBSCRIPTION, resource="stappdata"), tool_ctx)
    by_vnet = {v["vnet"]: v["resolves_private"] for v in result.data["vnets"]}
    assert by_vnet == {"vnet-app": False, "vnet-data": True}
    assert result.data["record"]["matches_endpoint"] and "PUBLIC address from: vnet-app" in result.answer


@respx.mock
async def test_subnet_health_findings(tool_ctx):
    vnet = res(VNET_APP, "Microsoft.Network/virtualNetworks", {"addressSpace": {"addressPrefixes": ["10.1.0.0/16"]},
        "subnets": [{"id": SNET_APP, "name": "snet-app", "properties": {"addressPrefix": "10.1.1.0/28", "ipConfigurations": [{"id": f"ip{i}"} for i in range(11)]}}],
        "virtualNetworkPeerings": [peering("app-to-hub", VNET_DATA, ["10.1.128.0/20"], state="Disconnected")]})
    respx.post(GRAPH_URL).mock(side_effect=fake_graph([vnet]))
    result = await netdiag.subnet_health(netdiag.SubnetArgs(scope=SUBSCRIPTION), tool_ctx)
    findings = [f["finding"] for f in result.data["findings"]]
    assert result.data["subnets"][0]["used_pct"] == 100.0
    assert any("100.0% full" in f for f in findings) and any("Disconnected" in f for f in findings) and any("overlap" in f for f in findings)


@respx.mock
async def test_readers_may_use_the_network_doctor(tool_ctx):
    tool_ctx.access = make_access(AccessLevel.RESOURCES, via="rbac:Reader")
    respx.post(GRAPH_URL).mock(side_effect=fake_graph(estate()))
    result = await execute_tool(netdiag.FIND, json.dumps({"scope": SUBSCRIPTION, "query": "db-vm"}), tool_ctx)
    assert result.status is ResponseStatus.OK and result.data["subnet_nsg"] == "nsg-data"
    assert result.sources[0].authorized_via == "rbac:Reader"
