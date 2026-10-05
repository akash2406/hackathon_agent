"""Validates the Entra ID access token on every API request.

Flow position: step 2 of "a user asking a question". The SPA signs the user in
with MSAL and sends ``Authorization: Bearer <token>`` where the token's audience
is *this backend API* (scope ``api://<api-client-id>/access_as_user``). We verify
signature, issuer, audience, tenant, expiry and the delegated scope, and keep the
raw token because it is the ``user_assertion`` for the OBO exchange (obo.py).

App-only tokens (client-credential tokens with no user) are rejected outright:
the entire design depends on there being a real user whose Azure RBAC scopes
the answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import Settings
from ..contracts import ErrorCode
from ..errors import ApiError


@dataclass(frozen=True)
class AuthenticatedUser:
    object_id: str  # Entra object ID: stable identity used as the session owner key
    tenant_id: str
    scopes: frozenset[str]
    display_name: str | None = None
    username: str | None = None  # UPN, for display only; it can change, so never used as a key
    # Entra app roles (roles claim) and security groups (groups claim) used by access.resolver.
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    groups_overage: bool = False  # too many groups for the token; resolver asks Graph
    # Kept for the OBO exchange; excluded from repr so it can't leak into logs.
    raw_token: str = field(default="", repr=False)


class SigningKeyResolver(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class EntraTokenValidator:
    def __init__(self, settings: Settings, key_resolver: SigningKeyResolver | None = None) -> None:
        self._tenant_id = settings.tenant_id
        self._audiences = [settings.effective_api_audience, settings.api_client_id]
        # v2.0 tokens (accessTokenAcceptedVersion=2) and v1.0 tokens have different issuers.
        self._issuers = [
            f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0",
            f"https://sts.windows.net/{settings.tenant_id}/",
        ]
        self._required_scope = settings.required_scope
        self._keys = key_resolver or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{settings.tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=3600,
        )

    async def validate(self, token: str) -> AuthenticatedUser:
        try:
            # PyJWKClient does blocking HTTP on a cache miss; keep it off the event loop.
            signing_key = await asyncio.to_thread(self._keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audiences,
                issuer=self._issuers,
                options={"require": ["exp", "iat", "aud", "iss", "oid", "tid"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise ApiError(ErrorCode.UNAUTHENTICATED, f"Invalid access token: {exc}", status_code=401) from exc

        if claims.get("tid") != self._tenant_id:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Token was issued for a different tenant.", status_code=401)
        if claims.get("idtyp") == "app" or "scp" not in claims:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                "App-only tokens are not accepted: CRIP answers must be scoped to a signed-in user's Azure RBAC.",
                status_code=403,
            )
        scopes = frozenset(str(claims["scp"]).split())
        if self._required_scope not in scopes:
            raise ApiError(ErrorCode.FORBIDDEN, f"Token lacks the '{self._required_scope}' scope.", status_code=403)

        return AuthenticatedUser(
            object_id=claims["oid"],
            tenant_id=claims["tid"],
            scopes=scopes,
            display_name=claims.get("name"),
            username=claims.get("preferred_username") or claims.get("upn"),
            roles=frozenset(str(r) for r in claims.get("roles", []) or []),
            groups=frozenset(str(g) for g in claims.get("groups", []) or []),
            groups_overage="groups" in (claims.get("_claim_names") or {}) or bool(claims.get("hasgroups")),
            raw_token=token,
        )


_bearer = HTTPBearer(auto_error=False)


async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthenticatedUser:
    """FastAPI dependency used by every endpoint that touches user data."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Missing bearer token.", status_code=401)
    validator: EntraTokenValidator = request.app.state.services.token_validator
    return await validator.validate(credentials.credentials)
