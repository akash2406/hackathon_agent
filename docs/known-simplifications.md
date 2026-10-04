# Known simplifications: where to start if CRIP becomes a real project

Hackathon trade-offs, made consciously. The non-negotiables are **not** simplified: real grounding,
on-behalf-of auth for every Azure data call, no hardcoded secrets.

## 1. No caching layer

Every question triggers live Azure calls. Cost Management has tight query quotas and only changes a
few times a day. **Start here:** cache tool results per `(user oid, scope, query hash)` for ~1 hour
(Azure Cache for Redis); keep the original `data_timestamp`/`sources` and add a "served from cache"
caveat. Never share entries across users: RBAC is per user.

## 2. SQLite on App Service storage

Zero setup, survives restarts, fine for one instance and demo traffic. Not for scale-out: SQLite on
the network-backed `/home` share does not support concurrent writers across instances. **Start here:**
set the `database-url` secret (Key Vault reference) to Azure Database for PostgreSQL; the app switches
automatically. Then add passwordless Entra auth, PgBouncer-safe settings and versioned migrations.

## 3. Read-only, no governed actions

Optimizer *finds* idle resources and Advisor savings; it never deletes or resizes anything.
**Start here:** a separate action agent with a proposal → human approval → narrowly scoped OBO call →
audit row flow, reviewed separately from the read-only agents.

## 4. No continuous evaluation of answer accuracy

Grounding guarantees every figure *has* provenance, not that the model's prose restates it perfectly
(the UI mitigates this by showing each tool's deterministic answer and chart next to the prose).
**Start here:** a nightly job comparing numbers in composed answers against the linked `data`, plus
an LLM-judge faithfulness score and a dashboard.

## 5. Anomaly detection is a simple statistic

Median/MAD over the selected window: explainable and deterministic, but it ignores weekly seasonality
and gradual drift. **Start here:** Cost Management's built-in anomaly alerts, or a seasonal baseline
(same weekday over 8 weeks).

## 6. Scope handling

Advisor, idle-resource and inventory tools work per subscription or resource group; spend and forecast
also accept management groups. No multi-subscription roll-up UI.

## Smaller items

- Throttling: basic retry (≤3, honours `Retry-After`). No circuit breaker, no per-user rate limit.
- Paging: at most 10 pages per query; beyond that answers are `partial`.
- One Foundry thread per conversation; delegated sub-runs use fresh threads that are not cleaned up.
- The UI does not reload past conversations (no `GET /api/sessions` yet).
- App Service health check uses `/health` (database ping); with one instance it cannot fail over.
- Charts are hand-rolled SVG (no charting library) to keep the bundle small; no export to CSV yet
  (each chart has a data table).
