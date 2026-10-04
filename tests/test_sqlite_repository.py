"""SQLite store (the zero-setup default): ordering, ownership, provenance, grounding constraint."""

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest

from crip_backend.contracts import AgentResponse, Confidence, ResponseStatus, Source
from crip_backend.persistence.sqlite_repository import SqliteChatRepository
from crip_backend.tools.context import ToolContribution

NOW = datetime(2026, 9, 21, tzinfo=UTC)


@pytest.fixture
async def repo(tmp_path):
    r = await SqliteChatRepository.open(tmp_path / "data" / "crip.db")
    yield r
    await r.close()


def grounded():
    return AgentResponse(
        agent="costpulse",
        status=ResponseStatus.OK,
        answer="42 USD",
        confidence=Confidence.from_score(0.9),
        data={"total": 42},
        query_used="POST x",
        data_timestamp=NOW,
        sources=[Source(tool="t", api="POST x", invoked_at=NOW, scope="/subscriptions/x")],
    )


async def test_messages_get_gap_free_sequence_under_concurrency(repo):
    s = await repo.create_session(owner_oid="oid-1", tenant_id="t", title="hello")
    records = await asyncio.gather(*(repo.append_message(s.id, owner_oid="oid-1", role="user", content=f"m{i}") for i in range(10)))
    assert sorted(r.seq for r in records) == list(range(1, 11))


async def test_sessions_are_owner_scoped(repo):
    s = await repo.create_session(owner_oid="oid-1", tenant_id="t", title="x")
    assert await repo.get_session(s.id, owner_oid="someone-else") is None
    await repo.set_thread(s.id, owner_oid="oid-1", thread_id="thread_9")
    assert (await repo.get_session(s.id, owner_oid="oid-1")).foundry_thread_id == "thread_9"


async def test_invocations_persist_and_db_rejects_ungrounded_ok_rows(repo):
    s = await repo.create_session(owner_oid="oid-1", tenant_id="t", title="x")
    m = await repo.append_message(
        s.id, owner_oid="oid-1", role="assistant", content="a", status=ResponseStatus.OK,
        contributions=[ToolContribution(agent="costpulse", tool="t", response=grounded(), latency_ms=5)],
    )
    conn = sqlite3.connect(repo.path)
    try:
        row = conn.execute("SELECT status, json_extract(sources, '$[0].auth'), json_extract(data, '$.total') FROM agent_invocations").fetchone()
        assert row == ("ok", "user_obo", 42)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO agent_invocations (id, message_id, agent_name, status, answer, confidence_level, confidence_score, latency_ms) "
                "VALUES ('x', ?, 'x', 'ok', 'fabricated', 'high', 0.9, 1)",
                (str(m.id),),
            )
    finally:
        conn.close()


async def test_unwritable_directory_falls_back_with_warning(tmp_path, caplog):
    from crip_backend.persistence import sqlite_repository

    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")  # mkdir under it fails
    path = sqlite_repository._writable_path(blocked / "crip.db")
    assert path != blocked / "crip.db"
    assert "will NOT survive a restart" in caplog.text
