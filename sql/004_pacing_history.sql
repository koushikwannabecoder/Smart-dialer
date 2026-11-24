ALTER TABLE provider_events DROP CONSTRAINT provider_events_event_type_check;
ALTER TABLE provider_events ADD CHECK (event_type IN (
    'INITIATED', 'RINGING', 'ANSWERED', 'CONNECTED', 'COMPLETED', 'FAILED', 'CANCELLED', 'NO_ANSWER'
));

CREATE INDEX call_attempts_recent_outcomes_idx
    ON call_attempts (campaign_id, provider, terminated_at DESC, id DESC)
    WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED') AND dispatch_status = 'CONFIRMED';
