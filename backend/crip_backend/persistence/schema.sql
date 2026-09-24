-- CRIP schema. Applied idempotently at backend startup inside the *allocated*
-- schema (search_path is set to it on every pooled connection). This file never
-- creates a database, server or schema: those are landing-zone allocations.

CREATE TABLE IF NOT EXISTS sessions (
    id                 UUID PRIMARY KEY,
    -- Entra object ID: stable for the lifetime of the account (a UPN can change).
    owner_oid          TEXT        NOT NULL,
    owner_tenant_id    TEXT        NOT NULL,
    foundry_thread_id  TEXT,
    title              TEXT,
    -- Per-session message counter; incremented atomically to assign seq.
    last_seq           INTEGER     NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sessions_owner ON sessions (owner_oid, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY,
    session_id  UUID        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- Explicit order: two messages can share a timestamp, they can never share a seq.
    seq         INTEGER     NOT NULL,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    status      TEXT        CHECK (status IN ('ok', 'partial', 'no_data', 'error')),
    error_code  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

-- One row per agent contribution to an answer: the audit evidence that
-- answers are grounded in real Azure calls.
CREATE TABLE IF NOT EXISTS agent_invocations (
    id                UUID PRIMARY KEY,
    message_id        UUID          NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    agent_name        TEXT          NOT NULL,
    tool_name         TEXT,
    status            TEXT          NOT NULL CHECK (status IN ('ok', 'partial', 'no_data', 'error')),
    answer            TEXT          NOT NULL,
    query_used        TEXT,
    data_timestamp    TIMESTAMPTZ,
    confidence_level  TEXT          NOT NULL CHECK (confidence_level IN ('high', 'medium', 'low')),
    confidence_score  NUMERIC(4, 3) NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
    data              JSONB,
    sources           JSONB         NOT NULL DEFAULT '[]'::jsonb,
    caveats           JSONB         NOT NULL DEFAULT '[]'::jsonb,
    latency_ms        INTEGER       NOT NULL,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    -- Database-level mirror of the AgentResponse grounding validator.
    CONSTRAINT grounded_rows_have_provenance CHECK (
        status NOT IN ('ok', 'partial')
        OR (query_used IS NOT NULL AND data_timestamp IS NOT NULL AND jsonb_array_length(sources) > 0)
    )
);
CREATE INDEX IF NOT EXISTS ix_agent_invocations_message ON agent_invocations (message_id);
