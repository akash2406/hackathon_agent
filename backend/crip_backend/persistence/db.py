"""asyncpg connection pool bound to the allocated schema.

Used by the chat endpoint (via the repository) and the readiness probe. The DSN
comes from the Key Vault secret mounted by the CSI driver; it is never in code,
config or an environment variable literal.

Deliberately simple for the hackathon build (see docs/known-simplifications.md):
a plain asyncpg pool with a connection-string password. Before production load,
revisit: transaction-mode pooler (PgBouncer) compatibility, prepared-statement
caching, and passwordless Entra token auth per connection.
"""

from __future__ import annotations

import json
from importlib import resources

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, schema: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    # schema is validated as a plain identifier in Settings before it gets here.
    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
        server_settings={"search_path": schema, "application_name": "crip-backend"},
        command_timeout=15,
    )


async def apply_schema(pool: asyncpg.Pool) -> None:
    sql = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
