"""PostgreSQL connection pool (optional store, used when the ``database-url`` secret is set).

Deliberately simple for the hackathon build (see docs/known-simplifications.md):
a plain asyncpg pool with a connection-string password from Key Vault. Before
production load, revisit: PgBouncer compatibility, prepared-statement caching,
and passwordless Entra token auth per connection.
"""

from __future__ import annotations

import json
from importlib import resources

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, schema: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    # Create the schema first (on its own connection) so search_path can point at it.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')  # schema validated as an identifier in Settings
    finally:
        await conn.close()
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
