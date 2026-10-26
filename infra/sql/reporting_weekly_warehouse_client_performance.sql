-- TrackFlow weekly business reporting destination.
-- Provision explicitly in an authorized PostgreSQL environment; this script is
-- not invoked by application imports or SQLModel.metadata.create_all().
-- IF NOT EXISTS makes creation repeatable and non-destructive, but does not
-- validate or reconcile an existing schema. Verify existing columns, types,
-- defaults, primary key, and UNIQUE constraint against this contract before use.
-- gen_random_uuid() must be available on the target PostgreSQL server; verify
-- server capability before applying this script.
-- Safe manual procedure (authorized, non-production PostgreSQL only): inspect
-- the target and existing object first; apply with psql -v ON_ERROR_STOP=1 -f
-- this file; then inspect information_schema.columns and pg_catalog constraints
-- (including pg_get_constraintdef) for exact names, types, defaults, nullability,
-- primary key, and UNIQUE grain. Stop for review on any mismatch; do not DROP,
-- truncate, or attempt automatic reconciliation.

CREATE SCHEMA IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS reporting.weekly_warehouse_client_performance (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse text NOT NULL,
    client_id text NOT NULL,
    week_start date NOT NULL,
    inbound_units_count integer NOT NULL DEFAULT 0,
    outbound_orders_count integer NOT NULL DEFAULT 0,
    stockout_events_count integer NOT NULL DEFAULT 0,
    discrepancy_events_count integer NOT NULL DEFAULT 0,
    discrepancy_rate numeric NOT NULL DEFAULT 0,
    computed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warehouse, client_id, week_start)
);
