"""scripts/deploy.py: variable files -> App Service settings, kept in sync with the Bicep template."""

import importlib.util
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

spec = importlib.util.spec_from_file_location("deploy", REPO_ROOT / "scripts" / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
sys.modules["deploy"] = deploy
spec.loader.exec_module(deploy)

BICEP = (REPO_ROOT / "infra" / "appservice" / "main.bicep").read_text(encoding="utf-8")
# Settings Bicep derives from the resources it creates; the script reads or keeps them instead.
FROM_RESOURCES = {"AZURE_CLIENT_ID", "APPLICATIONINSIGHTS_CONNECTION_STRING"}

CFG = {
    "resourceGroup": "rg-crip-dev", "namePrefix": "crip-dev", "location": "australiaeast",
    "apiClientId": "aaaaaaaa-0000-0000-0000-000000000001", "spaClientId": "",
    "azureAccessMode": "app_identity", "managementGroupId": "<management-group-id>",
    "platformAdminGroupIds": "g1,g2", "costReaderGroupIds": "", "readerGroupIds": "",
    "oboCredentialMode": "managed_identity", "oboClientSecretKeyVaultUri": "",
    "foundryProjectEndpoint": "https://x.services.ai.azure.com/api/projects/crip", "foundryModelDeployment": "gpt-4o",
    "uiDemoMode": "false",
}


def test_reads_the_committed_variable_files():
    cfg = deploy.load_config("dev", REPO_ROOT / ".azuredevops" / "vars", ["location=australiaeast"])
    assert cfg["namePrefix"] == "crip-dev" and cfg["location"] == "australiaeast"  # --set overrides
    assert cfg["spaClientId"] == "" and cfg["uiDemoMode"] in ("true", "false")
    assert cfg["pythonExecutable"] == "python3"  # common.yml is merged in


def test_parser_handles_quotes_and_comments():
    text = 'variables:\n  a: "x # not a comment"\n  b: plain   # comment\n  c: \'\'\nother:\n  d: no\n'
    assert deploy.parse_vars(text) == {"a": "x # not a comment", "b": "plain", "c": ""}


def test_settings_from_variables():
    want, remove = deploy.desired_settings(CFG, "tenant-1")
    assert want["CRIP_TENANT_ID"] == "tenant-1"
    assert want["CRIP_MANAGEMENT_GROUP_ID"] == ""  # a <placeholder> is never pushed to the app
    assert want["CRIP_PLATFORM_ADMIN_GROUP_IDS"] == "g1,g2" and want["CRIP_UI_DEMO_MODE"] == "false"
    assert set(remove) == {"CRIP_SPA_CLIENT_ID", "CRIP_SECRET_OBO_CLIENT_SECRET"}
    kv, _ = deploy.desired_settings({**CFG, "oboClientSecretKeyVaultUri": "https://kv.vault.azure.net/secrets/obo"}, "t")
    assert kv["CRIP_SECRET_OBO_CLIENT_SECRET"] == "@Microsoft.KeyVault(SecretUri=https://kv.vault.azure.net/secrets/obo)"


def test_script_and_bicep_agree_on_settings_and_startup():
    bicep_names = set(re.findall(r"\{ name: '([A-Z0-9_]+)'", BICEP)) - FROM_RESOURCES
    want, remove = deploy.desired_settings(CFG, "t")
    assert bicep_names == set(want) | set(remove)
    startup = re.search(r"var startupCommand = '([^']+)'", BICEP).group(1)
    assert startup == deploy.STARTUP_COMMAND
    assert f"linuxFxVersion: '{deploy.RUNTIME}'" in BICEP


def test_missing_values_are_reported_by_name():
    with pytest.raises(deploy.DeployError, match="apiClientId"):
        deploy.validate({**CFG, "apiClientId": "<app-registration-client-id>"}, ["settings", "code"], "skip")


def test_windows_az_cmd_is_bypassed_or_quoted(tmp_path):
    assert deploy._az_command("/usr/bin/az", ("a|b",)) == ["/usr/bin/az", "a|b"]
    (tmp_path / "wbin").mkdir()
    (tmp_path / "wbin" / "az.cmd").write_text("")
    (tmp_path / "python.exe").write_text("")
    cmd = deploy._az_command(str(tmp_path / "wbin" / "az.cmd"), ("--linux-fx-version", "PYTHON|3.12"))
    assert cmd[1:] == ["-IBm", "azure.cli", "--linux-fx-version", "PYTHON|3.12"]
    line = deploy._az_command(str(tmp_path / "az.cmd"), ("set", "PYTHON|3.12", "a b"))
    assert line.endswith('set "PYTHON|3.12" "a b"')


class FakeAz:
    def __init__(self, current):
        self.current = dict(current)
        self.calls, self.set, self.deleted = [], {}, []

    def __call__(self, *args, capture=True, check=True):
        self.calls.append(args)
        a = list(args)
        if a[:2] == ["account", "show"]:
            return "tenant-1" if "tenantId" in a else ""
        if a[:3] == ["deployment", "group", "show"]:
            return "crip-dev-abc123"
        if a[:2] == ["webapp", "show"]:
            return "crip-dev-abc123.azurewebsites.net"
        if a[:3] == ["webapp", "config", "show"]:
            return json.dumps({"fx": deploy.RUNTIME, "cmd": deploy.STARTUP_COMMAND})
        if a[:4] == ["webapp", "config", "appsettings", "list"]:
            return json.dumps([{"name": k, "value": v} for k, v in self.current.items()])
        if a[:4] == ["webapp", "config", "appsettings", "set"]:
            path = a[a.index("--settings") + 1][1:]
            self.set = {s["name"]: s["value"] for s in json.loads(Path(path).read_text(encoding="utf-8"))}
            return ""
        if a[:4] == ["webapp", "config", "appsettings", "delete"]:
            self.deleted = a[a.index("--setting-names") + 1: a.index("-o")]
            return ""
        raise AssertionError(f"unexpected az call {args}")


def write_vars(tmp_path, **values):
    (tmp_path / "common.yml").write_text("variables:\n  deploymentName: crip-appservice\n", encoding="utf-8")
    body = "\n".join(f'  {k}: "{v}"' for k, v in {**CFG, **values}.items())
    (tmp_path / "dev.yml").write_text(f"variables:\n{body}\n", encoding="utf-8")


def test_settings_step_pushes_only_changes(tmp_path, monkeypatch, capsys):
    want, _ = deploy.desired_settings(CFG, "tenant-1")
    current = {**want, "AZURE_CLIENT_ID": "mi", "CRIP_FOUNDRY_MODEL_DEPLOYMENT": "gpt-4o-mini", "CRIP_SPA_CLIENT_ID": "old"}
    fake = FakeAz(current)
    monkeypatch.setattr(deploy, "az", fake)
    write_vars(tmp_path)
    assert deploy.main(["--env", "dev", "--vars-dir", str(tmp_path), "--steps", "settings", "--no-smoke-test"]) == 0
    assert fake.set == {"CRIP_FOUNDRY_MODEL_DEPLOYMENT": "gpt-4o"}
    assert fake.deleted == ["CRIP_SPA_CLIENT_ID"]
    out = capsys.readouterr().out
    assert "updated CRIP_FOUNDRY_MODEL_DEPLOYMENT" in out and "removed CRIP_SPA_CLIENT_ID" in out
    assert "gpt-4o" not in out and CFG["apiClientId"] not in out  # names only, never values


def test_placeholders_stop_the_run_before_any_azure_call(tmp_path, monkeypatch, capsys):
    fake = FakeAz({})
    monkeypatch.setattr(deploy, "az", fake)
    write_vars(tmp_path, apiClientId="<app-registration-client-id>")
    assert deploy.main(["--env", "dev", "--vars-dir", str(tmp_path)]) == 1
    assert fake.calls == [] and "apiClientId" in capsys.readouterr().out


class _Site(BaseHTTPRequestHandler):
    config = {"spaClientId": CFG["apiClientId"], "demoMode": False}

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            self._send(200, '{"status":"ok"}')
        elif self.path == "/config.js":
            self._send(200, "window.CRIP_CONFIG = " + json.dumps(self.config) + ";\n", "application/javascript")
        else:
            self._send(200, '<div id="root"></div>', "text/html")

    def do_POST(self):  # noqa: N802
        self._send(401, '{"error":{"code":"unauthenticated"}}')

    def log_message(self, *_):
        pass


def test_smoke_test_checks_the_live_config():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        deploy.smoke_test(CFG, url, wait_minutes=1)
        _Site.config = {"spaClientId": "stale", "demoMode": False}
        with pytest.raises(deploy.DeployError):
            deploy.smoke_test(CFG, url, wait_minutes=0)
    finally:
        server.shutdown()
