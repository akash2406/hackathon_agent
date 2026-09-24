"""Constructs and holds the long-lived service objects (the composition root).

Built once in the app lifespan and stored on ``app.state.services``. Tests build
a ``Services`` with fakes instead, which is why routes depend on this container
rather than constructing clients themselves.

Credential boundaries are decided here and nowhere else:

* ``DefaultAzureCredential`` (Workload Identity in-cluster) -> Foundry only.
* The OBO provider -> Cost Management only (via ``ToolContext.arm_token``).
* Key Vault CSI-mounted files -> the PostgreSQL and App Insights connection strings.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx

from .agent_gateway.conversation import ConversationService
from .agent_gateway.definitions import load_definitions, verify_tool_coverage
from .agent_gateway.foundry_gateway import FoundryAgentGateway
from .auth.entra import EntraTokenValidator
from .auth.obo import OnBehalfOfTokenProvider
from .azure_clients.cost_management import CostManagementClient
from .config import Settings
from .persistence.repository import ChatRepository
from .secrets import SecretStore
from .tools.context import DelegatedTokenSource
from .tools.registry import TOOLS

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    token_validator: EntraTokenValidator
    tokens: DelegatedTokenSource
    cost_client: CostManagementClient
    conversation: ConversationService
    repository: ChatRepository
    _resources: AsyncExitStack = field(default_factory=AsyncExitStack)

    async def aclose(self) -> None:
        await self._resources.aclose()


async def build_services(settings: Settings) -> Services:
    # Imported lazily so unit tests never need Azure SDK async transports.
    from azure.ai.agents.aio import AgentsClient
    from azure.identity.aio import DefaultAzureCredential

    from .persistence.db import apply_schema, create_pool
    from .persistence.repository import PostgresChatRepository

    stack = AsyncExitStack()
    try:
        secrets = SecretStore(settings.secrets_dir)

        definitions = load_definitions(settings.foundry_definitions_dir)
        verify_tool_coverage(definitions, TOOLS)

        pool = await create_pool(
            secrets.get(settings.postgres_secret_name),
            settings.db_schema,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
        )
        stack.push_async_callback(pool.close)
        await apply_schema(pool)

        http = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        stack.push_async_callback(http.aclose)

        # The backend's OWN identity (Workload Identity). Used for Foundry only;
        # it never touches Cost Management.
        platform_credential = DefaultAzureCredential()
        stack.push_async_callback(platform_credential.close)
        agents_client: Any = AgentsClient(endpoint=settings.foundry_project_endpoint, credential=platform_credential)
        stack.push_async_callback(agents_client.close)

        gateway = FoundryAgentGateway(
            agents_client,
            run_timeout_seconds=settings.foundry_run_timeout_seconds,
            poll_interval_seconds=settings.foundry_poll_interval_seconds,
        )
        services = Services(
            settings=settings,
            token_validator=EntraTokenValidator(settings),
            tokens=OnBehalfOfTokenProvider(settings, secrets),
            cost_client=CostManagementClient(
                http,
                arm_endpoint=settings.arm_endpoint,
                api_version=settings.cost_management_api_version,
                subscriptions_api_version=settings.subscriptions_api_version,
                metric_column=settings.cost_metric_column,
                max_retries=settings.cost_max_retries,
                max_pages=settings.cost_max_pages,
            ),
            conversation=ConversationService(gateway, definitions),
            repository=PostgresChatRepository(pool),
            _resources=stack,
        )
        log.info("services ready: orchestrator=%s domains=%s", definitions.orchestrator.name, [d.key for d in definitions.domains])
        return services
    except BaseException:
        await stack.aclose()
        raise
