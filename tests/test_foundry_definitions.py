"""The Foundry definitions, the registration script and the backend tool registry agree."""

import register_agents

from crip_backend.agent_gateway.definitions import DELEGATION_PARAMETERS, load_definitions, verify_tool_coverage
from crip_backend.tools.registry import TOOLS

from .conftest import REPO_ROOT

DEFINITIONS = REPO_ROOT / "foundry" / "definitions"


def test_every_defined_tool_has_a_backend_handler():
    verify_tool_coverage(load_definitions(DEFINITIONS), TOOLS)


def test_tool_schemas_match_argument_models():
    for domain in load_definitions(DEFINITIONS).domains:
        for tool in domain.tools:
            model = TOOLS[tool.name].args_model
            assert set(tool.parameters["properties"]) == set(model.model_fields), tool.name
            required = {n for n, f in model.model_fields.items() if f.is_required()}
            assert set(tool.parameters.get("required", [])) == required, tool.name


def test_registration_and_backend_share_delegation_schema():
    assert register_agents.DELEGATION_PARAMETERS == DELEGATION_PARAMETERS


def test_orchestrator_tools_are_generated_from_domain_definitions():
    specs = {s["name"]: s for s in register_agents.build_agent_specs(*register_agents.load_definitions(DEFINITIONS))}
    defs = load_definitions(DEFINITIONS)
    orchestrator_tools = {t["name"] for t in specs[defs.orchestrator.name]["tools"]}
    assert orchestrator_tools == {d.delegation_tool.name for d in defs.domains}
    for d in defs.domains:
        assert {t["name"] for t in specs[d.name]["tools"]} == d.tool_names
