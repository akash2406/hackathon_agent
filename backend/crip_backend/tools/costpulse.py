"""CostPulse tools: grounded Azure spend, trends, anomalies and month-end forecast.

Flow position: where "a tool call reaching Azure" happens for cost questions.
The CostPulse agent (hosted in Foundry) calls one of these tools; the agent
gateway routes it here; we call Azure Cost Management with the user's OBO token
and turn the response into an ``AgentResponse``.

Honesty rules implemented here:

* Every figure is computed from the rows Azure returned. Statistics (anomaly
  detection) are deterministic, and their method is stated in the caveats.
* Forecast figures are Azure Cost Management's own forecast, labelled as such.
  CRIP never extrapolates on its own.
* ``data_timestamp`` is the latest billing day present in the rows (Cost
  Management lags 8-24h), never the time we made the call.
* A failed call returns ``status=error`` with the real HTTP status; an empty
  result returns ``status=no_data``. The contract makes it impossible to attach
  numbers to either.
"""

from __future__ import annotations

import json
import statistics
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..azure_clients import cost_management as cm
from ..azure_clients.arm import ArmResult, AzureApiError, InvalidScopeError, normalise_scope
from ..contracts import AgentResponse, ResponseStatus
from . import common
from .context import ToolContext

AGENT = "costpulse"
QUERY_COSTS = "costpulse_query_costs"
COST_TREND = "costpulse_cost_trend"
FORECAST = "costpulse_forecast_month_end"
LIST_SUBSCRIPTIONS = "costpulse_list_subscriptions"

_ROLE = "Cost Management Reader (or Reader)"


class GroupBy(StrEnum):
    RESOURCE_GROUP = "resource_group"
    RESOURCE_TYPE = "resource_type"
    SERVICE = "service"
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
_DIMENSION = {GroupBy.RESOURCE_GROUP: "ResourceGroupName", GroupBy.RESOURCE_TYPE: "ResourceType", GroupBy.SERVICE: "ServiceName"}
_GROUP_COLUMNS = {
    GroupBy.RESOURCE_GROUP: ("resourcegroupname", "resourcegroup"),
    GroupBy.RESOURCE_TYPE: ("resourcetype",),
    GroupBy.SERVICE: ("servicename",),
    GroupBy.TAG: ("tagvalue",),
}


# --------------------------------------------------------------------------- arguments
# Each mirrors the JSON schema in foundry/definitions/costpulse.json
# (tests/test_foundry_definitions.py keeps them in sync).


class QueryCostsArgs(BaseModel):
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


class CostTrendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)
    days: int = Field(default=30, ge=7, le=90)


class ForecastArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=300)


class ListSubscriptionsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- query bodies


def _aggregation(metric_column: str) -> dict[str, Any]:
    return {"totalCost": {"name": metric_column, "function": "Sum"}}


def _custom_window(today: date, days: int) -> dict[str, Any]:
    start = today - timedelta(days=days - 1)
    return {
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{today.isoformat()}T23:59:59Z"},
    }


def build_query_body(args: QueryCostsArgs, *, metric_column: str, today: date) -> dict[str, Any]:
    if args.group_by is GroupBy.TAG:
        grouping = {"type": "TagKey", "name": args.tag_key}
    else:
        grouping = {"type": "Dimension", "name": _DIMENSION[args.group_by]}

    if args.timeframe is Timeframe.MONTH_TO_DATE:
        window: dict[str, Any] = {"timeframe": "MonthToDate"}
    elif args.timeframe is Timeframe.LAST_MONTH:
        window = {"timeframe": "TheLastMonth"}
    else:
        window = _custom_window(today, 7 if args.timeframe is Timeframe.LAST_7_DAYS else 30)

    return {
        "type": "ActualCost",
        **window,
        "dataset": {
            # Daily granularity even though we report totals: the UsageDate column
            # is what tells us the true freshness of the data (data_timestamp).
            "granularity": "Daily",
            "aggregation": _aggregation(metric_column),
            "grouping": [grouping],
        },
    }


def build_trend_body(args: CostTrendArgs, *, metric_column: str, today: date) -> dict[str, Any]:
    return {
        "type": "ActualCost",
        **_custom_window(today, args.days),
        "dataset": {
            "granularity": "Daily",
            "aggregation": _aggregation(metric_column),
            # Per-service daily cost: totals per day AND what drove each spike.
            "grouping": [{"type": "Dimension", "name": "ServiceName"}],
        },
    }


def build_forecast_body(*, metric_column: str, today: date) -> dict[str, Any]:
    first = today.replace(day=1)
    last = today.replace(day=monthrange(today.year, today.month)[1])
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{first.isoformat()}T00:00:00Z", "to": f"{last.isoformat()}T23:59:59Z"},
        "dataset": {"granularity": "Daily", "aggregation": _aggregation(metric_column)},
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }


def _query_used(api: str, body: dict[str, Any]) -> str:
    return f"{api}\n{json.dumps(body, sort_keys=True)}"


# --------------------------------------------------------------------------- query_costs


async def query_costs(args: QueryCostsArgs, ctx: ToolContext) -> AgentResponse:
    try:
        scope = normalise_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    body = build_query_body(args, metric_column=arm.metric_column, today=arm.clock().date())
    query_used = _query_used(f"POST {cm.query_url(arm, scope)}", body)

    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        result = await cm.query(arm, scope=scope, body=body, token=token)
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=QUERY_COSTS, scope=scope, query_used=query_used, invoked_at=invoked_at, what="cost data", role_hint=_ROLE
        )
    return summarise_costs(args, scope=scope, result=result, query_used=query_used)


def summarise_costs(args: QueryCostsArgs, *, scope: str, result: ArmResult, query_used: str) -> AgentResponse:
    label = _TIMEFRAME_LABEL[args.timeframe]
    src = common.source(result, tool=QUERY_COSTS, scope=scope)
    if not result.items:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Cost Management returned no cost rows for {scope} ({label.lower()}).",
            confidence=common.HIGH,
            query_used=query_used,
            sources=[src],
            caveats=[common.COST_LATENCY_CAVEAT, "No rows can also mean spend is recorded under a different scope or billing account."],
        )

    index = common.column_index(cm.columns(result))
    cost_i = common.cost_column(index)
    group_i = next((index[n] for n in _GROUP_COLUMNS[args.group_by] if n in index), None)
    date_i = index.get("usagedate")
    currency_i = index.get("currency")
    if cost_i is None or group_i is None or date_i is None:
        return _unexpected_shape(result, query_used, src)

    untagged = "(untagged)" if args.group_by is GroupBy.TAG else "(unassigned)"
    totals: dict[tuple[str, str], float] = defaultdict(float)
    latest_day: date | None = None
    for row in result.items:
        group = str(row[group_i] or "").strip() or untagged
        currency = str(row[currency_i]) if currency_i is not None and row[currency_i] else "unknown"
        totals[(group, currency)] += float(row[cost_i] or 0.0)
        day = common.parse_usage_date(row[date_i])
        if day and (latest_day is None or day > latest_day):
            latest_day = day
    if latest_day is None:
        return _no_dates(query_used, src)

    currencies = sorted({currency for _, currency in totals})
    total_by_currency = {cur: round(sum(v for (_, c), v in totals.items() if c == cur), 2) for cur in currencies}
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    top = [{"group": g, "cost": round(v, 2), "currency": c} for (g, c), v in ranked[: args.top_n]]
    remainder = ranked[args.top_n :]

    caveats = [common.COST_LATENCY_CAVEAT, f"The latest billing day in the data is {latest_day.isoformat()} (UTC); it may be incomplete."]
    partial = _partial_reasons(result, currencies)
    caveats.extend(partial)

    group_noun = {
        GroupBy.RESOURCE_GROUP: "resource groups",
        GroupBy.RESOURCE_TYPE: "resource types",
        GroupBy.SERVICE: "services",
        GroupBy.TAG: f"values of tag '{args.tag_key}'",
    }[args.group_by]
    totals_text = ", ".join(f"{common.money(v)} {c}" for c, v in total_by_currency.items())
    leaders = "; ".join(f"{r['group']}: {common.money(r['cost'])} {r['currency']}" for r in top[:5])
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if partial else ResponseStatus.OK,
        answer=(
            f"{label} actual cost for {scope} is {totals_text} across {len(totals)} {group_noun}. "
            f"Largest: {leaders}. Data is present through {latest_day.isoformat()} (UTC)."
        ),
        confidence=common.MEDIUM if partial else common.HIGH,
        data={
            "kind": "breakdown",
            "scope": scope,
            "timeframe": args.timeframe.value,
            "group_by": args.group_by.value,
            "tag_key": args.tag_key,
            "total_by_currency": total_by_currency,
            "top": top,
            "remaining_groups": len(remainder),
            "remaining_cost_by_currency": {cur: round(sum(v for (_, c), v in remainder if c == cur), 2) for cur in currencies},
            "data_through": latest_day.isoformat(),
            "row_count": len(result.items),
        },
        query_used=query_used,
        data_timestamp=common.day_start(latest_day),
        sources=[src],
        caveats=caveats,
    )


# --------------------------------------------------------------------------- cost_trend

# Robust anomaly rule: a day is anomalous when it is more than 3 scaled MADs
# above the window's median AND at least 30% above it. Median/MAD (rather than
# mean/stddev) so that the spike itself does not inflate the baseline.
_MAD_SCALE = 1.4826
_MAD_THRESHOLD = 3.0
_MIN_RELATIVE_JUMP = 0.30


async def cost_trend(args: CostTrendArgs, ctx: ToolContext) -> AgentResponse:
    try:
        scope = normalise_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    body = build_trend_body(args, metric_column=arm.metric_column, today=arm.clock().date())
    query_used = _query_used(f"POST {cm.query_url(arm, scope)}", body)
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        result = await cm.query(arm, scope=scope, body=body, token=token)
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=COST_TREND, scope=scope, query_used=query_used, invoked_at=invoked_at, what="cost data", role_hint=_ROLE
        )
    return summarise_trend(args, scope=scope, result=result, query_used=query_used)


def summarise_trend(args: CostTrendArgs, *, scope: str, result: ArmResult, query_used: str) -> AgentResponse:
    src = common.source(result, tool=COST_TREND, scope=scope)
    if not result.items:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Cost Management returned no daily cost for {scope} in the last {args.days} days.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=[src],
            caveats=[common.COST_LATENCY_CAVEAT],
        )
    index = common.column_index(cm.columns(result))
    cost_i, date_i = common.cost_column(index), index.get("usagedate")
    service_i, currency_i = index.get("servicename"), index.get("currency")
    if cost_i is None or date_i is None or service_i is None:
        return _unexpected_shape(result, query_used, src)

    # Trend maths only makes sense in one currency: use the dominant one.
    by_currency: dict[str, float] = defaultdict(float)
    for row in result.items:
        by_currency[str(row[currency_i]) if currency_i is not None and row[currency_i] else "unknown"] += float(row[cost_i] or 0)
    currency = max(by_currency, key=lambda c: by_currency[c])

    daily: dict[date, float] = defaultdict(float)
    per_service: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for row in result.items:
        row_currency = str(row[currency_i]) if currency_i is not None and row[currency_i] else "unknown"
        day = common.parse_usage_date(row[date_i])
        if day is None or row_currency != currency:
            continue
        cost = float(row[cost_i] or 0)
        daily[day] += cost
        per_service[str(row[service_i] or "(unknown service)")][day] += cost
    if not daily:
        return _no_dates(query_used, src)

    days = sorted(daily)
    values = [daily[d] for d in days]
    latest = days[-1]
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values]) * _MAD_SCALE
    threshold = max(median + _MAD_THRESHOLD * mad, median * (1 + _MIN_RELATIVE_JUMP))

    anomalies = []
    for d in days:
        if daily[d] > threshold and daily[d] - median > 0.01:
            drivers = []
            for service, series in per_service.items():
                service_median = statistics.median([series.get(x, 0.0) for x in days])
                delta = series.get(d, 0.0) - service_median
                if delta > 0:
                    drivers.append({"service": service, "cost": round(series.get(d, 0.0), 2), "typical": round(service_median, 2), "delta": round(delta, 2)})
            drivers.sort(key=lambda x: x["delta"], reverse=True)
            anomalies.append(
                {"date": d.isoformat(), "cost": round(daily[d], 2), "baseline": round(median, 2), "delta": round(daily[d] - median, 2), "drivers": drivers[:3]}
            )

    half = len(days) // 2
    first_avg = statistics.mean(values[:half]) if half else values[0]
    second_avg = statistics.mean(values[half:])
    change_pct = round((second_avg - first_avg) / first_avg * 100, 1) if first_avg > 0 else None
    service_totals = sorted(
        ((s, sum(series.values())) for s, series in per_service.items()), key=lambda x: x[1], reverse=True
    )

    caveats = [
        common.COST_LATENCY_CAVEAT,
        f"The latest billing day in the data is {latest.isoformat()} (UTC); it may be incomplete and look lower than usual.",
        "Anomaly rule: a day is flagged when its cost is more than 3 scaled median absolute deviations above "
        "the window median and at least 30% above it. 'Drivers' are services whose cost that day exceeded their own median.",
        "Days with no cost rows are omitted from the series, not shown as zero.",
    ]
    partial = _partial_reasons(result, sorted(by_currency))
    caveats.extend(partial)

    direction = "flat"
    if change_pct is not None and abs(change_pct) >= 5:
        direction = "rising" if change_pct > 0 else "falling"
    anomaly_text = (
        "Anomalous days: " + "; ".join(
            f"{a['date']} {common.money(a['cost'])} {currency} (typical {common.money(a['baseline'])}"
            + (f", driven by {a['drivers'][0]['service']} +{common.money(a['drivers'][0]['delta'])}" if a["drivers"] else "")
            + ")"
            for a in anomalies
        )
        if anomalies
        else "No anomalous days detected."
    )
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if partial else ResponseStatus.OK,
        answer=(
            f"Daily cost for {scope} over {len(days)} days with data: total {common.money(sum(values))} {currency}, "
            f"typical day {common.money(median)} {currency}, trend {direction}"
            + (f" ({change_pct:+}% second half vs first half)" if change_pct is not None else "")
            + f". {anomaly_text} Data is present through {latest.isoformat()} (UTC)."
        ),
        confidence=common.MEDIUM if partial else common.HIGH,
        data={
            "kind": "trend",
            "scope": scope,
            "currency": currency,
            "series": [{"date": d.isoformat(), "cost": round(daily[d], 2)} for d in days],
            "anomalies": anomalies,
            "median_daily_cost": round(median, 2),
            "anomaly_threshold": round(threshold, 2),
            "total": round(sum(values), 2),
            "change_pct_second_half_vs_first": change_pct,
            "top_services": [{"group": s, "cost": round(v, 2), "currency": currency} for s, v in service_totals[:8]],
            "data_through": latest.isoformat(),
        },
        query_used=query_used,
        data_timestamp=common.day_start(latest),
        sources=[src],
        caveats=caveats,
    )


# --------------------------------------------------------------------------- forecast


async def forecast_month_end(args: ForecastArgs, ctx: ToolContext) -> AgentResponse:
    try:
        scope = normalise_scope(args.scope)
    except InvalidScopeError as exc:
        return common.invalid_scope(AGENT, str(exc))
    arm = ctx.arm
    today = arm.clock().date()
    body = build_forecast_body(metric_column=arm.metric_column, today=today)
    query_used = _query_used(f"POST {cm.forecast_url(arm, scope)}", body)
    token = await common.user_token(ctx, agent=AGENT, query_used=query_used)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        result = await cm.forecast(arm, scope=scope, body=body, token=token)
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=FORECAST, scope=scope, query_used=query_used, invoked_at=invoked_at, what="forecast", role_hint=_ROLE
        )
    return summarise_forecast(scope=scope, result=result, query_used=query_used, today=today)


def summarise_forecast(*, scope: str, result: ArmResult, query_used: str, today: date) -> AgentResponse:
    src = common.source(result, tool=FORECAST, scope=scope)
    month = today.strftime("%B %Y")
    if not result.items:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Cost Management returned neither actual nor forecast cost for {scope} in {month}.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=[src],
            caveats=[common.COST_LATENCY_CAVEAT],
        )
    index = common.column_index(cm.columns(result))
    cost_i, date_i = common.cost_column(index), index.get("usagedate")
    status_i, currency_i = index.get("coststatus"), index.get("currency")
    if cost_i is None or date_i is None or status_i is None:
        return _unexpected_shape(result, query_used, src)

    actual: dict[date, float] = defaultdict(float)
    forecast: dict[date, float] = defaultdict(float)
    currencies: set[str] = set()
    for row in result.items:
        day = common.parse_usage_date(row[date_i])
        if day is None:
            continue
        if currency_i is not None and row[currency_i]:
            currencies.add(str(row[currency_i]))
        target = actual if str(row[status_i]).lower() == "actual" else forecast
        target[day] += float(row[cost_i] or 0)

    if not actual:
        # Without actual billing days we cannot anchor the data timestamp honestly.
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer=f"Cost Management has no actual cost yet for {scope} in {month}, so no month-end projection is reported.",
            confidence=common.HIGH,
            query_used=query_used,
            sources=[src],
            caveats=[common.COST_LATENCY_CAVEAT],
        )

    currency = ", ".join(sorted(currencies)) or "unknown"
    latest_actual = max(actual)
    actual_total = round(sum(actual.values()), 2)
    forecast_total = round(sum(forecast.values()), 2)
    projected = round(actual_total + forecast_total, 2)
    caveats = [
        common.COST_LATENCY_CAVEAT,
        "Forecast figures are Azure Cost Management's own forecast, not CRIP's; they change as usage changes.",
        f"Actual cost is complete through {latest_actual.isoformat()} (UTC); later days in {month} are forecast.",
    ]
    partial_reasons = _partial_reasons(result, sorted(currencies))
    if not forecast:
        partial_reasons.append("Azure returned no forecast rows (forecasts need enough usage history); only actual cost is shown.")
    caveats.extend(partial_reasons)

    series = [{"date": d.isoformat(), "cost": round(v, 2), "kind": "actual"} for d, v in sorted(actual.items())]
    series += [{"date": d.isoformat(), "cost": round(v, 2), "kind": "forecast"} for d, v in sorted(forecast.items())]
    answer = (
        f"{month} for {scope}: actual cost so far {common.money(actual_total)} {currency} (through {latest_actual.isoformat()}). "
        + (
            f"Azure forecasts a further {common.money(forecast_total)} {currency}, for a projected month-end total of "
            f"{common.money(projected)} {currency}."
            if forecast
            else "Azure returned no forecast for the rest of the month."
        )
    )
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.PARTIAL if partial_reasons else ResponseStatus.OK,
        answer=answer,
        confidence=common.MEDIUM if partial_reasons else common.HIGH,
        data={
            "kind": "forecast",
            "scope": scope,
            "month": today.strftime("%Y-%m"),
            "currency": currency,
            "actual_to_date": actual_total,
            "forecast_remaining": forecast_total if forecast else None,
            "projected_month_end": projected if forecast else None,
            "series": series,
            "data_through": latest_actual.isoformat(),
        },
        query_used=query_used,
        data_timestamp=common.day_start(latest_actual),
        sources=[src],
        caveats=caveats,
    )


# --------------------------------------------------------------------------- subscriptions


async def list_subscriptions(args: ListSubscriptionsArgs, ctx: ToolContext) -> AgentResponse:
    arm = ctx.arm
    token = await common.user_token(ctx, agent=AGENT)
    if isinstance(token, AgentResponse):
        return token
    invoked_at = arm.clock()
    try:
        result = await cm.list_subscriptions(arm, token=token)
    except AzureApiError as exc:
        return common.failure(
            exc, agent=AGENT, tool=LIST_SUBSCRIPTIONS, scope="/subscriptions", query_used=exc.api, invoked_at=invoked_at, what="subscription list"
        )
    src = common.source(result, tool=LIST_SUBSCRIPTIONS, scope="/subscriptions")
    subs = [
        {"subscription_id": s.get("subscriptionId"), "display_name": s.get("displayName"), "state": s.get("state")}
        for s in result.items
    ]
    if ctx.access is not None:
        # In app_identity mode Azure lists everything CRIP's identity can read:
        # only show what this user is allowed to see, and at which level.
        allowed = ctx.access.subscriptions
        subs = [
            {**s, "access": allowed[str(s["subscription_id"]).lower()].level.name.lower()}
            for s in subs
            if str(s["subscription_id"]).lower() in allowed
        ]
    if not subs:
        return AgentResponse(
            agent=AGENT,
            status=ResponseStatus.NO_DATA,
            answer="No Azure subscriptions are visible to your account.",
            confidence=common.HIGH,
            query_used=result.api,
            sources=[src],
        )
    names = ", ".join(f"{s['display_name']} ({s['subscription_id']})" for s in subs[:20])
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.OK,
        answer=f"You can see {len(subs)} subscription(s): {names}.",
        confidence=common.HIGH,
        data={"kind": "subscriptions", "subscriptions": subs},
        query_used=result.api,
        # The subscription list is live control-plane data (no billing lag), so
        # the time Azure answered *is* when the data was true.
        data_timestamp=result.invoked_at,
        sources=[src],
    )


# --------------------------------------------------------------------------- shared


def _partial_reasons(result: ArmResult, currencies: list[str]) -> list[str]:
    reasons = []
    if result.truncated:
        reasons.append(f"Results were truncated after {result.pages} pages; totals cover only the rows retrieved.")
    if len(currencies) > 1:
        reasons.append(f"Costs are reported in multiple currencies ({', '.join(currencies)}) and are not summed across them.")
    return reasons


def _unexpected_shape(result: ArmResult, query_used: str, src: Any) -> AgentResponse:
    # Without a usage date we cannot state data freshness honestly, so we
    # refuse to report the numbers rather than report them unanchored.
    names = [c.get("name") for c in cm.columns(result)]
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.ERROR,
        answer=f"Cost Management returned an unexpected result shape (columns: {names}); no figures reported.",
        confidence=common.LOW,
        query_used=query_used,
        sources=[src],
    )


def _no_dates(query_used: str, src: Any) -> AgentResponse:
    return AgentResponse(
        agent=AGENT,
        status=ResponseStatus.ERROR,
        answer="Cost Management rows had no parseable usage dates, so data freshness cannot be stated; no figures reported.",
        confidence=common.LOW,
        query_used=query_used,
        sources=[src],
    )
