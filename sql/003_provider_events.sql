ALTER TABLE call_attempts DROP CONSTRAINT call_attempts_dispatch_status_check;
ALTER TABLE call_attempts
    ADD CHECK (dispatch_status IN ('PENDING', 'STARTED', 'UNKNOWN', 'CONFIRMED', 'REJECTED', 'CANCELLED')),
    ADD COLUMN provider_call_id text,
    ADD COLUMN dispatch_started_at timestamptz,
    ADD COLUMN provider_accepted_at timestamptz,
    ADD COLUMN answered_at timestamptz,
    ADD COLUMN connected_at timestamptz,
    ADD COLUMN terminated_at timestamptz,
    ADD COLUMN last_reconciled_at timestamptz,
    ADD COLUMN terminal_reason text,
    ADD COLUMN agent_lost_at timestamptz,
    ADD COLUMN answered_without_agent_at timestamptz,
    ADD UNIQUE (provider, provider_call_id);

CREATE TABLE provider_health (
    provider text PRIMARY KEY,
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    blocked_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO provider_health (provider) VALUES ('mock_fast'), ('mock_unreliable');
ALTER TABLE campaigns ADD COLUMN provider text NOT NULL DEFAULT 'mock_fast' REFERENCES provider_health (provider);

CREATE TABLE provider_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider text NOT NULL REFERENCES provider_health (provider),
    provider_event_id text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES call_attempts (id),
    event_type text NOT NULL CHECK (event_type IN (
        'INITIATED', 'RINGING', 'ANSWERED', 'CONNECTED', 'COMPLETED', 'FAILED', 'CANCELLED'
    )),
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    processed_at timestamptz,
    processing_error text,
    UNIQUE (provider, provider_event_id)
);
CREATE INDEX provider_events_pending_idx ON provider_events (received_at, id) WHERE processed_at IS NULL;
CREATE INDEX provider_events_attempt_idx ON provider_events (attempt_id);

-- The mock providers commit their own ledger independently of the dialer.
CREATE TABLE mock_calls (
    provider text NOT NULL REFERENCES provider_health (provider),
    idempotency_key text NOT NULL,
    attempt_id uuid,
    destination text,
    provider_call_id text,
    status text NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED', 'CANCELLED')),
    created_at timestamptz NOT NULL,
    cancelled_at timestamptz,
    PRIMARY KEY (provider, idempotency_key)
);

CREATE TABLE mock_events (
    provider text NOT NULL,
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key text NOT NULL,
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    deliver_at timestamptz NOT NULL,
    acked_at timestamptz,
    FOREIGN KEY (provider, idempotency_key) REFERENCES mock_calls (provider, idempotency_key)
);
CREATE INDEX mock_events_delivery_idx ON mock_events (provider, deliver_at, event_id) WHERE acked_at IS NULL;
CREATE INDEX mock_events_call_idx ON mock_events (provider, idempotency_key, occurred_at);
