"""Generic Azure Resource Manager HTTP client: retries, paging, provenance.

Flow position: the last hop of "a tool call reaching Azure". Every Azure data
API CRIP uses (Cost Management, Advisor, Resource Graph, subscriptions) is an
ARM API, so they all go through this client, and they all use the same token:
the signed-in user's OBO token, passed in on every call. The client has no
credential of its own, so there is no code path by which it could call Azure as
the platform identity.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# Management group, subscription, or resource group scope. Validated because the
# scope is interpolated into request paths.
_SCOPE_PATTERN = re.compile(
    rf"^(/subscriptions/{_GUID}(/resourceGroups/[-\w.()]{{1,90}})?"
    r"|/providers/Microsoft\.Management/managementGroups/[-\w.()]{1,90})$"
)
_BARE_SUBSCRIPTION = re.compile(rf"^{_GUID}$")
_SUBSCRIPTION_IN_SCOPE = re.compile(rf"^/subscriptions/({_GUID})(?:/resourceGroups/([-\w.()]+))?$")

# Cost Management exposes several throttling headers; honour the largest.
_RETRY_AFTER_HEADERS = (
    "retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
)
_RETRYABLE_STATUS = {429, 503}
_MAX_RETRY_DELAY_SECONDS = 30.0


class InvalidScopeError(ValueError):
    pass


def normalise_scope(scope: str) -> str:
    scope = scope.strip().rstrip("/")
    if _BARE_SUBSCRIPTION.match(scope):
        scope = f"/subscriptions/{scope}"
    if not scope.startswith("/"):
        scope = "/" + scope
    if not _SCOPE_PATTERN.match(scope):
        raise InvalidScopeError(
            f"'{scope}' is not a supported scope. Use a subscription id, "
            "/subscriptions/<id>, /subscriptions/<id>/resourceGroups/<name>, or a management group scope."
        )
    return scope


def split_subscription_scope(scope: str) -> tuple[str, str | None]:
    """'/subscriptions/<id>[/resourceGroups/<rg>]' -> (id, rg). Raises for other scopes."""
    match = _SUBSCRIPTION_IN_SCOPE.match(normalise_scope(scope))
    if not match:
        raise InvalidScopeError(f"'{scope}' must be a subscription or resource-group scope for this tool.")
    return match.group(1).lower(), match.group(2)


class AzureApiError(Exception):
    def __init__(self, status_code: int | None, code: str, message: str, *, request_id: str | None, api: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.api = api


@dataclass
class ArmResult:
    """A (possibly multi-page) ARM response plus the provenance a ``Source`` needs."""

    api: str  # "<METHOD> <first-page URL>", for citation
    payload: dict[str, Any]  # first page, as returned
    items: list[Any]  # accumulated items across pages (rows / value / data)
    invoked_at: datetime
    request_id: str | None
    http_status: int
    pages: int = 1
    truncated: bool = False  # more pages existed than we were allowed to fetch


class ArmClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        arm_endpoint: str = "https://management.azure.com",
        cost_api_version: str = "2023-11-01",
        subscriptions_api_version: str = "2022-12-01",
        advisor_api_version: str = "2023-01-01",
        resource_graph_api_version: str = "2022-10-01",
        metric_column: str = "Cost",
        max_retries: int = 3,
        max_pages: int = 10,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self.endpoint = arm_endpoint.rstrip("/")
        self.cost_api_version = cost_api_version
        self.subscriptions_api_version = subscriptions_api_version
        self.advisor_api_version = advisor_api_version
        self.resource_graph_api_version = resource_graph_api_version
        self.metric_column = metric_column
        self.max_pages = max_pages
        self.clock = clock
        self._max_retries = max_retries
        self._sleep = sleep

    def url(self, path: str, api_version: str, query: str = "") -> str:
        return f"{self.endpoint}{path}?api-version={api_version}{query}"

    async def request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        body: Any = None,
        page_items: Callable[[dict[str, Any]], list[Any]] = lambda p: list(p.get("value", [])),
        next_page: Callable[[dict[str, Any], Any], tuple[str, Any] | None] | None = None,
    ) -> ArmResult:
        """Send a request and follow paging.

        ``next_page(payload, body)`` returns the (url, body) of the next page or
        None. The default follows ``nextLink`` (ARM convention).
        """
        api = f"{method} {url}"
        invoked_at = self.clock()
        response = await self._send(method, url, token, api=api, json=body)
        payload = response.json()
        items = page_items(payload)
        pages = 1
        follow = next_page or _next_link
        nxt = follow(payload, body)
        while nxt and pages < self.max_pages:
            next_url, next_body = nxt
            # Only follow pages on the ARM host: the user's token must never be sent anywhere else.
            if not next_url.startswith(self.endpoint + "/"):
                break
            page = (await self._send(method, next_url, token, api=api, json=next_body)).json()
            items.extend(page_items(page))
            pages += 1
            nxt = follow(page, next_body)
        return ArmResult(
            api=api,
            payload=payload,
            items=items,
            invoked_at=invoked_at,
            request_id=response.headers.get("x-ms-request-id"),
            http_status=response.status_code,
            pages=pages,
            truncated=bool(nxt),
        )

    async def long_running(self, method: str, url: str, token: str, *, body: Any = None, timeout_s: float = 90.0) -> ArmResult:
        """Run an ARM long-running operation (202 + Location) to completion.

        Used by diagnostic *actions* such as Network Watcher's IP flow verify and
        a NIC's effective security rules: POSTs that change nothing but are
        evaluated asynchronously by Azure. Polls the ``Location`` header
        (only on the ARM host) until it returns the result.
        """
        api = f"{method} {url}"
        invoked_at = self.clock()
        response = await self._send(method, url, token, api=api, json=body)
        request_id = response.headers.get("x-ms-request-id")
        waited = 0.0
        while response.status_code == 202:
            location = response.headers.get("location")
            if not location or not location.startswith(self.endpoint + "/"):
                raise AzureApiError(None, "no_operation_location", "Azure accepted the request but gave no result location", request_id=request_id, api=api)
            if waited >= timeout_s:
                raise AzureApiError(None, "operation_timeout", f"Azure did not finish the operation within {int(timeout_s)}s", request_id=request_id, api=api)
            delay = min(_retry_delay(response, 1), 10.0)
            await self._sleep(delay)
            waited += max(delay, 1.0)
            response = await self._send("GET", location, token, api=api)
        payload = response.json() if response.content else {}
        return ArmResult(api=api, payload=payload, items=list(payload.get("value", [])) if isinstance(payload, dict) else [],
                         invoked_at=invoked_at, request_id=request_id, http_status=response.status_code)

    async def _send(self, method: str, url: str, token: str, *, api: str, json: Any = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.request(method, url, headers=headers, json=json)
            except httpx.HTTPError as exc:
                raise AzureApiError(None, "network_error", str(exc) or type(exc).__name__, request_id=None, api=api) from exc
            # Basic throttling retry: honour Retry-After, cap the wait.
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                await self._sleep(_retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise _to_error(response, api)
            return response
        raise AssertionError("unreachable")  # loop always returns or raises


def _next_link(payload: dict[str, Any], body: Any) -> tuple[str, Any] | None:
    link = payload.get("nextLink") or payload.get("properties", {}).get("nextLink")
    return (link, body) if link else None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    values = []
    for name in _RETRY_AFTER_HEADERS:
        raw = response.headers.get(name)
        if raw:
            try:
                values.append(float(raw))
            except ValueError:
                pass
    delay = max(values) if values else float(2**attempt)
    return min(max(delay, 0.0), _MAX_RETRY_DELAY_SECONDS)


def _to_error(response: httpx.Response, api: str) -> AzureApiError:
    code, message = "http_error", response.reason_phrase or "request failed"
    try:
        err = response.json().get("error", {})
        code = err.get("code", code)
        message = err.get("message", message)
    except ValueError:
        pass
    return AzureApiError(response.status_code, code, message, request_id=response.headers.get("x-ms-request-id"), api=api)
