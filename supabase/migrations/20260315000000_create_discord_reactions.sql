CREATE TABLE IF NOT EXISTS discord_reactions (
    message_id BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    emoji      TEXT NOT NULL,        -- Unicode char or "name:id" for custom
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id, user_id, emoji)
);

CREATE INDEX idx_discord_reactions_message_id ON discord_reactions(message_id);
CREATE INDEX idx_discord_reactions_user_id ON discord_reactions(user_id);

ALTER TABLE discord_reactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON discord_reactions FOR ALL USING (true);
