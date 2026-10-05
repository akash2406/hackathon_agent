"""Session, message and agent-invocation persistence.

Flow position: the chat endpoint persists the user's message before invoking the
Orchestrator, and the composed answer plus one ``agent_invocations`` row per
contribution afterwards, in a single transaction. Every read is filtered by the
owner's Entra object ID, so a session id belonging to someone else behaves
exactly like a session id that does not exist.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from ..contracts import ResponseStatus
from ..errors import PersistenceError
from ..tools.context import ToolContribution


@dataclass(frozen=True)
class SessionRecord:
    id: UUID
    owner_oid: str
    foundry_thread_id: str | None


@dataclass(frozen=True)
class MessageRecord:
    id: UUID
    session_id: UUID
    seq: int
    created_at: datetime


@dataclass(frozen=True)
class AccessEvent:
    """One request to CRIP, for the platform admins' usage log."""

    user_oid: str
    user_name: str | None
    action: str  # "chat", "tool:<name>", "me", "admin:<page>"
    scope: str | None
    outcome: str  # ok / partial / no_data / error / denied / failed
    latency_ms: int | None = None
    correlation_id: str | None = None
    detail: str | None = None  # e.g. the question (truncated)


def summarize_usage(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Shared by every store: aggregate access-log rows (newest first) into the usage report."""
    users: dict[str, int] = defaultdict(int)
    actions: dict[str, int] = defaultdict(int)
    scopes: dict[str, int] = defaultdict(int)
    denied = 0
    for r in rows:
        users[r["user_name"] or r["user_oid"]] += 1
        # Dashboard calls are logged per tool ("tool:<name>"); group them for the summary.
        actions["dashboard" if r["action"].startswith("tool:") else r["action"]] += 1
        if r["scope"]:
            scopes[r["scope"]] += 1
        denied += r["outcome"] == "denied"
    top = lambda d: [{"name": k, "events": v} for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:10]]  # noqa: E731
    return {
        "total_events": len(rows),
        "distinct_users": len(users),
        "denied": denied,
        "top_users": [{"user": u["name"], "events": u["events"]} for u in top(users)],
        "by_action": top(actions),
        "by_scope": top(scopes),
        "events": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k != "user_oid"} for r in rows[:limit]
        ],
    }


class ChatRepository(Protocol):
    async def create_session(self, *, owner_oid: str, tenant_id: str, title: str) -> SessionRecord: ...
    async def get_session(self, session_id: UUID, *, owner_oid: str) -> SessionRecord | None: ...
    async def set_thread(self, session_id: UUID, *, owner_oid: str, thread_id: str) -> None: ...
    async def append_message(
        self,
        session_id: UUID,
        *,
        owner_oid: str,
        role: str,
        content: str,
        status: ResponseStatus | None = None,
        error_code: str | None = None,
        contributions: list[ToolContribution] | None = None,
    ) -> MessageRecord: ...
    async def ping(self) -> None: ...
    async def log_access(self, event: AccessEvent) -> None: ...
    async def usage(self, *, since: datetime, limit: int = 100) -> dict[str, Any]: ...


class PostgresChatRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create_session(self, *, owner_oid: str, tenant_id: str, title: str) -> SessionRecord:
        session_id = uuid.uuid4()
        async with self._guard() as conn:
            await conn.execute(
                "INSERT INTO sessions (id, owner_oid, owner_tenant_id, title) VALUES ($1, $2, $3, $4)",
                session_id,
                owner_oid,
                tenant_id,
                title[:200],
            )
        return SessionRecord(id=session_id, owner_oid=owner_oid, foundry_thread_id=None)

    async def get_session(self, session_id: UUID, *, owner_oid: str) -> SessionRecord | None:
        async with self._guard() as conn:
            row = await conn.fetchrow(
                "SELECT id, owner_oid, foundry_thread_id FROM sessions WHERE id = $1 AND owner_oid = $2",
                session_id,
                owner_oid,
            )
        return SessionRecord(row["id"], row["owner_oid"], row["foundry_thread_id"]) if row else None

    async def set_thread(self, session_id: UUID, *, owner_oid: str, thread_id: str) -> None:
        async with self._guard() as conn:
            await conn.execute(
                "UPDATE sessions SET foundry_thread_id = $3, updated_at = now() WHERE id = $1 AND owner_oid = $2",
                session_id,
                owner_oid,
                thread_id,
            )

    async def append_message(
        self,
        session_id: UUID,
        *,
        owner_oid: str,
        role: str,
        content: str,
        status: ResponseStatus | None = None,
        error_code: str | None = None,
        contributions: list[ToolContribution] | None = None,
    ) -> MessageRecord:
        message_id = uuid.uuid4()
        async with self._guard() as conn, conn.transaction():
            # Row-locking increment: concurrent writers to one session get
            # distinct, gap-free sequence numbers.
            seq = await conn.fetchval(
                "UPDATE sessions SET last_seq = last_seq + 1, updated_at = now() "
                "WHERE id = $1 AND owner_oid = $2 RETURNING last_seq",
                session_id,
                owner_oid,
            )
            if seq is None:
                raise PersistenceError(f"session {session_id} not found for owner")
            created_at = await conn.fetchval(
                "INSERT INTO messages (id, session_id, seq, role, content, status, error_code) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING created_at",
                message_id,
                session_id,
                seq,
                role,
                content,
                status.value if status else None,
                error_code,
            )
            for c in contributions or []:
                r = c.response
                await conn.execute(
                    "INSERT INTO agent_invocations (id, message_id, agent_name, tool_name, status, answer, query_used, "
                    "data_timestamp, confidence_level, confidence_score, data, sources, caveats, latency_ms) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
                    uuid.uuid4(),
                    message_id,
                    c.agent,
                    c.tool,
                    r.status.value,
                    r.answer,
                    r.query_used,
                    r.data_timestamp,
                    r.confidence.level.value,
                    r.confidence.score,
                    r.data,
                    [s.model_dump(mode="json") for s in r.sources],
                    list(r.caveats),
                    c.latency_ms,
                )
        return MessageRecord(id=message_id, session_id=session_id, seq=seq, created_at=created_at)

    async def ping(self) -> None:
        async with self._guard() as conn:
            await conn.fetchval("SELECT 1")

    async def log_access(self, event: AccessEvent) -> None:
        async with self._guard() as conn:
            await conn.execute(
                "INSERT INTO access_log (id, user_oid, user_name, action, scope, outcome, latency_ms, correlation_id, detail) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                uuid.uuid4(), event.user_oid, event.user_name, event.action, event.scope, event.outcome,
                event.latency_ms, event.correlation_id, (event.detail or "")[:300] or None,
            )

    async def usage(self, *, since: datetime, limit: int = 100) -> dict[str, Any]:
        async with self._guard() as conn:
            rows = await conn.fetch(
                "SELECT at, user_oid, user_name, action, scope, outcome, latency_ms, detail FROM access_log "
                "WHERE at >= $1 ORDER BY at DESC LIMIT 5000",
                since,
            )
        return summarize_usage([dict(r) for r in rows], limit)

    def _guard(self) -> _PoolGuard:
        return _PoolGuard(self._pool)


class _PoolGuard:
    """Acquire a connection and translate driver/network errors into PersistenceError."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._ctx: object | None = None

    async def __aenter__(self) -> asyncpg.Connection:
        try:
            self._ctx = self._pool.acquire()
            return await self._ctx.__aenter__()  # type: ignore[attr-defined]
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            raise PersistenceError(str(exc)) from exc

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        await self._ctx.__aexit__(exc_type, exc, tb)  # type: ignore[attr-defined]
        if isinstance(exc, (asyncpg.PostgresError, OSError, TimeoutError)):
            raise PersistenceError(str(exc)) from exc
        return False
