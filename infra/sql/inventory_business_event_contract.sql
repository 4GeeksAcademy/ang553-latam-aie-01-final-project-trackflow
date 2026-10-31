-- TrackFlow inventory business-event contract migration.
-- Inspect and authorize the target before applying. This file is not executed
-- by application imports and must not be applied to historical databases by QA.

DO $$
BEGIN
    IF to_regclass('public.inventory_event_outbox') IS NULL THEN
        RAISE EXCEPTION
            'inventory_event_outbox baseline is required before this migration';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM inventory_event_outbox
        WHERE NOT (
            (source_table = 'stockentry' AND event_type = 'inbound_order_created')
            OR (source_table = 'commercial_order' AND event_type = 'outbound_order_created')
            OR (source_table = 'stockexit' AND event_type = 'stock_threshold_triggered')
            OR (
                source_table = 'inventory_physical_count'
                AND event_type = 'inventory_discrepancy_detected'
            )
        )
    ) THEN
        RAISE EXCEPTION
            'legacy inventory outbox rows must be drained or migrated first';
    END IF;
END $$;

ALTER TABLE sku
    ADD COLUMN IF NOT EXISTS minimum_stock_threshold integer;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_sku_minimum_stock_threshold_nonnegative'
          AND conrelid = 'sku'::regclass
    ) THEN
        ALTER TABLE sku ADD CONSTRAINT ck_sku_minimum_stock_threshold_nonnegative
            CHECK (
                minimum_stock_threshold IS NULL
                OR minimum_stock_threshold >= 0
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS commercial_order (
    id serial PRIMARY KEY,
    order_id uuid NOT NULL,
    client_id text NOT NULL,
    warehouse text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_commercial_order_order_id UNIQUE (order_id)
);

ALTER TABLE stockexit
    ADD COLUMN IF NOT EXISTS commercial_order_id integer
        REFERENCES commercial_order (id);

ALTER TABLE stockexit
    ADD COLUMN IF NOT EXISTS dispatch_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_stockexit_order_only_for_dispatch'
          AND conrelid = 'stockexit'::regclass
    ) THEN
        ALTER TABLE stockexit ADD CONSTRAINT ck_stockexit_order_only_for_dispatch
            CHECK (commercial_order_id IS NULL OR exit_type = 'dispatch');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_stockexit_dispatch_id'
          AND conrelid = 'stockexit'::regclass
    ) THEN
        ALTER TABLE stockexit ADD CONSTRAINT uq_stockexit_dispatch_id
            UNIQUE (dispatch_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_stockexit_order_requires_dispatch_id'
          AND conrelid = 'stockexit'::regclass
    ) THEN
        ALTER TABLE stockexit ADD CONSTRAINT ck_stockexit_order_requires_dispatch_id
            CHECK (commercial_order_id IS NULL OR dispatch_id IS NOT NULL);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_stockexit_commercial_order_id
    ON stockexit (commercial_order_id);

CREATE TABLE IF NOT EXISTS inventory_physical_count (
    id serial PRIMARY KEY,
    count_id uuid NOT NULL,
    sku_id integer NOT NULL REFERENCES sku (id),
    client_id text NOT NULL,
    warehouse text NOT NULL,
    expected_quantity integer NOT NULL,
    observed_quantity integer NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    user_uuid text NOT NULL,
    CONSTRAINT uq_inventory_physical_count_count_id UNIQUE (count_id),
    CONSTRAINT ck_inventory_physical_count_expected_nonnegative
        CHECK (expected_quantity >= 0),
    CONSTRAINT ck_inventory_physical_count_observed_nonnegative
        CHECK (observed_quantity >= 0)
);

CREATE TABLE IF NOT EXISTS inventory_threshold_evaluation (
    id serial PRIMARY KEY,
    source_table text NOT NULL,
    source_pk bigint NOT NULL,
    sku_id integer NOT NULL REFERENCES sku (id),
    client_id text,
    warehouse text NOT NULL,
    before_quantity integer NOT NULL,
    after_quantity integer NOT NULL,
    threshold_quantity integer,
    crossed boolean NOT NULL,
    event_id uuid,
    evaluated_at timestamptz NOT NULL,
    CONSTRAINT uq_threshold_evaluation_source
        UNIQUE (source_table, source_pk),
    CONSTRAINT ck_threshold_evaluation_source_table
        CHECK (source_table IN ('stockentry', 'stockexit')),
    CONSTRAINT ck_threshold_evaluation_quantities_nonnegative
        CHECK (before_quantity >= 0 AND after_quantity >= 0),
    CONSTRAINT ck_threshold_evaluation_threshold_nonnegative
        CHECK (threshold_quantity IS NULL OR threshold_quantity >= 0),
    CONSTRAINT ck_threshold_evaluation_crossed_event CHECK (
        (crossed = true AND event_id IS NOT NULL)
        OR (crossed = false AND event_id IS NULL)
    )
);

-- Replace both names used by the current SQL and SQLAlchemy definitions.
ALTER TABLE inventory_event_outbox
    DROP CONSTRAINT IF EXISTS inventory_event_outbox_source_table_check,
    DROP CONSTRAINT IF EXISTS inventory_event_outbox_event_type_check,
    DROP CONSTRAINT IF EXISTS ck_inventory_event_outbox_source_table,
    DROP CONSTRAINT IF EXISTS ck_inventory_event_outbox_event_type,
    DROP CONSTRAINT IF EXISTS ck_inventory_event_outbox_source_event_pair;

ALTER TABLE inventory_event_outbox
    ADD CONSTRAINT ck_inventory_event_outbox_source_event_pair CHECK (
        (source_table = 'stockentry' AND event_type = 'inbound_order_created')
        OR (source_table = 'commercial_order' AND event_type = 'outbound_order_created')
        OR (source_table = 'stockexit' AND event_type = 'stock_threshold_triggered')
        OR (
            source_table = 'inventory_physical_count'
            AND event_type = 'inventory_discrepancy_detected'
        )
    );
