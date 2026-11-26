-- TrackFlow WebSocket chat. Separate from consent-gated Agent Memory.
BEGIN;

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id varchar(64) PRIMARY KEY,
    agent_id varchar(64) NOT NULL DEFAULT 'first_line_cx' CHECK (agent_id = 'first_line_cx'),
    user_id varchar(64) NOT NULL,
    client_id varchar(36) NOT NULL REFERENCES trackflow_client(client_id) ON DELETE RESTRICT,
    status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','interrupted','closed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_owner
    ON chat_sessions (user_id, client_id, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id varchar(64) PRIMARY KEY,
    session_id varchar(64) NOT NULL REFERENCES chat_sessions(session_id) ON DELETE RESTRICT,
    role varchar(16) NOT NULL CHECK (role IN ('user','assistant')),
    content text NOT NULL DEFAULT '',
    status varchar(16) NOT NULL CHECK (status IN ('completed','streaming','interrupted','failed')),
    sequence bigint NOT NULL CHECK (sequence > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session_created
    ON chat_messages (session_id, sequence);

CREATE TABLE IF NOT EXISTS chat_generations (
    generation_id varchar(64) PRIMARY KEY,
    session_id varchar(64) NOT NULL REFERENCES chat_sessions(session_id) ON DELETE RESTRICT,
    user_message_id varchar(64) NOT NULL REFERENCES chat_messages(message_id) ON DELETE RESTRICT,
    assistant_message_id varchar(64) NOT NULL UNIQUE REFERENCES chat_messages(message_id) ON DELETE RESTRICT,
    status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','interrupted','failed')),
    chunk_sequence bigint NOT NULL DEFAULT 0 CHECK (chunk_sequence >= 0),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_generation_active
    ON chat_generations (session_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS chat_event_cursors (
    session_id varchar(64) PRIMARY KEY REFERENCES chat_sessions(session_id) ON DELETE RESTRICT,
    last_cursor bigint NOT NULL DEFAULT 0 CHECK (last_cursor >= 0)
);

CREATE TABLE IF NOT EXISTS chat_events (
    session_id varchar(64) NOT NULL REFERENCES chat_sessions(session_id) ON DELETE RESTRICT,
    cursor bigint NOT NULL CHECK (cursor > 0),
    event_type varchar(32) NOT NULL CHECK (
        event_type IN ('token_chunk','coverage_updated','generation_interrupted','generation_completed')
    ),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, cursor)
);
CREATE INDEX IF NOT EXISTS ix_chat_events_session_recorded
    ON chat_events (session_id, recorded_at);

COMMIT;
