"""SQLite implementation of ``ChatRepository``: the zero-setup default.

Used when no ``database-url`` secret is configured. In App Service the file
lives under ``/home`` (persistent storage shared by the app's instances), so
conversations and the grounding audit trail survive restarts and redeploys.

Hackathon trade-off (docs/known-simplifications.md): SQLite on App Service
storage is for a single instance and modest traffic. Set ``database-url`` to a
PostgreSQL server for anything more.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from uuid import UUID

import aiosqlite

from ..contracts import ResponseStatus
from ..errors import PersistenceError
from ..tools.context import ToolContribution
from .repository import MessageRecord, SessionRecord

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class SqliteChatRepository:
    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path
        # One connection, many coroutines: serialise statements so transactions never interleave.
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> SqliteChatRepository:
        path = _writable_path(path)
        conn = await aiosqlite.connect(path, isolation_level=None)  # explicit transactions below
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        schema = resources.files(__package__).joinpath("schema_sqlite.sql").read_text(encoding="utf-8")
        await conn.executescript(schema)
        log.info("SQLite store at %s", path)
        return cls(conn, path)

    async def close(self) -> None:
        await self._conn.close()

    async def create_session(self, *, owner_oid: str, tenant_id: str, title: str) -> SessionRecord:
        session_id = uuid.uuid4()
        await self._write(
            "INSERT INTO sessions (id, owner_oid, owner_tenant_id, title) VALUES (?, ?, ?, ?)",
            (str(session_id), owner_oid, tenant_id, title[:200]),
        )
        return SessionRecord(id=session_id, owner_oid=owner_oid, foundry_thread_id=None)

    async def get_session(self, session_id: UUID, *, owner_oid: str) -> SessionRecord | None:
        async with self._lock:
            try:
                async with self._conn.execute(
                    "SELECT id, owner_oid, foundry_thread_id FROM sessions WHERE id = ? AND owner_oid = ?",
                    (str(session_id), owner_oid),
                ) as cur:
                    row = await cur.fetchone()
            except sqlite3.Error as exc:
                raise PersistenceError(str(exc)) from exc
        return SessionRecord(UUID(row[0]), row[1], row[2]) if row else None

    async def set_thread(self, session_id: UUID, *, owner_oid: str, thread_id: str) -> None:
        await self._write(
            "UPDATE sessions SET foundry_thread_id = ?, updated_at = ? WHERE id = ? AND owner_oid = ?",
            (thread_id, _now(), str(session_id), owner_oid),
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
        created_at = _now()
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                try:
                    async with self._conn.execute(
                        "UPDATE sessions SET last_seq = last_seq + 1, updated_at = ? WHERE id = ? AND owner_oid = ? RETURNING last_seq",
                        (created_at, str(session_id), owner_oid),
                    ) as cur:
                        row = await cur.fetchone()
                    if row is None:
                        raise PersistenceError(f"session {session_id} not found for owner")
                    seq = int(row[0])
                    await self._conn.execute(
                        "INSERT INTO messages (id, session_id, seq, role, content, status, error_code, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(message_id), str(session_id), seq, role, content, status.value if status else None, error_code, created_at),
                    )
                    for c in contributions or []:
                        r = c.response
                        await self._conn.execute(
                            "INSERT INTO agent_invocations (id, message_id, agent_name, tool_name, status, answer, query_used, "
                            "data_timestamp, confidence_level, confidence_score, data, sources, caveats, latency_ms) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                str(uuid.uuid4()),
                                str(message_id),
                                c.agent,
                                c.tool,
                                r.status.value,
                                r.answer,
                                r.query_used,
                                r.data_timestamp.isoformat() if r.data_timestamp else None,
                                r.confidence.level.value,
                                r.confidence.score,
                                json.dumps(r.data) if r.data is not None else None,
                                json.dumps([s.model_dump(mode="json") for s in r.sources]),
                                json.dumps(list(r.caveats)),
                                c.latency_ms,
                            ),
                        )
                    await self._conn.execute("COMMIT")
                except BaseException:
                    await self._conn.execute("ROLLBACK")
                    raise
            except sqlite3.Error as exc:
                raise PersistenceError(str(exc)) from exc
        return MessageRecord(id=message_id, session_id=session_id, seq=seq, created_at=_parse(created_at))

    async def ping(self) -> None:
        async with self._lock:
            try:
                await self._conn.execute("SELECT 1")
            except sqlite3.Error as exc:
                raise PersistenceError(str(exc)) from exc

    async def _write(self, sql: str, params: tuple) -> None:
        async with self._lock:
            try:
                await self._conn.execute(sql, params)
            except sqlite3.Error as exc:
                raise PersistenceError(str(exc)) from exc


def _writable_path(path: Path) -> Path:
    """Use ``path`` if its directory is writable; otherwise fall back to /tmp (and say so loudly)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / f".crip-write-test-{uuid.uuid4().hex}"
        probe.write_text("ok")
        probe.unlink()
        return path
    except OSError as exc:
        fallback = Path("/tmp/crip.db") if Path("/tmp").is_dir() else Path.cwd() / "crip.db"
        log.warning(
            "SQLite directory %s is not writable (%s); using %s instead. Data will NOT survive a restart. "
            "On App Service set WEBSITES_ENABLE_APP_SERVICE_STORAGE=true or configure database-url.",
            path.parent,
            exc,
            fallback,
        )
        return fallback
