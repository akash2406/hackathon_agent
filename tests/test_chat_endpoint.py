"""POST /api/chat: happy path, failure path, auth and ownership."""

import httpx
import pytest

from crip_backend.agent_gateway.conversation import ConversationService
from crip_backend.agent_gateway.definitions import load_definitions
from crip_backend.agent_gateway.foundry_gateway import FoundryAgentGateway
from crip_backend.contracts import ErrorCode
from crip_backend.errors import ApiError
from crip_backend.main import create_app
from crip_backend.services import Services

from .conftest import OTHER_OID, REPO_ROOT, Fail, Final, InMemoryRepository, ScriptedAgentsClient, ToolCalls
from .test_orchestrator_routing import STUB_TOOLS, costpulse_policy


class FakeValidator:
    def __init__(self, user):
        self.user = user

    async def validate(self, token):
        if token != "good-token":
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Invalid access token.", status_code=401)
        return self.user


def orchestrator_policy(message, outputs):
    if not outputs:
        return ToolCalls([("ask_costpulse", {"question": message})])
    return Final("You have spent 230.75 USD this month (data through 2026-09-21).")


@pytest.fixture
def repo():
    return InMemoryRepository()


def make_app(settings, user, repo, token_source, cost_client, policies):
    gateway = FoundryAgentGateway(ScriptedAgentsClient(policies), poll_interval_seconds=0)
    services = Services(
        settings=settings,
        token_validator=FakeValidator(user),
        tokens=token_source,
        cost_client=cost_client,
        conversation=ConversationService(gateway, load_definitions(REPO_ROOT / "foundry" / "definitions"), STUB_TOOLS),
        repository=repo,
    )
    return create_app(services=services)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


AUTH = {"Authorization": "Bearer good-token"}


async def test_chat_happy_path_returns_grounded_answer_and_persists_provenance(settings, user, repo, token_source, cost_client):
    app = make_app(settings, user, repo, token_source, cost_client, {"crip-orchestrator": orchestrator_policy, "crip-costpulse": costpulse_policy})
    async with client_for(app) as client:
        resp = await client.post("/api/chat", json={"message": "How much did we spend this month?"}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok" and body["grounded"] is True
    [contribution] = body["contributions"]
    assert contribution["agent"] == "costpulse"
    assert contribution["query_used"].startswith("POST https://management.azure.com/")
    assert contribution["data_timestamp"] == "2026-09-21T00:00:00Z"
    assert contribution["sources"][0]["auth"] == "user_obo"

    assert [(m["seq"], m["role"]) for m in repo.messages] == [(1, "user"), (2, "assistant")]
    assert len(repo.invocations) == 1
    # A follow-up in the same session reuses the session and its Foundry thread.
    async with client_for(app) as client:
        again = await client.post("/api/chat", json={"message": "And by resource type?", "session_id": body["session_id"]}, headers=AUTH)
    assert again.status_code == 200
    assert [m["seq"] for m in repo.messages] == [1, 2, 3, 4]


async def test_orchestrator_failure_returns_typed_error_envelope(settings, user, repo, token_source, cost_client):
    app = make_app(settings, user, repo, token_source, cost_client, {"crip-orchestrator": lambda m, o: Fail(), "crip-costpulse": costpulse_policy})
    async with client_for(app) as client:
        resp = await client.post("/api/chat", json={"message": "cost?"}, headers=AUTH)

    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error["code"] == "orchestrator_unavailable" and error["retryable"] is True
    assert error["correlation_id"] == resp.headers["x-correlation-id"]
    assert "answer" not in resp.json()
    assert repo.messages[-1]["status"].value == "error"


async def test_missing_token_is_rejected_with_envelope(settings, user, repo, token_source, cost_client):
    app = make_app(settings, user, repo, token_source, cost_client, {"crip-orchestrator": orchestrator_policy, "crip-costpulse": costpulse_policy})
    async with client_for(app) as client:
        resp = await client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


async def test_another_users_session_is_not_found(settings, user, repo, token_source, cost_client):
    foreign = await repo.create_session(owner_oid=OTHER_OID, tenant_id="t", title="theirs")
    app = make_app(settings, user, repo, token_source, cost_client, {"crip-orchestrator": orchestrator_policy, "crip-costpulse": costpulse_policy})
    async with client_for(app) as client:
        resp = await client.post("/api/chat", json={"message": "hi", "session_id": str(foreign.id)}, headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"


async def test_ungrounded_reply_is_labelled(settings, user, repo, token_source, cost_client):
    app = make_app(settings, user, repo, token_source, cost_client, {"crip-orchestrator": lambda m, o: Final("Hello!"), "crip-costpulse": costpulse_policy})
    async with client_for(app) as client:
        body = (await client.post("/api/chat", json={"message": "hello"}, headers=AUTH)).json()
    assert body["grounded"] is False and body["status"] == "no_data"
    assert "not grounded" in body["caveats"][0]


async def test_health_endpoints(settings, user, repo, token_source, cost_client):
    app = make_app(settings, user, repo, token_source, cost_client, {"crip-orchestrator": orchestrator_policy, "crip-costpulse": costpulse_policy})
    async with client_for(app) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/health/live")).json() == {"status": "ok"}
