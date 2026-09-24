"""On-behalf-of (OBO) exchange: the user's API token -> an Azure Resource Manager token for that same user.

Flow position: the first step of "a tool call reaching Azure". When CostPulse's
tool needs to call Cost Management, the tool context asks this provider for a
token. The provider exchanges the user's incoming token (the ``user_assertion``)
at login.microsoftonline.com for a token scoped to
``https://management.azure.com/.default``. That token *is the user*, so Azure
RBAC decides what they can see - there is no application-level scope checking
standing in for it, and no platform identity with standing read access to
customer subscriptions.

This module is the only place an ARM token for cost data is produced.
``DefaultAzureCredential`` is deliberately not used here; it is reserved for the
backend's own operations (Foundry). ``DelegatedToken.assert_belongs_to`` is the
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
        # blip at pod start should fail one request, not crash-loop the pod.
        self._app: Any | None = msal_app
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
        if self._settings.obo_credential_mode is OboCredentialMode.FEDERATED:
            # Callable, not a string: kubelet rotates the projected token file, so
            # MSAL must re-read it on each exchange.
            return {"client_assertion": self._read_federated_assertion}
        if not self._settings.obo_client_secret_name:
            raise ValueError("CRIP_OBO_CLIENT_SECRET_NAME is required when CRIP_OBO_CREDENTIAL_MODE=secret_file")
        return self._secrets.get(self._settings.obo_client_secret_name)

    def _read_federated_assertion(self) -> str:
        path = self._settings.federated_token_file
        if path is None or not path.is_file():
            raise OboExchangeError(
                "federated_token_missing",
                "AZURE_FEDERATED_TOKEN_FILE is not available. Workload Identity must be enabled on the pod and the "
                "backend app registration must trust this service account (landing-zone allocations #4 and #6).",
            )
        return path.read_text(encoding="utf-8").strip()

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
