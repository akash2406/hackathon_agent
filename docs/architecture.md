# CRIP architecture

## Components

```
 Browser (React SPA, MSAL)
   │  Authorization: Bearer <token for the backend API>
   ▼
 frontend pod (nginx) ── /api/* ──► backend pod (FastAPI, crip_backend)              [AKS tenant namespace]
                                     │   │   │
          DefaultAzureCredential     │   │   └── user's OBO token ──► management.azure.com (Cost Management)
          (Workload Identity)        │   │
                     ┌───────────────┘   └── Key Vault CSI files ──► PostgreSQL (allocated schema)
                     ▼
        Azure AI Foundry Agent Service
          ├─ crip-orchestrator   (tools: ask_costpulse)
          └─ crip-costpulse      (tools: costpulse_query_costs, costpulse_list_subscriptions)
```

**Agents** live in Foundry. **Tools** are backend code. The backend runs agents and executes the
function tools they request.

## The OBO token flow, step by step

This is the property the whole design rests on: **the token that calls Cost Management is the signed-in
user's own delegated token**. No platform identity ever holds read access to customer subscriptions,
and there is no application-level scope checking standing in for Azure RBAC.

1. **User signs in.** The SPA (app registration "frontend SPA", allocation #6) signs the user in with
   MSAL and requests an access token for the scope `api://<backend-api-client-id>/access_as_user`.
   The token's audience is *our backend API*, not Azure. ([frontend/src/config.ts](../frontend/src/config.ts))
2. **Frontend calls the backend.** `POST /api/chat` with `Authorization: Bearer <that token>`.
   ([frontend/src/api.ts](../frontend/src/api.ts))
3. **Backend validates the token** on every request: RS256 signature against the tenant's JWKS, issuer,
   audience, tenant, expiry, and the `access_as_user` delegated scope. App-only tokens (no user) are
   rejected with 403. The raw token is kept in memory as the future *user assertion*.
   ([auth/entra.py](../backend/crip_backend/auth/entra.py))
4. **Backend runs the Orchestrator** in Foundry, authenticating *as itself* with
   `DefaultAzureCredential` (Workload Identity, allocation #4). That identity can manage agent threads
   (allocation #7). It has **no** access to cost data and is never used for it.
   ([services.py](../backend/crip_backend/services.py))
5. **The model asks for a tool.** The Orchestrator calls `ask_costpulse`, the backend runs CostPulse,
   and CostPulse's model calls `costpulse_query_costs(scope=…)`. Foundry puts the run in
   `requires_action`, and the backend picks up the function call.
   ([agent_gateway/foundry_gateway.py](../backend/crip_backend/agent_gateway/foundry_gateway.py))
6. **Backend exchanges the user's token on-behalf-of the user.** The tool asks its `ToolContext` for
   an ARM token. `OnBehalfOfTokenProvider` calls MSAL `acquire_token_on_behalf_of` with the user's
   token from step 3 as `user_assertion` and scope `https://management.azure.com/.default`. The backend
   API app registration authenticates this exchange with a **federated credential**: the pod's projected
   service-account token, so no client secret exists. The result is an ARM token **for the user**.
   ([auth/obo.py](../backend/crip_backend/auth/obo.py))
7. **Guard: the token is proven to be the user's.** Before *every* use (including cache hits),
   `DelegatedToken.assert_belongs_to(user)` checks the ARM token's `oid` equals the signed-in user's
   `oid`, that it is delegated (`scp` present, not `idtyp=app`), and that its audience is ARM. If not,
   the tool refuses to call Azure and returns an `error` contract.
   ([tools/context.py](../backend/crip_backend/tools/context.py))
8. **Cost Management is called directly with that token.** `CostManagementClient` has no credential
   of its own; the token is a required argument. Azure evaluates **the user's** RBAC on the requested
   scope. A 403 becomes an honest "you lack Cost Management Reader on X" answer, not a fallback.
   ([azure_clients/cost_management.py](../backend/crip_backend/azure_clients/cost_management.py))
9. **The response flows back.** The tool builds an `AgentResponse` (source, query, billing-data
   timestamp) → submitted to CostPulse as tool output → CostPulse answers → returned to the Orchestrator
   as its `ask_costpulse` output → the Orchestrator composes the final text. Separately, the backend
   attaches the tool's `AgentResponse` **verbatim** to the `ChatResponse` and writes it to
   `agent_invocations`. The user therefore sees provenance that never passed through model text.

**Caching.** The exchanged ARM token is cached in memory keyed by `(session_id, user oid)` until 5
minutes before expiry. One question can trigger several tool calls, and a conversation spans many
questions; re-exchanging each time adds an Entra round-trip and throttling risk without improving
security, because the token is already scoped to exactly this user. The `oid` in the key means a
session id can never yield another user's token. Each backend replica has its own cache.

### Credential boundaries (enforced in `services.py`)

| Credential | Used for | Never used for |
|---|---|---|
| User's API token (from SPA) | Authenticating to CRIP; OBO `user_assertion` | Sent to any Azure API directly |
| User's OBO ARM token | Cost Management, subscription list | Foundry, Key Vault, PostgreSQL |
| Workload Identity (managed identity #4) | Foundry agent runs, Key Vault CSI mount, agent registration Job | Reading customer cost data |
| Federated SA token → backend API app reg (#6) | Client assertion for the OBO exchange | Anything else |
| Key Vault secrets (files) | PostgreSQL DSN, App Insights connection string | |

## The response contract, and why grounding is structural

Every tool returns an `AgentResponse` ([contracts.py](../backend/crip_backend/contracts.py)):

| Field | Meaning |
|---|---|
| `agent` | Which agent's tool produced it (`costpulse`) |
| `status` | `ok` grounded & complete · `partial` grounded with caveats · `no_data` query succeeded, nothing found · `error` retrieval failed |
| `answer` | Natural-language statement of the result (deterministic, computed from the rows) |
| `confidence` | `{level, score}`; a validator makes them agree (high ≥ 0.75, medium 0.40–0.75, low < 0.40) |
| `data` | Structured figures backing the answer (totals, top groups, `data_through`) |
| `query_used` | The exact HTTP method, URL and JSON body sent to Azure |
| `data_timestamp` | When the data was true at the origin: the latest billing day present in the rows (Cost Management lags 8–24h), **not** the call time |
| `sources[]` | `{tool, api, invoked_at, scope, auth: "user_obo", request_id, http_status}`, one per Azure call |
| `caveats[]` | Gaps and limitations (latency, truncation, mixed currencies…) |

Validation rules (raised as `ValidationError` at construction time):

- `ok`/`partial` **must** have `sources`, `query_used` and `data_timestamp`.
- `no_data` **must** have `sources` and `query_used` (a real query ran and found nothing).
- `error` **must not** carry `data`, and must have low confidence.
- `Source.auth` is `Literal["user_obo"]`: a platform-identity call cannot even be expressed as a source.
- `ChatResponse.status` can only be `ok`/`partial` if some contribution is grounded.

The `agent_invocations` table repeats the first rule as a `CHECK` constraint.

**Why structural rather than by convention:** "remember to attach a source" is a review comment, and
review comments erode under deadline pressure. A validator cannot be forgotten. A code path that tries
to return a number without provenance fails loudly in tests, not quietly in front of a judge.
Structural enforcement is also what makes "grounding is real" auditable: every row in
`agent_invocations` is proof, because an unproven row cannot exist.

The API layer has a matching `ErrorEnvelope` (`{error: {code, message, correlation_id, retryable}}`)
used for **every** non-2xx response: auth failures, unknown session, Foundry failure/timeout, database
unavailable, unexpected exceptions ([errors.py](../backend/crip_backend/errors.py)).

## Why function tools (and not OpenAPI tools or connected agents)

Foundry can call HTTP endpoints itself (OpenAPI tool) or chain agents server-side (connected agents).
Neither can carry the signed-in user's token: Foundry would call our backend or Azure as a *platform*
identity, which the OBO requirement forbids. With **function tools**, the model still decides what to
call, but the call executes inside the user's request, where the OBO token is available. The Orchestrator
→ CostPulse hop is also a function tool (`ask_costpulse`) for the same reason.

## Why routing is never hardcoded

The Orchestrator is registered with one **delegation tool per domain agent** (`ask_costpulse` today),
generated from the domain definitions. Choosing which to call is the model's tool-calling decision.
The backend ([conversation.py](../backend/crip_backend/agent_gateway/conversation.py)) only executes
whatever delegation tool the model chose, by looking it up in the definitions. There is no
`if "cost" in message` anywhere. That kind of check looks fine with one agent and breaks silently when
a second one arrives, because real questions ("why is untagged storage so expensive?") span domains.
`tests/test_orchestrator_routing.py` proves both directions: a model tool call is routed; a message
mentioning "cost" with no model tool call is not.

## Persistence

Tables in the allocated schema ([schema.sql](../backend/crip_backend/persistence/schema.sql)):

- `sessions`: one conversation, owned by the user's Entra **object ID** (stable; UPNs can change),
  plus the Foundry thread id.
- `messages`: user and assistant messages, ordered by an explicit per-session `seq` assigned by an
  atomic row-locking counter (two messages in the same millisecond still have a defined order).
- `agent_invocations`: one row per contribution: agent, tool, status, `query_used`, `data_timestamp`,
  confidence, sources, caveats, data, latency. **This is the grounding audit trail.**

Every read filters by owner `oid`, so another user's session id behaves like a nonexistent one (404).

## Failure behaviour

| Failure | What the user sees |
|---|---|
| Missing/invalid token | 401 envelope `unauthenticated` |
| App-only token / missing scope | 403 envelope `forbidden` |
| OBO exchange fails (consent, misconfig) | Answer with an `error` contribution naming the Entra error; no Azure call made |
| User lacks RBAC on the scope | `error` contribution: "Azure denied access … needs Cost Management Reader" |
| Cost Management throttles | Retries honouring `Retry-After` (max 3, ≤30s each); then an honest `error` |
| No rows | `no_data`; never "0.00" |
| Paging truncated / mixed currencies | `partial` with caveats |
| Foundry run fails / times out | 502/504 envelope; the failure is persisted; no answer shown |
| Database down | 503 envelope `persistence_unavailable`; readiness probe fails |

---

## Adding the next agent

Worked example: an **Inventory** agent answering "what resources do I have?" via Azure Resource Graph.
Everything below copies an existing CostPulse pattern. Nothing in the Orchestrator, gateway, chat
endpoint, contract, persistence, frontend or Helm chart changes.

### 1. Write the tool handlers: copy [`tools/costpulse.py`](../backend/crip_backend/tools/costpulse.py)

Create `backend/crip_backend/tools/inventory.py`:

```python
AGENT = "inventory"
LIST_RESOURCES = "inventory_list_resources"

class ListResourcesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    resource_type: str | None = None

async def list_resources(args: ListResourcesArgs, ctx: ToolContext) -> AgentResponse:
    token = await ctx.arm_token()          # the user's OBO token: the ONLY token a tool may use
    ...call Azure (add a client in azure_clients/ that takes `token` as an argument)...
    return AgentResponse(agent=AGENT, status=..., query_used=..., data_timestamp=..., sources=[Source(...)], ...)
```

What a tool **must** return:

- Always an `AgentResponse`. Never raise for expected failures (403, 404, throttling, empty); turn them into `error`/`no_data` like `costpulse._failure`.
- `ok`/`partial` require `sources`, `query_used`, `data_timestamp` (the validator enforces this).
- `data_timestamp` = when the data was true at the origin. For live control-plane data (Resource Graph, ARM) the Azure response time is honest. For lagged data (cost, metrics) use the latest timestamp *in the data*.
- Get tokens only via `ctx.arm_token()`. For a different resource (e.g. Log Analytics) add a scope parameter to the OBO provider; still OBO, never `DefaultAzureCredential`.

### 2. Register the handlers: [`tools/registry.py`](../backend/crip_backend/tools/registry.py)

```python
ToolSpec(inventory.LIST_RESOURCES, inventory.AGENT, inventory.ListResourcesArgs, inventory.list_resources),
```

### 3. Define the agent: copy [`foundry/definitions/costpulse.json`](../foundry/definitions/costpulse.json) + `.md`

Create `foundry/definitions/inventory.json` (`"role": "domain"`, `"key": "inventory"`,
`"name": "crip-inventory"`), its `tools` (JSON schemas matching your args model), and a
`delegation_tool`:

```json
"delegation_tool": {
  "name": "ask_inventory",
  "description": "Ask the Inventory agent which Azure resources exist: counts and lists by type, location, resource group or tag."
}
```

Write `foundry/definitions/inventory.md` modelled on `costpulse.md` (same grounding rules). The
delegation tool's **description is the routing signal**. Write it as if explaining to a colleague when
to ask this agent, and make its boundary with other agents' descriptions clear.

### 4. Run the tests

`pytest` includes `test_foundry_definitions.py`, which fails if a defined tool has no handler, or if
the JSON schema's properties/required fields don't match the pydantic args model. Add tests for your
tool by copying `tests/test_costpulse_tool.py` (respx-mocked Azure, OBO token asserted on the wire).

### 5. Register in Foundry

Deploying runs the Helm hook, which runs `foundry/register_agents.py`. Locally:
`python foundry/register_agents.py --dry-run`, then without `--dry-run`. The script creates
`crip-inventory` **and updates `crip-orchestrator`**, whose tool list is generated from all domain
definitions, so it now has `ask_costpulse` *and* `ask_inventory`.

### Why the Orchestrator picks it up without being rewritten

- Foundry side: the Orchestrator's tools are *generated* from the domain definitions, and the model
  chooses among them using their descriptions.
- Backend side: `ConversationService` resolves whichever `ask_*` the model called via
  `AgentDefinitions.delegation(tool_name)`, runs that agent, and restricts it to its own declared
  tools. `load_definitions` picks up the new JSON file at startup, and `verify_tool_coverage` refuses to
  start if a handler is missing.
- Provenance: the new tool's `AgentResponse` lands in `ToolContext.contributions` like CostPulse's, so
  the API response, the UI citation and `agent_invocations` need no change.

If the new agent needs network egress beyond the list in the NetworkPolicy/firewall (e.g. a new Azure
API host), that is a landing-zone request, not a chart workaround.
