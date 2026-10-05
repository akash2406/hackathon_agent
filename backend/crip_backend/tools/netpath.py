"""Deterministic network path evaluation: NSG rules, routes, Azure Firewall rules, PaaS firewalls.

Pure functions over ARM resource JSON (as Resource Graph / ARM return it), no
I/O, so every verdict CRIP computes itself is explainable and unit-tested.
Network Doctor uses these when Azure's own diagnostics (Network Watcher) are not
available for a hop, e.g. the source is an App Service, or the hop is a hub
firewall, and labels such verdicts "evaluated by CRIP".

Every match is tri-state: ``True`` (matches), ``False`` (does not), ``None``
(CRIP cannot tell, e.g. a service tag or IP group it cannot expand). An
undecidable rule that could change the outcome makes the hop ``unknown``,
never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import Any

AZURE_PLATFORM_IP = "168.63.129.16"  # AzureLoadBalancer / Azure DNS / health probes
PRIVATE_RANGES = tuple(ip_network(p) for p in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))

Tri = bool | None

# Azure's built-in rules, used when a resource's JSON omits defaultSecurityRules.
DEFAULT_NSG_RULES: list[dict[str, Any]] = [
    {"name": "AllowVnetInBound", "properties": {"priority": 65000, "direction": "Inbound", "access": "Allow", "protocol": "*", "sourceAddressPrefix": "VirtualNetwork", "destinationAddressPrefix": "VirtualNetwork", "sourcePortRange": "*", "destinationPortRange": "*"}},
    {"name": "AllowAzureLoadBalancerInBound", "properties": {"priority": 65001, "direction": "Inbound", "access": "Allow", "protocol": "*", "sourceAddressPrefix": "AzureLoadBalancer", "destinationAddressPrefix": "*", "sourcePortRange": "*", "destinationPortRange": "*"}},
    {"name": "DenyAllInBound", "properties": {"priority": 65500, "direction": "Inbound", "access": "Deny", "protocol": "*", "sourceAddressPrefix": "*", "destinationAddressPrefix": "*", "sourcePortRange": "*", "destinationPortRange": "*"}},
    {"name": "AllowVnetOutBound", "properties": {"priority": 65000, "direction": "Outbound", "access": "Allow", "protocol": "*", "sourceAddressPrefix": "VirtualNetwork", "destinationAddressPrefix": "VirtualNetwork", "sourcePortRange": "*", "destinationPortRange": "*"}},
    {"name": "AllowInternetOutBound", "properties": {"priority": 65001, "direction": "Outbound", "access": "Allow", "protocol": "*", "sourceAddressPrefix": "*", "destinationAddressPrefix": "Internet", "sourcePortRange": "*", "destinationPortRange": "*"}},
    {"name": "DenyAllOutBound", "properties": {"priority": 65500, "direction": "Outbound", "access": "Deny", "protocol": "*", "sourceAddressPrefix": "*", "destinationAddressPrefix": "*", "sourcePortRange": "*", "destinationPortRange": "*"}},
]


# --------------------------------------------------------------------------- addresses


def parse_ip(value: str | None) -> IPv4Address | None:
    try:
        parsed = ip_address(str(value).strip())
    except ValueError:
        return None
    return parsed if isinstance(parsed, IPv4Address) else None


def networks(prefixes: list[str] | tuple[str, ...]) -> tuple[IPv4Network, ...]:
    out = []
    for p in prefixes:
        try:
            net = ip_network(str(p).strip(), strict=False)
        except ValueError:
            continue
        if isinstance(net, IPv4Network):
            out.append(net)
    return tuple(out)


def in_any(ip: str | None, nets: tuple[IPv4Network, ...]) -> bool:
    addr = parse_ip(ip)
    return bool(addr) and any(addr in n for n in nets)


def is_private(ip: str | None) -> bool:
    return in_any(ip, PRIVATE_RANGES)


@dataclass(frozen=True)
class Side:
    """One end of the flow as an NSG/firewall sees it."""

    ip: str | None  # None = a service's public endpoint whose address CRIP does not know
    asg_ids: frozenset[str] = frozenset()  # application security groups (lower-case ids)
    service_tags: frozenset[str] = frozenset()  # tags this end is known to belong to (e.g. "storage" for a public storage endpoint)


def _address_match(prefix: str, side: Side, vnet_space: tuple[IPv4Network, ...]) -> Tri:
    p = str(prefix).strip()
    low = p.lower()
    if low in ("*", "any", "0.0.0.0/0"):
        return True
    if side.ip is None:  # public service endpoint, address unknown
        if low == "internet" or low.split(".")[0] in side.service_tags:
            return True
        if low in ("virtualnetwork", "azureloadbalancer"):
            return False
        nets = networks([p])
        return False if nets and all(any(n.subnet_of(r) for r in PRIVATE_RANGES) for n in nets) else None
    if low == "virtualnetwork":
        # VNet + peered VNets (+ on-premises via a gateway, which CRIP cannot see).
        return True if in_any(side.ip, vnet_space) else (None if is_private(side.ip) else False)
    if low == "internet":
        return not in_any(side.ip, vnet_space) and not is_private(side.ip) and side.ip != AZURE_PLATFORM_IP
    if low == "azureloadbalancer":
        return side.ip == AZURE_PLATFORM_IP
    if "-" in p and "/" not in p:  # firewall-style range a.b.c.d-e.f.g.h
        lo, _, hi = p.partition("-")
        a, b, x = parse_ip(lo), parse_ip(hi), parse_ip(side.ip)
        if a and b and x:
            return a <= x <= b
    nets = networks([p])
    if nets:
        return in_any(side.ip, nets)
    # A service tag (Storage, Sql.WestEurope, AzureCloud...). Known only if this end was tagged.
    if low.split(".")[0] in side.service_tags:
        return True
    return None if side.service_tags or not is_private(side.ip) else False


def _any(values: list[Tri]) -> Tri:
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def _all(values: list[Tri]) -> Tri:
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def port_match(ranges: list[str], port: int) -> Tri:
    out: list[Tri] = []
    for r in ranges:
        r = str(r).strip()
        if r in ("*", "any", "Any"):
            return True
        lo, _, hi = r.partition("-")
        try:
            out.append(int(lo) <= port <= int(hi or lo))
        except ValueError:
            out.append(None)
    return _any(out) if out else False


def _list(props: dict[str, Any], single: str, plural: str) -> list[str]:
    values = list(props.get(plural) or [])
    if props.get(single):
        values.append(props[single])
    return [str(v) for v in values if str(v).strip()]


# --------------------------------------------------------------------------- NSG


@dataclass
class RuleDecision:
    access: str | None  # "Allow" / "Deny" / None (undetermined)
    rule: str | None = None
    priority: int | None = None
    undecidable: list[str] = field(default_factory=list)  # rules CRIP could not evaluate that could change the outcome

    @property
    def verdict(self) -> str:
        if self.access is None or self.undecidable:
            return "unknown"
        return "allow" if self.access == "Allow" else "deny"


def nsg_rule_matches(props: dict[str, Any], *, protocol: str, src: Side, dst: Side, port: int, vnet_space: tuple[IPv4Network, ...]) -> Tri:
    proto = str(props.get("protocol", "*"))
    if proto != "*" and proto.lower() != protocol.lower():
        return False
    src_ports = _list(props, "sourcePortRange", "sourcePortRanges") or ["*"]
    src_port_ok: Tri = True if "*" in src_ports else None  # the client's ephemeral port is unknown
    dst_port_ok = port_match(_list(props, "destinationPortRange", "destinationPortRanges"), port)

    def addr(prefixes: list[str], asgs: list[dict[str, Any]], side: Side) -> Tri:
        checks: list[Tri] = [_address_match(p, side, vnet_space) for p in prefixes]
        checks += [str(a.get("id", "")).lower() in side.asg_ids for a in asgs]
        return _any(checks) if checks else False

    src_ok = addr(_list(props, "sourceAddressPrefix", "sourceAddressPrefixes"), props.get("sourceApplicationSecurityGroups") or [], src)
    dst_ok = addr(_list(props, "destinationAddressPrefix", "destinationAddressPrefixes"), props.get("destinationApplicationSecurityGroups") or [], dst)
    return _all([src_port_ok, dst_port_ok, src_ok, dst_ok])


def evaluate_nsg(nsg: dict[str, Any], *, direction: str, protocol: str, src: Side, dst: Side, port: int, vnet_space: tuple[IPv4Network, ...]) -> RuleDecision:
    """First matching rule by priority, as Azure processes them; tracks undecidable rules above it."""
    props = nsg.get("properties") or {}
    rules = list(props.get("securityRules") or []) + list(props.get("defaultSecurityRules") or DEFAULT_NSG_RULES)
    rules = [r for r in rules if str((r.get("properties") or {}).get("direction", "")).lower() == direction.lower()]
    rules.sort(key=lambda r: int((r.get("properties") or {}).get("priority", 99999)))
    pending: list[tuple[str, str]] = []  # (rule label, access) of undecidable rules seen so far
    for rule in rules:
        rp = rule.get("properties") or {}
        label = f"{rule.get('name')} (priority {rp.get('priority')})"
        matched = nsg_rule_matches(rp, protocol=protocol, src=src, dst=dst, port=port, vnet_space=vnet_space)
        if matched is True:
            access = str(rp.get("access", "Deny")).capitalize()
            return RuleDecision(access, str(rule.get("name")), int(rp.get("priority", 0)), [lbl for lbl, acc in pending if acc != access])
        if matched is None:
            pending.append((label, str(rp.get("access", "Deny")).capitalize()))
    return RuleDecision(None, undecidable=[lbl for lbl, _ in pending])


# --------------------------------------------------------------------------- routes


@dataclass
class RouteDecision:
    next_hop_type: str  # VnetLocal / VNetPeering / Internet / VirtualAppliance / VirtualNetworkGateway / None
    prefix: str
    origin: str  # "User" (route table) or "Default" (Azure system route)
    route_name: str | None = None
    next_hop_ip: str | None = None
    peer_vnet_id: str | None = None
    undecidable: list[str] = field(default_factory=list)


def effective_route(dst_ip: str | None, *, user_routes: list[dict[str, Any]], vnet_space: tuple[IPv4Network, ...], peers: dict[str, tuple[IPv4Network, ...]]) -> RouteDecision:
    """Longest-prefix match over the subnet's route table and Azure's system routes (user routes win ties)."""
    addr = parse_ip(dst_ip)
    candidates: list[tuple[int, int, RouteDecision]] = []  # (prefix length, origin rank, decision)
    undecidable: list[str] = []
    for route in user_routes:
        rp = route.get("properties") or {}
        prefix = str(rp.get("addressPrefix", ""))
        nets = networks([prefix])
        if not nets:
            undecidable.append(f"{route.get('name')} ({prefix})")  # service-tag route
            continue
        if dst_ip is None and nets[0].prefixlen > 0 and not any(nets[0].subnet_of(r) for r in PRIVATE_RANGES):
            undecidable.append(f"{route.get('name')} ({prefix})")  # public prefix vs an unknown public address
            continue
        if (addr and addr in nets[0]) or (dst_ip is None and nets[0].prefixlen == 0):
            candidates.append((nets[0].prefixlen, 1, RouteDecision(
                str(rp.get("nextHopType", "None")), prefix, "User", route.get("name"), rp.get("nextHopIpAddress"))))
    for net in vnet_space:
        if addr and addr in net:
            candidates.append((net.prefixlen, 0, RouteDecision("VnetLocal", str(net), "Default")))
    for peer_id, spaces in peers.items():
        for net in spaces:
            if addr and addr in net:
                candidates.append((net.prefixlen, 0, RouteDecision("VNetPeering", str(net), "Default", peer_vnet_id=peer_id)))
    for net in PRIVATE_RANGES:
        if addr and addr in net:
            candidates.append((net.prefixlen, 0, RouteDecision("None", str(net), "Default")))
    candidates.append((0, 0, RouteDecision("Internet", "0.0.0.0/0", "Default")))
    _, _, best = max(candidates, key=lambda c: (c[0], c[1]))
    best.undecidable = undecidable
    return best


# --------------------------------------------------------------------------- Azure Firewall


@dataclass
class _FwCollection:
    order: tuple[int, int]
    name: str
    action: str
    rules: list[dict[str, Any]]


def _fw_collections(firewall: dict[str, Any], policy_groups: list[dict[str, Any]]) -> tuple[list[_FwCollection], bool]:
    """Network rule collections in processing order, and whether application rules exist."""
    out: list[_FwCollection] = []
    has_app_rules = False
    for group in policy_groups:  # firewall policy: rule collection groups
        gp = group.get("properties") or {}
        for rc in gp.get("ruleCollections") or []:
            if rc.get("ruleCollectionType") != "FirewallFilterRuleCollection":
                continue
            net = [r for r in rc.get("rules") or [] if r.get("ruleType") == "NetworkRule"]
            has_app_rules |= any(r.get("ruleType") == "ApplicationRule" for r in rc.get("rules") or [])
            if net:
                out.append(_FwCollection((int(gp.get("priority", 65000)), int(rc.get("priority", 65000))), str(rc.get("name")),
                                         str((rc.get("action") or {}).get("type", "Deny")), [_norm_fw_rule(r) for r in net]))
    fp = firewall.get("properties") or {}
    for rc in fp.get("networkRuleCollections") or []:  # classic rules on the firewall itself
        rp = rc.get("properties") or {}
        out.append(_FwCollection((0, int(rp.get("priority", 65000))), str(rc.get("name")),
                                 str((rp.get("action") or {}).get("type", "Deny")), [_norm_fw_rule(r) for r in rp.get("rules") or []]))
    has_app_rules |= bool(fp.get("applicationRuleCollections"))
    out.sort(key=lambda c: c.order)
    return out, has_app_rules


def _norm_fw_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": rule.get("name"),
        "protocols": [str(p).upper() for p in (rule.get("ipProtocols") or rule.get("protocols") or [])],
        "src": list(rule.get("sourceAddresses") or []),
        "src_groups": list(rule.get("sourceIpGroups") or []),
        "dst": list(rule.get("destinationAddresses") or []),
        "dst_groups": list(rule.get("destinationIpGroups") or []),
        "dst_fqdns": list(rule.get("destinationFqdns") or []),
        "ports": list(rule.get("destinationPorts") or []),
    }


def _fw_rule_matches(rule: dict[str, Any], *, protocol: str, src: Side, dst: Side, port: int) -> Tri:
    protos = rule["protocols"]
    if protos and "ANY" not in protos and protocol.upper() not in protos:
        return False
    no_space: tuple[IPv4Network, ...] = ()
    src_checks: list[Tri] = [_address_match(a, src, no_space) for a in rule["src"]] + [None for _ in rule["src_groups"]]
    dst_checks: list[Tri] = [_address_match(a, dst, no_space) for a in rule["dst"]] + [None for _ in rule["dst_groups"] + rule["dst_fqdns"]]
    return _all([port_match(rule["ports"], port), _any(src_checks) if src_checks else False, _any(dst_checks) if dst_checks else False])


def evaluate_firewall(firewall: dict[str, Any], policy_groups: list[dict[str, Any]], *, protocol: str, src: Side, dst: Side, port: int) -> RuleDecision:
    """Network rules by priority (DNAT is not relevant for private east-west flows); no match = deny unless application rules might allow it."""
    collections, has_app_rules = _fw_collections(firewall, policy_groups)
    pending: list[tuple[str, str]] = []
    for coll in collections:
        for rule in coll.rules:
            matched = _fw_rule_matches(rule, protocol=protocol, src=src, dst=dst, port=port)
            label = f"{coll.name} / {rule['name']}"
            if matched is True:
                return RuleDecision(coll.action.capitalize(), label, coll.order[1], [lbl for lbl, acc in pending if acc != coll.action.capitalize()])
            if matched is None:
                pending.append((label, coll.action.capitalize()))
    if has_app_rules and protocol.upper() == "TCP":
        return RuleDecision(None, undecidable=[lbl for lbl, _ in pending] + ["application rules (FQDN-based; not evaluated by CRIP)"])
    return RuleDecision("Deny", "no rule matched (Azure Firewall denies by default)", None, [lbl for lbl, acc in pending if acc == "Allow"])


# --------------------------------------------------------------------------- PaaS service firewalls


@dataclass
class ServiceDecision:
    verdict: str  # allow / deny / unknown
    detail: str
    fix: str | None = None


SERVICE_ENDPOINT_FOR = {
    "microsoft.storage/storageaccounts": "Microsoft.Storage",
    "microsoft.keyvault/vaults": "Microsoft.KeyVault",
    "microsoft.sql/servers": "Microsoft.Sql",
    "microsoft.documentdb/databaseaccounts": "Microsoft.AzureCosmosDB",
    "microsoft.web/sites": "Microsoft.Web",
}


def service_public_access(resource: dict[str, Any], *, src_subnet: dict[str, Any] | None, src_subnet_id: str | None,
                          sql_vnet_rules: list[dict[str, Any]] | None = None, sql_firewall_rules: list[dict[str, Any]] | None = None) -> ServiceDecision:
    """Would the service's own firewall accept a connection to its PUBLIC endpoint from this source?"""
    rtype = str(resource.get("type", "")).lower()
    props = resource.get("properties") or {}
    name = resource.get("name")
    if str(props.get("publicNetworkAccess", "")).lower() == "disabled":
        return ServiceDecision("deny", f"{name} has public network access disabled; only private endpoints can reach it.",
                               "Connect through its private endpoint (check the DNS hop), or enable selected-network access.")
    sub_id = (src_subnet_id or "").lower()
    endpoints = {str(se.get("service", "")).lower() for se in ((src_subnet or {}).get("properties") or {}).get("serviceEndpoints") or []}
    needed = SERVICE_ENDPOINT_FOR.get(rtype)
    has_endpoint = bool(needed) and (needed.lower() in endpoints or any(e.startswith(needed.lower()) for e in endpoints))

    def vnet_rule_decision(vnet_rule_ids: list[str], default_deny: bool, ip_rules: bool) -> ServiceDecision:
        if not default_deny:
            return ServiceDecision("allow", f"{name} accepts traffic from all networks.")
        if sub_id and sub_id in vnet_rule_ids:
            if not has_endpoint:
                return ServiceDecision("deny", f"{name} allows the source subnet, but the subnet has no {needed} service endpoint, so traffic arrives from a public IP.",
                                       f"Enable the {needed} service endpoint on the source subnet.")
            return ServiceDecision("allow", f"{name} allows the source subnet through a virtual network rule (service endpoint {needed}).")
        if ip_rules:
            return ServiceDecision("unknown", f"{name} only accepts selected networks. The source subnet is not listed; IP rules exist, and CRIP cannot see which public IP the source uses outbound.",
                                   f"Add a virtual network rule for the source subnet (with the {needed} service endpoint), or use a private endpoint.")
        return ServiceDecision("deny", f"{name} only accepts selected networks and the source subnet is not one of them.",
                               f"Add a virtual network rule for the source subnet (with the {needed} service endpoint), or use a private endpoint.")

    if rtype in ("microsoft.storage/storageaccounts", "microsoft.keyvault/vaults"):
        acls = props.get("networkAcls") or {}
        return vnet_rule_decision([str(r.get("id", "")).lower() for r in acls.get("virtualNetworkRules") or []],
                                  str(acls.get("defaultAction", "Allow")).lower() == "deny", bool(acls.get("ipRules")))
    if rtype == "microsoft.documentdb/databaseaccounts":
        return vnet_rule_decision([str(r.get("id", "")).lower() for r in props.get("virtualNetworkRules") or []],
                                  bool(props.get("isVirtualNetworkFilterEnabled")) or bool(props.get("ipRules")), bool(props.get("ipRules")))
    if rtype == "microsoft.sql/servers":
        if sql_vnet_rules is None or sql_firewall_rules is None:
            return ServiceDecision("unknown", f"CRIP could not read {name}'s firewall rules.")
        vnet_ids = [str((r.get("properties") or {}).get("virtualNetworkSubnetId", "")).lower() for r in sql_vnet_rules]
        if sub_id and sub_id in vnet_ids:
            return vnet_rule_decision(vnet_ids, True, False)
        if any((r.get("properties") or {}).get("startIpAddress") == "0.0.0.0" and (r.get("properties") or {}).get("endIpAddress") == "0.0.0.0" for r in sql_firewall_rules):
            return ServiceDecision("allow", f"{name} allows Azure services (the 'Allow Azure services and resources' firewall rule).")
        if sql_firewall_rules:
            return ServiceDecision("unknown", f"{name} allows {len(sql_firewall_rules)} IP range(s); CRIP cannot see which public IP the source uses outbound.",
                                   "Add a virtual network rule for the source subnet (with the Microsoft.Sql service endpoint), or use a private endpoint.")
        return ServiceDecision("deny", f"{name} has no firewall or virtual network rule that admits the source.",
                               "Add a virtual network rule for the source subnet (with the Microsoft.Sql service endpoint), or use a private endpoint.")
    return ServiceDecision("unknown", f"CRIP does not evaluate {rtype or 'this service'}'s access restrictions; public network access is not disabled.")


# --------------------------------------------------------------------------- subnets


def usable_addresses(prefix: str) -> int | None:
    nets = networks([prefix])
    return max(nets[0].num_addresses - 5, 0) if nets else None  # Azure reserves 5 addresses per subnet


def overlapping(spaces: dict[str, tuple[IPv4Network, ...]]) -> list[tuple[str, str, str]]:
    """Pairs of VNets whose address spaces overlap (they can never be peered)."""
    out = []
    names = sorted(spaces)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for na in spaces[a]:
                hit = next((nb for nb in spaces[b] if na.overlaps(nb)), None)
                if hit:
                    out.append((a, b, f"{na} overlaps {hit}"))
                    break
    return out
