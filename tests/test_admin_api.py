"""/api/me, admin endpoints and the usage log, over HTTP."""

import json

import httpx

from crip_backend.access.model import AccessLevel

from .conftest import SUBSCRIPTION, FakeAccessResolver, Final, InMemoryRepository, make_access
from .test_chat_endpoint import AUTH, make_app
from .test_orchestrator_routing import costpulse_policy


def app_for(settings, user, token_source, arm_client, access, repo=None):
    return make_app(settings, user, repo or InMemoryRepository(), token_source, arm_client,
                    {"crip-orchestrator": lambda m, o: Final("hi"), "crip-costpulse": costpulse_policy}, access=FakeAccessResolver(access))


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_me_describes_access(settings, user, token_source, arm_client):
    app = app_for(settings, user, token_source, arm_client, make_access(AccessLevel.RESOURCES, via="rbac:Reader"))
    async with client(app) as c:
        me = (await c.get("/api/me", headers=AUTH)).json()
    assert me["is_platform_admin"] is False
    assert me["subscriptions"] == [{"subscription_id": SUBSCRIPTION, "display_name": f"Sub {SUBSCRIPTION[:4]}", "level": "resources", "via": ["rbac:Reader"]}]
    assert me["access_mode"] == "app_identity"


async def test_admin_endpoints_refuse_non_admins_and_log_it(settings, user, token_source, arm_client):
    repo = InMemoryRepository()
    app = app_for(settings, user, token_source, arm_client, make_access(AccessLevel.COST), repo)
    async with client(app) as c:
        r = await c.get("/api/admin/usage", headers=AUTH)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert repo.access_log[0]["outcome"] == "denied"


async def test_admin_sees_usage_including_denied_dashboard_calls(settings, user, token_source, arm_client):
    repo = InMemoryRepository()
    reader = app_for(settings, user, token_source, arm_client, make_access(AccessLevel.RESOURCES, via="rbac:Reader"), repo)
    async with client(reader) as c:
        denied = await c.post("/api/tools/costpulse_query_costs", headers=AUTH, content=json.dumps({"scope": SUBSCRIPTION}))
    assert denied.status_code == 200 and denied.json()["answer"].startswith("Access denied")

    admin = app_for(settings, user, token_source, arm_client, make_access(AccessLevel.COST, admin=True), repo)
    async with client(admin) as c:
        usage = (await c.get("/api/admin/usage?days=7", headers=AUTH)).json()
        settings_view = (await c.get("/api/admin/settings", headers=AUTH)).json()
    assert usage["denied"] == 1
    assert usage["events"][0]["action"] == "tool:costpulse_query_costs" and usage["events"][0]["scope"] == SUBSCRIPTION
    assert settings_view["access_mode"] == "app_identity" and settings_view["graph"] == "not configured"
