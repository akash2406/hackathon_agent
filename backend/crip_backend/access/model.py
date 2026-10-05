"""Access model: what a signed-in user may see, per subscription.

Three levels, decided per subscription:

* ``RESOURCES``: inventory, network posture, policy, security score, non-cost
  Advisor. Granted by Azure *Reader*-type roles or the ``CRIP.Reader`` app role
  / reader groups.
* ``COST``: everything above plus spend, trends, forecast, savings. Granted by
  cost-capable Azure roles (Owner, Contributor, Cost Management
  Reader/Contributor, Billing Reader) or ``CRIP.CostReader`` / cost groups.
* **Platform admin** (a flag, not a level): ``CRIP.PlatformAdmin`` app role or
  platform groups. COST on every subscription in scope plus the Admin area.

The effective level is the *highest* grant from any source (Azure RBAC at the
subscription or above, app role, group). Why a Reader does not see cost: the
team decided cost visibility is a deliberate grant, so plain Reader (which
technically can read cost data) is limited to the resource views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

from ..azure_clients.arm import InvalidScopeError, normalise_scope


class AccessLevel(IntEnum):
    NONE = 0
    RESOURCES = 1
    COST = 2


class Requirement(IntEnum):
    """What a tool or endpoint needs. ADMIN is above any subscription level."""

    RESOURCES = 1
    COST = 2
    ADMIN = 3


APP_ROLE_PLATFORM_ADMIN = "CRIP.PlatformAdmin"
APP_ROLE_COST_READER = "CRIP.CostReader"
APP_ROLE_READER = "CRIP.Reader"

# Built-in Azure role definition IDs (identical in every tenant).
BUILTIN_ROLES: dict[str, str] = {
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "acdd72a7-3385-48ef-bd42-f606fba81ae7": "Reader",
    "72fafb9e-0641-4937-9268-a91bfd8191a3": "Cost Management Reader",
    "434105ed-43f6-45c7-a02f-909b2ba83430": "Cost Management Contributor",
    "fa23ad8b-c56e-40d8-ac0c-ce449e1d2c64": "Billing Reader",
    "39bc4728-0917-49c7-9d2c-d95423bc2eb4": "Security Reader",
    "43d0d8ad-25c7-4714-9337-8ba259a9fe05": "Monitoring Reader",
    "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9": "User Access Administrator",
}
COST_ROLES = frozenset({"Owner", "Contributor", "Cost Management Reader", "Cost Management Contributor", "Billing Reader"})
RESOURCE_ROLES = frozenset({"Reader", "Security Reader", "Monitoring Reader", "User Access Administrator"})


def level_for_role(role_name: str) -> AccessLevel:
    if role_name in COST_ROLES:
        return AccessLevel.COST
    if role_name in RESOURCE_ROLES:
        return AccessLevel.RESOURCES
    return AccessLevel.NONE  # custom / data-plane roles grant nothing here (documented)


@dataclass(frozen=True)
class SubscriptionAccess:
    subscription_id: str
    display_name: str
    level: AccessLevel
    via: tuple[str, ...]  # e.g. ("rbac:Reader", "group:cost-readers")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    via: str | None = None


_LEVEL_NAME = {AccessLevel.RESOURCES: "resource views", AccessLevel.COST: "cost views"}


@dataclass(frozen=True)
class UserAccess:
    object_id: str
    is_platform_admin: bool
    global_level: AccessLevel
    global_via: tuple[str, ...]
    subscriptions: dict[str, SubscriptionAccess]  # keyed by lower-case subscription id
    resolved_at: datetime
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def check(self, scope: str | None, required: Requirement) -> Decision:
        if required is Requirement.ADMIN:
            if self.is_platform_admin:
                return Decision(True, "platform admin", _first(self.global_via))
            return Decision(False, "This needs the CRIP Platform admin role (app role CRIP.PlatformAdmin or a platform group).")
        if scope is None:
            if self.subscriptions or self.is_platform_admin:
                return Decision(True, "any subscription", _first(self.global_via))
            return Decision(False, "You do not have access to any subscription covered by CRIP.")
        try:
            scope = normalise_scope(scope)
        except InvalidScopeError as exc:
            return Decision(False, str(exc))
        if scope.startswith("/providers/Microsoft.Management/managementGroups/"):
            if self.is_platform_admin:
                return Decision(True, "platform admin", _first(self.global_via))
            return Decision(False, "Management-group scope is available to platform admins only.")
        sub_id = scope.split("/")[2].lower()
        access = self.subscriptions.get(sub_id)
        if access is None:
            return Decision(False, f"You do not have access to subscription {sub_id} in CRIP.")
        if access.level < AccessLevel(int(required)):
            return Decision(
                False,
                f"Your access to {access.display_name} allows {_LEVEL_NAME.get(access.level, 'no views')} only. "
                "Cost views need Cost Management Reader, Contributor or Owner on the subscription, "
                "or the CRIP.CostReader role.",
            )
        return Decision(True, "subscription access", _first(access.via))


def _first(via: tuple[str, ...]) -> str | None:
    return via[0] if via else None
