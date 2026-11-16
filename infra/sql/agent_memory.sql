-- TrackFlow MEM-092 structured agent memory
-- Explicit PostgreSQL provisioning artifact. Apply only through the reviewed
-- migration process after incident_query_security.sql. Application imports do
-- not execute this file and these tables are outside SQLModel.metadata.
BEGIN;

CREATE TABLE public.agent_memory_conversation (
    id uuid PRIMARY KEY,
    user_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_memory_conversation_times CHECK (
        last_seen_at >= created_at
    ),
    CONSTRAINT uq_agent_memory_conversation_owner UNIQUE (id, user_id)
);
CREATE INDEX ix_agent_memory_conversation_owner
    ON public.agent_memory_conversation(user_id, last_seen_at);

CREATE TABLE public.agent_memory_proposal (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL,
    user_id varchar(64) NOT NULL,
    scope varchar(10) NOT NULL CHECK (scope IN ('global','client')),
    client_id varchar(36) REFERENCES public.trackflow_client(client_id) ON DELETE RESTRICT,
    category varchar(40) NOT NULL CHECK (
        category IN ('carrier_coverage','recurring_incident','b2b_report_preference')
    ),
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    canonical_key varchar(200) NOT NULL,
    origin_interaction_id varchar(64) NOT NULL,
    origin_message_sha256 char(64) NOT NULL CHECK (
        origin_message_sha256 ~ '^[0-9a-f]{64}$'
    ),
    status varchar(24) NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending','approved','rejected','edited','ambiguous_discard','expired')
    ),
    parent_proposal_id uuid REFERENCES public.agent_memory_proposal(id) ON DELETE RESTRICT,
    valid_until timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT uq_agent_memory_proposal_origin UNIQUE (
        user_id, conversation_id, origin_interaction_id
    ),
    CONSTRAINT ck_agent_memory_proposal_scope_client CHECK (
        (scope = 'global' AND client_id IS NULL) OR
        (scope = 'client' AND client_id IS NOT NULL)
    ),
    CONSTRAINT ck_agent_memory_proposal_resolution CHECK (
        (status = 'pending' AND resolved_at IS NULL) OR
        (status <> 'pending' AND resolved_at IS NOT NULL)
    ),
    CONSTRAINT ck_agent_memory_proposal_times CHECK (
        expires_at > created_at AND valid_until > created_at
    ),
    CONSTRAINT ck_agent_memory_b2b_scope CHECK (
        category <> 'b2b_report_preference' OR scope = 'client'
    ),
    CONSTRAINT fk_agent_memory_proposal_conversation_owner FOREIGN KEY (
        conversation_id, user_id
    ) REFERENCES public.agent_memory_conversation(id, user_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_agent_memory_one_pending_conversation
    ON public.agent_memory_proposal(user_id, conversation_id)
    WHERE status = 'pending';
CREATE INDEX ix_agent_memory_proposal_expiry
    ON public.agent_memory_proposal(status, expires_at);

CREATE TABLE public.agent_memory_decision (
    id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL UNIQUE REFERENCES public.agent_memory_proposal(id) ON DELETE RESTRICT,
    outcome varchar(24) NOT NULL CHECK (
        outcome IN ('approved','rejected','edited','ambiguous_discard','expired')
    ),
    actor_user_id varchar(64) NOT NULL,
    idempotency_key varchar(64) NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_memory_decision_actor_idempotency UNIQUE (
        actor_user_id, idempotency_key
    )
);

CREATE TABLE public.agent_memory_entry (
    id uuid PRIMARY KEY,
    scope varchar(10) NOT NULL CHECK (scope IN ('global','client')),
    client_id varchar(36) REFERENCES public.trackflow_client(client_id) ON DELETE RESTRICT,
    category varchar(40) NOT NULL CHECK (
        category IN ('carrier_coverage','recurring_incident','b2b_report_preference')
    ),
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    canonical_key varchar(200) NOT NULL,
    verification_status varchar(20) NOT NULL DEFAULT 'user_reported' CHECK (
        verification_status IN ('user_reported','system_verified')
    ),
    authorizing_decision_id uuid NOT NULL UNIQUE REFERENCES public.agent_memory_decision(id) ON DELETE RESTRICT,
    supersedes_entry_id uuid REFERENCES public.agent_memory_entry(id) ON DELETE RESTRICT,
    status varchar(16) NOT NULL DEFAULT 'active' CHECK (
        status IN ('active','superseded','expired')
    ),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    valid_from timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    created_by_user_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_memory_entry_scope_client CHECK (
        (scope = 'global' AND client_id IS NULL) OR
        (scope = 'client' AND client_id IS NOT NULL)
    ),
    CONSTRAINT ck_agent_memory_entry_times CHECK (
        expires_at > valid_from AND updated_at >= created_at
    )
);

CREATE UNIQUE INDEX uq_agent_memory_one_active_key
    ON public.agent_memory_entry(
        scope, COALESCE(client_id, ''), category, canonical_key
    ) WHERE status = 'active';
CREATE INDEX ix_agent_memory_entry_lookup
    ON public.agent_memory_entry(scope, client_id, category, status);

CREATE FUNCTION public.enforce_agent_memory_entry_authorization() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.agent_memory_decision AS decision
        JOIN public.agent_memory_proposal AS proposal
          ON proposal.id = decision.proposal_id
        WHERE decision.id = NEW.authorizing_decision_id
          AND decision.outcome = 'approved'
          AND proposal.status = 'approved'
          AND proposal.scope = NEW.scope
          AND proposal.client_id IS NOT DISTINCT FROM NEW.client_id
          AND proposal.category = NEW.category
          AND proposal.canonical_key = NEW.canonical_key
          AND proposal.content = NEW.content
    ) THEN
        RAISE EXCEPTION 'memory entry requires its matching approved proposal decision'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_memory_entry_authorization
BEFORE INSERT OR UPDATE OF authorizing_decision_id, scope, client_id, category, canonical_key, content
ON public.agent_memory_entry
FOR EACH ROW EXECUTE FUNCTION public.enforce_agent_memory_entry_authorization();

CREATE FUNCTION public.reject_agent_memory_decision_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent_memory_decision is append-only';
END;
$$;
CREATE TRIGGER agent_memory_decision_append_only
BEFORE UPDATE OR DELETE ON public.agent_memory_decision
FOR EACH ROW EXECUTE FUNCTION public.reject_agent_memory_decision_mutation();

COMMIT;

-- Recovery before COMMIT: ROLLBACK;
-- Recovery after deployment: use a reviewed forward migration. Never remove
-- decision rows that authorize an active memory entry.
