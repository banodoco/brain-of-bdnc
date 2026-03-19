-- Fix: reactors column is jsonb, not bigint[]
CREATE OR REPLACE FUNCTION sync_message_reactors()
RETURNS TRIGGER AS $$
DECLARE
    target_message_id bigint;
    reactor_ids jsonb;
    reactor_count int;
BEGIN
    target_message_id := COALESCE(NEW.message_id, OLD.message_id);

    SELECT
        COALESCE(jsonb_agg(DISTINCT user_id), '[]'::jsonb),
        COUNT(DISTINCT user_id)
    INTO reactor_ids, reactor_count
    FROM discord_reactions
    WHERE message_id = target_message_id
      AND removed_at IS NULL;

    UPDATE discord_messages
    SET reactors = reactor_ids,
        reaction_count = reactor_count
    WHERE message_id = target_message_id;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
