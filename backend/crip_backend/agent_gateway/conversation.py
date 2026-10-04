"""Runs one user question through the Orchestrator and any domain agents it delegates to.

Flow position ("a user asking a question"):

    chat endpoint -> ConversationService.ask
        -> Orchestrator run (Foundry)
            model calls ask_costpulse(question)           [model's decision]
            -> CostPulse run (Foundry, fresh thread)
                model calls costpulse_query_costs(...)    [model's decision]
                -> tools.registry.execute_tool -> Cost Management (user's OBO token)
            <- CostPulse text + the tool's AgentResponse(s)
        <- Orchestrator composed text
    <- ConversationResult; provenance is in ToolContext.contributions

Why routing is never hardcoded here, even with a single domain agent: a
keyword check ("if 'cost' in message") would appear to work today, then break
silently the day a second agent is added (a question about untagged storage
spend belongs to more than one agent). The Orchestrator is registered with one
delegation tool per domain agent, and *the model* decides which to call. This
module executes whatever delegation tool the model chose by looking it up in the
definitions, so adding an agent never touches this code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..contracts import AgentResponse, Confidence, ResponseStatus
from ..tools.context import ToolContext, ToolContribution
from ..tools.registry import TOOLS, ToolSpec, UnknownToolError, execute_tool
from .definitions import AgentDefinitions, DomainAgentDef
from .foundry_gateway import FoundryAgentGateway, FoundryRunError, ToolDispatcher

log = logging.getLogger(__name__)


class DelegationArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=4000)


@dataclass(frozen=True)
class ConversationResult:
    thread_id: str
    answer: str


class ConversationService:
    def __init__(
        self,
        gateway: FoundryAgentGateway,
        definitions: AgentDefinitions,
        tools: dict[str, ToolSpec] = TOOLS,
    ) -> None:
        self._gateway = gateway
        self._definitions = definitions
        self._tools = tools

    @property
    def definitions(self) -> AgentDefinitions:
        return self._definitions

    async def ask(self, *, thread_id: str | None, message: str, ctx: ToolContext) -> ConversationResult:
        # One Foundry thread per CRIP session, so the Orchestrator keeps the
        # conversation's context across questions.
        thread_id = thread_id or await self._gateway.create_thread()
        answer = await self._gateway.run(
            agent_name=self._definitions.orchestrator.name,
            thread_id=thread_id,
            message=message,
            dispatch=self._orchestrator_dispatch(ctx),
        )
        return ConversationResult(thread_id=thread_id, answer=answer)

    def _orchestrator_dispatch(self, ctx: ToolContext) -> ToolDispatcher:
        async def dispatch(tool_name: str, arguments_json: str) -> str:
            domain = self._definitions.delegation(tool_name)
            if domain is None:
                log.warning("orchestrator requested unknown tool", extra={"tool": tool_name})
                return json.dumps({"status": "error", "answer": f"No agent is available behind tool '{tool_name}'."})
            return await self._delegate(domain, arguments_json, ctx)

        return dispatch

    async def _delegate(self, domain: DomainAgentDef, arguments_json: str, ctx: ToolContext) -> str:
        try:
            args = DelegationArgs.model_validate_json(arguments_json or "{}")
        except ValidationError as exc:
            return json.dumps({"status": "error", "answer": f"Invalid delegation arguments: {exc.errors(include_url=False)}"})

        child = ctx.child()
        try:
            # Fresh thread per delegation: the Orchestrator already rewrites the
            # question to be self-contained, and it keeps domain runs isolated.
            sub_thread = await self._gateway.create_thread()
            domain_text = await self._gateway.run(
                agent_name=domain.name,
                thread_id=sub_thread,
                message=args.question,
                dispatch=self._domain_dispatch(domain, child),
            )
        except FoundryRunError as exc:
            log.warning("domain agent run failed", extra={"agent": domain.key, "error": str(exc)})
            failure = AgentResponse(
                agent=domain.key,
                status=ResponseStatus.ERROR,
                answer=f"The {domain.key} agent could not complete its run ({exc}). No data from it is available.",
                confidence=Confidence.from_score(0.0),
            )
            child.contributions.append(ToolContribution(agent=domain.key, tool=None, response=failure, latency_ms=0))
            ctx.contributions.extend(child.contributions)
            return json.dumps(_tool_output(domain.key, failure.answer, child.contributions))

        ctx.contributions.extend(child.contributions)
        return json.dumps(_tool_output(domain.key, domain_text, child.contributions))

    def _domain_dispatch(self, domain: DomainAgentDef, ctx: ToolContext) -> ToolDispatcher:
        async def dispatch(tool_name: str, arguments_json: str) -> str:
            # A domain agent may only use the tools it was registered with.
            if tool_name not in domain.tool_names:
                return json.dumps({"status": "error", "answer": f"Tool '{tool_name}' is not available to {domain.key}."})
            try:
                response = await execute_tool(tool_name, arguments_json, ctx, self._tools)
            except UnknownToolError:
                return json.dumps({"status": "error", "answer": f"Tool '{tool_name}' has no backend handler."})
            return response.model_dump_json()

        return dispatch


def _tool_output(agent_key: str, agent_text: str, contributions: list[ToolContribution]) -> dict:
    # The grounding block is serialised from the tool's own AgentResponse, not
    # from the domain agent's prose, so query_used / data_timestamp reach the
    # Orchestrator exactly as Azure produced them. (They reach the *user*
    # through ChatResponse.contributions, which bypasses model text entirely.)
    return {
        "agent": agent_key,
        "agent_answer": agent_text,
        "grounding": [
            c.response.model_dump(
                mode="json", include={"status", "answer", "data", "query_used", "data_timestamp", "caveats", "sources"}
            )
            for c in contributions
        ],
    }
