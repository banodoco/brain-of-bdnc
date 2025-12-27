-- Migration: Add tracking for items included in main summary
-- Created: 2025-12-27
-- Description: Adds columns to track which news items from channel summaries 
--              were included in the combined main summary

-- Add column to track which items were included in the main summary
-- This stores an array of message_ids that made it to the main summary
ALTER TABLE daily_summaries 
ADD COLUMN IF NOT EXISTS included_in_main_summary BOOLEAN DEFAULT FALSE;

-- For items that ARE in the main summary, we can also track which source items they came from
-- This is useful for the main summary record itself (stored with summary_channel_id)
ALTER TABLE daily_summaries 
ADD COLUMN IF NOT EXISTS source_message_ids JSONB DEFAULT '[]'::jsonb;

-- Add index for quick lookups of items included in main summary
CREATE INDEX IF NOT EXISTS idx_daily_summaries_included_in_main 
ON daily_summaries(date, included_in_main_summary) 
WHERE included_in_main_summary = TRUE;

-- Add comment explaining the columns
COMMENT ON COLUMN daily_summaries.included_in_main_summary IS 
'TRUE if this channel summary had items that were included in the combined main summary for the day';

COMMENT ON COLUMN daily_summaries.source_message_ids IS 
'For main summary records: array of message_ids from source channel summaries that were included. For channel summaries: array of this channel''s message_ids that made it to main summary.';

