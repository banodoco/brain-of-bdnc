-- Migration script to add summary-related tables to Supabase
-- Run this in your Supabase SQL editor

-- Channel Summary Table (stores thread IDs for summary threads)
CREATE TABLE IF NOT EXISTS channel_summary (
    channel_id BIGINT PRIMARY KEY,
    summary_thread_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (channel_id) REFERENCES discord_channels(channel_id) ON DELETE CASCADE
);

-- Index for channel_summary
CREATE INDEX IF NOT EXISTS idx_channel_summary_thread_id ON channel_summary(summary_thread_id);
CREATE INDEX IF NOT EXISTS idx_channel_summary_updated_at ON channel_summary(updated_at);

-- Daily Summaries Table (stores generated summaries)
CREATE TABLE IF NOT EXISTS daily_summaries (
    daily_summary_id BIGSERIAL PRIMARY KEY,
    date DATE NOT NULL,
    channel_id BIGINT NOT NULL,
    full_summary TEXT,
    short_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (channel_id) REFERENCES discord_channels(channel_id) ON DELETE CASCADE,
    UNIQUE(date, channel_id)
);

-- Indexes for daily_summaries
CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON daily_summaries(date);
CREATE INDEX IF NOT EXISTS idx_daily_summaries_channel_id ON daily_summaries(channel_id);
CREATE INDEX IF NOT EXISTS idx_daily_summaries_date_channel ON daily_summaries(date, channel_id);

-- Enable RLS
ALTER TABLE channel_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_summaries ENABLE ROW LEVEL SECURITY;

-- Create policies for service role access
DROP POLICY IF EXISTS "Service role can access all channel_summary" ON channel_summary;
CREATE POLICY "Service role can access all channel_summary" ON channel_summary
    FOR ALL USING (true);

DROP POLICY IF EXISTS "Service role can access all daily_summaries" ON daily_summaries;
CREATE POLICY "Service role can access all daily_summaries" ON daily_summaries
    FOR ALL USING (true);

-- Create trigger to auto-update updated_at for channel_summary
CREATE TRIGGER update_channel_summary_updated_at BEFORE UPDATE ON channel_summary
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Update sync_status table
INSERT INTO sync_status (table_name, sync_status) 
VALUES 
    ('channel_summary', 'pending'),
    ('daily_summaries', 'pending')
ON CONFLICT (table_name) DO NOTHING;

