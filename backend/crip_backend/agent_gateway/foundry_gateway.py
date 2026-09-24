"""Runs a Foundry-hosted agent on a thread and services its function-tool calls.

Flow position: the engine of "a user asking a question". For each agent run:

1. post the user's message to the Foundry thread and start a run;
2. poll the run; when it enters ``requires_action`` the model has decided to
   call one or more function tools - hand each (name, arguments) to the
   caller-supplied ``dispatch`` function and submit the outputs back;
3. when the run completes, return the agent's text for that run.

Why function tools executed here, rather than Foundry's OpenAPI tool or
connected agents calling the backend directly: a server-side Foundry callback
cannot carry the signed-in user's token, so it could only call Azure as a
platform identity - exactly what the OBO requirement forbids. With function
tools the model still chooses *what* to call, but the call executes inside this
request, where the user's delegated token is available.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from azure.ai.agents.models import ListSortOrder, ToolOutput
from azure.core.exceptions import AzureError

log = logging.getLogger(__name__)

ToolDispatcher = Callable[[str, str], Awaitable[str]]

_TERMINAL_FAILURES = {"failed", "cancelled", "expired", "incomplete"}


class FoundryRunError(RuntimeError):
    """A Foundry run could not produce an answer."""


class FoundryRunTimeout(FoundryRunError):
    pass


class AgentNotRegisteredError(FoundryRunError):
    pass


def _value(enum_or_str: Any) -> str:
    return str(getattr(enum_or_str, "value", enum_or_str) or "").lower()


class FoundryAgentGateway:
    def __init__(
        self,
        client: Any,  # azure.ai.agents.aio.AgentsClient (or a test double with the same surface)
        *,
        run_timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.5,
        max_tool_rounds: int = 8,
    ) -> None:
        self._client = client
        self._run_timeout = run_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._max_tool_rounds = max_tool_rounds
        self._agent_ids: dict[str, str] = {}

    async def agent_id(self, name: str) -> str:
        # Agents are resolved by name so no Foundry-generated IDs need to be
        # copied into config; registration is idempotent by name.
        if name not in self._agent_ids:
            try:
                async for agent in self._client.list_agents(order=ListSortOrder.DESCENDING):
                    if agent.name == name:
                        self._agent_ids[name] = agent.id
                        break
            except AzureError as exc:
                raise FoundryRunError(f"could not list Foundry agents: {exc}") from exc
            if name not in self._agent_ids:
                raise AgentNotRegisteredError(
                    f"Agent '{name}' is not registered in the Foundry project. Run foundry/register_agents.py "
                    "(the Helm post-install hook does this on deploy)."
                )
        return self._agent_ids[name]

    async def create_thread(self) -> str:
        try:
            thread = await self._client.threads.create()
        except AzureError as exc:
            raise FoundryRunError(f"could not create Foundry thread: {exc}") from exc
        return thread.id

    async def run(self, *, agent_name: str, thread_id: str, message: str, dispatch: ToolDispatcher) -> str:
        agent_id = await self.agent_id(agent_name)
        try:
            return await self._run(agent_name, agent_id, thread_id, message, dispatch)
        except AzureError as exc:
            raise FoundryRunError(f"Foundry call failed for agent '{agent_name}': {exc}") from exc

    async def _run(self, agent_name: str, agent_id: str, thread_id: str, message: str, dispatch: ToolDispatcher) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._run_timeout
        await self._client.messages.create(thread_id=thread_id, role="user", content=message)
        run = await self._client.runs.create(thread_id=thread_id, agent_id=agent_id)
        tool_rounds = 0

        while True:
            status = _value(run.status)
            if status == "requires_action":
                tool_rounds += 1
                if tool_rounds > self._max_tool_rounds:
                    await self._cancel(thread_id, run.id)
                    raise FoundryRunError(f"agent '{agent_name}' exceeded {self._max_tool_rounds} tool rounds")
                calls = [c for c in run.required_action.submit_tool_outputs.tool_calls if _value(getattr(c, "type", "function")) == "function"]
                # Parallel tool calls from one model turn execute concurrently.
                outputs = await asyncio.gather(*(self._invoke(dispatch, c) for c in calls))
                run = await self._client.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=[ToolOutput(tool_call_id=c.id, output=o) for c, o in zip(calls, outputs, strict=True)],
                )
                continue
            if status == "completed":
                break
            if status in _TERMINAL_FAILURES:
                raise FoundryRunError(f"run {run.id} for agent '{agent_name}' ended '{status}': {getattr(run, 'last_error', None)}")
            if loop.time() > deadline:
                await self._cancel(thread_id, run.id)
                raise FoundryRunTimeout(f"agent '{agent_name}' did not finish within {self._run_timeout:.0f}s")
            await asyncio.sleep(self._poll_interval)
            run = await self._client.runs.get(thread_id=thread_id, run_id=run.id)

        return await self._final_text(agent_name, thread_id, run.id)

    async def _invoke(self, dispatch: ToolDispatcher, call: Any) -> str:
        name, arguments = call.function.name, call.function.arguments
        try:
            return await dispatch(name, arguments)
        except Exception:
            log.exception("tool dispatch failed", extra={"tool": name})
            # Tell the model plainly that nothing was retrieved, so it cannot
            # mistake an empty output for "no costs".
            return json.dumps({"status": "error", "answer": f"The tool '{name}' failed unexpectedly; no data was retrieved."})

    async def _final_text(self, agent_name: str, thread_id: str, run_id: str) -> str:
        parts: list[str] = []
        async for msg in self._client.messages.list(thread_id=thread_id, run_id=run_id, order=ListSortOrder.ASCENDING):
            if _value(msg.role) == "assistant":
                parts.extend(t.text.value for t in msg.text_messages)
        text = "\n\n".join(p for p in parts if p).strip()
        if not text:
            raise FoundryRunError(f"agent '{agent_name}' completed without producing any text")
        return text

    async def _cancel(self, thread_id: str, run_id: str) -> None:
        try:
            await self._client.runs.cancel(thread_id=thread_id, run_id=run_id)
        except Exception:  # best effort; we are already failing
            log.warning("could not cancel run", extra={"run_id": run_id})
