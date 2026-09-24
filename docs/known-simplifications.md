# Known simplifications: where to start if CRIP becomes a real project

This build is a deliberately reduced slice of the target design, sized for a hackathon. The
non-negotiables are **not** simplified: real grounding, on-behalf-of auth, no hardcoded secrets.
Everything below is a conscious trade, listed with its starting point.

## 1. No caching layer

Every question triggers live Cost Management calls. That's fine for demo traffic, but Cost Management
has tight per-tenant and per-scope query quotas (QPU), and the data only changes a few times a day.
**Start here:** cache tool results keyed by `(user oid, scope, query body hash)` with a TTL of ~1 hour,
in Redis (a new landing-zone allocation). Keep the original `data_timestamp` and `sources` on cached
entries and add a caveat saying the result came from cache. Never share cache entries across users:
RBAC is per user. The OBO token cache is in-process per replica. Moving it to a shared store needs
encryption at rest and is only worth it at scale.

## 2. No production-grade PostgreSQL connection handling

A plain asyncpg pool with a password DSN from Key Vault. **Start here:**
- Passwordless Entra auth: fetch a token for `https://ossrdbms-aad.database.windows.net/.default` via
  Workload Identity per new connection (asyncpg `connect` with a password callable).
- If the shared server fronts connections with PgBouncer in transaction mode: disable asyncpg's
  prepared-statement cache (`statement_cache_size=0`) and avoid session-level state (our
  `search_path` server setting would need moving to schema-qualified SQL).
- Size the pool against the shared server's connection budget; add statement timeouts and retry on
  failover.
- Replace the idempotent `CREATE TABLE IF NOT EXISTS` at startup with versioned migrations (Alembic).

## 3. No governed write/action capability

CRIP is read-only end to end: no tool can change Azure state, and domain agents are restricted to the
tools in their own definition. **Start here:** actions need a separate, explicitly approved flow:
proposed-action records, human confirmation in the UI, a narrowly scoped OBO call, and a full audit
row, with action tools living on a separate agent whose registration is reviewed separately.

## 4. No continuous evaluation of answer accuracy

We enforce that every figure *has* provenance, but nothing automatically checks that the model's
composed prose *faithfully restates* the tool data (e.g. it didn't transpose two numbers). The UI
mitigates this by showing the tool's own deterministic `answer` and data next to the model's text.
**Start here:** a nightly job that samples `agent_invocations` + `messages` and checks that every
number in the composed answer appears in the linked `data`, plus an LLM-judge faithfulness score,
with an evaluation-sample table and a dashboard.

## 5. Single domain agent

Only CostPulse exists. The Orchestrator, gateway, contract and persistence are built for N agents
(model-driven routing over generated delegation tools). **Start here:**
[architecture.md, "Adding the next agent"](architecture.md#adding-the-next-agent).

## Smaller items worth knowing

- **Throttling:** basic retry (max 3, honours `Retry-After`, ≤30s per wait). No circuit breaker, no
  per-user rate limiting on `/api/chat`.
- **Paging:** at most 10 pages per cost query; beyond that the answer is `partial` with a caveat.
- **Scope choice:** if the user has several subscriptions and names none, CostPulse asks, or queries
  up to 3. There is no management-group roll-up UI.
- **Concurrent questions in one session:** Foundry rejects a new message while a run is active on the
  thread; the second request gets a 502 envelope. The UI prevents this by disabling input while busy.
- **Conversation history:** kept in Foundry threads and PostgreSQL, but the UI does not reload past
  sessions (no `GET /api/sessions` yet).
- **Delegated sub-runs** use a fresh Foundry thread per delegation, and those threads are not deleted.
  Add cleanup (or thread TTL) before real volume.
- **Frontend:** minimal styling, no markdown rendering (plain text, on purpose: model output is never
  injected as HTML), no automated UI tests.
- **Observability:** Azure Monitor OpenTelemetry auto-instrumentation only; no custom metrics for
  tool latency or grounding rates yet (the data is in `agent_invocations`).
