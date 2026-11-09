-- TrackFlow Incident Query & Tenant Security
-- Explicit PostgreSQL provisioning artifact. Review and apply through the
-- normal migration process; the application never executes this file.
-- This artifact defines application-enforced tenant structures only. It does
-- not define, enable, or certify PostgreSQL Row Level Security (RLS).
BEGIN;

CREATE TABLE public.trackflow_client (
    client_id varchar(36) PRIMARY KEY,
    display_name varchar(200) NOT NULL UNIQUE,
    is_active boolean NOT NULL DEFAULT true,
    created_by_user_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.user_client_membership (
    user_id varchar(64) NOT NULL,
    client_id varchar(36) NOT NULL REFERENCES public.trackflow_client(client_id) ON DELETE RESTRICT,
    can_view_sensitive boolean NOT NULL DEFAULT false,
    assigned_by_user_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, client_id)
);
CREATE INDEX ix_membership_client ON public.user_client_membership(client_id);

-- user_id intentionally has no FK: authenticated identities currently live in
-- TinyDB. The controlled API verifies that identity before this row is written.
CREATE TABLE public.incident (
    id integer GENERATED ALWAYS AS IDENTITY (
        SEQUENCE NAME public.incident_id_seq
    ) PRIMARY KEY,
    incident_id varchar(10) GENERATED ALWAYS AS (
        'TRF-' || substr(
            '000000' || CAST(id AS varchar),
            length('000000' || CAST(id AS varchar)) - 5,
            6
        )
    ) STORED NOT NULL UNIQUE,
    client_id varchar(36) NOT NULL REFERENCES public.trackflow_client(client_id) ON DELETE RESTRICT,
    incident_date date NOT NULL,
    country varchar(2) NOT NULL CHECK (country IN ('US','ES')),
    customer_type varchar(3) NOT NULL CHECK (customer_type IN ('B2B','B2C')),
    tracking_number varchar(200) NOT NULL,
    carrier varchar(50) NOT NULL,
    category varchar(40) NOT NULL CHECK (category IN ('LOST_PARCEL','DELAYED_DELIVERY','WRONG_ADDRESS','RETURN_REQUEST','DAMAGE')),
    description text NOT NULL,
    status varchar(20) NOT NULL CHECK (status IN ('OPEN','CLOSED','DISCARDED')),
    customer_email varchar(320),
    satisfaction_score integer CHECK (satisfaction_score BETWEEN 1 AND 5),
    source_channel varchar(20) NOT NULL CHECK (source_channel IN ('email','whatsapp','phone','backoffice')),
    source_reference varchar(200),
    created_by_user_id varchar(64) NOT NULL,
    updated_by_user_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT ck_incident_public_id_capacity CHECK (id BETWEEN 1 AND 999999),
    CONSTRAINT ck_incident_status_score CHECK (
        (status = 'CLOSED' AND satisfaction_score IS NOT NULL) OR
        (status <> 'CLOSED' AND satisfaction_score IS NULL)
    )
);
CREATE INDEX ix_incident_client_updated ON public.incident(client_id, updated_at);

CREATE TABLE public.incident_history (
    id integer GENERATED ALWAYS AS IDENTITY (
        SEQUENCE NAME public.incident_history_id_seq
    ) PRIMARY KEY,
    incident_pk integer NOT NULL REFERENCES public.incident(id) ON DELETE RESTRICT,
    version integer NOT NULL,
    event_type varchar(20) NOT NULL CHECK (event_type IN ('CREATED','STATUS_CHANGED')),
    previous_status varchar(20),
    new_status varchar(20) NOT NULL,
    reason text,
    actor_user_id varchar(64) NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_incident_history_version UNIQUE (incident_pk, version),
    CONSTRAINT ck_incident_history_statuses CHECK (
        new_status IN ('OPEN','CLOSED','DISCARDED') AND
        (previous_status IS NULL OR previous_status IN ('OPEN','CLOSED','DISCARDED'))
    ),
    CONSTRAINT ck_incident_history_transition CHECK (
        (event_type = 'CREATED' AND version = 1 AND previous_status IS NULL AND new_status = 'OPEN') OR
        (event_type = 'STATUS_CHANGED' AND version >= 2 AND previous_status IS NOT NULL AND (
            (previous_status = 'OPEN' AND new_status IN ('CLOSED','DISCARDED')) OR
            (previous_status = 'CLOSED' AND new_status = 'OPEN')
        ))
    ),
    CONSTRAINT ck_incident_history_reopen_reason CHECK (
        NOT (previous_status = 'CLOSED' AND new_status = 'OPEN') OR
        (reason IS NOT NULL AND length(btrim(reason)) > 0)
    )
);

CREATE FUNCTION public.reject_incident_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'incident_history is append-only';
END;
$$;
CREATE TRIGGER incident_history_append_only
BEFORE UPDATE OR DELETE ON public.incident_history
FOR EACH ROW EXECUTE FUNCTION public.reject_incident_history_mutation();

COMMIT;

-- Recovery before COMMIT: ROLLBACK;
-- Recovery after deployment: use a reviewed forward migration. Do not drop
-- these tables while operational incident or authorization records exist.
