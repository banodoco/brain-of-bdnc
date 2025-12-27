-- Migration: Drop redundant media_urls column
-- Created: 2025-12-27
-- Description: Removes media_urls column as the same data is now embedded 
--              directly in full_summary JSON at the item/subtopic level
--              as mainMediaUrls and subTopicMediaUrls

-- Drop the redundant column
ALTER TABLE daily_summaries 
DROP COLUMN IF EXISTS media_urls;

-- Note: The media URLs are now stored directly in the full_summary JSON:
-- - items[].mainMediaUrls - persisted URLs for mainMediaMessageId
-- - items[].subTopics[].subTopicMediaUrls - persisted URLs for subTopicMediaMessageIds

