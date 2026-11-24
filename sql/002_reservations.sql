CREATE TABLE pacing_decisions (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns (id),
    mode text NOT NULL CHECK (mode IN ('progressive', 'predictive')),
    requested_count integer NOT NULL,
    approved_count integer NOT NULL CHECK (approved_count BETWEEN 0 AND requested_count),
    reason text NOT NULL,
    inputs jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, id)
);

ALTER TABLE call_attempts
    ADD COLUMN decision_id uuid,
    ADD COLUMN provider text,
    ADD COLUMN idempotency_key text,
    ADD COLUMN approved_at timestamptz,
    ADD COLUMN dispatch_status text CHECK (dispatch_status = 'PENDING'),
    ADD FOREIGN KEY (campaign_id, decision_id) REFERENCES pacing_decisions (campaign_id, id),
    ADD UNIQUE (provider, idempotency_key),
    -- Older Phase 1 rows have no approval and acquire no dispatch job during migration.
    ADD CHECK (num_nonnulls(decision_id, provider, idempotency_key, approved_at, dispatch_status) IN (0, 5));

CREATE INDEX call_attempts_decision_idx ON call_attempts (decision_id);

CREATE UNIQUE INDEX call_attempts_active_agent_idx ON call_attempts (agent_id)
    WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED');
CREATE UNIQUE INDEX call_attempts_active_borrower_idx ON call_attempts (borrower_id)
    WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED');

CREATE TABLE dispatch_jobs (
    id uuid PRIMARY KEY,
    attempt_id uuid NOT NULL UNIQUE REFERENCES call_attempts (id),
    state text NOT NULL CHECK (state IN ('READY', 'LEASED', 'DONE', 'RECONCILE')),
    available_at timestamptz NOT NULL,
    lease_owner text,
    lease_until timestamptz,
    lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error text
);

CREATE INDEX dispatch_jobs_ready_idx ON dispatch_jobs (state, available_at);
CREATE INDEX dispatch_jobs_lease_expiry_idx ON dispatch_jobs (lease_until)
    WHERE state = 'LEASED';
