ALTER TABLE discord_messages ADD COLUMN is_bot BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill: mark existing bot messages using discord_members.bot
UPDATE discord_messages
SET is_bot = TRUE
WHERE author_id IN (
    SELECT member_id FROM discord_members WHERE bot = TRUE
);
