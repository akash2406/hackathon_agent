"""Runtime configuration, read from ``CRIP_*`` environment variables.

Only *non-secret* values live here: tenant/client IDs, endpoints, schema names,
and the *names* of secrets. Secret *values* are never read from environment
variables; they are read from files that the Key Vault CSI driver mounts into
the pod (see ``secrets.py``). The Helm chart renders these variables from the
landing-zone allocations (docs/landing-zone-requests.md).
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/crip_backend/config.py -> repo root is parents[2]. In the container
# image the path is overridden via CRIP_FOUNDRY_DEFINITIONS_DIR.
_DEFAULT_DEFINITIONS_DIR = Path(__file__).resolve().parents[2] / "foundry" / "definitions"


class OboCredentialMode(StrEnum):
    # The backend app registration trusts the pod's projected service-account
    # token via a federated identity credential. No secret exists anywhere.
    FEDERATED = "federated"
    # Local development only: a client secret stored in Key Vault / a local
    # secrets directory, read from a file by name. Never an env var literal.
    SECRET_FILE = "secret_file"


class Settings(BaseSettings):
    # .env (git-ignored) is a local-development convenience for non-secret values only.
    model_config = SettingsConfigDict(env_prefix="CRIP_", extra="ignore", env_file=".env", env_file_encoding="utf-8")

    environment: str = "local"
    log_level: str = "INFO"

    # --- Entra ID (landing-zone allocation #6)
    tenant_id: str
    api_client_id: str
    api_audience: str | None = None  # defaults to api://<api_client_id>
    required_scope: str = "access_as_user"
    obo_credential_mode: OboCredentialMode = OboCredentialMode.FEDERATED
    obo_client_secret_name: str | None = None  # only for SECRET_FILE mode
    # Set by the AKS workload identity webhook in-cluster.
    federated_token_file: Path | None = Field(default=None, validation_alias="AZURE_FEDERATED_TOKEN_FILE")

    # --- Secrets (allocation #5): directory where Key Vault CSI mounts secrets
    secrets_dir: Path = Path("/mnt/secrets-store")
    postgres_secret_name: str = "postgres-connection-string"
    appinsights_secret_name: str = "appinsights-connection-string"

    # --- PostgreSQL (allocation #2)
    db_schema: str
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # --- Foundry (allocation #7)
    foundry_project_endpoint: str
    # Agent names/tools come from these files (shared with foundry/register_agents.py).
    foundry_definitions_dir: Path = _DEFAULT_DEFINITIONS_DIR
    foundry_run_timeout_seconds: float = 120.0
    foundry_poll_interval_seconds: float = 0.5

    # --- Cost Management
    arm_endpoint: str = "https://management.azure.com"
    cost_management_api_version: str = "2023-11-01"
    subscriptions_api_version: str = "2022-12-01"
    cost_metric_column: str = "Cost"
    cost_max_retries: int = 3
    cost_max_pages: int = 10
    http_timeout_seconds: float = 60.0

    @property
    def effective_api_audience(self) -> str:
        return self.api_audience or f"api://{self.api_client_id}"

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @field_validator("db_schema")
    @classmethod
    def _valid_schema(cls, v: str) -> str:
        # The schema name is interpolated into search_path, so restrict it hard.
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v):
            raise ValueError("db_schema must be a lowercase PostgreSQL identifier")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
