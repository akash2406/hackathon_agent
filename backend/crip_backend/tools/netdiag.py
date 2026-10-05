"""Network Doctor tools: "why can't A reach B?" for application teams.

Available at the RESOURCES access level. Read-only: the Network Watcher and
effective-rules calls are POST *actions* that evaluate configuration and change
nothing. CRIP never installs agents (no Connection Troubleshoot) and never
changes a rule; it names the blocking rule and suggests the fix.

* ``netdiag_check_connectivity``: traces source -> destination hop by hop
  (NSGs, route, Azure Firewall, VNet peering, private endpoint DNS, the
  service's own firewall) and returns a verdict naming the blocking hop. When
  the source is a VM, Azure Network Watcher's IP flow verify and next hop give
  the authoritative verdicts; otherwise CRIP evaluates the configuration itself
  (``tools/netpath.py``) and labels those hops "evaluated by CRIP".
* ``netdiag_find_endpoint``: where a resource or IP lives on the network.
* ``netdiag_effective_rules``: Azure's effective NSG rules and routes for a VM / NIC.
* ``netdiag_private_endpoint_dns``: private endpoints, DNS zone, A record and
  which VNets will resolve the service privately.
* ``netdiag_subnet_health``: subnet IP exhaustion, peering state, address overlaps.

Shared hub components (firewall, DNS zones, peered VNets) may sit in other
subscriptions; they are looked up across the management group CRIP covers and
reported by name and verdict only.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_network
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..azure_clients import resource_graph
from ..azure_clients.arm import AzureApiError, InvalidScopeError, split_subscription_scope
from ..contracts import AgentResponse, ResponseStatus, Source
from . import common
from . import netpath as np
from .context import ToolContext

AGENT = "netdiag"
CHECK = "netdiag_check_connectivity"
FIND = "netdiag_find_endpoint"
EFFECTIVE = "netdiag_effective_rules"
DNS = "netdiag_private_endpoint_dns"
SUBNETS = "netdiag_subnet_health"

NETWORK_API = "2023-09-01"
DNS_API = "2018-09-01"
SQL_API = "2021-11-01"
DIAG_ROLE_HINT = "Reader plus the 'CRIP Network Diagnostics' role (or Network Contributor)"

# Resource names, ids, IPs, FQDNs or "vnet/subnet". No quotes, so values are safe inside KQL string literals.
_TARGET = r"^[A-Za-z0-9_.\-()/:]{1,300}$"
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

ENDPOINT_TYPES = (
    "microsoft.compute/virtualmachines", "microsoft.network/networkinterfaces", "microsoft.network/privateendpoints",
    "microsoft.web/sites", "microsoft.storage/storageaccounts", "microsoft.keyvault/vaults", "microsoft.sql/servers",
    "microsoft.documentdb/databaseaccounts", "microsoft.containerregistry/registries", "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.cache/redis", "microsoft.servicebus/namespaces", "microsoft.eventhub/namespaces", "microsoft.cognitiveservices/accounts",
)
_TYPE_RANK = {t: i for i, t in enumerate(ENDPOINT_TYPES)}
SERVICE_TAGS = {
    "microsoft.storage/storageaccounts": {"storage", "azurecloud"},
    "microsoft.sql/servers": {"sql", "azurecloud"},
    "microsoft.keyvault/vaults": {"azurekeyvault", "azurecloud"},
    "microsoft.documentdb/databaseaccounts": {"azurecosmosdb", "azurecloud"},
    "microsoft.web/sites": {"appservice", "azurecloud"},
    "microsoft.containerregistry/registries": {"azurecontainerregistry", "azurecloud"},
    "microsoft.servicebus/namespaces": {"servicebus", "azurecloud"},
    "microsoft.eventhub/namespaces": {"eventhub", "azurecloud"},
}
NOT_VISIBLE = [
    "Firewalls inside the operating system or container (Windows Firewall, iptables)",
    "Whether the application is running and listening on the port",
    "On-premises networks beyond a VPN / ExpressRoute gateway",
]


class Protocol(StrEnum):
    TCP = "tcp"
    UDP = "udp"


class CheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    source: str = Field(pattern=_TARGET)
    destination: str = Field(pattern=_TARGET)
    port: int = Field(ge=1, le=65535)
    protocol: Protocol = Protocol.TCP


class FindArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    query: str = Field(pattern=_TARGET)


class ResourceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    resource: str = Field(pattern=_TARGET)


class SubnetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    vnet: str | None = Field(default=None, pattern=_TARGET)


# --------------------------------------------------------------------------- Azure access for one tool call


class _Azure:
    """Runs the Resource Graph / ARM calls of one tool invocation and keeps their provenance."""

    def __init__(self, ctx: ToolContext, token: str, tool: str, scope: str, sub: str) -> None:
        self.ctx, self.token, self.tool, self.scope, self.sub = ctx, token, tool, scope, sub
        self.sources: list[Source] = []
        self.queries: list[str] = []
        self.truncated = False
        self.notes: list[str] = []

    def _wide(self) -> tuple[list[str], list[str] | None]:
        """Where shared network components may live: the management group CRIP covers, else the user's subscriptions."""
        if self.ctx.auth_kind == "app_identity" and self.ctx.management_group_id:
            return [], [self.ctx.management_group_id]
        subs = {self.sub}
        if self.ctx.access is not None:
            subs |= set(self.ctx.access.subscriptions)
        return sorted(subs), None

    async def graph(self, kql: str, *, wide: bool = False) -> list[dict[str, Any]]:
        subs, mgs = self._wide() if wide else ([self.sub], None)
        arm = self.ctx.arm
        self.queries.append(f"POST {resource_graph.resources_url(arm)}\n"
                            + json.dumps({"managementGroups": mgs} if mgs else {"subscriptions": subs}) + f"\n{kql}")
        result = await resource_graph.query(arm, subscriptions=subs, management_groups=mgs, kql=kql, token=self.token)
        self.sources.append(common.source(result, tool=self.tool, scope=f"/providers/Microsoft.Management/managementGroups/{mgs[0]}" if mgs else self.scope))
        self.truncated |= result.truncated
        return [r for r in result.items if isinstance(r, dict)]

    async def get(self, resource_path: str, api_version: str) -> list[dict[str, Any]]:
        url = self.ctx.arm.url(resource_path, api_version)
        self.queries.append(f"GET {url}")
        result = await self.ctx.arm.request("GET", url, self.token)
        self.sources.append(common.source(result, tool=self.tool, scope=self.scope))
        return [r for r in result.items if isinstance(r, dict)]

    async def action(self, resource_path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.ctx.arm.url(resource_path, NETWORK_API)
        self.queries.append(f"POST {url}" + (f"\n{json.dumps(body)}" if body else ""))
        result = await self.ctx.arm.long_running("POST", url, self.token, body=body)
        self.sources.append(common.source(result, tool=self.tool, scope=self.scope))
        return result.payload if isinstance(result.payload, dict) else {}

    def query_used(self) -> str:
        return "\n\n".join(self.queries)

    def timestamp(self) -> Any:
        return min(s.invoked_at for s in self.sources) if self.sources else None


def _q(values: list[str] | set[str]) -> str:
    """KQL dynamic list of resource ids (ids come from Azure responses; quotes are stripped defensively)."""
    return ", ".join(f"'{str(v).replace(chr(39), '')}'" for v in sorted(values) if v)


def _p(resource: dict[str, Any] | None) -> dict[str, Any]:
    return (resource or {}).get("properties") or {}


def _short(resource_id: str | None) -> str:
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1]


def _vnet_id_of(subnet_id: str | None) -> str | None:
    if not subnet_id or "/subnets/" not in subnet_id.lower():
        return None
    return subnet_id[: subnet_id.lower().index("/subnets/")]


def _subnet_label(subnet_id: str | None) -> str:
    if not subnet_id:
        return "unknown subnet"
    parts = subnet_id.rstrip("/").split("/")
    return f"{parts[-3]}/{parts[-1]}" if len(parts) >= 3 else subnet_id


# --------------------------------------------------------------------------- endpoints


@dataclass
class Endpoint:
    query: str
    kind: str  # vm, nic, private_endpoint, web_app, service, subnet, ip, external
    name: str
    id: str | None = None
    type: str | None = None
    subscription_id: str | None = None
    location: str | None = None
    ip: str | None = None
    ip_is_representative: bool = False  # an address picked from the subnet (App Service / subnet sources)
    nic_id: str | None = None
    vm_id: str | None = None
    subnet_id: str | None = None
    nic_nsg_id: str | None = None
    asg_ids: frozenset[str] = frozenset()
    fqdn: str | None = None
    resource: dict[str, Any] | None = None
    alternatives: list[str] = field(default_factory=list)

    @property
    def vnet_id(self) -> str | None:
        return _vnet_id_of(self.subnet_id)

    def summary(self) -> dict[str, Any]:
        return {
            "query": self.query, "kind": self.kind, "name": self.name, "type": self.type, "id": self.id,
            "ip": self.ip, "ip_is_representative": self.ip_is_representative, "subnet": _subnet_label(self.subnet_id) if self.subnet_id else None,
            "fqdn": self.fqdn, "location": self.location, "alternatives": self.alternatives,
        }


def _fqdn_of(resource: dict[str, Any], typed: str | None) -> str | None:
    if typed:
        return typed
    p, rtype = _p(resource), str(resource.get("type", "")).lower()
    host = None
    if rtype == "microsoft.storage/storageaccounts":
        host = (p.get("primaryEndpoints") or {}).get("blob")
    elif rtype == "microsoft.keyvault/vaults":
        host = p.get("vaultUri")
    elif rtype == "microsoft.documentdb/databaseaccounts":
        host = p.get("documentEndpoint")
    elif rtype == "microsoft.web/sites":
        host = p.get("defaultHostName")
    elif rtype == "microsoft.containerregistry/registries":
        host = p.get("loginServer")
    else:
        host = p.get("fullyQualifiedDomainName") or p.get("hostName")
    if not host:
        return None
    return re.sub(r"^https?://", "", str(host)).split("/")[0].split(":")[0].lower() or None


def privatelink_zone(fqdn: str) -> str:
    _, _, rest = fqdn.lower().rstrip(".").partition(".")
    return "privatelink.vaultcore.azure.net" if rest == "vault.azure.net" else f"privatelink.{rest}"


async def _resolve(az: _Azure, text: str, *, home_only: bool) -> Endpoint | None:
    """Resolve a name, resource id, FQDN, private IP or 'vnet/subnet' to an Endpoint."""
    text = text.strip()
    if _IPV4.match(text):
        return await _resolve_ip(az, text)
    if "/" in text and not text.startswith("/"):  # vnet/subnet
        vnet_name, _, subnet_name = text.partition("/")
        vnets = await az.graph(f"Resources | where type =~ 'microsoft.network/virtualnetworks' and name =~ '{vnet_name}' "
                               "| project id, name, type, location, subscriptionId, properties", wide=not home_only)
        for vnet in vnets:
            for s in _p(vnet).get("subnets") or []:
                if str(s.get("name", "")).lower() == subnet_name.lower():
                    return _subnet_endpoint(text, s, vnet)
        return None
    typed_fqdn = text.lower() if "." in text and not text.startswith("/") else None
    name = text.split(".")[0] if typed_fqdn else text
    match = f"id =~ '{text}'" if text.startswith("/") else f"name =~ '{name}'"
    rows = await az.graph(f"Resources | where type in~ ({_q(set(ENDPOINT_TYPES))}) and {match} "
                          "| project id, name, type, location, subscriptionId, resourceGroup, properties", wide=not home_only)
    if not rows:
        return None
    rows.sort(key=lambda r: (str(r.get("subscriptionId", "")).lower() != az.sub, _TYPE_RANK.get(str(r.get("type", "")).lower(), 99)))
    chosen = rows[0]
    endpoint = await _endpoint_from_resource(az, text, chosen, typed_fqdn)
    endpoint.alternatives = [f"{r.get('name')} ({r.get('type')}, {r.get('resourceGroup')})" for r in rows[1:5]]
    return endpoint


def _subnet_endpoint(query: str, subnet: dict[str, Any], vnet: dict[str, Any], kind: str = "subnet") -> Endpoint:
    prefix = _p(subnet).get("addressPrefix") or next(iter(_p(subnet).get("addressPrefixes") or []), None)
    rep = None
    if prefix:
        net = ip_network(prefix, strict=False)
        rep = str(net.network_address + 4) if net.num_addresses > 4 else str(net.network_address)
    return Endpoint(query=query, kind=kind, name=f"{vnet.get('name')}/{subnet.get('name')}", id=subnet.get("id"),
                    type="microsoft.network/virtualnetworks/subnets", subscription_id=str(vnet.get("subscriptionId", "")).lower(),
                    location=vnet.get("location"), ip=rep, ip_is_representative=True, subnet_id=subnet.get("id"))


async def _resolve_ip(az: _Azure, ip: str) -> Endpoint:
    nics = await az.graph("Resources | where type =~ 'microsoft.network/networkinterfaces' "
                          "| mv-expand ipc = properties.ipConfigurations "
                          f"| where tostring(ipc.properties.privateIPAddress) == '{ip}' "
                          "| project id, name, type, location, subscriptionId, properties", wide=True)
    if nics:
        return _nic_endpoint(ip, nics[0], ip)
    if not np.is_private(ip):
        return Endpoint(query=ip, kind="external", name=ip, ip=ip)
    vnets = await az.graph("Resources | where type =~ 'microsoft.network/virtualnetworks' "
                           "| project id, name, type, location, subscriptionId, properties", wide=True)
    for vnet in vnets:
        for s in _p(vnet).get("subnets") or []:
            prefixes = [_p(s).get("addressPrefix")] + list(_p(s).get("addressPrefixes") or [])
            if np.in_any(ip, np.networks([x for x in prefixes if x])):
                ep = _subnet_endpoint(ip, s, vnet, kind="ip")
                ep.ip, ep.ip_is_representative, ep.name = ip, False, f"{ip} in {ep.name}"
                return ep
    return Endpoint(query=ip, kind="external", name=f"{ip} (not in any VNet CRIP can see)", ip=ip)


def _nic_endpoint(query: str, nic: dict[str, Any], prefer_ip: str | None = None) -> Endpoint:
    p = _p(nic)
    configs = p.get("ipConfigurations") or []
    ipc = next((c for c in configs if _p(c).get("privateIPAddress") == prefer_ip), None) or next(
        (c for c in configs if _p(c).get("primary")), configs[0] if configs else {})
    ipp = _p(ipc)
    kind = "private_endpoint" if p.get("privateEndpoint") else ("vm" if p.get("virtualMachine") else "nic")
    return Endpoint(
        query=query, kind=kind, name=nic.get("name", ""), id=nic.get("id"), type="microsoft.network/networkinterfaces",
        subscription_id=str(nic.get("subscriptionId", "")).lower(), location=nic.get("location"), ip=ipp.get("privateIPAddress"),
        nic_id=nic.get("id"), vm_id=(p.get("virtualMachine") or {}).get("id"), subnet_id=(ipp.get("subnet") or {}).get("id"),
        nic_nsg_id=(p.get("networkSecurityGroup") or {}).get("id"),
        asg_ids=frozenset(str(a.get("id", "")).lower() for a in ipp.get("applicationSecurityGroups") or []),
    )


async def _endpoint_from_resource(az: _Azure, query: str, row: dict[str, Any], typed_fqdn: str | None) -> Endpoint:
    rtype = str(row.get("type", "")).lower()
    p = _p(row)
    if rtype == "microsoft.compute/virtualmachines":
        nic_ids = [n.get("id") for n in (p.get("networkProfile") or {}).get("networkInterfaces") or []]
        primary = next((n.get("id") for n in (p.get("networkProfile") or {}).get("networkInterfaces") or [] if (n.get("properties") or {}).get("primary")), None)
        nics = await az.graph(f"Resources | where id in~ ({_q(set(filter(None, nic_ids)))}) | project id, name, type, location, subscriptionId, properties", wide=True) if nic_ids else []
        nic = next((n for n in nics if str(n.get("id", "")).lower() == str(primary or "").lower()), nics[0] if nics else None)
        if nic is None:
            return Endpoint(query=query, kind="vm", name=row.get("name", ""), id=row.get("id"), type=rtype, location=row.get("location"))
        ep = _nic_endpoint(query, nic)
        ep.kind, ep.name, ep.id, ep.type, ep.vm_id = "vm", row.get("name", ""), row.get("id"), rtype, row.get("id")
        return ep
    if rtype == "microsoft.network/networkinterfaces":
        return _nic_endpoint(query, row)
    if rtype == "microsoft.network/privateendpoints":
        nic_ids = [n.get("id") for n in p.get("networkInterfaces") or []]
        nics = await az.graph(f"Resources | where id in~ ({_q(set(filter(None, nic_ids)))}) | project id, name, type, location, subscriptionId, properties", wide=True) if nic_ids else []
        ep = _nic_endpoint(query, nics[0]) if nics else Endpoint(query=query, kind="private_endpoint", name=row.get("name", ""))
        ep.kind, ep.name, ep.id, ep.type = "private_endpoint", row.get("name", ""), row.get("id"), rtype
        return ep
    ep = Endpoint(query=query, kind="web_app" if rtype == "microsoft.web/sites" else "service", name=row.get("name", ""), id=row.get("id"), type=rtype,
                  subscription_id=str(row.get("subscriptionId", "")).lower(), location=row.get("location"), resource=row, fqdn=_fqdn_of(row, typed_fqdn))
    if rtype == "microsoft.web/sites" and p.get("virtualNetworkSubnetId"):
        ep.subnet_id = p.get("virtualNetworkSubnetId")  # regional VNet integration: outbound traffic leaves from this subnet
    return ep


# --------------------------------------------------------------------------- network context


@dataclass
class _Net:
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)  # lower-case id -> resource

    def get(self, rid: str | None) -> dict[str, Any] | None:
        return self.resources.get(str(rid or "").lower())

    def subnet(self, subnet_id: str | None) -> dict[str, Any] | None:
        vnet = self.get(_vnet_id_of(subnet_id))
        return next((s for s in _p(vnet).get("subnets") or [] if str(s.get("id", "")).lower() == str(subnet_id or "").lower()), None)

    def space(self, vnet_id: str | None) -> tuple:
        return np.networks(list((_p(self.get(vnet_id)).get("addressSpace") or {}).get("addressPrefixes") or []))

    def peers(self, vnet_id: str | None) -> dict[str, tuple]:
        """Connected peerings of a VNet: remote VNet id -> its address space (as the peering reports it)."""
        out = {}
        for peering in _p(self.get(vnet_id)).get("virtualNetworkPeerings") or []:
            pp = _p(peering)
            if str(pp.get("peeringState", "")).lower() == "connected":
                remote = str((pp.get("remoteVirtualNetwork") or {}).get("id", "")).lower()
                out[remote] = np.networks(list((pp.get("remoteAddressSpace") or {}).get("addressPrefixes") or []))
        return out

    def tag_space(self, vnet_id: str | None) -> tuple:
        """What the VirtualNetwork service tag covers for NSGs in this VNet: its space plus connected peers."""
        spaces = list(self.space(vnet_id))
        for nets in self.peers(vnet_id).values():
            spaces.extend(nets)
        return tuple(spaces)

    def peering(self, from_vnet: str | None, to_vnet: str | None) -> dict[str, Any] | None:
        for peering in _p(self.get(from_vnet)).get("virtualNetworkPeerings") or []:
            if str((_p(peering).get("remoteVirtualNetwork") or {}).get("id", "")).lower() == str(to_vnet or "").lower():
                return peering
        return None


async def _load(az: _Azure, net: _Net, ids: set[str | None]) -> None:
    want = {str(i).lower() for i in ids if i} - set(net.resources)
    if want:
        rows = await az.graph(f"Resources | where id in~ ({_q(want)}) | project id, name, type, location, subscriptionId, resourceGroup, properties", wide=True)
        for r in rows:
            net.resources[str(r.get("id", "")).lower()] = r


# --------------------------------------------------------------------------- hops


def _hop(step: str, title: str, verdict: str, detail: str, *, resource: str | None = None, rule: str | None = None,
         evidence: str = "crip", fix: str | None = None) -> dict[str, Any]:
    return {"step": step, "title": title, "verdict": verdict, "detail": detail, "resource": resource, "rule": rule, "evidence": evidence, "fix": fix}


def _nsg_hop(net: _Net, nsg_id: str | None, *, where: str, direction: str, protocol: str, src: np.Side, dst: np.Side,
             port: int, vnet_id: str | None, peer_label: str) -> dict[str, Any] | None:
    title = f"{where} NSG ({direction.lower()})"
    if not nsg_id:
        return None
    nsg = net.get(nsg_id)
    if nsg is None:
        return _hop("nsg", title, "unknown", f"NSG {_short(nsg_id)} could not be read.", resource=_short(nsg_id))
    d = np.evaluate_nsg(nsg, direction=direction, protocol=protocol, src=src, dst=dst, port=port, vnet_space=net.tag_space(vnet_id))
    name = nsg.get("name")
    if d.verdict == "unknown":
        detail = (f"Decided by {d.rule} ({d.access}), but " if d.access else "") + "CRIP cannot evaluate: " + ", ".join(d.undecidable or ["no rule matched"])
        return _hop("nsg", title, "unknown", detail, resource=name, rule=d.rule)
    if d.verdict == "allow":
        return _hop("nsg", title, "allow", f"Allowed by rule {d.rule} (priority {d.priority}).", resource=name, rule=d.rule)
    fix = (f"Add an {direction.lower()} security rule to {name} allowing {protocol.upper()} {port} {peer_label}, "
           f"with a priority number lower than {d.priority}" + (" (the default deny-all rule)" if (d.priority or 0) >= 65000 else f" (where '{d.rule}' sits)") + ".")
    return _hop("nsg", title, "deny", f"Blocked by rule {d.rule} (priority {d.priority}).", resource=name, rule=d.rule, fix=fix)


def _overall(hops: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    for h in hops:
        if h["verdict"] == "deny":
            return "blocked", h
        if h["verdict"] == "unknown":
            # Keep looking for a definite block further down the path; otherwise unknown.
            blocked = next((x for x in hops[hops.index(h) + 1:] if x["verdict"] == "deny"), None)
            return ("blocked", blocked) if blocked else ("unknown", h)
    return "reachable", None


# --------------------------------------------------------------------------- connectivity check


async def check_connectivity(args: CheckArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, _rg = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{sub}"
    described = f"Network path {args.source} -> {args.destination}:{args.port}/{args.protocol.value} (Azure Resource Graph, Network Watcher)"
    token = await common.user_token(ctx, agent=AGENT, query_used=described)
    if isinstance(token, AgentResponse):
        return token
    az = _Azure(ctx, token, CHECK, scope, sub)
    invoked_at = ctx.arm.clock()
    try:
        return await _check(az, args, scope)
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=CHECK, scope=scope, query_used=az.query_used() or described,
                              invoked_at=invoked_at, what="network path", role_hint="Reader")


async def _check(az: _Azure, args: CheckArgs, scope: str) -> AgentResponse:
    protocol, port = args.protocol.value.upper(), args.port
    src, dst = await asyncio.gather(_resolve(az, args.source, home_only=True), _resolve(az, args.destination, home_only=False))
    if src is None or dst is None:
        missing = args.source if src is None else args.destination
        where = f"in {scope}" if src is None else "in the subscriptions CRIP can see"
        return AgentResponse(agent=AGENT, status=ResponseStatus.NO_DATA, confidence=common.HIGH,
                             answer=f"No VM, network interface, private endpoint, subnet or supported service named '{missing}' was found {where}. "
                                    "Use the exact resource name, a private IP address or 'vnet/subnet'.",
                             query_used=az.query_used(), sources=az.sources)
    if not src.subnet_id:
        return AgentResponse(agent=AGENT, status=ResponseStatus.NO_DATA, confidence=common.HIGH,
                             answer=f"{src.name} is not connected to a virtual network (no subnet / VNet integration), so its traffic does not "
                                    "pass through any NSG or route CRIP can check. Choose a VM, a VNet-integrated app or a subnet as the source.",
                             data={"kind": "connectivity_check", "source": src.summary(), "destination": dst.summary()},
                             query_used=az.query_used(), sources=az.sources, data_timestamp=az.timestamp())

    net = _Net()
    caveats: list[str] = []
    hops: list[dict[str, Any]] = []
    if src.ip_is_representative:
        caveats.append(f"{src.name} has no single address; CRIP evaluated rules for {src.ip}, an address in its subnet.")

    # ---- destination: private endpoint or public endpoint? (DNS) -------------------------------------------
    dns_hop, dst = await _destination_route(az, net, src, dst, caveats)
    await _load(az, net, {src.vnet_id, dst.vnet_id, src.nic_nsg_id, dst.nic_nsg_id})
    src_subnet, dst_subnet = net.subnet(src.subnet_id), net.subnet(dst.subnet_id)
    await _load(az, net, {
        (_p(src_subnet).get("networkSecurityGroup") or {}).get("id"), (_p(src_subnet).get("routeTable") or {}).get("id"),
        (_p(dst_subnet).get("networkSecurityGroup") or {}).get("id"),
    })

    src_side = np.Side(src.ip, src.asg_ids)
    dst_side = np.Side(dst.ip, dst.asg_ids, frozenset(SERVICE_TAGS.get(str(dst.type or "").lower(), set())) if dst.ip is None else frozenset())
    dst_label = dst.ip or f"{dst.name} (public endpoint)"
    hops.append(_hop("source", "Source", "info", f"{src.name}: {src.ip or 'no address'} in {_subnet_label(src.subnet_id)}"
                     + (f" ({src.kind.replace('_', ' ')})" if src.kind not in ("subnet", "ip") else ""), resource=src.name))
    if dns_hop:
        hops.append(dns_hop)

    # ---- Network Watcher (authoritative) when the source is a VM ---------------------------------------------
    nw = await _network_watcher(az, src, dst, protocol, port) if src.vm_id and dst.ip else None
    if nw and nw.get("note"):
        caveats.append(nw["note"])

    # ---- source NSGs (NIC first, then subnet, for outbound) -------------------------------------------------
    for nsg_id, where in ((src.nic_nsg_id, "Source NIC"), ((_p(src_subnet).get("networkSecurityGroup") or {}).get("id"), "Source subnet")):
        h = _nsg_hop(net, nsg_id, where=where, direction="Outbound", protocol=protocol, src=src_side, dst=dst_side, port=port,
                     vnet_id=src.vnet_id, peer_label=f"to {dst_label}")
        if h:
            hops.append(h)
    if not src.nic_nsg_id and not (_p(src_subnet).get("networkSecurityGroup") or {}).get("id"):
        hops.append(_hop("nsg", "Source NSGs (outbound)", "allow", "No NSG on the source subnet or network interface: nothing filters outbound traffic here."))
    if nw and nw.get("outbound"):
        _apply_flow_verdict(hops, nw["outbound"], "Outbound")

    # ---- route ------------------------------------------------------------------------------------------------
    route_table = net.get((_p(src_subnet).get("routeTable") or {}).get("id"))
    route = np.effective_route(dst.ip, user_routes=list(_p(route_table).get("routes") or []), vnet_space=net.space(src.vnet_id), peers=net.peers(src.vnet_id))
    next_hop_type, next_hop_ip, evidence = route.next_hop_type, route.next_hop_ip, "crip"
    if nw and nw.get("next_hop"):
        nh = nw["next_hop"]
        if str(nh.get("nextHopType", "")).lower() != next_hop_type.lower() and not (next_hop_type == "VNetPeering" and nh.get("nextHopType") == "VnetLocal"):
            caveats.append(f"Azure Network Watcher reports next hop {nh.get('nextHopType')}; CRIP's own evaluation said {next_hop_type}. Azure's answer is used.")
            next_hop_type = str(nh.get("nextHopType"))
        next_hop_ip = nh.get("nextHopIpAddress") or next_hop_ip
        evidence = "azure"
    route_desc = (f"route {route.route_name} ({route.prefix}) in {route_table.get('name')}" if route.origin == "User" and route_table else f"Azure system route {route.prefix}")
    via_hub: str | None = None  # VNet of a firewall/NVA the traffic is forwarded through
    if next_hop_type == "None":
        peering = net.peering(src.vnet_id, dst.vnet_id) if dst.vnet_id else None
        why = (f" The peering {peering.get('name')} exists but is {_p(peering).get('peeringState')}." if peering else
               (" The source and destination VNets are not peered." if dst.vnet_id and dst.vnet_id.lower() != str(src.vnet_id).lower() else ""))
        fix = ("Reconnect the peering on both sides (delete and re-create the side that shows Disconnected)." if peering else
               "Peer the two VNets (or route through the hub firewall with a user-defined route)." if dst.vnet_id else
               "Add a route to the destination, or check the address.")
        hops.append(_hop("route", "Route", "deny", f"No route to {dst_label}: {route_desc} drops the traffic (next hop None).{why}",
                         resource=route_table.get("name") if route_table else None, rule=route.route_name, evidence=evidence, fix=fix))
    elif next_hop_type == "VirtualAppliance":
        hops.append(_hop("route", "Route", "info", f"{route_desc} sends traffic to a network virtual appliance at {next_hop_ip}.",
                         resource=route_table.get("name") if route_table else None, rule=route.route_name, evidence=evidence))
        fw_hop, via_hub = await _firewall_hop(az, net, next_hop_ip, protocol, src_side, dst_side, port, src, dst_label)
        hops.append(fw_hop)
    elif next_hop_type == "VirtualNetworkGateway":
        hops.append(_hop("route", "Route", "unknown", f"{route_desc} sends traffic to the VPN / ExpressRoute gateway; CRIP cannot see beyond it.",
                         resource=route_table.get("name") if route_table else None, evidence=evidence))
    else:
        label = {"VnetLocal": "within the virtual network", "VNetPeering": "over VNet peering", "Internet": "to the internet"}.get(next_hop_type, next_hop_type)
        hops.append(_hop("route", "Route", "allow", f"{route_desc}: traffic goes {label}.", resource=route_table.get("name") if route_table else None,
                         rule=route.route_name, evidence=evidence))
    if route.undecidable:
        caveats.append("Routes using service tags were not evaluated: " + ", ".join(route.undecidable) + ".")

    # ---- peering into the destination VNet (via a hub, if forwarded) ------------------------------------------
    if dst.vnet_id and src.vnet_id and next_hop_type != "None" and (dst.vnet_id.lower() != src.vnet_id.lower() or via_hub):
        await _load(az, net, {via_hub} if via_hub else set())
        hops.extend(_peering_hops(net, src.vnet_id, via_hub, dst.vnet_id))

    # ---- destination side --------------------------------------------------------------------------------------
    if dst.ip is None and dst.resource is not None:
        hops.append(await _service_firewall_hop(az, net, src, dst, src_subnet))
    elif dst.subnet_id:
        pe_policies = str(_p(dst_subnet).get("privateEndpointNetworkPolicies", "Disabled")).lower()
        skip_subnet_nsg = dst.kind == "private_endpoint" and pe_policies not in ("enabled", "networksecuritygroupenabled")
        dst_subnet_nsg = (_p(dst_subnet).get("networkSecurityGroup") or {}).get("id")
        if skip_subnet_nsg and dst_subnet_nsg:
            hops.append(_hop("nsg", "Destination subnet NSG (inbound)", "info",
                             f"{_short(dst_subnet_nsg)} is not applied to private endpoints in this subnet (private endpoint network policies are disabled).",
                             resource=_short(dst_subnet_nsg)))
        dst_vnet_for_tags = dst.vnet_id
        for nsg_id, where in ((None if skip_subnet_nsg else dst_subnet_nsg, "Destination subnet"), (dst.nic_nsg_id, "Destination NIC")):
            h = _nsg_hop(net, nsg_id, where=where, direction="Inbound", protocol=protocol, src=src_side, dst=dst_side, port=port,
                         vnet_id=dst_vnet_for_tags, peer_label=f"from {src.ip} ({_subnet_label(src.subnet_id)})")
            if h:
                hops.append(h)
        if nw and nw.get("inbound"):
            _apply_flow_verdict(hops, nw["inbound"], "Inbound")
    elif dst.kind == "external":
        hops.append(_hop("destination", "Beyond Azure", "unknown", f"{dst.ip} is outside the virtual networks CRIP can see (internet or on-premises)."))

    hops.append(_hop("destination", "Destination", "info", f"{dst.name}" + (f" at {dst.ip}" if dst.ip else "") + f", {protocol} {port}.", resource=dst.name))
    return _connectivity_response(az, args, src, dst, hops, nw, caveats)


def _apply_flow_verdict(hops: list[dict[str, Any]], verdict: dict[str, Any], direction: str) -> None:
    """Overlay Azure's IP flow verify result on CRIP's NSG hops for that direction."""
    nsg_hops = [h for h in hops if h["step"] == "nsg" and f"({direction.lower()})" in h["title"] and h["verdict"] != "info"]
    access, rule = str(verdict.get("access", "")), str(verdict.get("ruleName", ""))
    if not nsg_hops:
        return
    if access == "Allow":
        for h in nsg_hops:
            if h["verdict"] != "allow":
                h["detail"] = f"Azure says this traffic is allowed (rule {rule}). " + h["detail"]
            h.update(verdict="allow", evidence="azure", fix=None)
        return
    rule_name = rule.split("/")[-1].lower()
    target = next((h for h in nsg_hops if (h.get("rule") or "").lower() == rule_name), None) or next((h for h in nsg_hops if h["verdict"] != "allow"), nsg_hops[-1])
    target.update(verdict="deny", evidence="azure", rule=rule.split("/")[-1],
                  detail=f"Azure Network Watcher: denied by {rule}." + ("" if target["verdict"] == "deny" else f" (CRIP's own evaluation: {target['detail']})"))
    if not target.get("fix"):
        target["fix"] = f"Add a {direction.lower()} rule allowing this traffic with a priority number lower than '{rule.split('/')[-1]}'."


async def _network_watcher(az: _Azure, src: Endpoint, dst: Endpoint, protocol: str, port: int) -> dict[str, Any] | None:
    """IP flow verify (out of the source, into a VM destination) and next hop. Unavailable -> a note, not a failure."""
    watchers = await az.graph("Resources | where type =~ 'microsoft.network/networkwatchers' "
                              f"| where location in~ ({_q({str(src.location or ''), str(dst.location or '')})}) | project id, location, subscriptionId", wide=True)

    def watcher_for(ep: Endpoint) -> str | None:
        return next((w["id"] for w in watchers if str(w.get("location", "")).lower() == str(ep.location or "").lower()
                     and str(w.get("subscriptionId", "")).lower() == str(ep.subscription_id or "").lower()), None)

    src_nw = watcher_for(src)
    if not src_nw:
        return {"note": f"Network Watcher is not enabled in {src.location} for the source subscription, so all verdicts were evaluated by CRIP."}
    out: dict[str, Any] = {}
    try:
        out["outbound"], out["next_hop"] = await asyncio.gather(
            az.action(f"{src_nw}/ipFlowVerify", {"targetResourceId": src.vm_id, "targetNicResourceId": src.nic_id, "direction": "Outbound", "protocol": protocol,
                                                  "localIPAddress": src.ip, "localPort": "*", "remoteIPAddress": dst.ip, "remotePort": str(port)}),
            az.action(f"{src_nw}/nextHop", {"targetResourceId": src.vm_id, "sourceIPAddress": src.ip, "destinationIPAddress": dst.ip, "targetNicResourceId": src.nic_id}),
        )
        dst_nw = watcher_for(dst) if dst.vm_id else None
        if dst_nw:
            out["inbound"] = await az.action(f"{dst_nw}/ipFlowVerify", {"targetResourceId": dst.vm_id, "targetNicResourceId": dst.nic_id, "direction": "Inbound",
                                                                      "protocol": protocol, "localIPAddress": dst.ip, "localPort": str(port), "remoteIPAddress": src.ip, "remotePort": "*"})
    except AzureApiError as exc:
        why = "CRIP's identity lacks the Network Watcher diagnostic permissions" if exc.status_code == 403 else f"{exc.status_code or ''} {exc.code}: {exc.message}".strip()
        return {**out, "note": f"Azure Network Watcher could not verify this flow ({why}); remaining verdicts were evaluated by CRIP."}
    return out


async def _destination_route(az: _Azure, net: _Net, src: Endpoint, dst: Endpoint, caveats: list[str]) -> tuple[dict[str, Any] | None, Endpoint]:
    """For a PaaS destination: does the source resolve it to a private endpoint? Returns the DNS hop and the endpoint to trace to."""
    if dst.kind not in ("service", "web_app") or dst.resource is None:
        return None, dst
    pes = await _private_endpoints(az, dst)
    if not pes:
        dst.ip, dst.subnet_id = None, None  # public endpoint (a web app's integration subnet is for its outbound traffic only)
        return _hop("dns", "Name resolution", "info", f"{dst.fqdn or dst.name} has no private endpoint, so it resolves to its public endpoint.",
                    resource=dst.fqdn), dst
    pe = pes[0]
    await _load(az, net, {src.vnet_id})
    zone_check = await _zone_check(az, net, dst.fqdn, pe, [src.vnet_id])
    vnet_result = zone_check["vnets"][0] if zone_check["vnets"] else {"resolves_private": None, "detail": "unknown"}
    if vnet_result["resolves_private"] is True:
        hop = _hop("dns", "Name resolution", "allow", f"{dst.fqdn} resolves to private endpoint {pe['name']} ({pe['ip']}). {vnet_result['detail']}",
                   resource=zone_check["zone"])
        return hop, Endpoint(query=dst.query, kind="private_endpoint", name=f"{dst.name} via {pe['name']}", id=pe["id"], type=dst.type,
                             subscription_id=dst.subscription_id, location=dst.location, ip=pe["ip"], nic_id=pe["nic_id"], subnet_id=pe["subnet_id"],
                             asg_ids=pe["asg_ids"], fqdn=dst.fqdn, resource=None)
    public_off = str(_p(dst.resource).get("publicNetworkAccess", "")).lower() == "disabled"
    verdict = "deny" if public_off and vnet_result["resolves_private"] is False else "unknown"
    fix = zone_check.get("fix") or f"Link the private DNS zone {zone_check['zone']} to {_short(src.vnet_id)} (or to the VNet of its DNS server)."
    hop = _hop("dns", "Name resolution", verdict,
               f"{dst.fqdn} has private endpoint {pe['name']} ({pe['ip']}), but from {_short(src.vnet_id)} it {vnet_result['detail']}"
               + (" Public network access is disabled, so the public address is unreachable." if verdict == "deny" else ""),
               resource=zone_check["zone"], fix=fix)
    if verdict == "unknown":
        caveats.append("CRIP could not confirm how the source resolves the name; the path is traced to the public endpoint.")
    dst.ip, dst.subnet_id = None, None
    return hop, dst


async def _private_endpoints(az: _Azure, dst: Endpoint) -> list[dict[str, Any]]:
    conns = _p(dst.resource).get("privateEndpointConnections") or []
    pe_ids = {str((_p(c).get("privateEndpoint") or {}).get("id", "")) for c in conns
              if str((_p(c).get("privateLinkServiceConnectionState") or {}).get("status", "Approved")).lower() == "approved"}
    pe_ids.discard("")
    if not pe_ids:
        return []
    pes = await az.graph(f"Resources | where id in~ ({_q(pe_ids)}) | project id, name, subscriptionId, properties", wide=True)
    nic_ids = {str(n.get("id", "")) for pe in pes for n in _p(pe).get("networkInterfaces") or []}
    nics = {str(n.get("id", "")).lower(): n for n in await az.graph(
        f"Resources | where id in~ ({_q(nic_ids)}) | project id, name, type, location, subscriptionId, properties", wide=True)} if nic_ids else {}
    out = []
    for pe in pes:
        nic = next((nics.get(str(n.get("id", "")).lower()) for n in _p(pe).get("networkInterfaces") or [] if str(n.get("id", "")).lower() in nics), None)
        ep = _nic_endpoint(pe.get("name", ""), nic) if nic else None
        groups = [g for c in _p(pe).get("privateLinkServiceConnections") or [] for g in _p(c).get("groupIds") or []]
        fqdns = [str(c.get("fqdn", "")).lower() for c in _p(pe).get("customDnsConfigs") or []]
        out.append({"id": pe.get("id"), "name": pe.get("name"), "ip": ep.ip if ep else None, "nic_id": ep.nic_id if ep else None,
                    "subnet_id": ep.subnet_id if ep else (_p(pe).get("subnet") or {}).get("id"), "asg_ids": ep.asg_ids if ep else frozenset(),
                    "groups": groups, "fqdns": fqdns})
    # Prefer the endpoint for the sub-resource the FQDN names (blob vs file...).
    want = (dst.fqdn or "").split(".")[1] if dst.fqdn and dst.fqdn.count(".") >= 2 else ""
    out.sort(key=lambda e: (dst.fqdn not in e["fqdns"], want.lower() not in [g.lower() for g in e["groups"]], e["ip"] is None))
    return out


async def _zone_check(az: _Azure, net: _Net, fqdn: str | None, pe: dict[str, Any], vnet_ids: list[str | None]) -> dict[str, Any]:
    """Private DNS zone, its A record for the service and, per VNet, whether it will resolve the private address."""
    if not fqdn:
        return {"zone": None, "zone_id": None, "record": None, "vnets": [{"vnet": _short(v), "resolves_private": None, "detail": "has no known FQDN."} for v in vnet_ids]}
    zone = privatelink_zone(fqdn)
    zones = await az.graph(f"Resources | where type =~ 'microsoft.network/privatednszones' and name =~ '{zone}' | project id, name, subscriptionId", wide=True)
    if not zones:
        return {"zone": zone, "zone_id": None, "record": None, "fix": f"Create the private DNS zone {zone}, add an A record for {fqdn.split('.')[0]} -> {pe['ip']} (or a DNS zone group on the private endpoint) and link it to the VNets that call the service.",
                "vnets": [{"vnet": _short(v), "resolves_private": False, "detail": f"resolves to the public address: no private DNS zone {zone} exists."} for v in vnet_ids]}
    zone_ids = [z["id"] for z in zones]
    links = await az.graph("Resources | where type =~ 'microsoft.network/privatednszones/virtualnetworklinks' "
                           f"| where {' or '.join(f'id startswith {chr(39)}{z}/{chr(39)}' for z in zone_ids)} "
                           "| project id, name, vnet = tostring(properties.virtualNetwork.id), state = tostring(properties.virtualNetworkLinkState)", wide=True)
    linked = {str(link.get("vnet", "")).lower() for link in links}
    label = fqdn.split(".")[0]
    record_ips: list[str] = []
    try:
        for zid in zone_ids[:2]:
            for rec in await az.get(f"{zid}/A", DNS_API):
                if str(rec.get("name", "")).lower() == label.lower():
                    record_ips += [str(a.get("ipv4Address")) for a in _p(rec).get("aRecords") or []]
    except AzureApiError as exc:
        az.notes.append(f"The A records of {zone} could not be read ({exc.status_code} {exc.code}).")
    record = {"name": label, "ips": record_ips, "matches_endpoint": pe["ip"] in record_ips if record_ips else False}
    fix = None
    if not record_ips:
        fix = f"Add an A record {label} -> {pe['ip']} in {zone} (attach a DNS zone group to private endpoint {pe['name']} to manage it automatically)."
    elif not record["matches_endpoint"]:
        fix = f"The A record {label} in {zone} points to {', '.join(record_ips)}, not to {pe['name']} ({pe['ip']}). Update it, or use a DNS zone group."
    vnets = []
    await _load(az, net, {v for v in vnet_ids if v})
    for v in vnet_ids:
        vnet = net.get(v)
        dns_servers = list((_p(vnet).get("dhcpOptions") or {}).get("dnsServers") or [])
        if str(v or "").lower() in linked:
            ok = bool(record_ips) and record["matches_endpoint"]
            vnets.append({"vnet": _short(v), "resolves_private": ok,
                          "detail": f"uses Azure DNS through the zone link{'' if ok else ', but the zone has no matching A record'}." if not dns_servers else
                          f"is linked to {zone}, but uses custom DNS servers ({', '.join(dns_servers)}); they must forward to Azure DNS."})
        elif dns_servers:
            server_vnets = [vid for vid in linked if net.get(vid) and np.in_any(dns_servers[0], net.space(vid))]
            if not server_vnets and linked:
                await _load(az, net, linked)
                server_vnets = [vid for vid in linked if np.in_any(dns_servers[0], net.space(vid))]
            vnets.append({"vnet": _short(v), "resolves_private": True if server_vnets and record["matches_endpoint"] else None,
                          "detail": (f"uses DNS server {dns_servers[0]} in {_short(server_vnets[0])}, which is linked to {zone} (assuming it forwards to Azure DNS)."
                                     if server_vnets else f"uses custom DNS servers ({', '.join(dns_servers)}) that CRIP cannot query; they need a forwarder for {zone}.")})
        else:
            vnets.append({"vnet": _short(v), "resolves_private": False,
                          "detail": f"resolves to the public address: {zone} is not linked to this VNet."})
            fix = fix or f"Link the private DNS zone {zone} to {_short(v)}."
    return {"zone": zone, "zone_id": zone_ids[0], "record": record, "linked_vnets": sorted(_short(x) for x in linked), "vnets": vnets, "fix": fix}


async def _firewall_hop(az: _Azure, net: _Net, ip: str | None, protocol: str, src: np.Side, dst: np.Side, port: int,
                        src_ep: Endpoint, dst_label: str) -> tuple[dict[str, Any], str | None]:
    if not ip:
        return _hop("firewall", "Network virtual appliance", "unknown", "The route names a virtual appliance without an address."), None
    fws = await az.graph("Resources | where type =~ 'microsoft.network/azurefirewalls' | mv-expand ipc = properties.ipConfigurations "
                         f"| where tostring(ipc.properties.privateIPAddress) == '{ip}' | project id, name, subscriptionId, properties", wide=True)
    if not fws:
        return _hop("firewall", f"Network virtual appliance {ip}", "unknown",
                    f"Traffic passes through an appliance at {ip} that is not an Azure Firewall CRIP can see; its rules are not visible.",
                    fix="Ask the network team to check the appliance's rules for this flow."), None
    fw = fws[0]
    fp = _p(fw)
    subnet_id = next((((_p(c).get("subnet") or {}).get("id")) for c in fp.get("ipConfigurations") or []), None)
    hub_vnet = _vnet_id_of(subnet_id)
    groups: list[dict[str, Any]] = []
    policy_id = (fp.get("firewallPolicy") or {}).get("id")
    if policy_id:
        policy = await az.graph(f"Resources | where id =~ '{policy_id}' | project id, base = tostring(properties.basePolicy.id)", wide=True)
        for pid in [p for p in [(policy[0].get("base") if policy else None), policy_id] if p]:
            groups += await az.get(f"{pid}/ruleCollectionGroups", NETWORK_API)
    d = np.evaluate_firewall(fw, groups, protocol=protocol, src=src, dst=dst, port=port)
    name = fw.get("name")
    if d.verdict == "allow":
        return _hop("firewall", f"Azure Firewall {name}", "allow", f"Allowed by network rule {d.rule}.", resource=name, rule=d.rule), hub_vnet
    if d.verdict == "deny":
        policy_name = _short(policy_id) if policy_id else name
        return _hop("firewall", f"Azure Firewall {name}", "deny", f"Denied: {d.rule}.", resource=name, rule=d.rule,
                    fix=f"Ask the network team to add a network rule to {policy_name} allowing {protocol} {port} from {src.ip} "
                        f"({_subnet_label(src_ep.subnet_id)}) to {dst_label}."), hub_vnet
    return _hop("firewall", f"Azure Firewall {name}", "unknown", "CRIP could not decide: " + ", ".join(d.undecidable or ["no evaluable rule"]) + ".",
                resource=name, rule=d.rule), hub_vnet


def _peering_hops(net: _Net, src_vnet: str, hub_vnet: str | None, dst_vnet: str) -> list[dict[str, Any]]:
    hops = []
    chain = [src_vnet] + ([hub_vnet] if hub_vnet else []) + [dst_vnet]
    # Traffic leaving the hub appliance is "forwarded" (it did not originate in the hub).
    pairs = [(a, b, i > 0) for i, (a, b) in enumerate(zip(chain, chain[1:])) if a.lower() != b.lower()]
    for a, b, forwarded in pairs:
        ab, ba = net.peering(a, b), net.peering(b, a)
        title = f"Peering {_short(a)} ↔ {_short(b)}"
        if ab is None and ba is None:
            hops.append(_hop("peering", title, "deny", f"{_short(a)} and {_short(b)} are not peered.",
                             fix=f"Peer {_short(a)} and {_short(b)} (both directions)."))
            continue
        state = _p(ab or ba).get("peeringState")
        if str(state).lower() != "connected":
            hops.append(_hop("peering", title, "deny", f"The peering is {state}, so no traffic flows.", resource=(ab or ba).get("name"),
                             fix="Re-create the peering on the side that shows Disconnected, then confirm both sides are Connected."))
            continue
        if forwarded and ba is not None and not _p(ba).get("allowForwardedTraffic"):
            hops.append(_hop("peering", title, "deny", f"Connected, but {_short(b)}'s peering {ba.get('name')} does not allow forwarded traffic, "
                             "so packets forwarded by the hub appliance are dropped.", resource=ba.get("name"),
                             fix=f"Enable 'Allow forwarded traffic' on peering {ba.get('name')} in {_short(b)}."))
            continue
        hops.append(_hop("peering", title, "allow", "Connected" + (" and allows forwarded traffic." if forwarded else "."), resource=(ab or ba).get("name")))
    if hub_vnet and src_vnet.lower() != hub_vnet.lower():
        back = net.peering(src_vnet, hub_vnet)
        if back is not None and not _p(back).get("allowForwardedTraffic"):
            hops.append(_hop("peering", f"Return path {_short(hub_vnet)} → {_short(src_vnet)}", "deny",
                             f"Peering {back.get('name')} in {_short(src_vnet)} does not allow forwarded traffic, so replies from the hub appliance are dropped.",
                             resource=back.get("name"), fix=f"Enable 'Allow forwarded traffic' on peering {back.get('name')} in {_short(src_vnet)}."))
    return hops


async def _service_firewall_hop(az: _Azure, net: _Net, src: Endpoint, dst: Endpoint, src_subnet: dict[str, Any] | None) -> dict[str, Any]:
    vnet_rules = fw_rules = None
    if str(dst.type).lower() == "microsoft.sql/servers":
        try:
            vnet_rules, fw_rules = await asyncio.gather(az.get(f"{dst.id}/virtualNetworkRules", SQL_API), az.get(f"{dst.id}/firewallRules", SQL_API))
        except AzureApiError as exc:
            az.notes.append(f"{dst.name}'s firewall rules could not be read ({exc.status_code} {exc.code}).")
    d = np.service_public_access(dst.resource or {}, src_subnet=src_subnet, src_subnet_id=src.subnet_id, sql_vnet_rules=vnet_rules, sql_firewall_rules=fw_rules)
    return _hop("service_firewall", f"{dst.name} network access", d.verdict, d.detail, resource=dst.name, fix=d.fix)


def _connectivity_response(az: _Azure, args: CheckArgs, src: Endpoint, dst: Endpoint, hops: list[dict[str, Any]],
                           nw: dict[str, Any] | None, caveats: list[str]) -> AgentResponse:
    verdict, decisive = _overall(hops)
    fixes = [h["fix"] for h in hops if h.get("fix") and h["verdict"] in ("deny", "unknown")]
    caveats += az.notes
    if src.alternatives:
        caveats.append(f"'{args.source}' also matches: {'; '.join(src.alternatives)}. Use the resource id to pick another.")
    if dst.alternatives:
        caveats.append(f"'{args.destination}' also matches: {'; '.join(dst.alternatives)}.")
    if az.truncated:
        caveats.append("Some Resource Graph results were truncated.")
    caveats.append("CRIP checks Azure networking only. Not visible: " + "; ".join(NOT_VISIBLE) + ".")
    flow = f"{src.name} → {dst.name} on {args.protocol.value.upper()} {args.port}"
    if verdict == "blocked":
        answer = f"Blocked: {flow}. {decisive['title']}: {decisive['detail']}" + (f" Fix: {decisive['fix']}" if decisive.get("fix") else "")
    elif verdict == "reachable":
        answer = (f"Azure networking allows {flow}: every hop CRIP checked allows it. If the connection still fails, look at the destination's "
                  "OS firewall and whether the service is listening on the port.")
    else:
        answer = f"Undetermined: {flow}. {decisive['title']}: {decisive['detail']}"
    used_azure = any(h["evidence"] == "azure" for h in hops)
    partial = verdict == "unknown" or az.truncated or bool(nw and nw.get("note"))
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if partial else ResponseStatus.OK,
        answer=answer,
        confidence=common.HIGH if verdict != "unknown" and (used_azure or not partial) else common.MEDIUM,
        data={
            "kind": "connectivity_check",
            "source": src.summary(), "destination": dst.summary(), "port": args.port, "protocol": args.protocol.value,
            "verdict": verdict, "blocked_at": decisive["title"] if verdict == "blocked" and decisive else None,
            "hops": hops, "fixes": fixes, "network_watcher_used": used_azure, "not_checked": NOT_VISIBLE,
        },
        query_used=az.query_used(),
        data_timestamp=az.timestamp(),
        sources=az.sources,
        caveats=caveats,
    )


# --------------------------------------------------------------------------- find endpoint


async def find_endpoint(args: FindArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, _ = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{sub}"
    token = await common.user_token(ctx, agent=AGENT, query_used=f"Resource Graph lookup of '{args.query}'")
    if isinstance(token, AgentResponse):
        return token
    az = _Azure(ctx, token, FIND, scope, sub)
    invoked_at = ctx.arm.clock()
    try:
        ep = await _resolve(az, args.query, home_only=False)
        net = _Net()
        if ep and ep.subnet_id:
            await _load(az, net, {ep.vnet_id, ep.nic_nsg_id})
            subnet = net.subnet(ep.subnet_id)
            await _load(az, net, {(_p(subnet).get("networkSecurityGroup") or {}).get("id"), (_p(subnet).get("routeTable") or {}).get("id")})
        pes = await _private_endpoints(az, ep) if ep and ep.kind in ("service", "web_app") and ep.resource else []
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=FIND, scope=scope, query_used=az.query_used(), invoked_at=invoked_at, what="network location")
    if ep is None:
        return AgentResponse(agent=AGENT, status=ResponseStatus.NO_DATA, confidence=common.HIGH, answer=f"Nothing named '{args.query}' was found.",
                         query_used=az.query_used(), sources=az.sources)
    subnet = net.subnet(ep.subnet_id) if ep.subnet_id else None
    vnet = net.get(ep.vnet_id)
    sp = _p(subnet)
    info = {
        **ep.summary(),
        "vnet": vnet.get("name") if vnet else None,
        "vnet_address_space": list((_p(vnet).get("addressSpace") or {}).get("addressPrefixes") or []),
        "subnet_prefix": sp.get("addressPrefix"),
        "subnet_nsg": _short((sp.get("networkSecurityGroup") or {}).get("id")) or None,
        "nic_nsg": _short(ep.nic_nsg_id) or None,
        "route_table": _short((sp.get("routeTable") or {}).get("id")) or None,
        "dns_servers": list((_p(vnet).get("dhcpOptions") or {}).get("dnsServers") or []) or ["Azure-provided"],
        "peerings": [{"name": p.get("name"), "remote": _short((_p(p).get("remoteVirtualNetwork") or {}).get("id")), "state": _p(p).get("peeringState")}
                     for p in _p(vnet).get("virtualNetworkPeerings") or []],
        "private_endpoints": [{"name": p["name"], "ip": p["ip"], "subnet": _subnet_label(p["subnet_id"])} for p in pes],
        "public_network_access": _p(ep.resource).get("publicNetworkAccess") if ep.resource else None,
    }
    where = f"{info['subnet']} ({info['subnet_prefix']})" if ep.subnet_id else ("no virtual network" if ep.kind in ("service", "web_app") else "outside Azure")
    return AgentResponse(
        agent=AGENT, status=ResponseStatus.PARTIAL if az.truncated else ResponseStatus.OK, confidence=common.HIGH,
        answer=f"{ep.name} ({ep.kind.replace('_', ' ')}) is at {ep.ip or 'no private address'} in {where}"
               + (f"; subnet NSG {info['subnet_nsg']}" if info["subnet_nsg"] else "; no subnet NSG")
               + (f"; route table {info['route_table']}" if info["route_table"] else "")
               + (f"; private endpoints: {', '.join(p['name'] + ' ' + str(p['ip']) for p in info['private_endpoints'])}" if pes else "") + ".",
        data={"kind": "network_endpoint", **info},
        query_used=az.query_used(), data_timestamp=az.timestamp(), sources=az.sources,
        caveats=([f"Also matches: {'; '.join(ep.alternatives)}."] if ep.alternatives else []) + az.notes,
    )


# --------------------------------------------------------------------------- effective rules


async def effective_rules(args: ResourceArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, _ = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{sub}"
    token = await common.user_token(ctx, agent=AGENT, query_used=f"Effective NSG rules and routes of '{args.resource}'")
    if isinstance(token, AgentResponse):
        return token
    az = _Azure(ctx, token, EFFECTIVE, scope, sub)
    invoked_at = ctx.arm.clock()
    try:
        ep = await _resolve(az, args.resource, home_only=True)
        if ep is None or not ep.nic_id:
            return AgentResponse(agent=AGENT, status=ResponseStatus.NO_DATA, confidence=common.HIGH,
                                 answer=f"No VM or network interface named '{args.resource}' was found in {scope}. Effective rules exist only for network interfaces.",
                                 query_used=az.query_used(), sources=az.sources)
        nsgs, routes = await asyncio.gather(az.action(f"{ep.nic_id}/effectiveNetworkSecurityGroups"), az.action(f"{ep.nic_id}/effectiveRouteTable"))
    except AzureApiError as exc:
        hint = DIAG_ROLE_HINT if exc.status_code == 403 else "Reader"
        failed = common.failure(exc, agent=AGENT, tool=EFFECTIVE, scope=scope, query_used=az.query_used(), invoked_at=invoked_at,
                                what="effective rules", role_hint=hint)
        if exc.status_code == 400:
            failed.caveats.append("Azure computes effective rules only for a network interface attached to a running VM.")
        return failed
    groups = []
    for g in nsgs.get("value") or []:
        assoc = g.get("association") or {}
        applied = ("subnet " + _subnet_label((assoc.get("subnet") or {}).get("id"))) if assoc.get("subnet") else "network interface"
        rules = []
        for r in g.get("effectiveSecurityRules") or []:
            rules.append({
                "name": str(r.get("name", "")).split("/")[-1], "direction": r.get("direction"), "access": r.get("access"), "priority": r.get("priority"),
                "protocol": r.get("protocol"),
                "source": ", ".join(r.get("sourceAddressPrefixes") or [r.get("sourceAddressPrefix") or "*"]),
                "destination": ", ".join(r.get("destinationAddressPrefixes") or [r.get("destinationAddressPrefix") or "*"]),
                "ports": ", ".join(r.get("destinationPortRanges") or [r.get("destinationPortRange") or "*"]),
            })
        rules.sort(key=lambda r: (r["direction"] != "Inbound", int(r["priority"] or 0)))
        groups.append({"nsg": _short((g.get("networkSecurityGroup") or {}).get("id")), "applied_to": applied, "rules": rules})
    route_rows = [{"source": r.get("source"), "state": r.get("state"), "prefixes": ", ".join(r.get("addressPrefix") or []),
                   "next_hop_type": r.get("nextHopType"), "next_hop_ip": ", ".join(r.get("nextHopIpAddress") or []) or None}
                  for r in routes.get("value") or []]
    route_rows.sort(key=lambda r: (r["state"] != "Active", r["source"] != "User"))
    default = next((r for r in route_rows if r["state"] == "Active" and "0.0.0.0/0" in r["prefixes"]), None)
    return AgentResponse(
        agent=AGENT, status=ResponseStatus.OK, confidence=common.HIGH,
        answer=f"{ep.name} ({ep.ip}) has {sum(len(g['rules']) for g in groups)} effective security rule(s) from {len(groups)} NSG(s)"
               + (f" ({', '.join(g['nsg'] + ' on ' + g['applied_to'] for g in groups)})" if groups else ", no NSG applies")
               + f" and {sum(1 for r in route_rows if r['state'] == 'Active')} active route(s)"
               + (f"; internet-bound traffic goes to {default['next_hop_type']}{' ' + default['next_hop_ip'] if default['next_hop_ip'] else ''}" if default else "") + ".",
        data={"kind": "effective_rules", "resource": ep.name, "ip": ep.ip, "subnet": _subnet_label(ep.subnet_id), "nsgs": groups, "routes": route_rows},
        query_used=az.query_used(), data_timestamp=az.timestamp(), sources=az.sources,
        caveats=["Effective rules and routes are computed by Azure for the running VM at the time of the call."],
    )


# --------------------------------------------------------------------------- private endpoint DNS


async def private_endpoint_dns(args: ResourceArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, _ = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{sub}"
    token = await common.user_token(ctx, agent=AGENT, query_used=f"Private endpoint and DNS check of '{args.resource}'")
    if isinstance(token, AgentResponse):
        return token
    az = _Azure(ctx, token, DNS, scope, sub)
    invoked_at = ctx.arm.clock()
    try:
        ep = await _resolve(az, args.resource, home_only=False)
        if ep is None or ep.resource is None:
            return AgentResponse(agent=AGENT, status=ResponseStatus.NO_DATA, confidence=common.HIGH,
                                 answer=f"No supported service (storage, Key Vault, SQL, Cosmos DB, App Service, ...) named '{args.resource}' was found.",
                                 query_used=az.query_used(), sources=az.sources)
        pes = await _private_endpoints(az, ep)
        net = _Net()
        vnets = await az.graph("Resources | where type =~ 'microsoft.network/virtualnetworks' | project id, name, type, location, subscriptionId, resourceGroup, properties")
        for v in vnets:
            net.resources[str(v.get("id", "")).lower()] = v
        check = await _zone_check(az, net, ep.fqdn, pes[0], [v["id"] for v in vnets]) if pes else None
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=DNS, scope=scope, query_used=az.query_used(), invoked_at=invoked_at, what="private endpoint DNS check")
    public = _p(ep.resource).get("publicNetworkAccess") or "Enabled"
    data: dict[str, Any] = {
        "kind": "private_endpoint_dns", "resource": ep.name, "type": ep.type, "fqdn": ep.fqdn, "public_network_access": public,
        "private_endpoints": [{"name": p["name"], "ip": p["ip"], "subnet": _subnet_label(p["subnet_id"]), "groups": p["groups"]} for p in pes],
    }
    if not pes:
        return AgentResponse(agent=AGENT, status=ResponseStatus.OK, confidence=common.HIGH, data=data,
                             answer=f"{ep.name} has no private endpoint; every caller reaches its public endpoint ({ep.fqdn}), public network access: {public}.",
                             query_used=az.query_used(), data_timestamp=az.timestamp(), sources=az.sources)
    assert check is not None
    data.update(zone=check["zone"], record=check["record"], linked_vnets=check.get("linked_vnets", []), vnets=check["vnets"], fix=check.get("fix"))
    private = [v["vnet"] for v in check["vnets"] if v["resolves_private"] is True]
    public_vnets = [v["vnet"] for v in check["vnets"] if v["resolves_private"] is False]
    rec = check["record"]
    record_text = ("no A record" if not rec or not rec["ips"] else f"A record {rec['name']} -> {', '.join(rec['ips'])}"
                   + ("" if rec["matches_endpoint"] else f" (does NOT match the endpoint's {pes[0]['ip']})"))
    return AgentResponse(
        agent=AGENT, status=ResponseStatus.PARTIAL if az.truncated else ResponseStatus.OK, confidence=common.HIGH, data=data,
        answer=f"{ep.fqdn} has private endpoint {pes[0]['name']} ({pes[0]['ip']}); zone {check['zone']}: {record_text}. "
               f"Resolves privately from {len(private)} VNet(s) in this subscription"
               + (f"; resolves to the PUBLIC address from: {', '.join(public_vnets)}" if public_vnets else "") + "."
               + (f" Fix: {check['fix']}" if check.get("fix") else ""),
        query_used=az.query_used(), data_timestamp=az.timestamp(), sources=az.sources,
        caveats=["CRIP reads DNS configuration (zones, links, records); it does not perform a live DNS lookup from inside the VNet."] + az.notes,
    )


# --------------------------------------------------------------------------- subnet health


async def subnet_health(args: SubnetArgs, ctx: ToolContext) -> AgentResponse:
    try:
        sub, rg = split_subscription_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    scope = f"/subscriptions/{sub}" + (f"/resourceGroups/{rg}" if rg else "")
    token = await common.user_token(ctx, agent=AGENT, query_used="Virtual networks, subnets and peerings (Azure Resource Graph)")
    if isinstance(token, AgentResponse):
        return token
    az = _Azure(ctx, token, SUBNETS, scope, sub)
    invoked_at = ctx.arm.clock()
    filters = (f"| where resourceGroup =~ '{rg}' " if rg else "") + (f"| where name =~ '{args.vnet}' " if args.vnet else "")
    try:
        vnets = await az.graph(f"Resources | where type =~ 'microsoft.network/virtualnetworks' {filters}| project id, name, resourceGroup, location, properties")
    except AzureApiError as exc:
        return common.failure(exc, agent=AGENT, tool=SUBNETS, scope=scope, query_used=az.query_used(), invoked_at=invoked_at, what="subnet health")
    if not vnets:
        return AgentResponse(agent=AGENT, status=ResponseStatus.NO_DATA, confidence=common.HIGH, answer=f"No virtual networks found in {scope}.",
                             query_used=az.query_used(), sources=az.sources)
    subnets, peerings, findings = [], [], []
    spaces: dict[str, tuple] = {}
    for vnet in vnets:
        vp = _p(vnet)
        spaces[vnet["name"]] = np.networks(list((vp.get("addressSpace") or {}).get("addressPrefixes") or []))
        for s in vp.get("subnets") or []:
            sp = _p(s)
            prefix = sp.get("addressPrefix") or next(iter(sp.get("addressPrefixes") or []), "")
            usable = np.usable_addresses(prefix) or 0
            used = len(sp.get("ipConfigurations") or [])
            delegation = ", ".join(_p(d).get("serviceName", "") for d in sp.get("delegations") or []) or None
            pct = round(100 * used / usable, 1) if usable else None
            subnets.append({"vnet": vnet["name"], "subnet": s.get("name"), "prefix": prefix, "usable": usable, "used": used, "free": max(usable - used, 0),
                            "used_pct": pct, "nsg": _short((sp.get("networkSecurityGroup") or {}).get("id")) or None,
                            "route_table": _short((sp.get("routeTable") or {}).get("id")) or None, "delegation": delegation,
                            "service_endpoints": [se.get("service") for se in sp.get("serviceEndpoints") or []]})
            if pct is not None and pct >= 80:
                findings.append({"severity": "High" if pct >= 95 else "Medium", "finding": f"Subnet {vnet['name']}/{s.get('name')} is {pct}% full ({used} of {usable} addresses)",
                                 "fix": "Add an address range to the subnet or move workloads; scaling out (VMSS, AKS nodes, private endpoints) will fail when it is full."})
        for p in vp.get("virtualNetworkPeerings") or []:
            pp = _p(p)
            remote = _short((pp.get("remoteVirtualNetwork") or {}).get("id"))
            spaces.setdefault(remote, np.networks(list((pp.get("remoteAddressSpace") or {}).get("addressPrefixes") or [])))
            row = {"vnet": vnet["name"], "peering": p.get("name"), "remote": remote, "state": pp.get("peeringState"), "sync": pp.get("peeringSyncLevel"),
                   "forwarded_traffic": bool(pp.get("allowForwardedTraffic")), "gateway_transit": bool(pp.get("allowGatewayTransit")), "use_remote_gateways": bool(pp.get("useRemoteGateways"))}
            peerings.append(row)
            if str(row["state"]).lower() != "connected":
                findings.append({"severity": "High", "finding": f"Peering {vnet['name']} → {remote} is {row['state']}", "fix": "Re-create the peering on the disconnected side."})
            elif row["sync"] and str(row["sync"]).lower() != "fullyinsync":
                findings.append({"severity": "Medium", "finding": f"Peering {vnet['name']} → {remote} is {row['sync']} (address space changed)", "fix": "Sync the peering so both sides see the new address space."})
    for a, b, detail in np.overlapping(spaces):
        findings.append({"severity": "Medium", "finding": f"Address spaces of {a} and {b} overlap ({detail})", "fix": "Overlapping VNets cannot be peered or routed to each other; re-address one of them."})
    order = {"High": 0, "Medium": 1, "Low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    subnets.sort(key=lambda s: -(s["used_pct"] or 0))
    delegated = [s for s in subnets if s["delegation"]]
    return AgentResponse(
        agent=AGENT, status=ResponseStatus.PARTIAL if az.truncated else ResponseStatus.OK, confidence=common.HIGH,
        answer=f"{len(vnets)} VNet(s), {len(subnets)} subnet(s), {len(peerings)} peering(s) in {scope}: "
               + ("; ".join(f["finding"] for f in findings[:3]) if findings else "no capacity, peering or overlap problems found") + ".",
        data={"kind": "subnet_health", "scope": scope, "vnet_count": len(vnets), "subnets": subnets, "peerings": peerings, "findings": findings},
        query_used=az.query_used(), data_timestamp=az.timestamp(), sources=az.sources,
        caveats=["Address use counts network interfaces and private endpoints in the subnet; Azure reserves 5 addresses per subnet."]
        + (["Delegated subnets (" + ", ".join(f"{s['vnet']}/{s['subnet']}" for s in delegated[:3]) + ") may use addresses that are not listed as IP configurations (e.g. App Service integration)."] if delegated else []),
    )
