"""Register (create or update, by name) the CRIP agents in Azure AI Foundry Agent Service.

This script is the only place agents are created. It reads ``definitions/*.json``
(+ their instruction ``.md`` files) and makes Foundry match them:

* each ``role: domain`` definition becomes an agent with its own function tools;
* the ``role: orchestrator`` definition becomes an agent whose tools are the
  ``delegation_tool`` of *every* domain definition.

That last point is why adding an agent never requires editing Orchestrator
routing: the Orchestrator's tool list is generated from the domain definitions,
and the model chooses among those tools itself.

Runs in-cluster as a Helm post-install/post-upgrade hook Job under the release's
Workload Identity (landing-zone allocation #7 grants it agent create/update), or
locally after ``az login``:

    python foundry/register_agents.py --endpoint <project endpoint> --model <deployment>

Idempotent: re-running with unchanged definitions updates agents in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

DEFINITIONS_DIR = Path(os.environ.get("CRIP_FOUNDRY_DEFINITIONS_DIR", Path(__file__).parent / "definitions"))

# Must stay identical to crip_backend.agent_gateway.definitions.DELEGATION_PARAMETERS
# (tests/test_foundry_definitions.py enforces this).
DELEGATION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "A self-contained question for the domain agent, including any subscription, "
            "scope, timeframe or grouping the user mentioned earlier in the conversation.",
        }
    },
    "required": ["question"],
    "additionalProperties": False,
}

log = logging.getLogger("register_agents")


def load_definitions(directory: Path = DEFINITIONS_DIR) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    orchestrator: dict[str, Any] | None = None
    domains: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["instructions"] = (directory / raw["instructions_file"]).read_text(encoding="utf-8")
        if raw["role"] == "orchestrator":
            orchestrator = raw
        elif raw["role"] == "domain":
            domains.append(raw)
        else:
            raise ValueError(f"{path.name}: unknown role {raw['role']!r}")
    if orchestrator is None or not domains:
        raise ValueError("definitions must include one orchestrator and at least one domain agent")
    return orchestrator, domains


def build_agent_specs(orchestrator: dict[str, Any], domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure function: definitions -> the agent payloads Foundry should hold."""
    specs = []
    for d in domains:
        specs.append(
            {
                "name": d["name"],
                "description": d.get("description"),
                "instructions": d["instructions"],
                "tools": [
                    {"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in d["tools"]
                ],
            }
        )
    specs.append(
        {
            "name": orchestrator["name"],
            "description": orchestrator.get("description"),
            "instructions": orchestrator["instructions"],
            "tools": [
                {"name": d["delegation_tool"]["name"], "description": d["delegation_tool"]["description"], "parameters": DELEGATION_PARAMETERS}
                for d in domains
            ],
        }
    )
    for spec in specs:
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
        spec["metadata"] = {"managed_by": "crip/foundry/register_agents.py", "definition_sha": digest}
    return specs


def upsert_agents(client: Any, specs: list[dict[str, Any]], model: str) -> dict[str, str]:
    from azure.ai.agents.models import FunctionDefinition, FunctionToolDefinition, ListSortOrder

    existing: dict[str, Any] = {}
    # Newest first, so on duplicate names the most recently created agent wins
    # (the backend resolves names the same way).
    for agent in client.list_agents(order=ListSortOrder.DESCENDING):
        if agent.name in existing:
            log.warning("multiple Foundry agents named %s; updating the most recently created", agent.name)
            continue
        existing[agent.name] = agent

    ids: dict[str, str] = {}
    for spec in specs:
        tools = [
            FunctionToolDefinition(
                function=FunctionDefinition(name=t["name"], description=t["description"], parameters=t["parameters"])
            )
            for t in spec["tools"]
        ]
        kwargs = dict(
            model=model,
            name=spec["name"],
            description=spec["description"],
            instructions=spec["instructions"],
            tools=tools,
            metadata=spec["metadata"],
            temperature=0.1,  # low temperature: we want faithful restatement of tool results, not creativity
        )
        if spec["name"] in existing:
            agent = client.update_agent(existing[spec["name"]].id, **kwargs)
            log.info("updated agent %s (%s)", spec["name"], agent.id)
        else:
            agent = client.create_agent(**kwargs)
            log.info("created agent %s (%s)", spec["name"], agent.id)
        ids[spec["name"]] = agent.id
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=os.environ.get("CRIP_FOUNDRY_PROJECT_ENDPOINT"), help="Foundry project endpoint")
    parser.add_argument("--model", default=os.environ.get("CRIP_FOUNDRY_MODEL_DEPLOYMENT"), help="Model deployment name")
    parser.add_argument("--dry-run", action="store_true", help="Print the agent payloads without calling Foundry")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    specs = build_agent_specs(*load_definitions())
    if args.dry_run:
        print(json.dumps(specs, indent=2))
        return 0
    missing = [flag for flag, value in (("--endpoint", args.endpoint), ("--model", args.model)) if not value]
    if missing:
        parser.error(f"missing {', '.join(missing)} (landing-zone allocation #7: Foundry project + model deployment)")

    from azure.ai.agents import AgentsClient
    from azure.identity import DefaultAzureCredential

    with AgentsClient(endpoint=args.endpoint, credential=DefaultAzureCredential()) as client:
        ids = upsert_agents(client, specs, args.model)
    print(json.dumps(ids, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
