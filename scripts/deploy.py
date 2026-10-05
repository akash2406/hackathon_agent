"""Deploy CRIP end to end: App Service settings, Foundry agents, and the code (UI + API).

    az login
    python scripts/deploy.py --env dev                 # settings -> agents -> code -> smoke test
    python scripts/deploy.py --env dev --provision     # also create/update the Azure resources (Bicep) first

Configuration comes from the same committed files the pipeline uses:
.azuredevops/vars/common.yml + .azuredevops/vars/<env>.yml. Edit a value there and
re-run: the script pushes it to the web app's settings. There is nothing to set
separately for the frontend: the UI reads its settings at runtime from
/config.js, which the backend builds from these same app settings.

Steps (``--steps`` picks a subset; default settings,agents,code):
  provision  az deployment group create with infra/appservice/main.bicep (only with --provision)
  settings   App Service runtime, startup command and app settings, changed ones only
  agents     create/update the 7 agents in Azure AI Foundry
               --agents cli  (default) now, as YOU (az login); needs "Azure AI User" on the project
               --agents app  by the web app at startup, with its managed identity
               --agents skip (also the default in demo mode, which needs no agents)
  code       build the zip (scripts/build_package.py), az webapp deploy, smoke test

Other options: --package <zip>, --skip-ui-build, --no-smoke-test,
--set name=value (override a variable for this run), --vars-dir <dir>.

Runs on Windows (PowerShell or Git Bash), Linux and macOS with Python 3.11+ and
the Azure CLI. Prints setting NAMES only, never values. No secrets are read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_package  # noqa: E402

STEPS = ("provision", "settings", "agents", "code")
# Must match infra/appservice/main.bicep (tests/test_deploy_script.py checks both).
STARTUP_COMMAND = "python -m uvicorn --app-dir backend --factory crip_backend.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers"
RUNTIME = "PYTHON|3.12"
REQUIRED = ("resourceGroup", "apiClientId", "foundryProjectEndpoint", "foundryModelDeployment")


class DeployError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    log(f"\n== {title}")


# --------------------------------------------------------------------------- configuration

_LINE = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")


def parse_vars(text: str) -> dict[str, str]:
    """The 'variables:' mapping of an Azure DevOps variable file (flat key: value pairs)."""
    out: dict[str, str] = {}
    in_vars = False
    for raw in text.splitlines():
        if raw.strip().startswith("#") or not raw.strip():
            continue
        if not raw.startswith((" ", "\t")):
            in_vars = raw.strip() == "variables:"
            continue
        m = _LINE.match(raw)
        if not (in_vars and m):
            continue
        key, value = m.groups()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def load_config(env: str, vars_dir: Path, overrides: list[str]) -> dict[str, str]:
    files = [vars_dir / "common.yml", vars_dir / f"{env}.yml"]
    for f in files:
        if not f.is_file():
            raise DeployError(f"Variable file not found: {f}")
    cfg: dict[str, str] = {}
    for f in files:
        cfg.update(parse_vars(f.read_text(encoding="utf-8")))
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep:
            raise DeployError(f"--set expects name=value, got '{item}'")
        cfg[key.strip()] = value
    return cfg


def is_placeholder(value: str | None) -> bool:
    return not value or bool(re.search(r"<[^>]+>", value))


def truthy(value: str | None) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def desired_settings(cfg: dict[str, str], tenant_id: str) -> tuple[dict[str, str], list[str]]:
    """App settings derived from the variable files, and optional settings to remove when empty."""
    opt = lambda k: "" if is_placeholder(cfg.get(k)) else cfg.get(k, "")  # noqa: E731
    settings = {
        # runtime (same as the Bicep template)
        "WEBSITES_PORT": "8000",
        "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
        "FORWARDED_ALLOW_IPS": "*",
        "CRIP_SQLITE_PATH": "/home/data/crip.db",
        # CRIP configuration (also what the UI reads through /config.js)
        "CRIP_ENVIRONMENT": cfg.get("namePrefix", ""),
        "CRIP_TENANT_ID": opt("tenantId") or tenant_id,
        "CRIP_API_CLIENT_ID": cfg["apiClientId"],
        "CRIP_AZURE_ACCESS_MODE": cfg.get("azureAccessMode") or "app_identity",
        "CRIP_MANAGEMENT_GROUP_ID": opt("managementGroupId"),
        "CRIP_PLATFORM_ADMIN_GROUP_IDS": opt("platformAdminGroupIds"),
        "CRIP_COST_READER_GROUP_IDS": opt("costReaderGroupIds"),
        "CRIP_READER_GROUP_IDS": opt("readerGroupIds"),
        "CRIP_OBO_CREDENTIAL_MODE": cfg.get("oboCredentialMode") or "managed_identity",
        "CRIP_FOUNDRY_PROJECT_ENDPOINT": cfg["foundryProjectEndpoint"],
        "CRIP_FOUNDRY_MODEL_DEPLOYMENT": cfg["foundryModelDeployment"],
        "CRIP_REGISTER_AGENTS_ON_STARTUP": "true",
        "CRIP_UI_DEMO_MODE": "true" if truthy(cfg.get("uiDemoMode")) else "false",
    }
    remove = []
    if opt("spaClientId"):
        settings["CRIP_SPA_CLIENT_ID"] = opt("spaClientId")
    else:
        remove.append("CRIP_SPA_CLIENT_ID")
    if opt("oboClientSecretKeyVaultUri"):  # a Key Vault reference: the secret itself never leaves Key Vault
        settings["CRIP_SECRET_OBO_CLIENT_SECRET"] = f"@Microsoft.KeyVault(SecretUri={opt('oboClientSecretKeyVaultUri')})"
    else:
        remove.append("CRIP_SECRET_OBO_CLIENT_SECRET")
    return settings, remove


def validate(cfg: dict[str, str], steps: list[str], agents: str) -> None:
    need = list(REQUIRED) + (["namePrefix", "location"] if "provision" in steps else [])
    missing = [k for k in need if is_placeholder(cfg.get(k))]
    if missing:
        raise DeployError("Fill these in the variable files (or pass --set name=value): " + ", ".join(missing)
                          + ".\nFor a demo without an app registration: apiClientId=00000000-0000-0000-0000-000000000000, "
                          "uiDemoMode=\"true\", and any https URL for foundryProjectEndpoint.")
    if agents == "cli" and "placeholder" in cfg["foundryProjectEndpoint"]:
        raise DeployError("foundryProjectEndpoint is a placeholder; cannot register agents. Use --agents skip.")


# --------------------------------------------------------------------------- Azure CLI


_CMD_UNSAFE = re.compile(r'[\s"&|<>^()%!]')


def _az_command(exe: str, args: tuple[str, ...]) -> list[str] | str:
    """How to run az with these arguments, verbatim.

    On Windows az is a batch file (az.cmd), and cmd.exe would treat characters
    like the '|' in PYTHON|3.12 as operators. The standard installer puts the
    CLI's own python.exe next to it, so call that directly (as az.cmd does);
    otherwise quote every argument that cmd.exe could misread.
    """
    if not exe.lower().endswith((".cmd", ".bat")):
        return [exe, *args]
    bundled = Path(exe).resolve().parent.parent / "python.exe"
    if bundled.is_file():
        return [str(bundled), "-IBm", "azure.cli", *args]
    quote = lambda a: a if a and not _CMD_UNSAFE.search(a) else '"' + a.replace('"', '\\"') + '"'  # noqa: E731
    return " ".join([quote(exe), *(quote(a) for a in args)])


def az(*args: str, capture: bool = True, check: bool = True) -> str:
    exe = shutil.which("az")  # az.cmd on Windows
    if exe is None:
        raise DeployError("Azure CLI (az) not found on PATH")
    proc = subprocess.run(_az_command(exe, args), capture_output=capture, text=True, encoding="utf-8",
                          env={**os.environ, "AZ_INSTALLER": os.environ.get("AZ_INSTALLER", "MSI")} if os.name == "nt" else None)
    if check and proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise DeployError(f"az {' '.join(args[:3])} ... failed: " + (err[-1] if err else f"exit {proc.returncode}"))
    return (proc.stdout or "").strip() if capture else ""


def az_json(*args: str) -> Any:
    out = az(*args, "-o", "json")
    return json.loads(out) if out else None


# --------------------------------------------------------------------------- steps


def provision(cfg: dict[str, str]) -> None:
    step("Provision Azure resources (Bicep)")
    rg, location = cfg["resourceGroup"], cfg["location"]
    az("group", "create", "-n", rg, "-l", location, "-o", "none")
    params = {
        "namePrefix": cfg["namePrefix"], "apiClientId": cfg["apiClientId"], "spaClientId": cfg.get("spaClientId", ""),
        "foundryProjectEndpoint": cfg["foundryProjectEndpoint"], "foundryModelDeployment": cfg["foundryModelDeployment"],
        "azureAccessMode": cfg.get("azureAccessMode") or "app_identity", "managementGroupId": "" if is_placeholder(cfg.get("managementGroupId")) else cfg.get("managementGroupId", ""),
        "platformAdminGroupIds": cfg.get("platformAdminGroupIds", ""), "costReaderGroupIds": cfg.get("costReaderGroupIds", ""),
        "readerGroupIds": cfg.get("readerGroupIds", ""), "oboCredentialMode": cfg.get("oboCredentialMode") or "managed_identity",
        "oboClientSecretKeyVaultUri": cfg.get("oboClientSecretKeyVaultUri", ""), "appServiceSku": cfg.get("appServiceSku") or "B1",
        "uiDemoMode": "true" if truthy(cfg.get("uiDemoMode")) else "false",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                   "contentVersion": "1.0.0.0", "parameters": {k: {"value": (v == "true") if k == "uiDemoMode" else v} for k, v in params.items()}}, f)
        param_file = f.name
    try:
        outputs = az_json("deployment", "group", "create", "-g", rg, "-n", cfg.get("deploymentName") or "crip-appservice",
                          "-f", str(ROOT / "infra" / "appservice" / "main.bicep"), "-p", f"@{param_file}", "--query", "properties.outputs")
    finally:
        os.unlink(param_file)
    log(f"  web app {outputs['webAppName']['value']} at {outputs['webAppUrl']['value']}")
    log(f"  managed identity principal id: {outputs['managedIdentityPrincipalId']['value']} (for scripts/grant-azure-access.sh)")


def resolve_app(cfg: dict[str, str]) -> tuple[str, str]:
    rg = cfg["resourceGroup"]
    app = "" if is_placeholder(cfg.get("webAppName")) else cfg.get("webAppName", "")
    if not app:
        app = az("deployment", "group", "show", "-g", rg, "-n", cfg.get("deploymentName") or "crip-appservice",
                 "--query", "properties.outputs.webAppName.value", "-o", "tsv", check=False)
        if not app:
            raise DeployError(f"No web app found: set webAppName in the variable file, or run with --provision to create it in {rg}.")
    host = az("webapp", "show", "-g", rg, "-n", app, "--query", "defaultHostName", "-o", "tsv")
    return app, f"https://{host}"


def apply_settings(cfg: dict[str, str], app: str) -> bool:
    """Bring runtime, startup command and app settings in line with the variable files. Returns True if anything changed."""
    step(f"App Service settings ({app})")
    rg = cfg["resourceGroup"]
    changed = False
    site = az_json("webapp", "config", "show", "-g", rg, "-n", app, "--query", "{fx: linuxFxVersion, cmd: appCommandLine}")
    if site["fx"] != RUNTIME or (site["cmd"] or "") != STARTUP_COMMAND:
        az("webapp", "config", "set", "-g", rg, "-n", app, "--linux-fx-version", RUNTIME, "--startup-file", STARTUP_COMMAND, "-o", "none")
        log(f"  set runtime {RUNTIME} and the startup command")
        changed = True

    tenant = az("account", "show", "--query", "tenantId", "-o", "tsv")
    want, remove = desired_settings(cfg, tenant)
    current = {s["name"]: s.get("value") for s in az_json("webapp", "config", "appsettings", "list", "-g", rg, "-n", app) or []}
    if not current.get("AZURE_CLIENT_ID"):
        identities = az_json("webapp", "identity", "show", "-g", rg, "-n", app, "--query", "userAssignedIdentities") or {}
        if len(identities) == 1:
            want["AZURE_CLIENT_ID"] = next(iter(identities.values()))["clientId"]
        else:
            log("  WARNING: AZURE_CLIENT_ID is not set and the web app has no single user-assigned identity; run with --provision.")
    diff = {k: v for k, v in want.items() if current.get(k) != v}
    stale = [k for k in remove if k in current]
    if diff:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump([{"name": k, "value": v, "slotSetting": False} for k, v in diff.items()], f)
            settings_file = f.name
        try:
            az("webapp", "config", "appsettings", "set", "-g", rg, "-n", app, "--settings", f"@{settings_file}", "-o", "none")
        finally:
            os.unlink(settings_file)
        for k in sorted(diff):
            log(f"  {'updated' if k in current else 'added'} {k}")
        changed = True
    if stale:
        az("webapp", "config", "appsettings", "delete", "-g", rg, "-n", app, "--setting-names", *stale, "-o", "none")
        for k in stale:
            log(f"  removed {k}")
        changed = True
    if not changed:
        log("  already up to date")
    return changed


def tools_python() -> str:
    """A Python that can import the backend (for agent registration): the current one, else a venv in .local."""
    probe = subprocess.run([sys.executable, "-c", "import azure.ai.agents, azure.identity, pydantic_settings"], capture_output=True)
    if probe.returncode == 0:
        return sys.executable
    venv = ROOT / ".local" / "deploy-venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        log(f"  creating {venv.relative_to(ROOT)} for the Foundry SDK (one-time)")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    reqs = build_package.requirements()
    req_file = venv / "requirements.txt"
    if not req_file.exists() or req_file.read_text(encoding="utf-8") != reqs:
        req_file.write_text(reqs, encoding="utf-8")
        subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req_file)], check=True)
    return str(py)


def register_agents(cfg: dict[str, str]) -> None:
    step("Foundry agents (as your Azure CLI identity)")
    py = tools_python()
    proc = subprocess.run([py, str(ROOT / "foundry" / "register_agents.py"),
                           "--endpoint", cfg["foundryProjectEndpoint"], "--model", cfg["foundryModelDeployment"]],
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-6:])
        raise DeployError("Agent registration failed:\n" + tail + "\nYour account needs the 'Azure AI User' role on the Foundry project "
                          "(or use --agents app to let the web app register them with its managed identity).")
    ids = json.loads(proc.stdout[proc.stdout.index("{"):])
    for name, agent_id in ids.items():
        log(f"  {name}: {agent_id}")


def deploy_code(cfg: dict[str, str], app: str, package: Path | None, skip_ui_build: bool) -> None:
    if package is None:
        step("Build package")
        package = ROOT / ".local" / "crip-app.zip"
        build_package.main(["--out", str(package)] + (["--skip-ui-build"] if skip_ui_build else []))
    if not package.is_file():
        raise DeployError(f"Package not found: {package}")
    step(f"Deploy code to {app} (App Service installs the Python packages; a few minutes)")
    az("webapp", "deploy", "-g", cfg["resourceGroup"], "-n", app, "--src-path", str(package), "--type", "zip",
       "--restart", "true", "--timeout", "1500000", "-o", "none")
    log("  deployed")


def http(url: str, *, method: str = "GET", body: bytes | None = None, timeout: float = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return 0, str(exc)


def smoke_test(cfg: dict[str, str], url: str, wait_minutes: int = 10) -> None:
    step(f"Smoke test {url}")
    deadline = time.time() + wait_minutes * 60
    while http(f"{url}/health/live")[0] != 200:
        if time.time() > deadline:
            raise DeployError(f"{url}/health/live did not answer within {wait_minutes} minutes. Check: az webapp log tail -g {cfg['resourceGroup']} -n <app>")
        time.sleep(15)
    expected = {"spaClientId": cfg["apiClientId"] if is_placeholder(cfg.get("spaClientId")) else cfg["spaClientId"],
                "demoMode": truthy(cfg.get("uiDemoMode"))}

    def live_config() -> dict[str, Any]:
        m = re.search(r"window\.CRIP_CONFIG = (\{.*\});", http(f"{url}/config.js")[1])
        return json.loads(m.group(1)) if m else {}

    # After a settings change App Service restarts the app; wait until the new instance answers.
    while any(live_config().get(k) != v for k, v in expected.items()) and time.time() < deadline:
        time.sleep(10)
    checks = [("UI config uses the new settings (/config.js)", all(live_config().get(k) == v for k, v in expected.items()))]
    status, body = http(f"{url}/health")
    checks.append(("store reachable (/health)", status == 200))
    status, body = http(f"{url}/")
    checks.append(("UI served", status == 200 and 'id="root"' in body))
    status, body = http(f"{url}/api/chat", method="POST", body=b'{"message":"ping"}')
    checks.append(("API requires sign-in (401)", status == 401 and '"unauthenticated"' in body))
    for name, ok in checks:
        log(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not all(ok for _, ok in checks):
        raise DeployError("Smoke test failed (see above).")


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, help="selects .azuredevops/vars/<env>.yml (e.g. dev, prod)")
    parser.add_argument("--provision", action="store_true", help="run the Bicep template first")
    parser.add_argument("--steps", default=None, help=f"comma-separated subset of {','.join(STEPS)}")
    parser.add_argument("--agents", choices=("cli", "app", "skip"), default=None, help="how to register the Foundry agents")
    parser.add_argument("--package", type=Path, default=None, help="deploy this zip instead of building one")
    parser.add_argument("--skip-ui-build", action="store_true", help="reuse frontend/dist when building")
    parser.add_argument("--no-smoke-test", action="store_true")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="override a variable for this run")
    parser.add_argument("--vars-dir", type=Path, default=ROOT / ".azuredevops" / "vars")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.env, args.vars_dir, args.set)
        steps = [s.strip() for s in args.steps.split(",")] if args.steps else (["provision"] if args.provision else []) + ["settings", "agents", "code"]
        unknown = set(steps) - set(STEPS)
        if unknown:
            raise DeployError(f"Unknown step(s): {', '.join(sorted(unknown))}")
        agents = args.agents or ("skip" if truthy(cfg.get("uiDemoMode")) else "cli")
        validate(cfg, steps, agents if "agents" in steps else "skip")
        az("account", "show", "-o", "none")  # fails early with a clear message if not logged in
        log(f"Environment {args.env}: resource group {cfg['resourceGroup']}, steps {', '.join(steps)}, agents: {agents}"
            + (" (demo mode)" if truthy(cfg.get("uiDemoMode")) else ""))

        if "provision" in steps:
            provision(cfg)
        app, url = resolve_app(cfg)
        log(f"Web app: {app} ({url})")
        if "settings" in steps:
            apply_settings(cfg, app)
        if "agents" in steps:
            if agents == "cli":
                register_agents(cfg)
            elif agents == "app":
                log("\n== Foundry agents: the web app registers them at startup with its managed identity (check Log stream)")
            else:
                log("\n== Foundry agents: skipped" + (" (demo mode does not use them)" if truthy(cfg.get("uiDemoMode")) else ""))
        if "code" in steps:
            deploy_code(cfg, app, args.package, args.skip_ui_build)
        if not args.no_smoke_test and ("code" in steps or "settings" in steps):
            smoke_test(cfg, url)
        log(f"\nDone. CRIP: {url}")
        return 0
    except DeployError as exc:
        log(f"\nERROR: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        log(f"\nERROR: command failed ({exc.returncode}): {' '.join(map(str, exc.cmd[:3]))} ...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
