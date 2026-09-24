"""Incoming Entra token validation: delegated user tokens only."""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from crip_backend.auth.entra import EntraTokenValidator
from crip_backend.errors import ApiError

from .conftest import API_CLIENT_ID, TENANT, USER_OID

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StaticKeys:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def token(**overrides):
    claims = {
        "aud": f"api://{API_CLIENT_ID}",
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "oid": USER_OID,
        "scp": "access_as_user",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "name": "Test User",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="RS256")


@pytest.fixture
def validator(settings):
    return EntraTokenValidator(settings, key_resolver=StaticKeys())


async def test_valid_user_token(validator):
    raw = token()
    user = await validator.validate(raw)
    assert user.object_id == USER_OID and user.raw_token == raw
    assert raw not in repr(user)  # the token never ends up in logs via repr


@pytest.mark.parametrize(
    "overrides,status",
    [
        ({"aud": "api://someone-else"}, 401),
        ({"tid": "99999999-9999-9999-9999-999999999999"}, 401),
        ({"exp": int(time.time()) - 3600}, 401),
        ({"scp": None, "roles": ["Reader"], "idtyp": "app"}, 403),
        ({"scp": "other_scope"}, 403),
    ],
)
async def test_rejected_tokens(validator, overrides, status):
    with pytest.raises(ApiError) as exc:
        await validator.validate(token(**overrides))
    assert exc.value.status_code == status
