# CI/CD: Azure DevOps on a self-hosted agent pool

Everything is deployed by [`.azuredevops/azure-pipelines.yml`](../.azuredevops/azure-pipelines.yml),
running on a **self-hosted** agent pool. Configuration comes from **committed variable files**,
not a variable group.

```
.azuredevops/
  azure-pipelines.yml            the pipeline
  vars/common.yml                shared settings (tool names, base images, approvers)
  vars/dev.yml                   dev landing-zone allocations + pool + service connections
  vars/prod.yml                  prod landing-zone allocations + pool + service connections
  templates/agent-prereqs.yml    fails fast if the agent lacks python/node/docker/helm/kubectl
  templates/validate-variables.yml  fails fast, naming each unset "<...>" variable
```

## Stages

| Stage | Runs when | What it does |
|---|---|---|
| Build | every PR and push | pytest (fresh venv), frontend `npm ci` + build, gitleaks secret scan, `helm lint` + "chart refuses to render without allocations" guard |
| Package | not a PR | validates every variable in `vars/<env>.yml`, logs in to ACR, `docker build` + `docker push` backend and frontend tagged with the build id, removes local images, publishes the chart |
| Approve | `deploy=true`; prod only from `main` | `ManualValidation@1` in an **agentless** job, so no self-hosted agent is held while waiting |
| Deploy | after approval | deployment job to environment `crip-<env>`: `helm upgrade --install --atomic --wait`, then a smoke test through the ingress (expects the backend's typed 401 envelope from an unauthenticated `/api/chat`) |

A push to `main` builds and deploys to **dev** (after approval). For **prod**, queue the pipeline
manually on `main` with *Target environment = prod*.

## Why variable files are safe here

The files contain only identifiers: names of service connections, namespaces, client IDs, endpoints,
and Key Vault **secret names**. None of these is a secret. The pipeline itself needs no secrets:

- ACR push and AKS deploy authenticate through **service connections**, referenced by name.
- Application secrets (PostgreSQL DSN, App Insights connection string) stay in Key Vault and are
  mounted into pods by the CSI driver.
- The OBO exchange uses a federated credential, so there is no client secret at all.

gitleaks runs on every build to keep it that way. **Never add a secret to `vars/*.yml`.** If
something secret is ever needed in the pipeline, use a secret variable or a Key Vault-linked variable
group for that single value.

## Python packages from the internal Nexus repository

Every `pip install` the pipeline runs goes to Nexus, never public PyPI:

- the backend test job (including the `hatchling` build backend for the editable install);
- the backend image build (`docker build`), including the Helm registration Job's image.

[`templates/pip-config.yml`](../.azuredevops/templates/pip-config.yml) writes a pip config into the
job's temp directory (deleted after the job). The agent's pip reads it through `PIP_CONFIG_FILE`, and
`docker build` receives it as a **BuildKit secret** (`--secret id=pipconf`), which the Dockerfile
mounts only for the `pip install` step. The Nexus URL and any credentials are therefore never written
into an image layer, the image history, or the repository.

| Setting | Where | Notes |
|---|---|---|
| `pipIndexUrl` | `vars/common.yml` | Nexus PyPI repository URL ending in `/simple`, e.g. `https://nexus.example.com/repository/pypi-group/simple`. Must be HTTPS. |
| `pipCaBundlePath` | `vars/common.yml` (optional) | PEM file **on the agent** trusting the corporate CA that signed Nexus's certificate. Passed to the Docker build as secret `pipca`. TLS verification is never turned off. |
| `nexusUsername`, `nexusPassword` | **Secret** pipeline variables (Pipelines → Edit → Variables → *Keep this value secret*) | Only if Nexus refuses anonymous reads. Never put these in `vars/*.yml`. They are URL-encoded into the index URL and masked in logs. |

Requirements: Docker with BuildKit (the default builder in Docker 23+). Nexus must proxy or host every
package in `backend/pyproject.toml`, including build dependencies (`hatchling`) and the dev
dependencies (`pytest`, `pytest-asyncio`, `respx`).

## One-time setup

1. **Agent pool:** register self-hosted Linux agents into a pool (Project settings → Agent pools).
   The agents need the tools and network access listed below. Put the pool name in
   `vars/<env>.yml` → `agentPoolName`.
2. **Service connections** (Project settings → Service connections):
   - *Docker Registry* → Azure Container Registry, with push rights on the allocated repository path
     (allocation #3). Name → `acrServiceConnection`.
   - *Kubernetes*, scoped to the allocated namespace (allocation #1). Name → `aksServiceConnection`.
3. **Environments** (Pipelines → Environments): create `crip-dev` and `crip-prod`. Optionally add
   approvals/checks there too; they stack with the in-pipeline ManualValidation gate.
4. **Fill the variable files:** replace every `<...>` in `vars/common.yml` (`approvers`, `pipIndexUrl`) and
   `vars/<env>.yml` from the landing-zone allocations
   ([landing-zone-requests.md](landing-zone-requests.md)). The Package stage fails and names each
   variable still unset.
5. **Create the pipeline:** Pipelines → New → Azure Repos Git (or GitHub) → *Existing YAML file* →
   `/.azuredevops/azure-pipelines.yml`. On the first run, authorise the pipeline to use the agent
   pool, both service connections and the environment.

## Self-hosted agent requirements

Installed on the agent (checked by `templates/agent-prereqs.yml` at the start of each job):

| Tool | Used by |
|---|---|
| Python 3.12 (`python3.12`, with `venv`) | backend tests |
| Node.js 22 + npm | frontend build |
| Docker (agent user in the `docker` group) | image build/push; gitleaks fallback |
| Helm 3 | lint, deploy |
| kubectl (+ `kubelogin` if the cluster uses Entra ID auth with local accounts disabled) | HelmDeploy |
| gitleaks (optional; otherwise run via Docker) | secret scan |
| bash, curl | scripts, smoke test |

Outbound network access from the agent:

| Destination | Why |
|---|---|
| Azure DevOps (`dev.azure.com`, `*.visualstudio.com`) | agent ↔ service |
| `<registry>.azurecr.io` (+ `*.data.azurecr.io`) | image push |
| AKS API server (private endpoint if the cluster is private) | helm deploy |
| `login.microsoftonline.com` | service-connection auth, kubelogin |
| Internal **Nexus** (`pipIndexUrl` host, HTTPS) | all Python packages (tests and image build); public PyPI is not used |
| npm (`registry.npmjs.org`) **or** an internal mirror via `.npmrc` on the agent | frontend dependency install |
| Docker Hub, **or** an ACR mirror set in `vars/common.yml` (`baseImagePython`, `baseImageNode`, `baseImageNginx`, `gitleaksImage`) | base images |
| The ingress host (`ingressHost`) | post-deploy smoke test |

## Adding an environment

Copy `vars/dev.yml` to `vars/<name>.yml`, fill it in, add `<name>` to the `targetEnvironment`
parameter's `values` list in `azure-pipelines.yml`, and create environment `crip-<name>`.
