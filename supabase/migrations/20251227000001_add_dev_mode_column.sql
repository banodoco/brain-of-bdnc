-- Migration: Add dev_mode column to daily_summaries
-- Created: 2025-12-27
-- Description: Track whether summaries were created in development mode

-- Add dev_mode column
ALTER TABLE daily_summaries 
ADD COLUMN IF NOT EXISTS dev_mode BOOLEAN DEFAULT FALSE;

-- Add index for dev mode records (useful for cleanup queries)
CREATE INDEX IF NOT EXISTS idx_daily_summaries_dev_mode 
ON daily_summaries(dev_mode) 
WHERE dev_mode = TRUE;

-- Add comment
COMMENT ON COLUMN daily_summaries.dev_mode IS 
'TRUE if this summary was created during development/testing. Useful for filtering and cleanup.';

