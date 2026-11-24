-- Apply after rfp_approval.sql. Grants are provisioned explicitly by operators.
BEGIN;

CREATE TABLE IF NOT EXISTS rfp_sse_commercial_grants (
    principal_id uuid NOT NULL,
    environment text NOT NULL CHECK (environment IN ('production', 'qa')),
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY (principal_id, environment),
    FOREIGN KEY (principal_id, environment)
        REFERENCES rfp_principals(principal_id, environment) ON DELETE RESTRICT,
    CHECK (expires_at IS NULL OR expires_at > granted_at),
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);

COMMIT;
