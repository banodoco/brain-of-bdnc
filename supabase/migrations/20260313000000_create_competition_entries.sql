-- Competitions table — defines a competition and its config
CREATE TABLE IF NOT EXISTS competitions (
    slug TEXT PRIMARY KEY,                   -- e.g. 'ltx-living-dead'
    name TEXT NOT NULL,                      -- e.g. 'LTX Living Dead Competition'
    channel_id BIGINT,                       -- the channel entries come from
    voting_channel_id BIGINT,                -- where voting happens (can be same channel)
    voting_hours INTEGER DEFAULT 24,
    min_join_weeks INTEGER DEFAULT 4,        -- min weeks in server for vote to count
    voting_header TEXT,                       -- custom voting announcement (markdown)
    status TEXT DEFAULT 'setup',             -- setup | voting | closed
    voting_starts_at TIMESTAMPTZ,            -- scheduled start (null = manual trigger)
    voting_started_at TIMESTAMPTZ,
    voting_ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Competition entries — any message tagged as an entry
CREATE TABLE IF NOT EXISTS competition_entries (
    id SERIAL PRIMARY KEY,
    competition_slug TEXT NOT NULL REFERENCES competitions(slug) ON DELETE CASCADE,
    message_id BIGINT NOT NULL REFERENCES discord_messages(message_id),
    channel_id BIGINT NOT NULL,
    author_id BIGINT NOT NULL REFERENCES discord_members(member_id),
    author_name TEXT NOT NULL,
    entry_number INTEGER,                    -- assigned at voting time
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(competition_slug, message_id)
);

CREATE INDEX IF NOT EXISTS idx_competition_entries_slug ON competition_entries(competition_slug);
CREATE INDEX IF NOT EXISTS idx_competition_entries_author ON competition_entries(author_id);

-- RLS
ALTER TABLE competitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE competition_entries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role can access all competitions" ON competitions
    FOR ALL USING (true);
CREATE POLICY "Service role can access all competition_entries" ON competition_entries
    FOR ALL USING (true);
