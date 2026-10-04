"""Azure Advisor recommendations (cost category), read with the user's OBO token."""

from __future__ import annotations

from urllib.parse import quote

from .arm import ArmClient, ArmResult

_COST_FILTER = "&$filter=" + quote("Category eq 'Cost'")


def recommendations_url(arm: ArmClient, subscription_id: str) -> str:
    return arm.url(f"/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations", arm.advisor_api_version, _COST_FILTER)


async def cost_recommendations(arm: ArmClient, *, subscription_id: str, token: str) -> ArmResult:
    return await arm.request("GET", recommendations_url(arm, subscription_id), token)
