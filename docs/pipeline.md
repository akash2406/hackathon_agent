# CI/CD: Azure DevOps -> Azure App Service

[`.azuredevops/azure-pipelines.yml`](../.azuredevops/azure-pipelines.yml) builds **one image** (UI +
API), pushes it to ACR and points the App Service web app at it. Packages come from the public npm
and PyPI registries. No internal artifact feed is needed.

```
.azuredevops/
  azure-pipelines.yml             the pipeline
  vars/common.yml                 shared settings (agent pool, image name, approvers)
  vars/dev.yml, vars/prod.yml     per-environment: service connection, resource group, Entra/Foundry IDs
  templates/agent-prereqs.yml     fails fast if a self-hosted agent lacks python/node/docker/az
  templates/validate-variables.yml  fails fast, naming each unset "<...>" variable
  templates/resolve-names.yml     finds ACR + web app names (variable file or Bicep outputs)
```

## Stages

| Stage | When | What |
|---|---|---|
| Build | every PR and push | pytest, UI build (typecheck), gitleaks secret scan |
| Provision | run parameter `provisionInfra=true` | `az deployment group create` with `infra/appservice/main.bicep` |
| Package | not a PR | `docker build` the root `Dockerfile`, push `:<build id>` and `:latest` to ACR |
| Approve | prod only, main branch only | `ManualValidation` (agentless, holds no agent) |
| Deploy | after Package (dev) / Approve (prod) | `AzureWebAppContainer@1` sets the image; smoke test waits for `/health`, checks the UI and the `/api/chat` 401 envelope |

A push to `main` deploys **dev**. For **prod**, queue the pipeline on `main` with *Target environment
= prod*. First run for a new environment: tick *provisionInfra*.

## One-time setup

1. **Service connection:** Project settings → Service connections → *Azure Resource Manager*
   (workload identity federation recommended), scoped to the resource group. It needs **Contributor**;
   for the Provision stage also the right to create role assignments (Owner, or *Role Based Access
   Control Administrator*). Put its name in `vars/<env>.yml` → `azureServiceConnection`.
2. **Variable files:** fill the `<...>` values in `vars/common.yml` and `vars/<env>.yml`.
3. **Environments:** Pipelines → Environments → create `crip-dev` and `crip-prod` (add approvals there
   too if you like).
4. **Pipeline:** Pipelines → New → *Existing YAML file* → `/.azuredevops/azure-pipelines.yml`; authorise
   the service connection and environments on the first run.
5. The Entra app registration and Foundry role are one-time manual steps:
   [azure-setup.md](azure-setup.md) steps 2-3.

## Agents: Microsoft-hosted or self-hosted

`agentPoolName: ""` in `vars/common.yml` uses Microsoft-hosted `ubuntu-latest` (all tools present).
Set it to your self-hosted pool name to use your own agents. They need **Python 3.12, Node 22,
Docker (BuildKit) and the Azure CLI**, plus outbound access to Azure DevOps, `pypi.org` /
`files.pythonhosted.org`, `registry.npmjs.org`, Docker Hub, your ACR, `management.azure.com`,
`login.microsoftonline.com` and the web app URL (smoke test).

## Why the variable files are safe to commit

They hold identifiers only: names, IDs, endpoints, and (optionally) a Key Vault secret **URI**. The
pipeline needs no secrets: Azure access is the service connection, and the app's secrets (if any)
are Key Vault references resolved by App Service. gitleaks runs on every build to keep it that way.
**Never put a secret value in `vars/*.yml`.**
