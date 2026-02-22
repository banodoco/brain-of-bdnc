-- Add is_speaker column to discord_members.
-- TRUE = member should have the Speaker role (default for everyone).
-- FALSE = member is muted (Speaker role intentionally removed via /mute).
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS is_speaker BOOLEAN DEFAULT TRUE;
