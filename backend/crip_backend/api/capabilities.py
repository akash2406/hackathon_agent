"""``GET /api/capabilities``: which domain agents are available and example questions for each.

Generated from ``foundry/definitions``, so the UI's suggestion chips update
automatically when a new agent is added. Contains no user data, so it needs no
token (the sign-in page can show it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..services import Services
from . import get_services

router = APIRouter(prefix="/api", tags=["capabilities"])


class AgentCapability(BaseModel):
    key: str
    name: str
    description: str
    summary: str
    examples: list[str]


class Capabilities(BaseModel):
    agents: list[AgentCapability]


@router.get("/capabilities", response_model=Capabilities)
async def get_capabilities(services: Services = Depends(get_services)) -> Capabilities:
    definitions = services.conversation.definitions
    return Capabilities(
        agents=[
            AgentCapability(
                key=d.key,
                name=d.name,
                description=d.delegation_tool.description,
                summary=d.summary or d.description,
                examples=list(d.examples),
            )
            for d in definitions.domains
        ]
    )
