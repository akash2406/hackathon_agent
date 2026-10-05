"""Azure Advisor recommendations, read with the data token (user OBO or CRIP's identity)."""

from __future__ import annotations

from urllib.parse import quote

from .arm import ArmClient, ArmResult

# Advisor category names as the API returns them -> friendly keys used by CRIP.
CATEGORIES = {
    "Cost": "cost",
    "Security": "security",
    "HighAvailability": "reliability",
    "OperationalExcellence": "operational_excellence",
    "Performance": "performance",
}


def recommendations_url(arm: ArmClient, subscription_id: str, category: str | None = "Cost") -> str:
    query = "&$filter=" + quote(f"Category eq '{category}'") if category else ""
    return arm.url(f"/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations", arm.advisor_api_version, query)


async def recommendations(arm: ArmClient, *, subscription_id: str, token: str, category: str | None) -> ArmResult:
    """All recommendations (category=None) or one Advisor category."""
    return await arm.request("GET", recommendations_url(arm, subscription_id, category), token)


async def cost_recommendations(arm: ArmClient, *, subscription_id: str, token: str) -> ArmResult:
    return await recommendations(arm, subscription_id=subscription_id, token=token, category="Cost")
