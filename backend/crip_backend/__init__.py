"""CRIP backend service.

This package is the *backend service* half of CRIP's architecture. It is not an
agent: the Orchestrator and CostPulse agents live in Azure AI Foundry Agent
Service (see ``/foundry``). This service

* authenticates the signed-in user (``auth``),
* drives Foundry-hosted agent runs and executes the function tools they request
  (``agent_gateway`` + ``tools``),
* calls Azure with the user's own on-behalf-of token (``azure``),
* and records provenance for every answer in PostgreSQL (``persistence``).
"""
