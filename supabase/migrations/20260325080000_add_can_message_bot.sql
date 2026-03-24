-- Allow explicitly approved members to message the Discord bot.

ALTER TABLE members
    ADD COLUMN IF NOT EXISTS can_message_bot BOOLEAN DEFAULT FALSE;

UPDATE members
SET can_message_bot = TRUE
WHERE member_id = 1485999704696164502;
