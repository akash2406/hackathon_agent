# CRIP: Cloud Resource Intelligence Platform (hackathon build)

CRIP answers Azure cost questions in plain language ("What's my month-to-date spend by resource
group?"). Every answer is grounded in a **real Azure Cost Management API call made with the signed-in
user's own identity**, and shows its citation inline: the exact query sent, the Azure scope queried,
and the date the billing data is actually current to.

It deploys as a **Flow 2 tenant application** into an existing DaaS multi-tenant AKS landing zone. It
provisions no Azure infrastructure; everything it needs is a landing-zone allocation
([docs/landing-zone-requests.md](docs/landing-zone-requests.md)).

## Scope of this build (deliberately reduced)

| In this build | Not in this build |
|---|---|
| Two agents: **Orchestrator** + **CostPulse** | Inventory, storage, network, compliance agents |
| Read-only questions about cost | Any write/action capability |
| Direct Cost Management calls per question | A caching layer |
| Simple PostgreSQL pool | Production connection-pool tuning |

This scope is a **hackathon time constraint, not a design flaw**. The patterns here (contract,
OBO-authenticated tools, model-driven routing) are the ones further agents follow. See
[docs/architecture.md, "Adding the next agent"](docs/architecture.md#adding-the-next-agent) and
[docs/known-simplifications.md](docs/known-simplifications.md).

## The one distinction to keep sharp: agents vs. the backend service

| Term | What it is | Where it lives |
|---|---|---|
| **Agent** | Orchestrator and CostPulse. They are **registered in Azure AI Foundry Agent Service**, not Python classes and not containers in AKS. | Foundry. Defined in [`foundry/definitions/`](foundry/definitions) and registered by [`foundry/register_agents.py`](foundry/register_agents.py). |
| **Backend service** | The FastAPI app running in the AKS namespace. It invokes the Foundry agents, manages their threads, and **executes the tools they call** (with the user's OBO token). | [`backend/crip_backend/`](backend/crip_backend) |

Nothing in the backend is an agent. The code that talks to Foundry is
[`agent_gateway`](backend/crip_backend/agent_gateway), a *client of* agents.

## How a question flows (short version)

1. The user signs in (MSAL) and the frontend sends the question with a token for the backend API.
2. The backend validates the token and runs the **Orchestrator** agent in Foundry.
3. The Orchestrator's model decides to call its `ask_costpulse` tool, and the backend runs the **CostPulse** agent.
4. CostPulse's model calls `costpulse_query_costs`. The backend exchanges the user's token **on-behalf-of**
   the user for an ARM token and calls Cost Management **as the user**.
5. The tool returns a validated `AgentResponse` contract. It flows back up through both agents, and the
   backend attaches it verbatim to the API response and records it in `agent_invocations`.

Full step-by-step walkthrough: [docs/architecture.md](docs/architecture.md).

## Repository layout

```
backend/        FastAPI backend service (Python 3.12), package crip_backend
  crip_backend/
    contracts.py        AgentResponse / ChatResponse / ErrorEnvelope (grounding enforced by validators)
    auth/               Entra token validation (entra.py) + on-behalf-of exchange (obo.py)
    azure_clients/      Cost Management REST client (token passed in, never owned)
    tools/              Tool handlers Foundry agents call back into (costpulse.py, registry.py)
    agent_gateway/      Foundry client: runs agents, services tool calls, delegation (conversation.py)
    persistence/        asyncpg pool, schema.sql, repository
    api/                /api/chat, /api/tools/{name}, /health
foundry/        Agent definitions (JSON + instruction .md) and register_agents.py
frontend/       React + TypeScript chat UI with MSAL sign-in (Vite; nginx in the container)
infra/helm/     Helm chart: the only deployment artifact (no Terraform anywhere)
docs/           architecture.md, landing-zone-requests.md, known-simplifications.md
tests/          Backend test suite (pytest)
.azuredevops/   CI/CD pipeline (self-hosted pool) + per-environment variable files in vars/
scripts/        Local-development helpers
```

## Local development

### What runs with no Azure access at all

```bash
# Python 3.12. Packages come from the internal Nexus PyPI repository (same as the pipeline):
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip config --site set global.index-url https://<nexus-host>/repository/<pypi-repository>/simple
#   (if Nexus needs auth, use your own credentials in a user-level pip.conf or keyring; never commit them)
pip install -e "backend[dev]"
pytest                                              # 55 tests: contracts, CostPulse tool, OBO cache/guard,
                                                    # orchestrator routing, chat endpoint, Foundry definitions
python foundry/register_agents.py --dry-run         # prints the exact agent payloads Foundry would receive

cd frontend && npm install && npm run build         # typecheck + production build
```

The tests use explicit test doubles (respx for Cost Management HTTP, a scripted Foundry client, an
in-memory repository). There is **no "mock mode" in the application itself**, on purpose: the app
cannot produce an answer without a real Cost Management call.

### What needs live Azure (and which allocations)

Running the full app locally needs: the two Entra app registrations (#6), a Foundry project with the
agents registered (#7), your own `az login` with Cost Management Reader on some subscription, and a
PostgreSQL database (local docker is fine).

```bash
# 1. Local secrets directory (mirrors the Key Vault CSI mount; git-ignored)
python scripts/init_local_secrets.py
#    then paste a DEV-ONLY client secret of the backend API app registration into .secrets/obo-client-secret

# 2. Local PostgreSQL (password comes from .secrets/, the "crip" schema is created for you)
docker compose up -d postgres

# 3. Backend config (non-secret values only)
cp .env.example .env        # fill in tenant id, API client id, Foundry endpoint

# 4. Register the agents in your Foundry project (uses your az login)
az login
python foundry/register_agents.py --endpoint "<foundry project endpoint>" --model "<model deployment>"

# 5. Run the backend (DefaultAzureCredential -> your az login, for Foundry only)
uvicorn --factory crip_backend.main:create_app --reload --port 8000

# 6. Run the frontend: fill in frontend/public/config.js (public IDs, not secrets), then
cd frontend && npm run dev   # http://localhost:5173; add it as a SPA redirect URI on the SPA app registration
```

Why a client secret locally but not in AKS: in-cluster, the backend app registration trusts the pod's
Workload Identity token (federated credential), so no secret exists anywhere. On a laptop there is no
such token. The dev secret is read from a git-ignored file, never from code or an env-var literal.

## Deploying

All deployment goes through `.azuredevops/azure-pipelines.yml` on a **self-hosted** Azure DevOps
agent pool. Per-environment settings live in committed variable files (`.azuredevops/vars/common.yml`,
`dev.yml`, `prod.yml`; non-secret identifiers only). The pipeline:

1. builds, tests and secret-scans;
2. pushes both images to the allocated ACR path;
3. waits for **manual approval**;
4. runs `helm upgrade --install` into the allocated namespace;
5. smoke-tests through the ingress.

The chart refuses to render if any landing-zone allocation is missing. The Helm post-install hook
registers or updates the Foundry agents using the release's Workload Identity. Setup steps and agent
requirements: [docs/pipeline.md](docs/pipeline.md).

## Proving grounding to a judge

- In the UI, every answer shows the exact query, scope, Azure `x-ms-request-id`, and data timestamp.
- In the database:
  ```sql
  SELECT created_at, agent_name, tool_name, status, data_timestamp, sources->0->>'auth' AS auth,
         sources->0->>'request_id' AS azure_request_id, query_used
  FROM <schema>.agent_invocations ORDER BY created_at DESC LIMIT 20;
  ```
  `auth` is always `user_obo`. The `request_id` can be looked up with Azure support.
- A row claiming `ok`/`partial` without a query, a source and a data timestamp is impossible: it is
  rejected by the Pydantic contract *and* by a CHECK constraint in the table.
