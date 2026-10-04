"""CostPulse tool function: grounded answers, honest failures, OBO token on the wire."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from crip_backend.auth.obo import OboExchangeError
from crip_backend.contracts import ResponseStatus
from crip_backend.tools import costpulse
from crip_backend.tools.registry import execute_tool

from .conftest import OTHER_OID, SUBSCRIPTION, FakeTokenSource, cost_query_payload, make_arm_token

QUERY_URL = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CostManagement/query"
    "?api-version=2023-11-01"
)
ROWS = [
    [120.5, 20260920, "rg-app", "USD"],
    [30.25, 20260921, "rg-app", "USD"],
    [80.0, 20260921, "rg-data", "USD"],
]


def args(**kw):
    return costpulse.QueryCostsArgs(scope=SUBSCRIPTION, **kw)


@respx.mock
async def test_grounded_answer_uses_billing_date_and_user_token(tool_ctx, token_source):
    route = respx.post(QUERY_URL).mock(
        return_value=httpx.Response(200, json=cost_query_payload(ROWS), headers={"x-ms-request-id": "req-1"})
    )

    result = await costpulse.query_costs(args(), tool_ctx)

    assert result.status is ResponseStatus.OK
    assert result.data["total_by_currency"] == {"USD": 230.75}
    assert result.data["top"][0] == {"group": "rg-app", "cost": 150.75, "currency": "USD"}
    # data_timestamp is the latest billing day in the rows, NOT the call time (2026-09-22 10:00).
    assert result.data_timestamp == datetime(2026, 9, 21, tzinfo=UTC)
    source = result.sources[0]
    assert source.scope == f"/subscriptions/{SUBSCRIPTION}"
    assert source.auth == "user_obo" and source.request_id == "req-1" and source.http_status == 200
    assert result.query_used.startswith(f"POST {QUERY_URL}")
    # The bearer token on the wire is exactly the user's OBO token.
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {token_source.token}"
    assert token_source.calls == [(tool_ctx.session_id, tool_ctx.user.object_id)]


@respx.mock
async def test_empty_result_is_no_data_not_zero(tool_ctx):
    respx.post(QUERY_URL).mock(return_value=httpx.Response(200, json=cost_query_payload([])))
    result = await costpulse.query_costs(args(timeframe="last_7_days"), tool_ctx)
    assert result.status is ResponseStatus.NO_DATA
    assert result.data is None
    assert result.sources and result.query_used


@respx.mock
async def test_forbidden_is_an_honest_error(tool_ctx):
    respx.post(QUERY_URL).mock(
        return_value=httpx.Response(403, json={"error": {"code": "AuthorizationFailed", "message": "no access"}})
    )
    result = await costpulse.query_costs(args(), tool_ctx)
    assert result.status is ResponseStatus.ERROR
    assert result.data is None
    assert "403" in result.answer and "Cost Management Reader" in result.answer
    assert result.sources[0].http_status == 403


@respx.mock
async def test_throttling_is_retried(tool_ctx, fake_sleep):
    respx.post(QUERY_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "3"}),
            httpx.Response(200, json=cost_query_payload(ROWS)),
        ]
    )
    result = await costpulse.query_costs(args(), tool_ctx)
    assert result.status is ResponseStatus.OK
    assert fake_sleep.delays == [3.0]


@respx.mock
async def test_truncated_paging_is_partial(tool_ctx):
    next_link = QUERY_URL + "&$skiptoken=abc"
    respx.post(next_link).mock(return_value=httpx.Response(200, json=cost_query_payload(ROWS[1:], next_link=next_link)))
    respx.post(QUERY_URL).mock(return_value=httpx.Response(200, json=cost_query_payload(ROWS[:1], next_link=next_link)))
    tool_ctx.arm.max_pages = 2
    result = await costpulse.query_costs(args(), tool_ctx)
    assert result.status is ResponseStatus.PARTIAL
    assert any("truncated" in c for c in result.caveats)


@respx.mock
async def test_obo_failure_means_no_azure_call(tool_ctx):
    route = respx.post(QUERY_URL)
    tool_ctx.tokens = FakeTokenSource(error=OboExchangeError("invalid_grant", "AADSTS65001 consent required"))
    result = await costpulse.query_costs(args(), tool_ctx)
    assert result.status is ResponseStatus.ERROR
    assert "AADSTS65001" in result.answer
    assert not route.called


@pytest.mark.parametrize("bad_token", [make_arm_token(app_only=True), make_arm_token(oid=OTHER_OID)])
@respx.mock
async def test_non_user_token_is_refused_before_calling_azure(tool_ctx, bad_token):
    route = respx.post(QUERY_URL)
    tool_ctx.tokens = FakeTokenSource(token=bad_token)
    result = await costpulse.query_costs(args(), tool_ctx)
    assert result.status is ResponseStatus.ERROR
    assert not route.called


async def test_invalid_arguments_are_returned_to_the_model(tool_ctx):
    result = await execute_tool(costpulse.QUERY_COSTS, '{"scope": "x", "group_by": "tag"}', tool_ctx)
    assert result.status is ResponseStatus.ERROR and "tag_key" in result.answer
    assert tool_ctx.contributions == []  # nothing reached Azure, nothing to audit


async def test_invalid_scope_is_rejected(tool_ctx):
    result = await costpulse.query_costs(costpulse.QueryCostsArgs(scope="/subscriptions/../../evil"), tool_ctx)
    assert result.status is ResponseStatus.ERROR


def test_query_body_groups_by_tag():
    body = costpulse.build_query_body(
        costpulse.QueryCostsArgs(scope=SUBSCRIPTION, group_by="tag", tag_key="env", timeframe="last_30_days"),
        metric_column="Cost",
        today=datetime(2026, 9, 22).date(),
    )
    assert body["dataset"]["grouping"] == [{"type": "TagKey", "name": "env"}]
    assert body["timePeriod"] == {"from": "2026-08-24T00:00:00Z", "to": "2026-09-22T23:59:59Z"}


# --------------------------------------------------------------------------- trend + anomalies

FORECAST_URL = QUERY_URL.replace("/query?", "/forecast?")


def trend_payload(rows):
    return {
        "properties": {
            "nextLink": None,
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "ServiceName", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": rows,
        }
    }


def steady_days(n=14, start=20260901, compute=10.0, storage=2.0):
    rows = []
    for i in range(n):
        day = start + i
        rows.append([compute, day, "Virtual Machines", "USD"])
        rows.append([storage, day, "Storage", "USD"])
    return rows


@respx.mock
async def test_trend_flags_spike_and_names_the_driver(tool_ctx):
    rows = steady_days()
    rows.append([95.0, 20260910, "Azure OpenAI", "USD"])  # one-day spike from a new service
    route = respx.post(QUERY_URL).mock(return_value=httpx.Response(200, json=trend_payload(rows)))

    result = await costpulse.cost_trend(costpulse.CostTrendArgs(scope=SUBSCRIPTION, days=30), tool_ctx)

    body = json.loads(route.calls.last.request.content)
    assert body["dataset"]["grouping"] == [{"type": "Dimension", "name": "ServiceName"}]
    assert result.status is ResponseStatus.OK
    [anomaly] = result.data["anomalies"]
    assert anomaly["date"] == "2026-09-10" and anomaly["cost"] == 107.0 and anomaly["baseline"] == 12.0
    assert anomaly["drivers"][0]["service"] == "Azure OpenAI"
    assert result.data_timestamp == datetime(2026, 9, 14, tzinfo=UTC)  # latest billing day, not call time
    assert any("Anomaly rule" in c for c in result.caveats)


@respx.mock
async def test_trend_without_spikes_reports_none(tool_ctx):
    respx.post(QUERY_URL).mock(return_value=httpx.Response(200, json=trend_payload(steady_days())))
    result = await costpulse.cost_trend(costpulse.CostTrendArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.data["anomalies"] == []
    assert "No anomalous days" in result.answer


# --------------------------------------------------------------------------- forecast


def forecast_payload(rows):
    return {
        "properties": {
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "CostStatus", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": rows,
        }
    }


@respx.mock
async def test_forecast_combines_actual_and_azure_forecast(tool_ctx):
    rows = [[100.0, 20260920, "Actual", "USD"], [110.0, 20260921, "Actual", "USD"]]
    rows += [[105.0, 20260900 + d, "Forecast", "USD"] for d in range(22, 31)]
    route = respx.post(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload(rows)))

    result = await costpulse.forecast_month_end(costpulse.ForecastArgs(scope=SUBSCRIPTION), tool_ctx)

    body = json.loads(route.calls.last.request.content)
    assert body["timePeriod"] == {"from": "2026-09-01T00:00:00Z", "to": "2026-09-30T23:59:59Z"}
    assert body["includeActualCost"] is True
    assert result.status is ResponseStatus.OK
    assert result.data["actual_to_date"] == 210.0
    assert result.data["forecast_remaining"] == 945.0
    assert result.data["projected_month_end"] == 1155.0
    assert result.data_timestamp == datetime(2026, 9, 21, tzinfo=UTC)
    assert any("Azure Cost Management's own forecast" in c for c in result.caveats)


@respx.mock
async def test_forecast_without_forecast_rows_is_partial(tool_ctx):
    respx.post(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload([[50.0, 20260921, "Actual", "USD"]])))
    result = await costpulse.forecast_month_end(costpulse.ForecastArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.PARTIAL
    assert result.data["projected_month_end"] is None


@respx.mock
async def test_forecast_unsupported_is_honest_error(tool_ctx):
    respx.post(FORECAST_URL).mock(
        return_value=httpx.Response(422, json={"error": {"code": "BadRequest", "message": "Forecast not supported for this offer"}})
    )
    result = await costpulse.forecast_month_end(costpulse.ForecastArgs(scope=SUBSCRIPTION), tool_ctx)
    assert result.status is ResponseStatus.ERROR and result.data is None
    assert "Forecast not supported" in result.answer
