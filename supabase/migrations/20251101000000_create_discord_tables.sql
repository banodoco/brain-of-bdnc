-- SQL script to create Discord data tables in Supabase
-- This script creates the necessary tables to store Discord messages, members, and channels

-- Enable Row Level Security (RLS) for all tables
-- You may need to adjust RLS policies based on your security requirements

-- Discord Messages Table
CREATE TABLE IF NOT EXISTS discord_messages (
    message_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    author_id BIGINT NOT NULL,
    content TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    attachments JSONB DEFAULT '[]'::jsonb,
    embeds JSONB DEFAULT '[]'::jsonb,
    reaction_count INTEGER DEFAULT 0,
    reactors JSONB DEFAULT '[]'::jsonb,
    reference_id BIGINT,
    edited_at TIMESTAMPTZ,
    is_pinned BOOLEAN DEFAULT FALSE,
    thread_id BIGINT,
    message_type TEXT,
    flags INTEGER,
    is_deleted BOOLEAN DEFAULT FALSE,
    indexed_at TIMESTAMPTZ DEFAULT NOW(),
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for discord_messages
CREATE INDEX IF NOT EXISTS idx_discord_messages_channel_id ON discord_messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_created_at ON discord_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_discord_messages_author_id ON discord_messages(author_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_reference_id ON discord_messages(reference_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_synced_at ON discord_messages(synced_at);
CREATE INDEX IF NOT EXISTS idx_discord_messages_thread_id ON discord_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_discord_messages_is_deleted ON discord_messages(is_deleted);

-- Discord Members Table
CREATE TABLE IF NOT EXISTS discord_members (
    member_id BIGINT PRIMARY KEY,
    username TEXT NOT NULL,
    global_name TEXT,
    server_nick TEXT,
    avatar_url TEXT,
    discriminator TEXT,
    bot BOOLEAN DEFAULT FALSE,
    system BOOLEAN DEFAULT FALSE,
    accent_color INTEGER,
    banner_url TEXT,
    discord_created_at TIMESTAMPTZ,
    guild_join_date TIMESTAMPTZ,
    role_ids JSONB DEFAULT '[]'::jsonb,
    twitter_handle TEXT,
    instagram_handle TEXT,
    youtube_handle TEXT,
    tiktok_handle TEXT,
    website TEXT,
    sharing_consent BOOLEAN DEFAULT FALSE,
    dm_preference BOOLEAN DEFAULT TRUE,
    permission_to_curate BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for discord_members
CREATE INDEX IF NOT EXISTS idx_discord_members_username ON discord_members(username);
CREATE INDEX IF NOT EXISTS idx_discord_members_synced_at ON discord_members(synced_at);
CREATE INDEX IF NOT EXISTS idx_discord_members_updated_at ON discord_members(updated_at);
CREATE INDEX IF NOT EXISTS idx_discord_members_bot ON discord_members(bot);

-- Discord Channels Table
CREATE TABLE IF NOT EXISTS discord_channels (
    channel_id BIGINT PRIMARY KEY,
    channel_name TEXT NOT NULL,
    category_id BIGINT,
    description TEXT,
    suitable_posts TEXT,
    unsuitable_posts TEXT,
    rules TEXT,
    setup_complete BOOLEAN DEFAULT FALSE,
    nsfw BOOLEAN DEFAULT FALSE,
    enriched BOOLEAN DEFAULT FALSE,
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for discord_channels
CREATE INDEX IF NOT EXISTS idx_discord_channels_synced_at ON discord_channels(synced_at);
CREATE INDEX IF NOT EXISTS idx_discord_channels_category_id ON discord_channels(category_id);
CREATE INDEX IF NOT EXISTS idx_discord_channels_setup_complete ON discord_channels(setup_complete);

-- Full-text search for messages (PostgreSQL specific)
CREATE INDEX IF NOT EXISTS idx_discord_messages_content_fts ON discord_messages USING gin(to_tsvector('english', content));

-- Sync tracking table to keep track of sync status
CREATE TABLE IF NOT EXISTS sync_status (
    id SERIAL PRIMARY KEY,
    table_name TEXT NOT NULL UNIQUE,
    last_sync_timestamp TIMESTAMPTZ DEFAULT NOW(),
    records_synced INTEGER DEFAULT 0,
    sync_status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Insert initial sync status records
INSERT INTO sync_status (table_name, sync_status) 
VALUES 
    ('discord_messages', 'pending'),
    ('discord_members', 'pending'),
    ('discord_channels', 'pending')
ON CONFLICT (table_name) DO NOTHING;

-- Enable RLS on all tables (optional - adjust based on your security needs)
ALTER TABLE discord_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE discord_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_status ENABLE ROW LEVEL SECURITY;

-- Create policies for service role access (adjust as needed)
-- These policies allow the service role to access all data
DROP POLICY IF EXISTS "Service role can access all discord_messages" ON discord_messages;
CREATE POLICY "Service role can access all discord_messages" ON discord_messages
    FOR ALL USING (true);

DROP POLICY IF EXISTS "Service role can access all discord_members" ON discord_members;
CREATE POLICY "Service role can access all discord_members" ON discord_members
    FOR ALL USING (true);

DROP POLICY IF EXISTS "Service role can access all discord_channels" ON discord_channels;
CREATE POLICY "Service role can access all discord_channels" ON discord_channels
    FOR ALL USING (true);

DROP POLICY IF EXISTS "Service role can access all sync_status" ON sync_status;
CREATE POLICY "Service role can access all sync_status" ON sync_status
    FOR ALL USING (true);

-- Create a function to update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create triggers to automatically update updated_at
CREATE TRIGGER update_discord_members_updated_at BEFORE UPDATE ON discord_members
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_sync_status_updated_at BEFORE UPDATE ON sync_status
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Create a view for recent messages (useful for dashboards)
CREATE OR REPLACE VIEW recent_messages AS
SELECT 
    dm.*,
    dmem.username,
    dmem.global_name,
    dc.channel_name
FROM discord_messages dm
LEFT JOIN discord_members dmem ON dm.author_id = dmem.member_id
LEFT JOIN discord_channels dc ON dm.channel_id = dc.channel_id
WHERE dm.created_at >= NOW() - INTERVAL '7 days'
AND dm.is_deleted = FALSE
ORDER BY dm.created_at DESC;

-- Create a view for message statistics
CREATE OR REPLACE VIEW message_stats AS
SELECT 
    dc.channel_name,
    dc.channel_id,
    COUNT(*) as message_count,
    COUNT(DISTINCT dm.author_id) as unique_authors,
    MAX(dm.created_at) as last_message_at,
    MIN(dm.created_at) as first_message_at
FROM discord_messages dm
JOIN discord_channels dc ON dm.channel_id = dc.channel_id
WHERE dm.is_deleted = FALSE
GROUP BY dc.channel_id, dc.channel_name
ORDER BY message_count DESC;

-- Grant necessary permissions (adjust based on your setup)
-- GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
-- GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
-- GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO service_role;
