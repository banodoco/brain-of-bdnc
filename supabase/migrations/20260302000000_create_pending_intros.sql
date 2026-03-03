CREATE TABLE pending_intros (
    id SERIAL PRIMARY KEY,
    member_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL UNIQUE,
    channel_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'approved', 'expired'
    approved_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ
);
CREATE INDEX idx_pending_intros_member ON pending_intros(member_id);
CREATE INDEX idx_pending_intros_status ON pending_intros(status);
