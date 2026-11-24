-- Apply after rfp_intake.sql. The decision survives expiry of its SSE event.
BEGIN;

CREATE TABLE IF NOT EXISTS rfp_intake_decisions (
    ticket_id uuid PRIMARY KEY REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
    decision jsonb NOT NULL CHECK (jsonb_typeof(decision) = 'object')
);

CREATE TABLE IF NOT EXISTS rfp_sse_cursor (
    singleton boolean PRIMARY KEY CHECK (singleton),
    last_cursor bigint NOT NULL CHECK (last_cursor >= 0),
    purged_through bigint NOT NULL CHECK (purged_through BETWEEN 0 AND last_cursor)
);
INSERT INTO rfp_sse_cursor VALUES (true, 0, 0) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS rfp_sse_events (
    cursor bigint PRIMARY KEY CHECK (cursor > 0),
    event_type text NOT NULL CHECK (event_type = 'rfp_ticket_created'),
    ticket_id uuid NOT NULL REFERENCES rfp_intake_decisions(ticket_id) ON DELETE RESTRICT,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (event_type, ticket_id)
);

COMMIT;
