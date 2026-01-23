-- Migration: Add shared posts tracking for post-share notifications
-- 
-- Enables:
--   1. Tracking when a user was first shared (to send one-time notification)
--   2. Recording shared posts with platform post IDs (to enable deletion)

-- Add first_shared_at to track when user's content was first shared
ALTER TABLE discord_members 
ADD COLUMN IF NOT EXISTS first_shared_at TIMESTAMPTZ;

-- Create shared_posts table to track individual shares
CREATE TABLE IF NOT EXISTS shared_posts (
    id SERIAL PRIMARY KEY,
    discord_message_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    platform TEXT NOT NULL,  -- 'twitter', 'instagram', etc.
    platform_post_id TEXT NOT NULL,  -- e.g., tweet ID
    platform_post_url TEXT,  -- full URL to the post
    shared_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,  -- NULL = still live, timestamp = when deleted
    delete_eligible_until TIMESTAMPTZ,  -- when delete option expires (6 hours after share)
    UNIQUE(discord_message_id, platform)
);

-- Index for looking up posts by user (for potential future "delete all my posts" feature)
CREATE INDEX IF NOT EXISTS idx_shared_posts_user ON shared_posts(discord_user_id);

-- Index for looking up by platform post ID (for delete operations)
CREATE INDEX IF NOT EXISTS idx_shared_posts_platform_id ON shared_posts(platform, platform_post_id);
