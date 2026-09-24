"""HTTP client for the Azure Cost Management Query API and the ARM subscriptions list.

Flow position: the last hop of "a tool call reaching Azure". The CostPulse tool
builds a query and calls ``query()`` with the user's OBO token; this client
POSTs it to ``management.azure.com``, follows ``nextLink`` paging, retries on
throttling, and returns the raw columns/rows plus the provenance (URL, request
id, invocation time) the tool needs to build a ``Source``.

It has no credential of its own: the token is a required argument on every
method, so there is no code path by which this client could call Azure as the
platform identity.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

# Management group, subscription, or resource group scope. Validated because the
# scope is interpolated into the request path.
_SCOPE_PATTERN = re.compile(
    r"^(/subscriptions/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(/resourceGroups/[-\w.()]{1,90})?"
    r"|/providers/Microsoft\.Management/managementGroups/[-\w.()]{1,90})$"
)
_BARE_SUBSCRIPTION = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

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


class AzureApiError(Exception):
    def __init__(self, status_code: int | None, code: str, message: str, *, request_id: str | None, api: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.api = api


@dataclass
class CostQueryResult:
    api: str  # "POST <url>" of the first page, for citation
    columns: list[dict[str, Any]]
    rows: list[list[Any]]
    invoked_at: datetime
    request_id: str | None
    http_status: int
    pages: int
    truncated: bool  # more pages existed than we were allowed to fetch


@dataclass
class SubscriptionListResult:
    api: str
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    invoked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    request_id: str | None = None
    http_status: int = 200


class CostManagementClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        arm_endpoint: str = "https://management.azure.com",
        api_version: str = "2023-11-01",
        subscriptions_api_version: str = "2022-12-01",
        metric_column: str = "Cost",
        max_retries: int = 3,
        max_pages: int = 10,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self.metric_column = metric_column
        self.clock = clock
        self._arm = arm_endpoint.rstrip("/")
        self._api_version = api_version
        self._subscriptions_api_version = subscriptions_api_version
        self._max_retries = max_retries
        self._max_pages = max_pages
        self._sleep = sleep

    def query_url(self, scope: str) -> str:
        return f"{self._arm}{scope}/providers/Microsoft.CostManagement/query?api-version={self._api_version}"

    async def query(self, *, scope: str, body: dict[str, Any], token: str) -> CostQueryResult:
        url = self.query_url(scope)
        api = f"POST {url}"
        invoked_at = self.clock()
        response = await self._send("POST", url, token, api=api, json=body)
        payload = response.json()
        props = payload.get("properties", {})
        columns = list(props.get("columns", []))
        rows = list(props.get("rows", []))
        pages = 1
        next_link = props.get("nextLink")
        # Only follow nextLinks on the ARM host: the user's token must never be sent anywhere else.
        while next_link and next_link.startswith(self._arm + "/") and pages < self._max_pages:
            page = (await self._send("POST", next_link, token, api=api, json=body)).json().get("properties", {})
            rows.extend(page.get("rows", []))
            next_link = page.get("nextLink")
            pages += 1
        return CostQueryResult(
            api=api,
            columns=columns,
            rows=rows,
            invoked_at=invoked_at,
            request_id=response.headers.get("x-ms-request-id"),
            http_status=response.status_code,
            pages=pages,
            truncated=bool(next_link),
        )

    async def list_subscriptions(self, *, token: str) -> SubscriptionListResult:
        url = f"{self._arm}/subscriptions?api-version={self._subscriptions_api_version}"
        api = f"GET {url}"
        invoked_at = self.clock()
        response = await self._send("GET", url, token, api=api)
        subscriptions = list(response.json().get("value", []))
        return SubscriptionListResult(
            api=api,
            subscriptions=subscriptions,
            invoked_at=invoked_at,
            request_id=response.headers.get("x-ms-request-id"),
            http_status=response.status_code,
        )

    async def _send(self, method: str, url: str, token: str, *, api: str, json: Any = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.request(method, url, headers=headers, json=json)
            except httpx.HTTPError as exc:
                raise AzureApiError(None, "network_error", str(exc) or type(exc).__name__, request_id=None, api=api) from exc
            # Basic throttling retry (hackathon scope): honour Retry-After, cap the wait.
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                await self._sleep(_retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise _to_error(response, api)
            return response
        raise AssertionError("unreachable")  # loop always returns or raises


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
