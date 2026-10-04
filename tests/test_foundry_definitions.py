"""The Foundry definitions, the registration logic and the backend tool registry agree."""

from types import SimpleNamespace

import register_agents

from crip_backend.agent_gateway.definitions import load_definitions, verify_tool_coverage
from crip_backend.agent_gateway.registration import build_agent_specs, upsert_agents
from crip_backend.tools.registry import TOOLS

from .conftest import REPO_ROOT

DEFINITIONS = REPO_ROOT / "foundry" / "definitions"


def test_every_defined_tool_has_a_backend_handler():
    verify_tool_coverage(load_definitions(DEFINITIONS), TOOLS)


def test_every_backend_tool_is_exposed_to_some_agent():
    exposed = {t for d in load_definitions(DEFINITIONS).domains for t in d.tool_names}
    assert exposed == set(TOOLS)


def test_tool_schemas_match_argument_models():
    for domain in load_definitions(DEFINITIONS).domains:
        for tool in domain.tools:
            model = TOOLS[tool.name].args_model
            assert set(tool.parameters["properties"]) == set(model.model_fields), tool.name
            required = {n for n, f in model.model_fields.items() if f.is_required()}
            assert set(tool.parameters.get("required", [])) == required, tool.name
            assert TOOLS[tool.name].agent == domain.key, tool.name


def test_orchestrator_tools_are_generated_from_domain_definitions():
    defs = load_definitions(DEFINITIONS)
    specs = {s["name"]: s for s in build_agent_specs(defs)}
    assert {t["name"] for t in specs[defs.orchestrator.name]["tools"]} == {d.delegation_tool.name for d in defs.domains}
    assert {d.key for d in defs.domains} == {"costpulse", "optimizer", "inventory"}
    for d in defs.domains:
        assert {t["name"] for t in specs[d.name]["tools"]} == d.tool_names
        assert d.examples, f"{d.key} should ship example questions for the UI"


def test_cli_dry_run_uses_backend_registration(capsys):
    assert register_agents.main(["--dry-run"]) == 0
    assert '"crip-orchestrator"' in capsys.readouterr().out


class FakeAsyncAgentsClient:
    def __init__(self, existing):
        self.existing = existing
        self.created, self.updated = [], []

    async def list_agents(self, **_):
        for agent in self.existing:
            yield agent

    async def create_agent(self, **kwargs):
        self.created.append(kwargs["name"])
        return SimpleNamespace(id=f"new_{kwargs['name']}")

    async def update_agent(self, agent_id, **kwargs):
        self.updated.append((agent_id, kwargs["name"]))
        return SimpleNamespace(id=agent_id)


async def test_upsert_updates_existing_and_creates_missing_agents():
    client = FakeAsyncAgentsClient([SimpleNamespace(name="crip-costpulse", id="asst_existing")])
    ids = await upsert_agents(client, build_agent_specs(load_definitions(DEFINITIONS)), "gpt-4o")
    assert client.updated == [("asst_existing", "crip-costpulse")]
    assert set(client.created) == {"crip-optimizer", "crip-inventory", "crip-orchestrator"}
    assert ids["crip-costpulse"] == "asst_existing"
