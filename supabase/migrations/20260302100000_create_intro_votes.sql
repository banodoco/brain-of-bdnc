CREATE TABLE intro_votes (
    id SERIAL PRIMARY KEY,
    intro_id INTEGER NOT NULL REFERENCES pending_intros(id),
    message_id BIGINT NOT NULL,
    voter_id BIGINT NOT NULL,
    voter_role TEXT NOT NULL,  -- 'approver' or 'super_approver'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(message_id, voter_id)
);
CREATE INDEX idx_intro_votes_message ON intro_votes(message_id);
