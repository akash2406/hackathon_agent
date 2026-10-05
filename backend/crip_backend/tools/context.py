"""Per-question context handed to every tool handler.

Created for each chat question or dashboard call and threaded through the
Orchestrator run, any domain-agent runs, and every tool call. It carries *who is
asking* and *what they may see* (``access``), how Azure is read (``tokens``),
and collects every ``AgentResponse`` a tool produces, which is how provenance
reaches the API response and the ``agent_invocations`` table without ever
passing through a model's text.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID

from ..access.model import UserAccess
from ..auth.entra import AuthenticatedUser
from ..azure_clients.arm import ArmClient
from ..contracts import AgentResponse


class AzureDataTokens(Protocol):
    """``OnBehalfOfTokenProvider`` (user_obo) or ``AppIdentityTokens`` (app_identity)."""

    kind: str

    async def token_for(self, *, session_id: UUID | None, user: AuthenticatedUser) -> str: ...


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
    tokens: AzureDataTokens
    arm: ArmClient
    # None only in unit tests that exercise tools without the access layer.
    access: UserAccess | None = None
    # CRIP's own store, for the usage-log tool (platform admins).
    repository: Any | None = None
    # Microsoft Graph (principal names for the access review) and the management
    # group CRIP covers (estate-wide cost); both optional.
    graph: Any | None = None
    management_group_id: str | None = None
    contributions: list[ToolContribution] = field(default_factory=list)

    @property
    def auth_kind(self) -> str:
        return self.tokens.kind

    async def arm_token(self) -> str:
        """The token tools send to Azure. In user_obo mode the provider re-verifies it is the user's own."""
        return await self.tokens.token_for(session_id=self.session_id, user=self.user)

    def child(self) -> ToolContext:
        """A context for a delegated domain-agent run, with its own contribution list.

        Parallel delegations each get their own list so their contributions can
        be attributed correctly before being merged into the parent.
        """
        return replace(self, contributions=[])
