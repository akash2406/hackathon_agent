"""Create the git-ignored .secrets/ directory for local development.

It mirrors the Key Vault CSI mount the backend reads in AKS
(/mnt/secrets-store/<secret-name>):

    .secrets/local-postgres-password      random password for docker-compose Postgres
    .secrets/postgres-connection-string   DSN the backend reads (CRIP_POSTGRES_SECRET_NAME)
    .secrets/obo-client-secret            YOU paste a dev client secret of the backend API
                                          app registration here (only for local OBO)

Usage:  python scripts/init_local_secrets.py
Existing files are never overwritten.
"""

from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".secrets"


def write_once(name: str, value: str) -> None:
    path = SECRETS / name
    if path.exists():
        print(f"kept     {path.relative_to(ROOT)}")
        return
    path.write_text(value + "\n", encoding="utf-8")
    print(f"created  {path.relative_to(ROOT)}")


def main() -> None:
    SECRETS.mkdir(exist_ok=True)
    password_file = SECRETS / "local-postgres-password"
    password = password_file.read_text().strip() if password_file.exists() else secrets.token_urlsafe(24)
    write_once("local-postgres-password", password)
    write_once("postgres-connection-string", f"postgresql://crip:{password}@localhost:5432/crip")
    obo = SECRETS / "obo-client-secret"
    if not obo.exists():
        print(f"TODO     paste a dev client secret for the backend API app registration into {obo.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
