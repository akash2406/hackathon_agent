"""OBO exchange: per-session caching and the delegated-token guard."""

import uuid
from types import SimpleNamespace

import pytest

from crip_backend.auth.obo import ARM_SCOPE, NotDelegatedTokenError, OboExchangeError, OnBehalfOfTokenProvider
from crip_backend.config import OboCredentialMode
from crip_backend.secrets import SecretStore

from .conftest import OTHER_OID, USER_OID, make_arm_token


class FakeMsal:
    def __init__(self, result_for_user=None):
        self.calls = []
        self._result_for_user = result_for_user or (lambda assertion: {"access_token": make_arm_token(USER_OID), "expires_in": 3600})

    def acquire_token_on_behalf_of(self, *, user_assertion, scopes):
        self.calls.append((user_assertion, scopes))
        return self._result_for_user(user_assertion)


def provider(settings, msal_app, clock=lambda: 1_000_000.0):
    return OnBehalfOfTokenProvider(settings, SecretStore(settings.secrets_dir), msal_app=msal_app, clock=clock)


async def test_exchanges_the_users_own_token_for_arm_scope(settings, user):
    msal_app = FakeMsal()
    token = await provider(settings, msal_app).get_token(session_id=uuid.uuid4(), user=user)
    assert msal_app.calls == [(user.raw_token, [ARM_SCOPE])]
    token.assert_belongs_to(user)


async def test_token_is_cached_per_session(settings, user):
    msal_app = FakeMsal()
    p = provider(settings, msal_app)
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    await p.get_token(session_id=s1, user=user)
    await p.get_token(session_id=s1, user=user)
    assert len(msal_app.calls) == 1
    await p.get_token(session_id=s2, user=user)
    assert len(msal_app.calls) == 2


async def test_expired_cache_entry_is_re_exchanged(settings, user):
    now = [1_000_000.0]
    msal_app = FakeMsal()
    p = provider(settings, msal_app, clock=lambda: now[0])
    sid = uuid.uuid4()
    await p.get_token(session_id=sid, user=user)
    now[0] += 3600 - 60  # inside the refresh skew
    await p.get_token(session_id=sid, user=user)
    assert len(msal_app.calls) == 2


async def test_exchange_error_is_raised(settings, user):
    msal_app = FakeMsal(lambda _: {"error": "invalid_grant", "error_description": "AADSTS65001"})
    with pytest.raises(OboExchangeError, match="AADSTS65001"):
        await provider(settings, msal_app).get_token(session_id=uuid.uuid4(), user=user)


@pytest.mark.parametrize("bad", [make_arm_token(OTHER_OID), make_arm_token(app_only=True), make_arm_token(aud="https://graph.microsoft.com")])
async def test_non_delegated_or_foreign_token_is_rejected(settings, user, bad):
    msal_app = FakeMsal(lambda _: {"access_token": bad, "expires_in": 3600})
    with pytest.raises(NotDelegatedTokenError):
        await provider(settings, msal_app).get_token(session_id=uuid.uuid4(), user=user)


def test_managed_identity_assertion_uses_the_web_apps_identity(settings, monkeypatch):
    requested = {}

    class FakeManagedIdentityCredential:
        def __init__(self, client_id):
            requested["client_id"] = client_id

        def get_token(self, scope):
            requested["scope"] = scope
            return SimpleNamespace(token="mi-assertion")

    import azure.identity

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", FakeManagedIdentityCredential)
    settings.obo_credential_mode = OboCredentialMode.MANAGED_IDENTITY
    settings.managed_identity_client_id = "mi-client-id"
    p = provider(settings, FakeMsal())
    credential = p._client_credential()
    assert credential["client_assertion"]() == "mi-assertion"
    assert requested == {"client_id": "mi-client-id", "scope": "api://AzureADTokenExchange/.default"}


def test_managed_identity_mode_without_identity_explains_setup(settings):
    settings.obo_credential_mode = OboCredentialMode.MANAGED_IDENTITY
    settings.managed_identity_client_id = None
    with pytest.raises(OboExchangeError, match="AZURE_CLIENT_ID"):
        provider(settings, FakeMsal())._managed_identity_assertion()


def test_client_secret_mode_reads_secret_by_name(settings, monkeypatch):
    monkeypatch.setenv("CRIP_SECRET_OBO_CLIENT_SECRET", "s3cr3t-from-key-vault-reference")
    assert provider(settings, FakeMsal())._client_credential() == "s3cr3t-from-key-vault-reference"
