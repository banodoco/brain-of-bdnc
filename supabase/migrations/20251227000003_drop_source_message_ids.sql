-- Migration: Drop redundant source_message_ids column
-- Created: 2025-12-27
-- Description: Removes source_message_ids column as inclusion is now represented
--              at item/subtopic level inside full_summary JSON via included_in_main.

ALTER TABLE daily_summaries
DROP COLUMN IF EXISTS source_message_ids;


