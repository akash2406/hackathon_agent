"""Constructs and holds the long-lived service objects (the composition root).

Built once in the app lifespan and stored on ``app.state.services``. Tests build
a ``Services`` with fakes instead, which is why routes depend on this container
rather than constructing clients themselves.

Credential boundaries are decided here and nowhere else:

* ``DefaultAzureCredential`` (the App Service managed identity in Azure, your
  ``az login`` locally) -> Foundry (running and registering agents) and, in the
  default ``app_identity`` access mode, Azure data reads and Microsoft Graph.
  What each *user* may see is decided by ``AccessResolver`` before any read.
* ``user_obo`` access mode -> Azure data calls run as the user (OBO provider).
* Secret store (Key Vault references / local files) -> database URL, OBO client
  secret, App Insights connection string.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx

from .agent_gateway.conversation import ConversationService
from .agent_gateway.definitions import AgentDefinitions, load_definitions, verify_tool_coverage
from .agent_gateway.foundry_gateway import FoundryAgentGateway
from .auth.entra import EntraTokenValidator
from .auth.app_identity import AppIdentityTokens
from .auth.obo import OnBehalfOfTokenProvider
from .azure_clients.arm import ArmClient
from .config import Settings
from .persistence.repository import ChatRepository
from .secrets import SecretStore
from .access.graph import GraphClient
from .access.resolver import AccessResolver
from .tools.context import AzureDataTokens
from .tools.registry import TOOLS

log = logging.getLogger(__name__)

_REGISTRATION_TIMEOUT_SECONDS = 90


@dataclass
class Services:
    settings: Settings
    token_validator: EntraTokenValidator
    tokens: AzureDataTokens  # how Azure data is read: app identity (default) or user OBO
    arm: ArmClient
    conversation: ConversationService
    repository: ChatRepository
    access: AccessResolver  # who may see what
    graph: GraphClient | None = None  # principal names / group membership (app_identity mode)
    agent_names: list[str] = field(default_factory=list)
    _resources: AsyncExitStack = field(default_factory=AsyncExitStack)

    async def aclose(self) -> None:
        await self._resources.aclose()


async def build_repository(settings: Settings, secrets: SecretStore, stack: AsyncExitStack) -> ChatRepository:
    """PostgreSQL if a ``database-url`` secret exists, otherwise SQLite (zero setup)."""
    dsn = secrets.get_optional(settings.database_url_secret_name)
    if dsn:
        from .persistence.db import apply_schema, create_pool
        from .persistence.repository import PostgresChatRepository

        pool = await create_pool(dsn, settings.db_schema, min_size=settings.db_pool_min_size, max_size=settings.db_pool_max_size)
        stack.push_async_callback(pool.close)
        await apply_schema(pool)
        log.info("persistence: PostgreSQL (schema %s)", settings.db_schema)
        return PostgresChatRepository(pool)

    from .persistence.sqlite_repository import SqliteChatRepository

    repo = await SqliteChatRepository.open(settings.sqlite_path)
    stack.push_async_callback(repo.close)
    log.info("persistence: SQLite at %s", repo.path)
    return repo


async def register_agents(agents_client: Any, definitions: AgentDefinitions, settings: Settings) -> None:
    """Create/update the Foundry agents at startup. Failure is logged, not fatal.

    The app still serves the UI and health probes; chat requests then fail with
    an honest "agent not registered" error until registration succeeds.
    """
    from .agent_gateway.registration import build_agent_specs, upsert_agents

    if not settings.foundry_model_deployment:
        log.error("CRIP_REGISTER_AGENTS_ON_STARTUP is true but CRIP_FOUNDRY_MODEL_DEPLOYMENT is not set; skipping registration")
        return
    try:
        ids = await asyncio.wait_for(
            upsert_agents(agents_client, build_agent_specs(definitions), settings.foundry_model_deployment),
            timeout=_REGISTRATION_TIMEOUT_SECONDS,
        )
        log.info("Foundry agents registered: %s", ids)
    except Exception:
        log.exception("Foundry agent registration failed; chat will report 'agent not registered' until it succeeds")


async def build_services(settings: Settings) -> Services:
    # Imported lazily so unit tests never need Azure SDK async transports.
    from azure.ai.agents.aio import AgentsClient
    from azure.identity.aio import DefaultAzureCredential

    stack = AsyncExitStack()
    try:
        secrets = SecretStore(settings.secrets_dir)

        definitions = load_definitions(settings.foundry_definitions_dir)
        verify_tool_coverage(definitions, TOOLS)

        repository = await build_repository(settings, secrets, stack)

        http = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        stack.push_async_callback(http.aclose)

        # The app's OWN identity. Used for Foundry only; never for Azure data.
        platform_credential = DefaultAzureCredential()
        stack.push_async_callback(platform_credential.close)
        agents_client: Any = AgentsClient(endpoint=settings.foundry_project_endpoint, credential=platform_credential)
        stack.push_async_callback(agents_client.close)

        if settings.register_agents_on_startup:
            await register_agents(agents_client, definitions, settings)

        gateway = FoundryAgentGateway(
            agents_client,
            run_timeout_seconds=settings.foundry_run_timeout_seconds,
            poll_interval_seconds=settings.foundry_poll_interval_seconds,
        )
        arm = ArmClient(
            http,
            arm_endpoint=settings.arm_endpoint,
            cost_api_version=settings.cost_management_api_version,
            subscriptions_api_version=settings.subscriptions_api_version,
            advisor_api_version=settings.advisor_api_version,
            resource_graph_api_version=settings.resource_graph_api_version,
            metric_column=settings.cost_metric_column,
            max_retries=settings.arm_max_retries,
            max_pages=settings.arm_max_pages,
        )
        if settings.azure_access_mode == "user_obo":
            obo = OnBehalfOfTokenProvider(settings, secrets)
            data_tokens: Any = obo
            graph = None
            token_fn = lambda user: obo.token_for(session_id=None, user=user)  # noqa: E731
        else:
            app_tokens = AppIdentityTokens(platform_credential)
            data_tokens = app_tokens
            graph = GraphClient(http, app_tokens.graph, settings.graph_endpoint)
            token_fn = lambda user: app_tokens.arm()  # noqa: E731
        services = Services(
            settings=settings,
            token_validator=EntraTokenValidator(settings),
            tokens=data_tokens,
            arm=arm,
            conversation=ConversationService(gateway, definitions),
            repository=repository,
            access=AccessResolver(settings, arm, token_fn, graph),
            graph=graph,
            agent_names=[definitions.orchestrator.name, *(d.name for d in definitions.domains)],
            _resources=stack,
        )
        log.info("services ready: orchestrator=%s domains=%s", definitions.orchestrator.name, [d.key for d in definitions.domains])
        return services
    except BaseException:
        await stack.aclose()
        raise
