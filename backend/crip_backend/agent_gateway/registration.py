"""Create or update (by name) the CRIP agents in Azure AI Foundry Agent Service.

Used two ways:

* at app startup when ``CRIP_REGISTER_AGENTS_ON_STARTUP=true`` (App Service:
  runs as the web app's managed identity, so deploying the image is enough);
* from the CLI, ``python foundry/register_agents.py`` (uses your ``az login``).

From ``foundry/definitions``:

* each ``role: domain`` definition becomes an agent with its own function tools;
* the ``role: orchestrator`` definition becomes an agent whose tools are the
  ``delegation_tool`` of *every* domain definition.

That last point is why adding an agent never requires editing Orchestrator
routing: the Orchestrator's tool list is generated from the domain definitions,
and the model chooses among those tools itself. Idempotent: re-running with
unchanged definitions updates agents in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from azure.ai.agents.models import FunctionDefinition, FunctionToolDefinition, ListSortOrder

from .definitions import AgentDefinitions

log = logging.getLogger(__name__)

# Low temperature: agents should restate tool results faithfully, not creatively.
AGENT_TEMPERATURE = 0.1


def build_agent_specs(definitions: AgentDefinitions) -> list[dict[str, Any]]:
    """Pure function: definitions -> the agent payloads Foundry should hold."""
    specs: list[dict[str, Any]] = []
    for d in definitions.domains:
        specs.append(
            {
                "name": d.name,
                "description": d.description,
                "instructions": d.instructions,
                "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in d.tools],
            }
        )
    o = definitions.orchestrator
    specs.append(
        {
            "name": o.name,
            "description": o.description,
            "instructions": o.instructions,
            "tools": [
                {"name": d.delegation_tool.name, "description": d.delegation_tool.description, "parameters": d.delegation_tool.parameters}
                for d in definitions.domains
            ],
        }
    )
    for spec in specs:
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
        spec["metadata"] = {"managed_by": "crip", "definition_sha": digest}
    return specs


def _tool_definitions(spec: dict[str, Any]) -> list[FunctionToolDefinition]:
    return [
        FunctionToolDefinition(function=FunctionDefinition(name=t["name"], description=t["description"], parameters=t["parameters"]))
        for t in spec["tools"]
    ]


async def upsert_agents(client: Any, specs: list[dict[str, Any]], model: str) -> dict[str, str]:
    """``client`` is an ``azure.ai.agents.aio.AgentsClient``. Returns {agent name: agent id}."""
    existing: dict[str, Any] = {}
    # Newest first, so on duplicate names the most recently created agent wins
    # (the gateway resolves names the same way).
    async for agent in client.list_agents(order=ListSortOrder.DESCENDING):
        if agent.name in existing:
            log.warning("multiple Foundry agents named %s; updating the most recently created", agent.name)
            continue
        existing[agent.name] = agent

    ids: dict[str, str] = {}
    for spec in specs:
        kwargs = dict(
            model=model,
            name=spec["name"],
            description=spec["description"],
            instructions=spec["instructions"],
            tools=_tool_definitions(spec),
            metadata=spec["metadata"],
            temperature=AGENT_TEMPERATURE,
        )
        if spec["name"] in existing:
            agent = await client.update_agent(existing[spec["name"]].id, **kwargs)
            log.info("updated agent %s (%s)", spec["name"], agent.id)
        else:
            agent = await client.create_agent(**kwargs)
            log.info("created agent %s (%s)", spec["name"], agent.id)
        ids[spec["name"]] = agent.id
    return ids
