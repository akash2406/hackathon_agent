"""Create the git-ignored .secrets/ directory for local development.

The app reads a secret named ``<name>`` from env var ``CRIP_SECRET_<NAME>`` (in
App Service: a Key Vault reference) or, locally, from ``.secrets/<name>``:

    .secrets/obo-client-secret        YOU paste a client secret of the API app registration here
                                      (only needed when CRIP_OBO_CREDENTIAL_MODE=client_secret, the local default)
    .secrets/local-postgres-password  random password, only for `docker compose --profile postgres`
    .secrets/database-url             optional; if present the app uses PostgreSQL instead of SQLite

Usage:  python scripts/init_local_secrets.py
Existing files are never overwritten.
"""

from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".secrets"


def main() -> None:
    SECRETS.mkdir(exist_ok=True)
    pw = SECRETS / "local-postgres-password"
    if not pw.exists():
        pw.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
        print(f"created  {pw.relative_to(ROOT)}  (for the optional postgres compose profile)")
    obo = SECRETS / "obo-client-secret"
    if not obo.exists():
        print(f"TODO     paste a client secret of the API app registration into {obo.relative_to(ROOT)}")
    print("SQLite is used unless .secrets/database-url exists. Nothing else is required.")


if __name__ == "__main__":
    main()
