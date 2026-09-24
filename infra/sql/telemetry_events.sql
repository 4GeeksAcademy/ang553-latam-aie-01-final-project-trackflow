CREATE TABLE IF NOT EXISTS telemetry_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp timestamptz NOT NULL,
    service text NOT NULL,
    event_type text NOT NULL,
    level text DEFAULT 'info',
    value numeric,
    message text,
    tags jsonb DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_timestamp
    ON telemetry_events (timestamp);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_event_type
    ON telemetry_events (event_type);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_tags_gin
    ON telemetry_events USING GIN (tags);
