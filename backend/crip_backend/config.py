"""Runtime configuration, read from ``CRIP_*`` environment variables.

In Azure App Service these are the web app's *App Settings* (set by
``infra/appservice/main.bicep``). Only non-secret values are plain settings;
secret values are App Settings that are **Key Vault references**, read through
``secrets.py`` by name. Locally, non-secret values come from ``.env`` and secrets
from files in ``.secrets/``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/crip_backend/config.py -> repo root is parents[2]. The container image
# overrides both paths via CRIP_FOUNDRY_DEFINITIONS_DIR / CRIP_STATIC_DIR.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class AzureAccessMode(StrEnum):
    # CRIP's managed identity reads Azure (read-only roles on the management
    # group); CRIP decides per user what they may see (access.resolver).
    APP_IDENTITY = "app_identity"
    # Every Azure call runs as the signed-in user (on-behalf-of).
    USER_OBO = "user_obo"


class OboCredentialMode(StrEnum):
    # The API app registration trusts the web app's user-assigned managed
    # identity (federated identity credential). No secret exists anywhere.
    MANAGED_IDENTITY = "managed_identity"
    # A client secret of the API app registration, read by name from the secret
    # store (App Setting Key Vault reference in Azure, .secrets/ file locally).
    CLIENT_SECRET = "client_secret"


class Settings(BaseSettings):
    # .env (git-ignored) is a local-development convenience for non-secret values only.
    model_config = SettingsConfigDict(env_prefix="CRIP_", extra="ignore", env_file=".env", env_file_encoding="utf-8")

    environment: str = "local"
    log_level: str = "INFO"

    # --- Entra ID. One app registration can be both the SPA and the API
    # (simplest setup); set spa_client_id only if you use two registrations.
    tenant_id: str
    api_client_id: str
    api_audience: str | None = None  # defaults to api://<api_client_id>
    spa_client_id: str | None = None  # defaults to api_client_id
    required_scope: str = "access_as_user"

    # --- Access model (docs/access-model.md)
    azure_access_mode: AzureAccessMode = AzureAccessMode.APP_IDENTITY
    # Limit CRIP to subscriptions under this management group (id/name), and/or an explicit list.
    management_group_id: str | None = None
    subscription_ids: str = ""  # comma-separated; empty = all the app identity can see
    # Grant levels from the user's Azure role assignments on each subscription.
    rbac_access_check: bool = True
    # Entra security group object ids (comma-separated) mapped to access levels.
    platform_admin_group_ids: str = ""
    cost_reader_group_ids: str = ""
    reader_group_ids: str = ""
    access_cache_seconds: int = 900
    graph_endpoint: str = "https://graph.microsoft.com"
    obo_credential_mode: OboCredentialMode = OboCredentialMode.CLIENT_SECRET
    obo_client_secret_name: str = "obo-client-secret"
    # User-assigned managed identity of the web app (App Service sets nothing
    # automatically; Bicep sets AZURE_CLIENT_ID so DefaultAzureCredential uses it too).
    managed_identity_client_id: str | None = Field(
        default=None, validation_alias=AliasChoices("CRIP_MANAGED_IDENTITY_CLIENT_ID", "AZURE_CLIENT_ID")
    )

    # --- Secrets: env var CRIP_SECRET_<NAME> first, then a file in this directory.
    secrets_dir: Path = Path(".secrets")
    database_url_secret_name: str = "database-url"
    appinsights_secret_name: str = "appinsights-connection-string"

    # --- Persistence: PostgreSQL if the database-url secret exists, else SQLite.
    sqlite_path: Path = Path(".local/crip.db")
    db_schema: str = "crip"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # --- Foundry
    foundry_project_endpoint: str
    foundry_model_deployment: str | None = None
    # Create/update the agents from foundry/definitions when the app starts
    # (uses the app's managed identity; needs foundry_model_deployment).
    register_agents_on_startup: bool = False
    foundry_definitions_dir: Path = _REPO_ROOT / "foundry" / "definitions"
    foundry_run_timeout_seconds: float = 120.0
    foundry_poll_interval_seconds: float = 0.5

    # --- Web UI: built React app served by the backend (same origin as the API).
    static_dir: Path = _REPO_ROOT / "frontend" / "dist"
    # Show the UI with clearly labelled SAMPLE data and no sign-in (for previews only).
    # The sample data lives in the browser bundle; the API never serves it. Keep false in Azure.
    ui_demo_mode: bool = False

    # --- Azure Resource Manager (Cost Management, Advisor, Resource Graph)
    arm_endpoint: str = "https://management.azure.com"
    cost_management_api_version: str = "2023-11-01"
    subscriptions_api_version: str = "2022-12-01"
    advisor_api_version: str = "2023-01-01"
    resource_graph_api_version: str = "2022-10-01"
    cost_metric_column: str = "Cost"
    arm_max_retries: int = 3
    arm_max_pages: int = 10
    http_timeout_seconds: float = 60.0

    @property
    def effective_api_audience(self) -> str:
        return self.api_audience or f"api://{self.api_client_id}"

    @property
    def effective_spa_client_id(self) -> str:
        return self.spa_client_id or self.api_client_id

    @property
    def api_scope(self) -> str:
        return f"{self.effective_api_audience}/{self.required_scope}"

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @field_validator("db_schema")
    @classmethod
    def _valid_schema(cls, v: str) -> str:
        # The schema name is interpolated into SQL, so restrict it hard.
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v):
            raise ValueError("db_schema must be a lowercase PostgreSQL identifier")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
