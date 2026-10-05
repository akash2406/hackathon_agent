"""On-behalf-of (OBO) exchange: the user's API token -> an Azure Resource Manager token for that same user.

Flow position: the first step of "a tool call reaching Azure". When CostPulse's
tool needs to call Cost Management, the tool context asks this provider for a
token. The provider exchanges the user's incoming token (the ``user_assertion``)
at login.microsoftonline.com for a token scoped to
``https://management.azure.com/.default``. That token *is the user*, so Azure
RBAC decides what they can see - there is no application-level scope checking
standing in for it, and no platform identity with standing read access to
customer subscriptions.

This module is the only place an ARM token for user data is produced (Cost
Management, Advisor, Resource Graph all use it). ``DefaultAzureCredential`` is
deliberately not used for those calls; it is reserved for the backend's own
operations (Foundry). The app's managed identity appears here only as the
*client credential* of the app registration (proving which app is asking), never
as the identity that reads Azure data. ``DelegatedToken.assert_belongs_to`` is the
runtime proof that every Cost Management call carries the signed-in user's
delegated token: it checks the exchanged token's ``oid`` equals the user's and
that it is a delegated (``scp``) token, not an app-only one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import jwt
import msal

from ..config import OboCredentialMode, Settings
from ..secrets import SecretStore
from .entra import AuthenticatedUser

ARM_SCOPE = "https://management.azure.com/.default"
_ARM_AUDIENCES = {"https://management.azure.com/", "https://management.azure.com", "https://management.core.windows.net/"}
# Refresh this long before expiry so a token never expires mid Cost Management call.
_EXPIRY_SKEW_SECONDS = 300
# Audience Entra expects for a managed-identity token used as a federated client assertion.
_FEDERATION_SCOPE = "api://AzureADTokenExchange/.default"


class OboExchangeError(RuntimeError):
    def __init__(self, error: str, description: str) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description


class NotDelegatedTokenError(RuntimeError):
    """The token about to be used for Azure is not the signed-in user's delegated token."""


@dataclass(frozen=True)
class DelegatedToken:
    access_token: str = field(repr=False)
    expires_at: float
    subject_oid: str

    def assert_belongs_to(self, user: AuthenticatedUser) -> None:
        # Unverified decode is intentional: this is not an authentication check
        # (Azure validates the signature), it is a guard that we never send a
        # platform/app token or another user's token to Cost Management.
        claims: dict[str, Any] = jwt.decode(self.access_token, options={"verify_signature": False})
        if claims.get("idtyp") == "app" or "scp" not in claims:
            raise NotDelegatedTokenError("ARM token is app-only; refusing to use it for user cost data")
        if claims.get("oid") != user.object_id or self.subject_oid != user.object_id:
            raise NotDelegatedTokenError("ARM token subject does not match the signed-in user")
        if claims.get("aud") not in _ARM_AUDIENCES:
            raise NotDelegatedTokenError(f"ARM token has unexpected audience {claims.get('aud')!r}")


class OnBehalfOfTokenProvider:
    kind = "user_obo"

    def __init__(
        self,
        settings: Settings,
        secrets: SecretStore,
        *,
        msal_app: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._clock = clock
        # Built lazily on first exchange: MSAL fetches authority metadata from
        # login.microsoftonline.com when constructed, and a transient Entra
        # blip at container start should fail one request, not crash the app.
        self._app: Any | None = msal_app
        self._mi_credential: Any | None = None
        # Keyed by (session_id, user oid). Why per session and not per request:
        # a single question can trigger several tool calls (list subscriptions,
        # then a cost query, then a follow-up), and a conversation spans many
        # questions. Re-exchanging on every call adds a login.microsoftonline.com
        # round-trip and throttling exposure for no security benefit - the
        # exchanged token is already scoped to exactly this user. The oid is part
        # of the key so a session id can never yield another user's token.
        self._cache: dict[tuple[str, str], DelegatedToken] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _client_credential(self) -> str | dict[str, Callable[[], str]]:
        if self._settings.obo_credential_mode is OboCredentialMode.MANAGED_IDENTITY:
            # Callable, not a string: MSAL asks for a fresh assertion per exchange
            # and the managed-identity token is short-lived.
            return {"client_assertion": self._managed_identity_assertion}
        return self._secrets.get(self._settings.obo_client_secret_name)

    def _managed_identity_assertion(self) -> str:
        """A token for the web app's user-assigned managed identity, used as the app registration's credential.

        The API app registration has a federated identity credential that trusts
        this managed identity, so the OBO exchange needs no client secret.
        """
        client_id = self._settings.managed_identity_client_id
        if not client_id:
            raise OboExchangeError(
                "managed_identity_missing",
                "CRIP_OBO_CREDENTIAL_MODE=managed_identity needs AZURE_CLIENT_ID (the web app's user-assigned "
                "managed identity). See docs/azure-setup.md.",
            )
        if self._mi_credential is None:
            from azure.identity import ManagedIdentityCredential

            self._mi_credential = ManagedIdentityCredential(client_id=client_id)
        return self._mi_credential.get_token(_FEDERATION_SCOPE).token

    async def token_for(self, *, session_id: UUID | None, user: AuthenticatedUser) -> str:
        """The user's delegated ARM token, re-verified to belong to them on every use."""
        token = await self.get_token(session_id=session_id, user=user)
        token.assert_belongs_to(user)
        return token.access_token

    async def get_token(self, *, session_id: UUID | None, user: AuthenticatedUser) -> DelegatedToken:
        if session_id is None:
            return await self._exchange(user)
        key = (str(session_id), user.object_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # parallel tool calls in one run share one exchange
            cached = self._cache.get(key)
            if cached and cached.expires_at - _EXPIRY_SKEW_SECONDS > self._clock():
                return cached
            token = await self._exchange(user)
            self._evict_expired(keep=key)
            self._cache[key] = token
            return token

    def _acquire(self, user_assertion: str) -> dict[str, Any]:
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                self._settings.api_client_id,
                authority=self._settings.authority,
                client_credential=self._client_credential(),
            )
        return self._app.acquire_token_on_behalf_of(user_assertion=user_assertion, scopes=[ARM_SCOPE])

    async def _exchange(self, user: AuthenticatedUser) -> DelegatedToken:
        try:
            # MSAL is synchronous (requests); keep it off the event loop.
            result = await asyncio.to_thread(self._acquire, user.raw_token)
        except OboExchangeError:
            raise
        except Exception as exc:  # network failure talking to Entra
            raise OboExchangeError("obo_request_failed", str(exc)) from exc
        if "access_token" not in result:
            raise OboExchangeError(result.get("error", "obo_failed"), result.get("error_description", "no detail"))
        token = DelegatedToken(
            access_token=result["access_token"],
            expires_at=self._clock() + float(result.get("expires_in", 3600)),
            subject_oid=user.object_id,
        )
        token.assert_belongs_to(user)
        return token

    def _evict_expired(self, keep: tuple[str, str]) -> None:
        now = self._clock()
        for key in [k for k, t in self._cache.items() if t.expires_at <= now and k != keep]:
            self._cache.pop(key, None)
            self._locks.pop(key, None)
