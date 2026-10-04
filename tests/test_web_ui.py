"""The single container serves the SPA, its runtime config and the API from one origin."""

import httpx
import pytest

from crip_backend.main import create_app

from .test_chat_endpoint import make_app
from .test_orchestrator_routing import costpulse_policy
from .conftest import Final


@pytest.fixture
def ui_app(settings, user, token_source, arm_client, tmp_path):
    from .conftest import InMemoryRepository

    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<div id="root"></div>')
    (static / "assets" / "app.js").write_text("console.log('crip')")
    (static / "favicon.svg").write_text("<svg/>")
    settings.static_dir = static
    settings.spa_client_id = None  # single app registration
    return make_app(settings, user, InMemoryRepository(), token_source, arm_client, {"crip-orchestrator": lambda m, o: Final("hi"), "crip-costpulse": costpulse_policy})


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_spa_and_assets_are_served(ui_app):
    async with client(ui_app) as c:
        assert 'id="root"' in (await c.get("/")).text
        assert (await c.get("/assets/app.js")).text == "console.log('crip')"
        assert (await c.get("/favicon.svg")).status_code == 200
        deep = await c.get("/conversations/123")
        assert 'id="root"' in deep.text and deep.headers["cache-control"] == "no-store"
        assert deep.headers["x-content-type-options"] == "nosniff"


async def test_config_js_is_built_from_settings(ui_app, settings):
    async with client(ui_app) as c:
        js = (await c.get("/config.js")).text
    assert f'"tenantId": "{settings.tenant_id}"' in js
    assert f'"spaClientId": "{settings.api_client_id}"' in js  # defaults to the API registration
    assert f'"apiScope": "api://{settings.api_client_id}/access_as_user"' in js
    assert '"demoMode": false' in js  # sample-data preview is opt-in only


async def test_unknown_api_path_gets_error_envelope_not_html(ui_app):
    async with client(ui_app) as c:
        r = await c.get("/api/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_path_traversal_does_not_escape_static_dir(ui_app):
    async with client(ui_app) as c:
        r = await c.get("/..%2F..%2Fpyproject.toml")
    assert 'id="root"' in r.text  # falls back to the SPA shell, never a file outside dist


async def test_capabilities_lists_agents_and_examples(ui_app):
    async with client(ui_app) as c:
        caps = (await c.get("/api/capabilities")).json()
    keys = {a["key"] for a in caps["agents"]}
    assert keys == {"costpulse", "optimizer", "inventory"}
    assert all(a["examples"] for a in caps["agents"])
    assert all(a["summary"] and len(a["summary"]) < 120 for a in caps["agents"])  # short, user-facing


async def test_api_only_when_ui_not_built(settings, user, token_source, arm_client):
    from .conftest import InMemoryRepository

    app = make_app(settings, user, InMemoryRepository(), token_source, arm_client, {"crip-orchestrator": lambda m, o: Final("hi"), "crip-costpulse": costpulse_policy})
    async with client(app) as c:
        assert (await c.get("/")).status_code == 404
        assert (await c.get("/health/live")).status_code == 200


def test_create_app_is_importable():
    assert callable(create_app)
