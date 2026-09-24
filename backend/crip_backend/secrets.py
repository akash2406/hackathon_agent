"""Reads secret values from files mounted by the Key Vault CSI driver.

Used at startup only (not in the per-question flow): the PostgreSQL connection
string and the Application Insights connection string are read once from
``/mnt/secrets-store/<secret-name>``. Code and config refer to secrets by
*name*; the value exists only in Key Vault and in the pod's tmpfs mount.

For local development point ``CRIP_SECRETS_DIR`` at a git-ignored directory
(``.secrets/``) containing one file per secret.
"""

from __future__ import annotations

import re
from pathlib import Path

_SECRET_NAME = re.compile(r"[0-9A-Za-z-]{1,127}")  # Key Vault secret-name rules


class MissingSecretError(RuntimeError):
    """Raised when a required secret is absent: an allocation is missing, not a bug to work around."""


class SecretStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def get(self, name: str) -> str:
        value = self.get_optional(name)
        if value is None:
            raise MissingSecretError(
                f"Secret '{name}' not found in {self._directory}. It must be allocated in the landing-zone "
                "Key Vault scope and listed in the SecretProviderClass (docs/landing-zone-requests.md, #5)."
            )
        return value

    def get_optional(self, name: str) -> str | None:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError(f"invalid secret name: {name!r}")
        path = self._directory / name
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None
