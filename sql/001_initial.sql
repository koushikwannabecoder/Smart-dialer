CREATE TABLE campaigns (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('progressive', 'predictive')),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agents (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns (id),
    state text NOT NULL CHECK (state IN (
        'OFFLINE', 'AVAILABLE', 'RESERVED', 'DIALING', 'CONNECTED', 'WRAP_UP', 'PAUSED'
    )),
    last_seen_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, id)
);

CREATE INDEX agents_campaign_state_idx ON agents (campaign_id, state, id);

CREATE TABLE borrowers (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns (id),
    destination text NOT NULL,
    status text NOT NULL CHECK (status IN ('eligible', 'active', 'done')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, destination),
    UNIQUE (campaign_id, id)
);

CREATE INDEX borrowers_campaign_status_idx ON borrowers (campaign_id, status, id);

CREATE TABLE call_attempts (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns (id),
    agent_id uuid NOT NULL,
    borrower_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN (
        'RESERVED', 'DISPATCHING', 'UNKNOWN', 'INITIATED', 'RINGING', 'ANSWERED',
        'CONNECTED', 'COMPLETED', 'FAILED', 'CANCELLED'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (campaign_id, agent_id) REFERENCES agents (campaign_id, id),
    FOREIGN KEY (campaign_id, borrower_id) REFERENCES borrowers (campaign_id, id)
);
