"""Orchestrator routing is the model's tool-calling decision, executed faithfully by the backend.

The ScriptedAgentsClient plays the model: each policy decides which tool (if
any) to call. These tests prove the backend (a) executes whichever delegation
tool the model picks, (b) delegates to nothing when the model calls nothing -
even when the message says "cost" - and (c) passes provenance through untouched.
"""

import json
from datetime import UTC, datetime

from crip_backend.agent_gateway.conversation import ConversationService
from crip_backend.agent_gateway.definitions import load_definitions
from crip_backend.agent_gateway.foundry_gateway import FoundryAgentGateway
from crip_backend.contracts import AgentResponse, Confidence, ResponseStatus, Source
from crip_backend.tools import costpulse
from crip_backend.tools.registry import ToolSpec

from .conftest import REPO_ROOT, SUBSCRIPTION, Fail, Final, ScriptedAgentsClient, ToolCalls

DATA_TS = datetime(2026, 9, 21, tzinfo=UTC)
QUERY = f"POST https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.CostManagement/query"


async def stub_query_costs(args, ctx):
    return AgentResponse(
        agent="costpulse",
        status=ResponseStatus.OK,
        answer="Month-to-date cost is 230.75 USD.",
        confidence=Confidence.from_score(0.9),
        data={"total_by_currency": {"USD": 230.75}},
        query_used=QUERY,
        data_timestamp=DATA_TS,
        sources=[Source(tool=costpulse.QUERY_COSTS, api=QUERY, invoked_at=datetime.now(UTC), scope=args.scope)],
    )


STUB_TOOLS = {
    costpulse.QUERY_COSTS: ToolSpec(costpulse.QUERY_COSTS, "costpulse", costpulse.QueryCostsArgs, stub_query_costs),
    costpulse.LIST_SUBSCRIPTIONS: ToolSpec(costpulse.LIST_SUBSCRIPTIONS, "costpulse", costpulse.ListSubscriptionsArgs, stub_query_costs),
}


def costpulse_policy(message, outputs):
    if not outputs:
        return ToolCalls([(costpulse.QUERY_COSTS, {"scope": SUBSCRIPTION})])
    return Final("CostPulse: 230.75 USD month-to-date, data through 2026-09-21.")


def service(policies):
    client = ScriptedAgentsClient(policies)
    gateway = FoundryAgentGateway(client, poll_interval_seconds=0)
    return ConversationService(gateway, load_definitions(REPO_ROOT / "foundry" / "definitions"), STUB_TOOLS), client


async def test_model_tool_call_is_routed_to_costpulse(tool_ctx):
    def orchestrator(message, outputs):
        if not outputs:
            return ToolCalls([("ask_costpulse", {"question": f"Month-to-date cost for {SUBSCRIPTION}?"})])
        return Final("Composed: " + json.loads(outputs[0])["agent_answer"])

    svc, client = service({"crip-orchestrator": orchestrator, "crip-costpulse": costpulse_policy})
    result = await svc.ask(thread_id=None, message="How much have we spent this month?", ctx=tool_ctx)

    assert client.runs_by_agent == {"crip-orchestrator": 1, "crip-costpulse": 1}
    assert result.answer.startswith("Composed: CostPulse")
    assert [c.agent for c in tool_ctx.contributions] == ["costpulse"]
    # Provenance reaches the Orchestrator exactly as the tool produced it.
    grounding = json.loads(client.tool_outputs_seen["crip-orchestrator"][0])["grounding"][0]
    assert grounding["query_used"] == QUERY
    assert grounding["data_timestamp"] == "2026-09-21T00:00:00Z"


async def test_no_tool_call_means_no_delegation_even_if_message_mentions_cost(tool_ctx):
    svc, client = service(
        {"crip-orchestrator": lambda m, o: Final("CRIP answers Azure cost questions."), "crip-costpulse": costpulse_policy}
    )
    await svc.ask(thread_id=None, message="What does 'cost' mean in CRIP?", ctx=tool_ctx)
    assert client.runs_by_agent["crip-costpulse"] == 0
    assert tool_ctx.contributions == []


async def test_unknown_tool_chosen_by_model_is_reported_not_executed(tool_ctx):
    def orchestrator(message, outputs):
        return ToolCalls([("ask_payroll", {"question": "list salaries"})]) if not outputs else Final(outputs[0])

    svc, client = service({"crip-orchestrator": orchestrator, "crip-costpulse": costpulse_policy})
    result = await svc.ask(thread_id=None, message="show me payroll", ctx=tool_ctx)
    assert "No agent is available" in result.answer
    assert client.runs_by_agent["crip-costpulse"] == 0


async def test_domain_agent_cannot_use_tools_outside_its_definition(tool_ctx):
    def orchestrator(message, outputs):
        return ToolCalls([("ask_costpulse", {"question": "q"})]) if not outputs else Final("done")

    def rogue_costpulse(message, outputs):
        return ToolCalls([("delete_resource_group", {"name": "prod"})]) if not outputs else Final(outputs[0])

    svc, client = service({"crip-orchestrator": orchestrator, "crip-costpulse": rogue_costpulse})
    await svc.ask(thread_id=None, message="q", ctx=tool_ctx)
    assert "not available to costpulse" in client.tool_outputs_seen["crip-costpulse"][0]
    assert tool_ctx.contributions == []


async def test_failed_domain_run_becomes_an_honest_error_contribution(tool_ctx):
    def orchestrator(message, outputs):
        return ToolCalls([("ask_costpulse", {"question": "q"})]) if not outputs else Final("CostPulse failed.")

    svc, _ = service({"crip-orchestrator": orchestrator, "crip-costpulse": lambda m, o: Fail()})
    await svc.ask(thread_id=None, message="q", ctx=tool_ctx)
    [contribution] = tool_ctx.contributions
    assert contribution.response.status is ResponseStatus.ERROR
    assert contribution.response.data is None
