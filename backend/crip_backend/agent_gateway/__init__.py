"""Client-side gateway to agents hosted in Azure AI Foundry Agent Service.

Nothing in this package *is* an agent. The agents (Orchestrator, CostPulse) are
registered in Foundry by ``foundry/register_agents.py``; this package invokes
them, manages their threads, and services the function-tool calls they make.
"""
