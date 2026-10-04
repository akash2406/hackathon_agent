"""Reads secret values by *name*: environment (App Service Key Vault reference) first, then a file.

Lookup order for secret ``database-url``:

1. env var ``CRIP_SECRET_DATABASE_URL``. In App Service this App Setting is a
   Key Vault reference (``@Microsoft.KeyVault(SecretUri=...)``), so the value
   lives in Key Vault and App Service resolves it at runtime;
2. file ``<CRIP_SECRETS_DIR>/database-url``. Locally, ``.secrets/`` (git-ignored).

Code and config only ever refer to the secret's name.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SECRET_NAME = re.compile(r"[0-9A-Za-z-]{1,127}")  # Key Vault secret-name rules


class MissingSecretError(RuntimeError):
    pass


def env_var_for(name: str) -> str:
    return "CRIP_SECRET_" + name.upper().replace("-", "_")


class SecretStore:
    def __init__(self, directory: Path, environ: dict[str, str] | None = None) -> None:
        self._directory = directory
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> str:
        value = self.get_optional(name)
        if value is None:
            raise MissingSecretError(
                f"Secret '{name}' not found: set App Setting {env_var_for(name)} (ideally a Key Vault reference) "
                f"or create the file {self._directory / name}."
            )
        return value

    def get_optional(self, name: str) -> str | None:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError(f"invalid secret name: {name!r}")
        value = (self._environ.get(env_var_for(name)) or "").strip()
        # An unresolved Key Vault reference is passed through literally by App
        # Service; treat it as missing rather than as the secret.
        if value and not value.startswith("@Microsoft.KeyVault("):
            return value
        path = self._directory / name
        if path.is_file():
            return path.read_text(encoding="utf-8").strip() or None
        return None
