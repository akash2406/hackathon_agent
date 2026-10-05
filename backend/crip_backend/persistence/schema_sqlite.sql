-- CRIP schema for SQLite (default store: zero setup, single instance).
-- Mirrors schema.sql (PostgreSQL), including the grounding CHECK constraint.

CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    -- Entra object ID: stable for the lifetime of the account (a UPN can change).
    owner_oid          TEXT    NOT NULL,
    owner_tenant_id    TEXT    NOT NULL,
    foundry_thread_id  TEXT,
    title              TEXT,
    last_seq           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_sessions_owner ON sessions (owner_oid, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT    NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT    NOT NULL,
    status      TEXT    CHECK (status IN ('ok', 'partial', 'no_data', 'error')),
    error_code  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS agent_invocations (
    id                TEXT PRIMARY KEY,
    message_id        TEXT    NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    agent_name        TEXT    NOT NULL,
    tool_name         TEXT,
    status            TEXT    NOT NULL CHECK (status IN ('ok', 'partial', 'no_data', 'error')),
    answer            TEXT    NOT NULL,
    query_used        TEXT,
    data_timestamp    TEXT,
    confidence_level  TEXT    NOT NULL CHECK (confidence_level IN ('high', 'medium', 'low')),
    confidence_score  REAL    NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
    data              TEXT    CHECK (data IS NULL OR json_valid(data)),
    sources           TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(sources)),
    caveats           TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(caveats)),
    latency_ms        INTEGER NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Database-level mirror of the AgentResponse grounding validator.
    CONSTRAINT grounded_rows_have_provenance CHECK (
        status NOT IN ('ok', 'partial')
        OR (query_used IS NOT NULL AND data_timestamp IS NOT NULL AND json_array_length(sources) > 0)
    )
);
CREATE INDEX IF NOT EXISTS ix_agent_invocations_message ON agent_invocations (message_id);

-- Every request to CRIP (chat, dashboard tool calls, denials): the platform admins' usage log.
CREATE TABLE IF NOT EXISTS access_log (
    id              TEXT PRIMARY KEY,
    at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    user_oid        TEXT    NOT NULL,
    user_name       TEXT,
    action          TEXT    NOT NULL,
    scope           TEXT,
    outcome         TEXT    NOT NULL,
    latency_ms      INTEGER,
    correlation_id  TEXT,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS ix_access_log_at ON access_log (at DESC);
