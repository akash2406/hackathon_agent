"""Shared fixtures and test doubles.

No test here talks to Azure, Entra, Foundry or PostgreSQL. HTTP to Cost
Management is intercepted with respx; Foundry is replaced by a scripted client
whose "model" decisions are explicit in each test; the database is an in-memory
repository. The doubles implement the same interfaces the production code uses.
"""

from __future__ import annotations

import itertools
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest

from crip_backend.auth.entra import AuthenticatedUser
from crip_backend.auth.obo import DelegatedToken
from crip_backend.azure_clients.arm import ArmClient
from crip_backend.config import Settings
from crip_backend.contracts import ResponseStatus
from crip_backend.persistence.repository import MessageRecord, SessionRecord
from crip_backend.tools.context import ToolContext, ToolContribution

TENANT = "11111111-1111-1111-1111-111111111111"
API_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
USER_OID = "33333333-3333-3333-3333-333333333333"
OTHER_OID = "44444444-4444-4444-4444-444444444444"
SUBSCRIPTION = "55555555-5555-5555-5555-555555555555"
FIXED_NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
_TEST_SIGNING_KEY = "unit-test-only-signing-key-not-a-secret-000"  # tokens here are never sent anywhere
REPO_ROOT = Path(__file__).resolve().parents[1]


def make_arm_token(oid: str = USER_OID, *, app_only: bool = False, aud: str = "https://management.azure.com/") -> str:
    claims: dict[str, Any] = {"aud": aud, "oid": oid, "tid": TENANT, "exp": int(time.time()) + 3600}
    if app_only:
        claims["idtyp"] = "app"
        claims["roles"] = ["Reader"]
    else:
        claims["scp"] = "user_impersonation"
    return jwt.encode(claims, _TEST_SIGNING_KEY, algorithm="HS256")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # never let a developer's local .env leak into tests
        tenant_id=TENANT,
        api_client_id=API_CLIENT_ID,
        db_schema="crip",
        sqlite_path=tmp_path / "crip.db",
        static_dir=tmp_path / "no-ui",
        foundry_project_endpoint="https://example.services.ai.azure.com/api/projects/crip",
        secrets_dir=tmp_path,
        foundry_definitions_dir=REPO_ROOT / "foundry" / "definitions",
    )


@pytest.fixture
def user() -> AuthenticatedUser:
    return AuthenticatedUser(
        object_id=USER_OID,
        tenant_id=TENANT,
        scopes=frozenset({"access_as_user"}),
        display_name="Test User",
        username="test.user@example.com",
        raw_token="incoming-user-api-token",
    )


class FakeTokenSource:
    """Stands in for OnBehalfOfTokenProvider; hands out a user-shaped delegated ARM token."""

    def __init__(self, token: str | None = None, error: Exception | None = None) -> None:
        self.token = token or make_arm_token()
        self.error = error
        self.calls: list[tuple[UUID | None, str]] = []

    async def get_token(self, *, session_id: UUID | None, user: AuthenticatedUser) -> DelegatedToken:
        self.calls.append((session_id, user.object_id))
        if self.error:
            raise self.error
        return DelegatedToken(access_token=self.token, expires_at=time.time() + 3600, subject_oid=user.object_id)


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
async def arm_client(fake_sleep: FakeSleep):
    async with httpx.AsyncClient() as http:
        yield ArmClient(http, sleep=fake_sleep, clock=lambda: FIXED_NOW, max_retries=2)


@pytest.fixture
def token_source() -> FakeTokenSource:
    return FakeTokenSource()


@pytest.fixture
def tool_ctx(user: AuthenticatedUser, token_source: FakeTokenSource, arm_client: ArmClient) -> ToolContext:
    return ToolContext(user=user, session_id=uuid.uuid4(), tokens=token_source, arm=arm_client)


def cost_query_payload(rows: list[list[Any]], *, next_link: str | None = None) -> dict[str, Any]:
    return {
        "properties": {
            "nextLink": next_link,
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "ResourceGroupName", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": rows,
        }
    }


# --------------------------------------------------------------------------- scripted Foundry


@dataclass
class ToolCalls:
    calls: list[tuple[str, dict[str, Any]]]


@dataclass
class Final:
    text: str


class Fail:
    """Makes the run end in status 'failed'."""


# A "policy" plays the model: given the user message and the tool outputs so far, it returns the next step.
Policy = Callable[[str, list[str]], ToolCalls | Final | Fail]


@dataclass
class _Run:
    id: str
    thread_id: str
    agent_name: str
    message: str
    outputs: list[str] = field(default_factory=list)
    status: str = "queued"
    required_action: Any = None
    last_error: Any = None
    final_text: str | None = None


class ScriptedAgentsClient:
    """Implements the slice of azure.ai.agents.aio.AgentsClient that FoundryAgentGateway uses."""

    def __init__(self, policies: dict[str, Policy]) -> None:
        self._policies = policies
        self._agents = [SimpleNamespace(name=name, id=f"asst_{name}") for name in policies]
        self._ids = itertools.count(1)
        self._runs: dict[str, _Run] = {}
        self._pending_message: dict[str, str] = {}
        self.runs_by_agent: dict[str, int] = {name: 0 for name in policies}
        self.tool_outputs_seen: dict[str, list[str]] = {name: [] for name in policies}
        self.threads = SimpleNamespace(create=self._create_thread)
        self.messages = SimpleNamespace(create=self._create_message, list=self._list_messages)
        self.runs = SimpleNamespace(
            create=self._create_run, get=self._get_run, submit_tool_outputs=self._submit, cancel=self._cancel
        )

    async def list_agents(self, **_: Any):
        for agent in self._agents:
            yield agent

    async def _create_thread(self, **_: Any):
        return SimpleNamespace(id=f"thread_{next(self._ids)}")

    async def _create_message(self, *, thread_id: str, role: str, content: str, **_: Any):
        self._pending_message[thread_id] = content
        return SimpleNamespace(id=f"msg_{next(self._ids)}")

    async def _create_run(self, *, thread_id: str, agent_id: str, **_: Any):
        name = agent_id.removeprefix("asst_")
        self.runs_by_agent[name] += 1
        run = _Run(id=f"run_{next(self._ids)}", thread_id=thread_id, agent_name=name, message=self._pending_message[thread_id])
        self._runs[run.id] = run
        self._advance(run)
        return run

    async def _get_run(self, *, thread_id: str, run_id: str, **_: Any):
        return self._runs[run_id]

    async def _submit(self, *, thread_id: str, run_id: str, tool_outputs: list[Any], **_: Any):
        run = self._runs[run_id]
        outputs = [o.output for o in tool_outputs]
        run.outputs.extend(outputs)
        self.tool_outputs_seen[run.agent_name].extend(outputs)
        self._advance(run)
        return run

    async def _cancel(self, *, thread_id: str, run_id: str, **_: Any):
        self._runs[run_id].status = "cancelled"

    async def _list_messages(self, *, thread_id: str, run_id: str, **_: Any):
        run = self._runs[run_id]
        if run.final_text:
            yield SimpleNamespace(role="assistant", text_messages=[SimpleNamespace(text=SimpleNamespace(value=run.final_text))])

    def _advance(self, run: _Run) -> None:
        step = self._policies[run.agent_name](run.message, run.outputs)
        if isinstance(step, Fail):
            run.status, run.last_error = "failed", {"code": "server_error", "message": "scripted failure"}
        elif isinstance(step, Final):
            run.status, run.final_text, run.required_action = "completed", step.text, None
        else:
            calls = [
                SimpleNamespace(id=f"call_{next(self._ids)}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                for n, a in step.calls
            ]
            run.status = "requires_action"
            run.required_action = SimpleNamespace(submit_tool_outputs=SimpleNamespace(tool_calls=calls))


# --------------------------------------------------------------------------- in-memory repository


class InMemoryRepository:
    def __init__(self) -> None:
        self.sessions: dict[UUID, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.invocations: list[ToolContribution] = []

    async def create_session(self, *, owner_oid: str, tenant_id: str, title: str) -> SessionRecord:
        sid = uuid.uuid4()
        self.sessions[sid] = {"owner_oid": owner_oid, "thread": None, "last_seq": 0}
        return SessionRecord(sid, owner_oid, None)

    async def get_session(self, session_id: UUID, *, owner_oid: str) -> SessionRecord | None:
        s = self.sessions.get(session_id)
        if s is None or s["owner_oid"] != owner_oid:
            return None
        return SessionRecord(session_id, owner_oid, s["thread"])

    async def set_thread(self, session_id: UUID, *, owner_oid: str, thread_id: str) -> None:
        self.sessions[session_id]["thread"] = thread_id

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
        s = self.sessions[session_id]
        s["last_seq"] += 1
        record = MessageRecord(uuid.uuid4(), session_id, s["last_seq"], datetime.now(UTC))
        self.messages.append(
            {"id": record.id, "seq": record.seq, "role": role, "content": content, "status": status, "error_code": error_code}
        )
        self.invocations.extend(contributions or [])
        return record

    async def ping(self) -> None:
        return None
