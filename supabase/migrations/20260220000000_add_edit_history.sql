-- Add edit_history column to discord_messages
-- Stores an ordered array of previous content versions, oldest first.
-- Each entry: {"content": "...", "edited_at": "<iso>|null", "recorded_at": "<iso>"}
-- The current/latest content remains in the existing `content` column.

ALTER TABLE discord_messages
    ADD COLUMN IF NOT EXISTS edit_history JSONB DEFAULT '[]'::jsonb;
