-- Transactional inventory-event outbox for the operational PostgreSQL database.
-- Provision explicitly only after inspecting and authorizing the target database.
-- Application imports and SQLModel.metadata.create_all() do not apply this DDL.

CREATE TABLE IF NOT EXISTS inventory_event_outbox (
    event_id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    source_table text NOT NULL,
    source_pk bigint NOT NULL,
    event_type text NOT NULL,
    tags jsonb NOT NULL,
    published_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_table, source_pk, event_type),
    CONSTRAINT ck_inventory_event_outbox_source_event_pair CHECK (
        (source_table = 'stockentry' AND event_type = 'inbound_order_created')
        OR (source_table = 'commercial_order' AND event_type = 'outbound_order_created')
        OR (source_table = 'stockexit' AND event_type = 'stock_threshold_triggered')
        OR (
            source_table = 'inventory_physical_count'
            AND event_type = 'inventory_discrepancy_detected'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_inventory_event_outbox_pending
    ON inventory_event_outbox (occurred_at, event_id)
    WHERE published_at IS NULL;
