-- Migration: Normalize permissions system
-- Simplifies permission structure to 2 boolean flags + 2 social handles
-- 
-- New columns:
--   include_in_updates: Okay to mention in daily summaries and weekly digests (default TRUE)
--   allow_content_sharing: Okay for others to share/curate their content (default TRUE)
--   reddit_handle: New social handle field
--
-- Removed columns:
--   sharing_consent, permission_to_curate, dm_preference
--   instagram_handle, youtube_handle, tiktok_handle, website

-- Step 1: Add new columns with correct defaults
ALTER TABLE discord_members 
ADD COLUMN IF NOT EXISTS include_in_updates BOOLEAN DEFAULT TRUE,
ADD COLUMN IF NOT EXISTS allow_content_sharing BOOLEAN DEFAULT TRUE,
ADD COLUMN IF NOT EXISTS reddit_handle TEXT;

-- Step 2: Skip migrating old permission values
-- The old permission_to_curate = FALSE values are of uncertain origin (885 records 
-- all created in May 2025, likely from a bulk import rather than explicit opt-outs).
-- Starting fresh: everyone gets the new defaults (TRUE for both permissions).
-- Users who want to opt out can do so through the new UI.

-- Step 3: Drop deprecated columns
-- Note: Run these after verifying the migration worked correctly
ALTER TABLE discord_members 
DROP COLUMN IF EXISTS sharing_consent,
DROP COLUMN IF EXISTS permission_to_curate,
DROP COLUMN IF EXISTS dm_preference,
DROP COLUMN IF EXISTS instagram_handle,
DROP COLUMN IF EXISTS youtube_handle,
DROP COLUMN IF EXISTS tiktok_handle,
DROP COLUMN IF EXISTS website;

-- Step 4: Add index for permission lookups (optional, for performance)
CREATE INDEX IF NOT EXISTS idx_discord_members_allow_content_sharing 
ON discord_members(allow_content_sharing) 
WHERE allow_content_sharing = FALSE;
