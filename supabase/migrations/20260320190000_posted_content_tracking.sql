-- Track which Discord messages correspond to server_content entries.
-- Enables the bot to auto-sync content when server_content is updated.

CREATE TABLE IF NOT EXISTS posted_content (
    guild_id    BIGINT NOT NULL REFERENCES server_config(guild_id),
    content_key TEXT NOT NULL,
    channel_id  BIGINT NOT NULL,
    message_ids BIGINT[] NOT NULL DEFAULT '{}',
    thread_id   BIGINT,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, content_key)
);

ALTER TABLE posted_content ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow service role full access on posted_content"
    ON posted_content FOR ALL
    USING (true) WITH CHECK (true);

-- Auto-update updated_at on server_content changes
CREATE OR REPLACE FUNCTION update_server_content_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_server_content_updated ON server_content;
CREATE TRIGGER trg_server_content_updated
    BEFORE UPDATE ON server_content
    FOR EACH ROW
    EXECUTE FUNCTION update_server_content_timestamp();
