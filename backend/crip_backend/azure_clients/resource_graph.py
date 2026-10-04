"""Azure Resource Graph queries, read with the user's OBO token.

Resource Graph only returns resources the caller can read, so results are
naturally scoped to the signed-in user's RBAC, like every other CRIP call.
Paging uses ``$skipToken`` in the request body rather than ``nextLink``.
"""

from __future__ import annotations

from typing import Any

from .arm import ArmClient, ArmResult


def resources_url(arm: ArmClient) -> str:
    return arm.url("/providers/Microsoft.ResourceGraph/resources", arm.resource_graph_api_version)


async def query(arm: ArmClient, *, subscriptions: list[str], kql: str, token: str, top: int = 1000) -> ArmResult:
    url = resources_url(arm)
    body: dict[str, Any] = {
        "subscriptions": subscriptions,
        "query": kql,
        "options": {"resultFormat": "objectArray", "$top": top},
    }

    def next_page(payload: dict[str, Any], current: Any) -> tuple[str, Any] | None:
        token_ = payload.get("$skipToken")
        if not token_:
            return None
        nxt = dict(current)
        nxt["options"] = {**current["options"], "$skipToken": token_}
        return url, nxt

    return await arm.request(
        "POST", url, token, body=body, page_items=lambda p: list(p.get("data", [])), next_page=next_page
    )
