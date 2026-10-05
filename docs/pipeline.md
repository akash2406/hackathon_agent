# CI/CD: Azure DevOps -> Azure App Service

[`.azuredevops/azure-pipelines.yml`](../.azuredevops/azure-pipelines.yml) builds **one code package**
(the API, the agent definitions and the built UI in one zip) and deploys it to the App Service web
app. No container registry or Docker is needed. Packages come from the public npm and PyPI
registries: the UI is built on the agent, and App Service installs the Python packages during
deployment. No internal artifact feed is needed.

```
.azuredevops/
  azure-pipelines.yml             the pipeline
  vars/common.yml                 shared settings (agent pool, approvers)
  vars/dev.yml, vars/prod.yml     per-environment: service connection, resource group, Entra/Foundry IDs
  templates/agent-prereqs.yml     fails fast if a self-hosted agent lacks python/node/az
  templates/validate-variables.yml  fails fast, naming each unset "<...>" variable
  templates/resolve-names.yml     finds the web app name (variable file or Bicep outputs)
scripts/
  build_package.py                builds the zip (also usable on your machine)
  deploy-appservice.sh            zip deploy + smoke test (the pipeline runs this same script)
```

## Stages

| Stage | When | What |
|---|---|---|
| Build | every PR and push | pytest, UI build (typecheck), gitleaks secret scan |
| Provision | run parameter `provisionInfra=true` | `az deployment group create` with `infra/appservice/main.bicep` |
| Package | not a PR | `scripts/build_package.py`: `npm ci` + build, zip API + definitions + UI, publish artifact `app` |
| Approve | prod only, main branch only | `ManualValidation` (agentless, holds no agent) |
| Deploy | after Package (dev) / Approve (prod) | `scripts/deploy-appservice.sh`: `az webapp deploy` (zip), App Service installs `requirements.txt`; smoke test waits for `/health`, checks the UI and the `/api/chat` 401 envelope |

A push to `main` deploys **dev**. For **prod**, queue the pipeline on `main` with *Target environment
= prod*. First run for a new environment: tick *provisionInfra*.

## Deploying without the pipeline

```bash
az login
bash scripts/deploy-appservice.sh --resource-group <rg>      # builds the zip, deploys, smoke-tests
```

The web app name is read from the Bicep deployment outputs (or pass `--name`). Use
`--package <zip>` to deploy an existing package, `--skip-ui-build` to reuse `frontend/dist`. Runs in
Git Bash on Windows, WSL, Linux and macOS.

## One-time setup

1. **Service connection:** Project settings → Service connections → *Azure Resource Manager*
   (workload identity federation recommended), scoped to the resource group. **Contributor** is
   enough for deployments and for the Provision stage (the template creates no role assignments).
   Put its name in `vars/<env>.yml` → `azureServiceConnection`.
2. **Variable files:** fill the `<...>` values in `vars/common.yml` and `vars/<env>.yml`.
3. **Environments:** Pipelines → Environments → create `crip-dev` and `crip-prod` (add approvals there
   too if you like).
4. **Pipeline:** Pipelines → New → *Existing YAML file* → `/.azuredevops/azure-pipelines.yml`; authorise
   the service connection and environments on the first run.
5. The Entra app registration, the access grants and the Foundry role are one-time steps:
   [azure-setup.md](azure-setup.md) steps 2-3.

### First deployment before the app registration exists

Set `uiDemoMode: "true"` in `vars/<env>.yml`, `apiClientId` to any GUID (e.g.
`00000000-0000-0000-0000-000000000000`), `managementGroupId: ""`, and a real or placeholder
`foundryProjectEndpoint`. The site then shows the UI with clearly labelled **sample data** and no
sign-in; the API still requires sign-in and returns no Azure data. Switch `uiDemoMode` back to
`"false"` (and fill the real values) once the steps in [azure-setup.md](azure-setup.md) are done.

## Agents: Microsoft-hosted or self-hosted

`agentPoolName: ""` in `vars/common.yml` uses Microsoft-hosted `ubuntu-latest` (all tools present).
Set it to your self-hosted pool name to use your own agents. They need **Python 3.12, Node 22 and the
Azure CLI** (Docker only if gitleaks is not installed), plus outbound access to Azure DevOps,
`pypi.org` / `files.pythonhosted.org`, `registry.npmjs.org`, `management.azure.com`,
`login.microsoftonline.com` and the web app URL (`*.azurewebsites.net` and `*.scm.azurewebsites.net`
for the zip upload and the smoke test). The web app itself downloads Python packages from PyPI
during deployment.

## Why the variable files are safe to commit

They hold identifiers only: names, IDs, endpoints, and (optionally) a Key Vault secret **URI**. The
pipeline needs no secrets: Azure access is the service connection, and the app's secrets (if any)
are Key Vault references resolved by App Service. gitleaks runs on every build to keep it that way.
**Never put a secret value in `vars/*.yml`.**
