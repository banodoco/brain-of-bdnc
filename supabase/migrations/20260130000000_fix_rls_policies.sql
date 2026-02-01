-- Migration: Fix RLS policies to allow read-only public access
-- 
-- Problem: Existing policies use USING(true) FOR ALL, which allows anyone to
-- SELECT, INSERT, UPDATE, and DELETE. This is a security risk.
--
-- Solution: Replace with SELECT-only policies for public data tables.
-- Service role bypasses RLS entirely, so it still has full access.

-- ============================================================
-- Discord Messages - Allow read, block write
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all discord_messages" ON discord_messages;

CREATE POLICY "Allow public read access to messages" ON discord_messages
    FOR SELECT USING (true);

-- No INSERT/UPDATE/DELETE policies = anon/authenticated cannot modify

-- ============================================================
-- Discord Members - Allow read, block write
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all discord_members" ON discord_members;

CREATE POLICY "Allow public read access to members" ON discord_members
    FOR SELECT USING (true);

-- ============================================================
-- Discord Channels - Allow read, block write
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all discord_channels" ON discord_channels;

CREATE POLICY "Allow public read access to channels" ON discord_channels
    FOR SELECT USING (true);

-- ============================================================
-- Daily Summaries - Allow read, block write
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all daily_summaries" ON daily_summaries;

CREATE POLICY "Allow public read access to daily_summaries" ON daily_summaries
    FOR SELECT USING (true);

-- ============================================================
-- Channel Summary - Allow read, block write
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all channel_summary" ON channel_summary;

CREATE POLICY "Allow public read access to channel_summary" ON channel_summary
    FOR SELECT USING (true);

-- ============================================================
-- Shared Posts - Internal only, no public access
-- (Contains platform post IDs that could be used for abuse)
-- ============================================================
ALTER TABLE shared_posts ENABLE ROW LEVEL SECURITY;

-- No policy = no public access

-- ============================================================
-- Sync Status - Internal only, no public access
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all sync_status" ON sync_status;

-- No policy = no public access (service role still has access via bypass)

-- ============================================================
-- System Logs - Internal only, no public access
-- ============================================================
DROP POLICY IF EXISTS "Service role can access all system_logs" ON system_logs;

-- No policy = no public access
