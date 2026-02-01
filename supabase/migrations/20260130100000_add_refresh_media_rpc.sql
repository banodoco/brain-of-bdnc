-- RPC function to get message data for URL refresh
-- Casts BIGINT columns to TEXT to avoid JavaScript precision loss
-- (JavaScript numbers lose precision for values > 2^53)

CREATE OR REPLACE FUNCTION get_message_for_refresh(p_message_id TEXT)
RETURNS TABLE (
    message_id TEXT,
    channel_id TEXT,
    thread_id TEXT,
    attachments JSONB
)
LANGUAGE sql
SECURITY DEFINER
AS $$
    SELECT 
        message_id::TEXT,
        channel_id::TEXT,
        thread_id::TEXT,
        attachments
    FROM discord_messages
    WHERE message_id = p_message_id::BIGINT;
$$;

-- Grant execute to authenticated and anon roles
GRANT EXECUTE ON FUNCTION get_message_for_refresh(TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION get_message_for_refresh(TEXT) TO anon;
GRANT EXECUTE ON FUNCTION get_message_for_refresh(TEXT) TO service_role;
