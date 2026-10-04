"""Secret store: App Service Key Vault reference (env) first, then local file."""

import pytest

from crip_backend.secrets import MissingSecretError, SecretStore, env_var_for


def test_env_var_name():
    assert env_var_for("database-url") == "CRIP_SECRET_DATABASE_URL"


def test_env_wins_over_file(tmp_path):
    (tmp_path / "database-url").write_text("from-file")
    store = SecretStore(tmp_path, environ={"CRIP_SECRET_DATABASE_URL": "from-key-vault-reference"})
    assert store.get("database-url") == "from-key-vault-reference"


def test_file_fallback(tmp_path):
    (tmp_path / "obo-client-secret").write_text("local-dev-secret\n")
    assert SecretStore(tmp_path, environ={}).get("obo-client-secret") == "local-dev-secret"


def test_unresolved_key_vault_reference_is_treated_as_missing(tmp_path):
    store = SecretStore(tmp_path, environ={"CRIP_SECRET_OBO_CLIENT_SECRET": "@Microsoft.KeyVault(SecretUri=https://kv/secrets/x)"})
    with pytest.raises(MissingSecretError, match="CRIP_SECRET_OBO_CLIENT_SECRET"):
        store.get("obo-client-secret")


def test_secret_names_are_validated(tmp_path):
    with pytest.raises(ValueError):
        SecretStore(tmp_path, environ={}).get_optional("../etc/passwd")
