"""Per-question context handed to every tool handler.

Created by the chat endpoint for each question and threaded through the
Orchestrator run, any domain-agent runs it delegates to, and every tool call
those runs make. It carries *who is asking* (so tools can obtain the user's OBO
token) and collects every ``AgentResponse`` a tool produces, which is how
provenance reaches the API response and the ``agent_invocations`` table without
ever passing through a model's text.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol
from uuid import UUID

from ..auth.entra import AuthenticatedUser
from ..auth.obo import DelegatedToken
from ..azure_clients.arm import ArmClient
from ..contracts import AgentResponse


class DelegatedTokenSource(Protocol):
    async def get_token(self, *, session_id: UUID | None, user: AuthenticatedUser) -> DelegatedToken: ...


@dataclass(frozen=True)
class ToolContribution:
    agent: str
    tool: str | None  # None when the contribution is a failed domain-agent run rather than a tool result
    response: AgentResponse
    latency_ms: int


@dataclass
class ToolContext:
    user: AuthenticatedUser
    session_id: UUID | None
    tokens: DelegatedTokenSource
    arm: ArmClient
    contributions: list[ToolContribution] = field(default_factory=list)

    async def arm_token(self) -> str:
        """The signed-in user's OBO-exchanged ARM token. The only token tools may send to Azure."""
        token = await self.tokens.get_token(session_id=self.session_id, user=self.user)
        # Re-checked on every use, including cache hits: cheap, and it makes
        # "Azure only ever sees the user's own token" an enforced invariant
        # rather than a property of how the cache happens to be keyed.
        token.assert_belongs_to(self.user)
        return token.access_token

    def child(self) -> ToolContext:
        """A context for a delegated domain-agent run, with its own contribution list.

        Parallel delegations each get their own list so their contributions can
        be attributed correctly before being merged into the parent.
        """
        return replace(self, contributions=[])
