"""Resolves a signed-in user's ``UserAccess``: which subscriptions, at which level, and why.

Flow position: right after token validation, before any tool runs. Sources,
combined by taking the highest grant per subscription:

1. **App roles** in the token (``roles`` claim): CRIP.PlatformAdmin / CostReader / Reader.
2. **Entra groups** in the token (``groups`` claim; Graph lookup on overage)
   matched against the configured group IDs.
3. **Azure RBAC**: role assignments for the user (including via groups) at the
   subscription or above, read with the app identity via
   ``roleAssignments?$filter=assignedTo('<oid>')``. Resource-group-level grants
   do not count: cost and posture views are subscription-wide.

Subscriptions in scope ("the catalog") are those the app's identity can see,
optionally limited to one management group. Results are cached per user for
``access_cache_seconds`` so dashboards don't re-check on every card.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ..auth.entra import AuthenticatedUser
from ..azure_clients import resource_graph
from ..azure_clients.arm import ArmClient, AzureApiError
from ..config import Settings
from .graph import GraphClient, GraphError
from .model import (
    APP_ROLE_COST_READER,
    APP_ROLE_PLATFORM_ADMIN,
    APP_ROLE_READER,
    BUILTIN_ROLES,
    AccessLevel,
    SubscriptionAccess,
    UserAccess,
    level_for_role,
)

log = logging.getLogger(__name__)

TokenFn = Callable[[AuthenticatedUser], Awaitable[str]]
_ROLE_ASSIGNMENTS_API = "2022-04-01"


def _csv(value: str) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in value.split(",") if v.strip())


class AccessResolver:
    def __init__(
        self,
        settings: Settings,
        arm: ArmClient,
        token_for: TokenFn,
        graph: GraphClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._arm = arm
        self._token_for = token_for
        self._graph = graph
        self._clock = clock
        self._admin_groups = _csv(settings.platform_admin_group_ids)
        self._cost_groups = _csv(settings.cost_reader_group_ids)
        self._reader_groups = _csv(settings.reader_group_ids)
        self._cache: dict[str, tuple[float, UserAccess]] = {}
        self._catalog: tuple[float, list[tuple[str, str]]] | None = None
        self._catalog_lock = asyncio.Lock()

    async def resolve(self, user: AuthenticatedUser) -> UserAccess:
        cached = self._cache.get(user.object_id)
        if cached and cached[0] > self._clock():
            return cached[1]
        access = await self._resolve(user)
        self._cache[user.object_id] = (self._clock() + self._settings.access_cache_seconds, access)
        return access

    # ------------------------------------------------------------------ global grants

    async def _global_grants(self, user: AuthenticatedUser) -> tuple[bool, AccessLevel, list[str], list[str]]:
        warnings: list[str] = []
        via: list[str] = []
        admin = APP_ROLE_PLATFORM_ADMIN in user.roles
        level = AccessLevel.NONE
        if admin:
            via.append(f"app-role:{APP_ROLE_PLATFORM_ADMIN}")
        if APP_ROLE_COST_READER in user.roles:
            level = AccessLevel.COST
            via.append(f"app-role:{APP_ROLE_COST_READER}")
        elif APP_ROLE_READER in user.roles:
            level = AccessLevel.RESOURCES
            via.append(f"app-role:{APP_ROLE_READER}")

        groups = {g.lower() for g in user.groups}
        if user.groups_overage and (self._admin_groups or self._cost_groups or self._reader_groups):
            if self._graph is None:
                warnings.append("Your group list was too large to include in the token and Graph is not configured.")
            else:
                try:
                    groups |= {g.lower() for g in await self._graph.transitive_group_ids(user.object_id)}
                except GraphError as exc:
                    warnings.append(f"Group membership could not be read from Microsoft Graph ({exc}).")
        if groups & self._admin_groups:
            admin = True
            via.append("group:platform-admin")
        if groups & self._cost_groups and level < AccessLevel.COST:
            level = AccessLevel.COST
            via.append("group:cost-reader")
        elif groups & self._reader_groups and level < AccessLevel.RESOURCES:
            level = AccessLevel.RESOURCES
            via.append("group:reader")
        if admin:
            level = AccessLevel.COST
        return admin, level, via, warnings

    # ------------------------------------------------------------------ catalog

    async def catalog(self, user: AuthenticatedUser) -> list[tuple[str, str]]:
        """(subscription id, display name) for every subscription CRIP covers."""
        if self._settings.azure_access_mode == "user_obo":
            return await self._list_subscriptions(await self._token_for(user))  # per user in OBO mode
        async with self._catalog_lock:
            if self._catalog and self._catalog[0] > self._clock():
                return self._catalog[1]
            token = await self._token_for(user)
            subs = await self._list_subscriptions(token)
            explicit = _csv(self._settings.subscription_ids)
            if explicit:
                subs = [s for s in subs if s[0] in explicit]
            mg = self._settings.management_group_id
            if mg:
                in_mg = await self._subscriptions_in_management_group(token, [s[0] for s in subs], mg)
                subs = [s for s in subs if s[0] in in_mg]
            self._catalog = (self._clock() + 600, subs)
            return subs

    async def _list_subscriptions(self, token: str) -> list[tuple[str, str]]:
        result = await self._arm.request("GET", self._arm.url("/subscriptions", self._arm.subscriptions_api_version), token)
        return [
            (str(s.get("subscriptionId", "")).lower(), s.get("displayName") or s.get("subscriptionId"))
            for s in result.items
            if s.get("subscriptionId") and s.get("state", "Enabled") != "Disabled"
        ]

    async def _subscriptions_in_management_group(self, token: str, sub_ids: list[str], mg: str) -> set[str]:
        if not sub_ids:
            return set()
        safe_mg = mg.replace("'", "")
        kql = (
            "resourcecontainers | where type =~ 'microsoft.resources/subscriptions'"
            " | mv-expand ancestor = properties.managementGroupAncestorsChain"
            f" | where tostring(ancestor.name) =~ '{safe_mg}' | project subscriptionId"
        )
        result = await resource_graph.query(self._arm, subscriptions=sub_ids, kql=kql, token=token)
        return {str(r.get("subscriptionId", "")).lower() for r in result.items}

    # ------------------------------------------------------------------ RBAC

    async def _rbac(self, token: str, subscription_id: str, user_oid: str) -> tuple[AccessLevel, list[str]]:
        url = self._arm.url(
            f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleAssignments",
            _ROLE_ASSIGNMENTS_API,
            f"&$filter=assignedTo('{user_oid}')",
        )
        result = await self._arm.request("GET", url, token)
        best, via = AccessLevel.NONE, []
        for ra in result.items:
            props = ra.get("properties", {})
            scope = str(props.get("scope", "")).lower()
            # Subscription itself, a management group above it, or the root: not resource groups.
            if not (scope == f"/subscriptions/{subscription_id}" or scope.startswith("/providers/microsoft.management/managementgroups/") or scope == "/"):
                continue
            role = BUILTIN_ROLES.get(str(props.get("roleDefinitionId", "")).rsplit("/", 1)[-1].lower())
            if role is None:
                continue
            level = level_for_role(role)
            if level > AccessLevel.NONE:
                via.append(f"rbac:{role}")
                best = max(best, level)
        return best, sorted(set(via), key=via.index)

    # ------------------------------------------------------------------ combine

    async def _resolve(self, user: AuthenticatedUser) -> UserAccess:
        admin, global_level, global_via, warnings = await self._global_grants(user)
        try:
            catalog = await self.catalog(user)
        except AzureApiError as exc:
            warnings.append(f"CRIP could not list subscriptions ({exc.status_code} {exc.code}).")
            catalog = []

        subscriptions: dict[str, SubscriptionAccess] = {}
        need_rbac = self._settings.rbac_access_check and global_level < AccessLevel.COST
        token = await self._token_for(user) if (need_rbac and catalog) else ""
        semaphore = asyncio.Semaphore(8)

        async def one(sub_id: str, name: str) -> None:
            level, via = global_level, list(global_via)
            if need_rbac:
                async with semaphore:
                    try:
                        rbac_level, rbac_via = await self._rbac(token, sub_id, user.object_id)
                    except AzureApiError as exc:
                        warnings.append(f"RBAC check failed for {name} ({exc.status_code} {exc.code}).")
                        rbac_level, rbac_via = AccessLevel.NONE, []
                level = max(level, rbac_level)
                # The grant that decided the level is listed first (it is what gets audited).
                via = rbac_via + via if rbac_level > global_level else via + rbac_via
            if level > AccessLevel.NONE:
                subscriptions[sub_id] = SubscriptionAccess(sub_id, name, level, tuple(dict.fromkeys(via)))

        await asyncio.gather(*(one(s, n) for s, n in catalog))
        access = UserAccess(
            object_id=user.object_id,
            is_platform_admin=admin,
            global_level=global_level,
            global_via=tuple(global_via),
            subscriptions=dict(sorted(subscriptions.items(), key=lambda kv: kv[1].display_name.lower())),
            resolved_at=datetime.now(UTC),
            warnings=tuple(warnings),
        )
        log.info(
            "access resolved",
            extra={"user": user.object_id, "admin": admin, "subscriptions": json.dumps({k: v.level.name for k, v in subscriptions.items()})},
        )
        return access

    def invalidate(self, user_oid: str | None = None) -> None:
        if user_oid:
            self._cache.pop(user_oid, None)
        else:
            self._cache.clear()
            self._catalog = None


def describe(access: UserAccess) -> dict[str, Any]:
    """JSON-friendly view of a user's access (for /api/me and the UI)."""
    return {
        "is_platform_admin": access.is_platform_admin,
        "global_level": access.global_level.name.lower(),
        "global_via": list(access.global_via),
        "subscriptions": [
            {"subscription_id": s.subscription_id, "display_name": s.display_name, "level": s.level.name.lower(), "via": list(s.via)}
            for s in access.subscriptions.values()
        ],
        "resolved_at": access.resolved_at.isoformat(),
        "warnings": list(access.warnings),
    }
