"""Microsoft Graph lookups with the app's identity: principal names and group membership.

Needs the Graph *application* permission ``Directory.Read.All`` granted to the
web app's managed identity (``scripts/grant-graph-permission.sh``). Without it
calls fail with 403 and the UI says so; nothing is guessed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx


class GraphError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"Microsoft Graph {status}: {message}")
        self.status = status


class GraphClient:
    def __init__(self, http: httpx.AsyncClient, token: Callable[[], Awaitable[str]], endpoint: str = "https://graph.microsoft.com") -> None:
        self._http = http
        self._token = token
        self._base = endpoint.rstrip("/") + "/v1.0"

    async def _request(self, method: str, url: str, json: Any = None) -> dict[str, Any]:
        try:
            response = await self._http.request(method, url, json=json, headers={"Authorization": f"Bearer {await self._token()}"})
        except httpx.HTTPError as exc:
            raise GraphError(None, str(exc)) from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message", response.reason_phrase)
            except ValueError:
                message = response.reason_phrase
            raise GraphError(response.status_code, message)
        return response.json()

    async def principals(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """object id -> {name, type, upn, user_type} for users, groups and service principals."""
        found: dict[str, dict[str, Any]] = {}
        unique = sorted({i for i in ids if i})
        for start in range(0, len(unique), 1000):
            payload = await self._request(
                "POST",
                f"{self._base}/directoryObjects/getByIds",
                {"ids": unique[start : start + 1000], "types": ["user", "group", "servicePrincipal"]},
            )
            for obj in payload.get("value", []):
                otype = str(obj.get("@odata.type", "")).rsplit(".", 1)[-1]
                found[obj["id"]] = {
                    "name": obj.get("displayName"),
                    "type": otype,
                    "upn": obj.get("userPrincipalName") or obj.get("appId"),
                    "user_type": obj.get("userType"),
                }
        return found

    async def transitive_group_ids(self, user_oid: str) -> set[str]:
        url: str | None = f"{self._base}/users/{user_oid}/transitiveMemberOf/microsoft.graph.group?$select=id&$top=999"
        ids: set[str] = set()
        while url:
            payload = await self._request("GET", url)
            ids.update(g["id"] for g in payload.get("value", []))
            url = payload.get("@odata.nextLink")
        return ids
