# CRIP architecture

## Components

```
 Browser (React SPA, MSAL)                          Azure App Service (one Linux container)
   │  GET /, /assets/*, /config.js  ───────────►   FastAPI  ── serves the built UI + runtime config
   │  POST /api/chat  (Bearer: token for CRIP API)──►       ── /api: validates token, runs agents, executes tools
                                                     │   │   │
          managed identity (DefaultAzureCredential)  │   │   └─ user's OBO token ─► management.azure.com
                       ┌─────────────────────────────┘   │       Cost Management (query, forecast)
                       ▼                                 │       Advisor (cost recommendations)
        Azure AI Foundry Agent Service                   │       Resource Graph (inventory, idle resources)
          ├─ crip-orchestrator  tools: ask_costpulse, ask_optimizer, ask_inventory
          ├─ crip-costpulse     tools: query_costs, cost_trend, forecast_month_end, list_subscriptions
          ├─ crip-optimizer     tools: advisor_recommendations, find_idle_resources
          └─ crip-inventory     tools: resource_summary
                                                         └─ SQLite on /home (or PostgreSQL): sessions, messages,
                                                            agent_invocations (the grounding audit trail)
```

**Agents** live in Foundry. **Tools** are backend code. The app runs agents and executes the function
tools they request.

## A question, step by step (the OBO flow)

The property everything rests on: **every Azure data call carries the signed-in user's own
delegated token.** No platform identity reads customer data, and there is no application-level scope
checking standing in for Azure RBAC.

1. **Sign-in.** The SPA (MSAL, app registration `api://<appId>`) gets a token for the scope
   `api://<appId>/access_as_user`. Its audience is *CRIP's API*, not Azure.
   ([frontend/src/config.ts](../frontend/src/config.ts); tenant/client IDs come from `/config.js`,
   generated from App Settings)
2. **Request.** `POST /api/chat` with `Authorization: Bearer <token>`.
3. **Validation** on every request: RS256 signature against the tenant JWKS, issuer, audience, tenant,
   expiry, delegated scope `access_as_user`. App-only tokens → 403. ([auth/entra.py](../backend/crip_backend/auth/entra.py))
4. **Orchestrator run** in Foundry, authenticated as the app's **managed identity**
   (`DefaultAzureCredential`). That identity can run agents; it has no access to cost data.
   ([services.py](../backend/crip_backend/services.py))
5. **The model picks tools.** The Orchestrator calls e.g. `ask_costpulse` and `ask_optimizer` (in
   parallel if it wants); each runs its specialist agent; the specialist calls a function tool; the
   Foundry run enters `requires_action` and the app picks the call up.
   ([agent_gateway/foundry_gateway.py](../backend/crip_backend/agent_gateway/foundry_gateway.py))
6. **On-behalf-of exchange.** The tool asks its `ToolContext` for an ARM token. MSAL
   `acquire_token_on_behalf_of(user_assertion=<token from step 1>, scope=https://management.azure.com/.default)`.
   The app registration proves *which app* is asking with a **federated credential: a token of the web
   app's managed identity** (`CRIP_OBO_CREDENTIAL_MODE=managed_identity`), so no client secret exists.
   The result is an ARM token **for the user**. ([auth/obo.py](../backend/crip_backend/auth/obo.py))
7. **Guard.** Before every use (including cache hits) `DelegatedToken.assert_belongs_to(user)` checks
   the ARM token's `oid` is the user's, it is delegated (`scp`, not `idtyp=app`) and its audience is
   ARM; otherwise the tool refuses and returns an `error` contract.
8. **Azure call** through `ArmClient`, which has no credential of its own (the token is an argument).
   Azure applies **the user's** RBAC; a 403 becomes an honest "you need Reader on X" answer.
   ([azure_clients/arm.py](../backend/crip_backend/azure_clients/arm.py))
9. **Back up the chain.** The tool's `AgentResponse` → specialist (tool output) → Orchestrator
   (`grounding` block) → composed answer. Separately, the app attaches each tool's `AgentResponse`
   **verbatim** to the `ChatResponse` and writes it to `agent_invocations`, so the citations the user
   sees never pass through model text.

The ARM token is cached per `(session, user oid)` until 5 minutes before expiry (one question can
make several calls; a conversation many questions). The `oid` in the key means a session id can never
yield another user's token.

### Credential boundaries

| Credential | Used for | Never used for |
|---|---|---|
| User's CRIP token (from the SPA) | Authenticating to CRIP; OBO `user_assertion` | Sent to Azure directly |
| User's OBO ARM token | Cost Management, Advisor, Resource Graph, subscription list | Foundry |
| Web app managed identity | Foundry (run + register agents), ACR pull, OBO client assertion | Reading any customer Azure data |
| Key Vault references / `.secrets/` | Optional DB URL, optional OBO client secret | |

## The contract, and why grounding is structural

Every tool returns an `AgentResponse` ([contracts.py](../backend/crip_backend/contracts.py)):

| Field | Meaning |
|---|---|
| `agent` | Which agent's tool produced it |
| `status` | `ok` grounded & complete · `partial` grounded with caveats · `no_data` query succeeded, nothing found · `error` retrieval failed |
| `answer` | Deterministic statement of the result, computed from Azure's response |
| `confidence` | `{level, score}`; a validator makes them agree |
| `data` | Structured figures (+ a `kind` the UI uses to pick a chart) |
| `query_used` | Exact HTTP method, URL and body (or KQL) sent to Azure |
| `data_timestamp` | When the data was true **at the origin**: latest billing day for cost data, Advisor's assessment time, response time for live inventory. For mixed answers (idle resources + their cost) the *older* of the two |
| `sources[]` | `{tool, api, invoked_at, scope, auth: "user_obo", request_id, http_status}`, one per Azure call |
| `caveats[]` | Latency, truncation, mixed currencies, "Advisor's estimate", the anomaly rule… |

Rules enforced at construction time: `ok`/`partial` **must** have sources, query and timestamp;
`no_data` must have the query and source; `error` must not carry `data`; `Source.auth` can only be
`user_obo`; a `ChatResponse` can only be `ok`/`partial` if some contribution is grounded. The database
mirrors the first rule with a CHECK constraint (SQLite and PostgreSQL).

Why structural: "remember to attach a source" erodes under deadline pressure; a validator cannot be
forgotten. That is also what makes `agent_invocations` audit evidence: an unproven row cannot exist.

### Honesty rules for the new analytics

- **Anomalies** (`costpulse_cost_trend`) use a stated, deterministic rule on Azure's daily numbers:
  more than 3 scaled MADs above the window median *and* ≥30% above it. Drivers are services whose
  cost that day exceeded their own median. The rule is in the caveats of every answer.
- **Forecast** figures are Azure Cost Management's own forecast (`/forecast` API). CRIP never
  extrapolates. Without actual billing days there is no projection (`no_data`).
- **Savings** are Azure Advisor's estimates, labelled as such. **Idle resources** are priced with
  *actual* month-to-date cost from Cost Management; if pricing fails the list is still shown, as
  `partial`, with the failure stated.

## Why function tools (not OpenAPI tools or connected agents)

Foundry could call HTTP endpoints itself or chain agents server-side, but neither can carry the
user's token: Foundry would act as a platform identity. With **function tools** the model still
decides what to call, but the call executes inside the user's request where the OBO token exists.

## Why routing is never hardcoded

The Orchestrator is registered with one **delegation tool per domain agent**, generated from the
definitions; choosing among them is the model's tool-calling decision. The app only executes whichever
delegation the model chose ([conversation.py](../backend/crip_backend/agent_gateway/conversation.py)).
No `if "cost" in message`. That breaks the moment questions span domains, and the best questions do
("why did my bill go up and what can I do?" needs CostPulse **and** Optimizer).
`tests/test_orchestrator_routing.py` proves both directions.

## Hosting: one container on App Service

- The [`Dockerfile`](../Dockerfile) builds the UI with Node, then runs FastAPI, which serves `/api`,
  `/health`, a generated `/config.js`, and the SPA with deep-link fallback. One origin means no CORS
  and one redirect URI.
- Configuration = App Settings ([config.py](../backend/crip_backend/config.py)); secrets by name
  ([secrets.py](../backend/crip_backend/secrets.py)): env `CRIP_SECRET_<NAME>` (Key Vault reference)
  or `.secrets/<name>` locally.
- Storage: SQLite at `/home/data/crip.db` (App Service persistent storage) by default; PostgreSQL if
  `database-url` is configured.
- Agents: created/updated by the app at startup (`CRIP_REGISTER_AGENTS_ON_STARTUP`) from
  `foundry/definitions`, so deploying a new image also deploys new agent instructions and tools.

## Failure behaviour

| Failure | What the user sees |
|---|---|
| Missing/invalid token | 401 envelope `unauthenticated` |
| App-only token / missing scope | 403 envelope `forbidden` |
| OBO exchange fails (consent, FIC misconfig) | `error` contribution naming the Entra error; no Azure call made |
| User lacks RBAC on the scope | `error` contribution: "Azure denied access … needs Reader / Cost Management Reader" |
| Throttling (429/503) | Retries honouring `Retry-After` (max 3, ≤30 s each), then an honest `error` |
| No rows / no recommendations / no idle resources | `no_data`, never "0.00" |
| Paging truncated, mixed currencies, cost enrichment failed | `partial` with the reason in caveats |
| Foundry run fails / times out | 502/504 envelope; failure persisted; no answer shown |
| Database down | 503 envelope; `/health` fails |

---

## Adding the next agent

Example: a **Budgets** agent. Three files, nothing in the Orchestrator, gateway, chat endpoint,
contract, persistence or deployment changes.

### 1. Tools: copy [`tools/optimizer.py`](../backend/crip_backend/tools/optimizer.py)

```python
AGENT = "budgets"
LIST = "budgets_status"

class StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str

async def status(args: StatusArgs, ctx: ToolContext) -> AgentResponse:
    token = await common.user_token(ctx, agent=AGENT)      # the user's OBO token, the ONLY token a tool may use
    if isinstance(token, AgentResponse):
        return token
    result = await ctx.arm.request("GET", ctx.arm.url(f"{args.scope}/providers/Microsoft.Consumption/budgets", "2023-05-01"), token)
    ...
    return AgentResponse(agent=AGENT, status=..., data={"kind": "...", ...}, query_used=..., data_timestamp=..., sources=[common.source(result, ...)])
```

A tool **must**: return an `AgentResponse` for expected failures (use `common.failure`); set
`data_timestamp` to when the data was true at the origin; get tokens only via
`common.user_token`/`ctx.arm_token()`; add `data.kind` if you want a chart.

### 2. Register: [`tools/registry.py`](../backend/crip_backend/tools/registry.py)

```python
ToolSpec(budgets.LIST, budgets.AGENT, budgets.StatusArgs, budgets.status),
```

### 3. Define: copy [`foundry/definitions/optimizer.json`](../foundry/definitions/optimizer.json) + `.md`

`foundry/definitions/budgets.json`: `"role": "domain"`, `"key": "budgets"`, `"name": "crip-budgets"`,
the tool JSON schemas (must match the args model), `examples` (shown in the UI), and the delegation
tool. **Its description is the routing signal**:

```json
"delegation_tool": { "name": "ask_budgets", "description": "Ask the Budgets agent whether spend is within Azure budgets ..." }
```

### 4. Test and ship

`pytest` (`test_foundry_definitions.py` fails if a tool has no handler or the schema and model
disagree; copy `tests/test_optimizer_tools.py` for the tool). Optionally add a renderer for the new
`data.kind` in `frontend/src/components/Insights.tsx`. Deploy the image: at startup the app creates
`crip-budgets` **and updates `crip-orchestrator`** with the new `ask_budgets` tool. The model starts
routing to it; no routing code changed.
