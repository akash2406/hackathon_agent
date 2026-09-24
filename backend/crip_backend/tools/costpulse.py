"""CostPulse tools: grounded Azure cost answers from the Cost Management Query API.

Flow position: this is where "a tool call reaching Azure" happens. The
CostPulse agent (hosted in Foundry) asks for ``costpulse_query_costs`` or
``costpulse_list_subscriptions``; the agent gateway routes the call here; we get
the user's OBO token from the ``ToolContext``, call Azure, and turn the raw
response into an ``AgentResponse``.

Honesty rules implemented here:

* Every number in ``data``/``answer`` is computed from the rows Azure returned.
  Nothing is estimated, extrapolated or defaulted.
* ``data_timestamp`` is the latest billing day present in the returned rows
  (Cost Management lags 8-24h), never the time we made the call.
* A failed call returns ``status=error`` with the real HTTP status and Azure
  error code; an empty result returns ``status=no_data``. The contract makes it
  impossible to attach numbers to either.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..auth.obo import NotDelegatedTokenError, OboExchangeError
from ..azure_clients.cost_management import AzureApiError, CostQueryResult, InvalidScopeError, normalise_scope
from ..contracts import AgentResponse, Confidence, ResponseStatus, Source
from .context import ToolContext

AGENT = "costpulse"
QUERY_COSTS = "costpulse_query_costs"
LIST_SUBSCRIPTIONS = "costpulse_list_subscriptions"

LATENCY_CAVEAT = "Azure Cost Management data typically lags 8-24 hours behind actual usage."
_HIGH = Confidence.from_score(0.9)
_MEDIUM = Confidence.from_score(0.6)
_LOW = Confidence.from_score(0.1)


class GroupBy(StrEnum):
    RESOURCE_GROUP = "resource_group"
    RESOURCE_TYPE = "resource_type"
    TAG = "tag"


class Timeframe(StrEnum):
    MONTH_TO_DATE = "month_to_date"
    LAST_MONTH = "last_month"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"


_TIMEFRAME_LABEL = {
    Timeframe.MONTH_TO_DATE: "Month-to-date",
    Timeframe.LAST_MONTH: "Last calendar month",
    Timeframe.LAST_7_DAYS: "Last 7 days",
    Timeframe.LAST_30_DAYS: "Last 30 days",
}
_GROUP_COLUMNS = {
    GroupBy.RESOURCE_GROUP: ("resourcegroupname", "resourcegroup"),
    GroupBy.RESOURCE_TYPE: ("resourcetype",),
    GroupBy.TAG: ("tagvalue",),
}
_COST_COLUMNS = ("totalcost", "cost", "pretaxcost", "costusd")


class QueryCostsArgs(BaseModel):
    """Arguments the CostPulse agent supplies. Mirrors foundry/definitions/costpulse.json."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    group_by: GroupBy = GroupBy.RESOURCE_GROUP
    tag_key: str | None = Field(default=None, min_length=1, max_length=512)
    timeframe: Timeframe = Timeframe.MONTH_TO_DATE
    top_n: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _tag_needs_key(self) -> QueryCostsArgs:
        if self.group_by is GroupBy.TAG and not self.tag_key:
            raise ValueError("tag_key is required when group_by is 'tag'")
        return self


class ListSubscriptionsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def build_query_body(args: QueryCostsArgs, *, metric_column: str, today: date) -> dict[str, Any]:
    if args.group_by is GroupBy.TAG:
        grouping = {"type": "TagKey", "name": args.tag_key}
    else:
        name = "ResourceGroupName" if args.group_by is GroupBy.RESOURCE_GROUP else "ResourceType"
        grouping = {"type": "Dimension", "name": name}

    time_window: dict[str, Any]
    if args.timeframe is Timeframe.MONTH_TO_DATE:
        time_window = {"timeframe": "MonthToDate"}
    elif args.timeframe is Timeframe.LAST_MONTH:
        time_window = {"timeframe": "TheLastMonth"}
    else:
        days = 7 if args.timeframe is Timeframe.LAST_7_DAYS else 30
        start = today - timedelta(days=days - 1)
        time_window = {
            "timeframe": "Custom",
            "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{today.isoformat()}T23:59:59Z"},
        }

    return {
        "type": "ActualCost",
        **time_window,
        "dataset": {
            # Daily granularity even though we report totals: the UsageDate column
            # is what tells us the true freshness of the data (data_timestamp).
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": metric_column, "function": "Sum"}},
            "grouping": [grouping],
        },
    }


def _query_used(api: str, body: dict[str, Any]) -> str:
    return f"{api}\n{json.dumps(body, sort_keys=True)}"


async def query_costs(args: QueryCostsArgs, ctx: ToolContext) -> AgentResponse:
    try:
        scope = normalise_scope(args.scope)
    except InvalidScopeError as exc:
        return AgentResponse(agent=AGENT, status=ResponseStatus.ERROR, answer=str(exc), confidence=_LOW)

    client = ctx.cost_client
    body = build_query_body(args, metric_column=client.metric_column, today=client.clock().date())
    api = f"POST {client.query_url(scope)}"
    query_used = _query_used(api, body)

    try:
        token = await ctx.arm_token()
    except (OboExchangeError, NotDelegatedTokenError) as exc:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.ERROR,
            answer=f"I could not obtain a delegated Azure token for you, so no cost data was retrieved ({exc}).",
            confidence=_LOW,
            query_used=query_used,
            caveats=["No Azure call was made; nothing in this answer comes from cost data."],
        )

    invoked_at = client.clock()
    try:
        result = await client.query(scope=scope, body=body, token=token)
    except AzureApiError as exc:
        return _failure(exc, scope=scope, tool=QUERY_COSTS, query_used=query_used, invoked_at=invoked_at)

    source = Source(
        tool=QUERY_COSTS,
        api=result.api,
        invoked_at=result.invoked_at,
        scope=scope,
        request_id=result.request_id,
        http_status=result.http_status,
    )
    return summarise_costs(args, scope=scope, result=result, query_used=query_used, source=source)


def summarise_costs(
    args: QueryCostsArgs, *, scope: str, result: CostQueryResult, query_used: str, source: Source
) -> AgentResponse:
    label = _TIMEFRAME_LABEL[args.timeframe]
    if not result.rows:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Cost Management returned no cost rows for {scope} ({label.lower()}).",
            confidence=_HIGH,
            query_used=query_used,
            sources=[source],
            caveats=[LATENCY_CAVEAT, "No rows can also mean spend is recorded under a different scope or billing account."],
        )

    index = {str(c.get("name", "")).lower(): i for i, c in enumerate(result.columns)}
    cost_i = next((index[n] for n in _COST_COLUMNS if n in index), None)
    group_i = next((index[n] for n in _GROUP_COLUMNS[args.group_by] if n in index), None)
    date_i = index.get("usagedate")
    currency_i = index.get("currency")
    if cost_i is None or group_i is None or date_i is None:
        # Without a usage date we cannot state data freshness honestly, so we
        # refuse to report the numbers rather than report them unanchored.
        columns = [c.get("name") for c in result.columns]
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.ERROR,
            answer=f"Cost Management returned an unexpected result shape (columns: {columns}); no figures reported.",
            confidence=_LOW,
            query_used=query_used,
            sources=[source],
        )

    untagged = "(untagged)" if args.group_by is GroupBy.TAG else "(unassigned)"
    totals: dict[tuple[str, str], float] = defaultdict(float)
    latest_day: date | None = None
    for row in result.rows:
        group = str(row[group_i] or "").strip() or untagged
        currency = str(row[currency_i]) if currency_i is not None and row[currency_i] else "unknown"
        totals[(group, currency)] += float(row[cost_i] or 0.0)
        day = _parse_usage_date(row[date_i])
        if day and (latest_day is None or day > latest_day):
            latest_day = day

    if latest_day is None:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.ERROR,
            answer="Cost Management rows had no parseable usage dates, so data freshness cannot be stated; no figures reported.",
            confidence=_LOW,
            query_used=query_used,
            sources=[source],
        )

    currencies = sorted({currency for _, currency in totals})
    total_by_currency = {cur: round(sum(v for (_, c), v in totals.items() if c == cur), 2) for cur in currencies}
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    top = [{"group": g, "cost": round(v, 2), "currency": c} for (g, c), v in ranked[: args.top_n]]
    remainder = ranked[args.top_n :]

    caveats = [LATENCY_CAVEAT, f"The latest billing day in the data is {latest_day.isoformat()} (UTC); it may be incomplete."]
    partial_reasons = []
    if result.truncated:
        partial_reasons.append(
            f"Results were truncated after {result.pages} pages; totals cover only the rows retrieved."
        )
    if len(currencies) > 1:
        partial_reasons.append(f"Costs are reported in multiple currencies ({', '.join(currencies)}) and are not summed across them.")
    caveats.extend(partial_reasons)

    group_noun = {GroupBy.RESOURCE_GROUP: "resource groups", GroupBy.RESOURCE_TYPE: "resource types", GroupBy.TAG: f"values of tag '{args.tag_key}'"}[args.group_by]
    totals_text = ", ".join(f"{_money(v)} {c}" for c, v in total_by_currency.items())
    leaders = "; ".join(f"{r['group']}: {_money(r['cost'])} {r['currency']}" for r in top[:5])
    answer = (
        f"{label} actual cost for {scope} is {totals_text} across {len(totals)} {group_noun}. "
        f"Largest: {leaders}. Data is present through {latest_day.isoformat()} (UTC)."
    )

    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if partial_reasons else ResponseStatus.OK,
        answer=answer,
        confidence=_MEDIUM if partial_reasons else _HIGH,
        data={
            "scope": scope,
            "timeframe": args.timeframe.value,
            "group_by": args.group_by.value,
            "tag_key": args.tag_key,
            "total_by_currency": total_by_currency,
            "top": top,
            "remaining_groups": len(remainder),
            "remaining_cost_by_currency": {
                cur: round(sum(v for (_, c), v in remainder if c == cur), 2) for cur in currencies
            },
            "data_through": latest_day.isoformat(),
            "row_count": len(result.rows),
        },
        query_used=query_used,
        data_timestamp=datetime(latest_day.year, latest_day.month, latest_day.day, tzinfo=UTC),
        sources=[source],
        caveats=caveats,
    )


async def list_subscriptions(args: ListSubscriptionsArgs, ctx: ToolContext) -> AgentResponse:
    client = ctx.cost_client
    try:
        token = await ctx.arm_token()
    except (OboExchangeError, NotDelegatedTokenError) as exc:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.ERROR,
            answer=f"I could not obtain a delegated Azure token for you ({exc}).",
            confidence=_LOW,
        )
    invoked_at = client.clock()
    try:
        result = await client.list_subscriptions(token=token)
    except AzureApiError as exc:
        return _failure(exc, scope="/subscriptions", tool=LIST_SUBSCRIPTIONS, query_used=exc.api, invoked_at=invoked_at)

    source = Source(
        tool=LIST_SUBSCRIPTIONS,
        api=result.api,
        invoked_at=result.invoked_at,
        scope="/subscriptions",
        request_id=result.request_id,
        http_status=result.http_status,
    )
    subs = [
        {"subscription_id": s.get("subscriptionId"), "display_name": s.get("displayName"), "state": s.get("state")}
        for s in result.subscriptions
    ]
    if not subs:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer="No Azure subscriptions are visible to your account.",
            confidence=_HIGH,
            query_used=result.api,
            sources=[source],
        )
    names = ", ".join(f"{s['display_name']} ({s['subscription_id']})" for s in subs[:20])
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.OK,
        answer=f"You can see {len(subs)} subscription(s): {names}.",
        confidence=_HIGH,
        data={"subscriptions": subs},
        query_used=result.api,
        # The subscription list is live control-plane data (no billing lag), so
        # the time Azure answered *is* when the data was true.
        data_timestamp=result.invoked_at,
        sources=[source],
    )


def _failure(exc: AzureApiError, *, scope: str, tool: str, query_used: str, invoked_at: datetime) -> AgentResponse:
    if exc.status_code == 403:
        reason = (
            f"Azure denied access to cost data at {scope} (403 {exc.code}). Your account needs Cost Management "
            "Reader (or Reader) on that scope."
        )
    elif exc.status_code == 401:
        reason = f"Azure rejected the delegated token (401 {exc.code})."
    elif exc.status_code == 404:
        reason = f"The scope {scope} was not found or is not visible to your account (404 {exc.code})."
    elif exc.status_code == 429:
        reason = "Cost Management is throttling requests and the retries were exhausted (429). Please try again shortly."
    elif exc.status_code is None:
        reason = f"The call to Azure failed before a response was received ({exc.message})."
    else:
        reason = f"Cost Management returned {exc.status_code} {exc.code}: {exc.message}"
    source = Source(
        tool=tool, api=exc.api, invoked_at=invoked_at, scope=scope, request_id=exc.request_id, http_status=exc.status_code
    )
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.ERROR,
        answer=f"Cost data could not be retrieved. {reason}",
        confidence=_LOW,
        query_used=query_used,
        sources=[source],
        caveats=["No cost figures are available for this request."],
    )


def _parse_usage_date(value: Any) -> date | None:
    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():  # 20260921
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _money(value: float) -> str:
    return f"{value:,.2f}"
