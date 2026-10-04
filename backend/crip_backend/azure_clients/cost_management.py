"""Azure Cost Management: Query API, Forecast API, and the subscriptions list.

Thin wrappers over ``ArmClient``. The query and forecast endpoints are
``POST {scope}/providers/Microsoft.CostManagement/{query|forecast}`` and return
``properties.columns`` + ``properties.rows`` with ``nextLink`` paging.
"""

from __future__ import annotations

from typing import Any

from .arm import ArmClient, ArmResult


def _rows(payload: dict[str, Any]) -> list[Any]:
    return list(payload.get("properties", {}).get("rows", []))


def columns(result: ArmResult) -> list[dict[str, Any]]:
    return list(result.payload.get("properties", {}).get("columns", []))


def query_url(arm: ArmClient, scope: str) -> str:
    return arm.url(f"{scope}/providers/Microsoft.CostManagement/query", arm.cost_api_version)


def forecast_url(arm: ArmClient, scope: str) -> str:
    return arm.url(f"{scope}/providers/Microsoft.CostManagement/forecast", arm.cost_api_version)


async def query(arm: ArmClient, *, scope: str, body: dict[str, Any], token: str) -> ArmResult:
    return await arm.request("POST", query_url(arm, scope), token, body=body, page_items=_rows)


async def forecast(arm: ArmClient, *, scope: str, body: dict[str, Any], token: str) -> ArmResult:
    return await arm.request("POST", forecast_url(arm, scope), token, body=body, page_items=_rows)


async def list_subscriptions(arm: ArmClient, *, token: str) -> ArmResult:
    return await arm.request("GET", arm.url("/subscriptions", arm.subscriptions_api_version), token)
