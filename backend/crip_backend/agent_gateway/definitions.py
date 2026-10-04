"""Loads the agent definitions in ``foundry/definitions`` - the same files the registration script registers.

Why the backend reads these files instead of hardcoding agent/tool names: the
Orchestrator's routing is the *model's* tool-calling decision over the set of
delegation tools it was registered with. The backend's job is only to execute
whichever delegation tool the model picks. Driving both registration and
execution from one set of definition files means adding a domain agent is a
data change (a new JSON file + its tool handlers), not a change to routing code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Every delegation tool the Orchestrator sees has this same argument shape.
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


@dataclass(frozen=True)
class FunctionToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class DomainAgentDef:
    key: str  # short name used in contracts, e.g. "costpulse"
    name: str  # Foundry agent name, e.g. "crip-costpulse"
    description: str
    instructions: str  # contents of the definition's instructions .md
    delegation_tool: FunctionToolDef
    tools: tuple[FunctionToolDef, ...]
    examples: tuple[str, ...] = ()  # sample questions, shown as suggestions in the UI
    summary: str = ""  # one-line, user-facing description for the UI

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)


@dataclass(frozen=True)
class OrchestratorDef:
    key: str
    name: str
    description: str
    instructions: str


@dataclass(frozen=True)
class AgentDefinitions:
    orchestrator: OrchestratorDef
    domains: tuple[DomainAgentDef, ...]

    def delegation(self, tool_name: str) -> DomainAgentDef | None:
        return next((d for d in self.domains if d.delegation_tool.name == tool_name), None)


def load_definitions(directory: Path) -> AgentDefinitions:
    orchestrator: OrchestratorDef | None = None
    domains: list[DomainAgentDef] = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        role = raw.get("role")
        if role == "orchestrator":
            orchestrator = OrchestratorDef(
                key=raw["key"], name=raw["name"], description=raw.get("description", ""), instructions=_instructions(directory, raw)
            )
        elif role == "domain":
            delegation = raw["delegation_tool"]
            domains.append(
                DomainAgentDef(
                    key=raw["key"],
                    name=raw["name"],
                    description=raw.get("description", ""),
                    instructions=_instructions(directory, raw),
                    delegation_tool=FunctionToolDef(delegation["name"], delegation["description"], DELEGATION_PARAMETERS),
                    tools=tuple(FunctionToolDef(t["name"], t["description"], t["parameters"]) for t in raw["tools"]),
                    examples=tuple(raw.get("examples", [])),
                    summary=raw.get("summary", ""),
                )
            )
        else:
            raise ValueError(f"{path.name}: unknown role {role!r}")
    if orchestrator is None:
        raise ValueError(f"no orchestrator definition found in {directory}")
    if not domains:
        raise ValueError(f"no domain agent definitions found in {directory}")
    return AgentDefinitions(orchestrator=orchestrator, domains=tuple(domains))


def _instructions(directory: Path, raw: dict[str, Any]) -> str:
    return (directory / raw["instructions_file"]).read_text(encoding="utf-8")


def verify_tool_coverage(definitions: AgentDefinitions, handlers: dict[str, Any]) -> None:
    """Fail fast at startup if Foundry could ask for a tool the backend cannot execute."""
    missing = sorted(t for d in definitions.domains for t in d.tool_names if t not in handlers)
    if missing:
        raise RuntimeError(f"Tools defined in foundry/definitions have no backend handler: {missing}")
