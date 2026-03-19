-- Append-only log of every reaction add/remove event
CREATE TABLE IF NOT EXISTS discord_reaction_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id bigint NOT NULL,
    user_id bigint NOT NULL,
    emoji text NOT NULL,
    action text NOT NULL CHECK (action IN ('add', 'remove')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Index for querying by message
CREATE INDEX IF NOT EXISTS idx_reaction_log_message_id ON discord_reaction_log (message_id);
-- Index for querying by user
CREATE INDEX IF NOT EXISTS idx_reaction_log_user_id ON discord_reaction_log (user_id);
-- Index for time-range queries
CREATE INDEX IF NOT EXISTS idx_reaction_log_created_at ON discord_reaction_log (created_at);
