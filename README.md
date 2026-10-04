# CRIP: a FinOps copilot for Azure, grounded in live Azure data

Ask in plain language: *"Why did my Azure bill go up, what will it be at month end, and where can I
save?"* CRIP's AI agents answer from **live Azure data queried with the signed-in user's own
permissions**, and every answer shows its proof inline: the exact Azure query, the scope, Azure's
request id, and how fresh the data is. Read-only: CRIP never changes anything.

It ships as **one container** (React UI + FastAPI API) that runs on **Azure App Service**.

## What it can answer

| Agent (in Azure AI Foundry) | Answers | Azure data source |
|---|---|---|
| **CostPulse** · spend | Spend by resource group / type / service / tag; **daily trend with spike (anomaly) detection and the services that caused each spike**; **month-end forecast** | Cost Management Query + Forecast APIs |
| **Optimizer** · savings | **Azure Advisor cost recommendations with savings estimates**; **idle/orphaned resources** (unattached disks, unused public IPs, orphaned NICs, empty App Service plans, stopped-not-deallocated VMs) **priced with their actual month-to-date cost** | Advisor, Resource Graph, Cost Management |
| **Inventory** · resources | What exists by type / region; **tag coverage** (e.g. % of resources with an `owner` tag, worst resource groups) | Resource Graph |
| **Orchestrator** | Routes each question to one or more specialists *by its own tool-calling decision* and composes one answer | (the specialists' results) |

Multi-agent questions are the showcase. "Give me a cost overview" calls CostPulse (spend + forecast)
and Optimizer (savings) together. The UI renders a chart per result: daily columns with spikes
flagged, actual vs Azure forecast, ranked bars, savings and coverage tiles.

## Why it's trustworthy (the part judges ask about)

- **Grounding is enforced by types, not convention.** Every tool returns an `AgentResponse`. An `ok`
  answer *cannot be constructed* without an Azure source, the query used and a data timestamp; an
  `error` cannot carry numbers. The database enforces the same rule with a CHECK constraint.
- **The user's own identity, not a service account.** Every Azure data call uses the user's token,
  exchanged on-behalf-of (OBO). Answers are naturally limited to what *that user* may see. The app's
  own managed identity can't read customer data.
- **Provenance bypasses the model.** Citations shown in the UI are the tools' own results, attached by
  the backend, never re-typed by the LLM.
- **Honest failures.** A 403 says "you need Cost Management Reader on X". No data says no data.
  Forecasts are Azure's own forecast, labelled as such.

## The distinction to keep sharp: agents vs. the app

| Term | What it is | Where |
|---|---|---|
| **Agent** | Orchestrator, CostPulse, Optimizer, Inventory. Registered in **Azure AI Foundry Agent Service**, not Python classes. | Defined in [`foundry/definitions/`](foundry/definitions); registered automatically at app start (or with [`foundry/register_agents.py`](foundry/register_agents.py)) |
| **App** | One container: FastAPI serves the React UI and the API, runs the agents in Foundry, and **executes the tools they call** with the user's OBO token. | [`backend/crip_backend/`](backend/crip_backend), [`frontend/`](frontend), [`Dockerfile`](Dockerfile) |

## Repository layout

```
Dockerfile          the one image: builds the UI, serves UI + API (App Service ready)
backend/            FastAPI app (Python 3.12), package crip_backend
  crip_backend/
    contracts.py        AgentResponse / ChatResponse / ErrorEnvelope (grounding enforced by validators)
    auth/               Entra token validation + on-behalf-of exchange
    azure_clients/      ARM client (retries, paging) + Cost Management, Advisor, Resource Graph
    tools/              costpulse.py, optimizer.py, inventory.py, registry.py
    agent_gateway/      Foundry client: runs agents, services tool calls, registration
    persistence/        SQLite (default) or PostgreSQL: sessions, messages, agent_invocations
    api/ main.py        /api/chat, /api/capabilities, /api/tools/{name}, /health, UI + /config.js
foundry/            agent definitions (JSON + instructions) and the registration CLI
frontend/           React + TypeScript chat UI with MSAL sign-in and charts
infra/appservice/   Bicep: managed identity, ACR, App Insights, App Service plan + web app
.azuredevops/       pipeline: test -> (provision) -> build+push image -> deploy -> smoke test
scripts/            setup-entra-app.sh, local-e2e.sh, init_local_secrets.py
docs/               architecture, azure-setup, pipeline, demo-script, known-simplifications
tests/              pytest suite
```

## The app

A sidebar app with six pages, with the logo on every page (see [docs/branding.md](docs/branding.md)):

| Page | What it shows |
|---|---|
| **Overview** | Month-to-date spend, Azure's month-end forecast, Advisor savings and idle cost; daily trend with spikes; top resource groups; top savings |
| **Spend** | Actual cost by resource group / service / resource type / tag, for any of four time windows |
| **Trends & forecast** | Daily cost (14-90 days), spike cards with the services that drove them, month-end forecast, top services |
| **Savings** | Advisor recommendations ranked by savings, idle resources priced with real cost |
| **Inventory & tags** | Resources by type and region, tag coverage for owner / costCenter / environment / project |
| **Ask CRIP** | The multi-agent chat: one question, several specialists, one grounded answer |

Dashboard pages call the grounded tools directly (`/api/tools/{name}`): fast, deterministic, no LLM,
and every card shows its proof. *Ask CRIP* goes through the Foundry agents.

**Demo mode** (`CRIP_UI_DEMO_MODE=true`, the default for `docker compose` without a `.env`) shows every
screen with clearly labelled **sample data** and no sign-in, for previews. A banner and a
"Sample data · not from Azure" chip on every card make it impossible to mistake for real data. It is
always off in Azure (Bicep sets it to `false`).

## Run it

### 1. No Azure needed

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e "backend[dev]"
pytest                                                 # contracts, tools, OBO, routing, API, UI hosting, SQLite
python foundry/register_agents.py --dry-run            # exact agent payloads for Foundry
cd frontend && npm install && npm run build            # typecheck + build

bash scripts/local-e2e.sh                              # build the container, run it, 18 automated checks
```

`local-e2e.sh` runs the real App Service image. Without a `.env` it starts in *smoke* mode
(placeholder IDs): UI, config, health, auth errors and storage all work, but answering needs Azure.
There is **no mock mode** in the app, on purpose.

### 2. Against your Azure tenant (laptop)

```bash
az login
bash scripts/setup-entra-app.sh --name crip-$USER --local-secret   # app registration + .secrets/obo-client-secret
cp .env.example .env                                                 # fill tenant id, client id, Foundry endpoint/model
python foundry/register_agents.py                                    # creates the 4 agents in your Foundry project
uvicorn --factory crip_backend.main:create_app --port 8000           # UI + API on http://localhost:8000
```

Open http://localhost:8000, sign in, ask. (`cd frontend && npm run dev` gives hot reload on :5173.)

### 3. Deploy to Azure App Service

Follow **[docs/azure-setup.md](docs/azure-setup.md)** (about 15 minutes). In short:

1. `az deployment group create` with [`infra/appservice/main.bicep`](infra/appservice/main.bicep): creates the managed identity, ACR, App Insights and web app;
2. `scripts/setup-entra-app.sh --url <web app url> --mi-principal-id <from Bicep outputs>`: sign-in plus OBO with no secret;
3. give the managed identity **Azure AI User** on your Foundry project;
4. build and push the image (`az acr build` or the pipeline): the app registers its agents on startup.

CI/CD: [docs/pipeline.md](docs/pipeline.md). For the demo: [docs/demo-script.md](docs/demo-script.md).

## Adding another agent

Copy the pattern: a tools module, entries in `tools/registry.py`, a `foundry/definitions/<agent>.json`
+ `.md`. The Orchestrator picks it up automatically, because its tools are generated from the
definitions and the model routes. Walkthrough:
[docs/architecture.md#adding-the-next-agent](docs/architecture.md#adding-the-next-agent).

## Proving grounding

Every answer's sources are stored in `agent_invocations`:

```bash
# SQLite (default; in App Service: SSH into the container)
python -c "import sqlite3; [print(r) for r in sqlite3.connect('/home/data/crip.db').execute(
  \"select created_at, agent_name, tool_name, status, data_timestamp, json_extract(sources,'$[0].auth'),
   json_extract(sources,'$[0].request_id') from agent_invocations order by created_at desc limit 10\")]"
```

`auth` is always `user_obo`; `request_id` is Azure's own id for the call.
