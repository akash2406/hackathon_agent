"""Azure data tokens for the two access modes.

* ``AppIdentityTokens`` (default, ``CRIP_AZURE_ACCESS_MODE=app_identity``): the
  web app's managed identity reads Azure with read-only roles granted on the
  management group. *Who may see what* is decided by ``access.resolver`` before
  any call is made.
* ``OnBehalfOfTokenProvider`` (``user_obo``, in ``obo.py``): every call runs as the user.

Both expose ``kind`` (stamped onto every ``Source``) and ``token_for``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .entra import AuthenticatedUser

ARM_SCOPE = "https://management.azure.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class AppIdentityTokens:
    kind = "app_identity"

    def __init__(self, credential: Any) -> None:
        # azure.identity.aio credential (DefaultAzureCredential -> managed identity in App Service).
        # It caches and refreshes tokens itself.
        self._credential = credential

    async def token_for(self, *, session_id: UUID | None, user: AuthenticatedUser) -> str:
        return (await self._credential.get_token(ARM_SCOPE)).token

    async def arm(self) -> str:
        return (await self._credential.get_token(ARM_SCOPE)).token

    async def graph(self) -> str:
        return (await self._credential.get_token(GRAPH_SCOPE)).token
