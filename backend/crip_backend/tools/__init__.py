"""Tool implementations that Foundry-hosted agents call back into.

A *tool* is backend code (not an agent). Foundry agents are registered with
function-tool definitions (``/foundry/definitions``); when a run needs one, the
backend executes the matching handler here with the signed-in user's context and
returns an ``AgentResponse`` as the tool output.
"""
