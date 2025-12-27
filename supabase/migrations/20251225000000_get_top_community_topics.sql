-- Migration: Create function to get top community topics with media
-- Run this in Supabase SQL Editor or as a migration

CREATE OR REPLACE FUNCTION get_top_community_topics(target_date DATE DEFAULT CURRENT_DATE)
RETURNS TABLE (
  channel_id BIGINT,
  channel_name TEXT,
  topic_title TEXT,
  topic_main_text TEXT,
  topic_sub_topics JSONB,
  media_message_ids TEXT[],
  media_count INT
) 
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  WITH parsed_topics AS (
    -- Parse all topics from summaries, excluding the aggregate channel
    SELECT 
      ds.channel_id,
      dc.channel_name,
      topic->>'title' AS topic_title,
      topic->>'mainText' AS topic_main_text,
      topic->'subTopics' AS topic_sub_topics,
      -- Collect all media message IDs for this topic
      ARRAY_REMOVE(
        ARRAY[topic->>'mainMediaMessageId'] || 
        ARRAY(
          SELECT jsonb_array_elements_text(sub->'subTopicMediaMessageIds')
          FROM jsonb_array_elements(topic->'subTopics') AS sub
        ),
        NULL
      ) AS media_ids
    FROM daily_summaries ds
    JOIN discord_channels dc ON ds.channel_id = dc.channel_id
    CROSS JOIN LATERAL jsonb_array_elements(ds.full_summary::jsonb) AS topic
    WHERE ds.date = target_date
      AND ds.channel_id != 1138790297355174039  -- Exclude aggregate channel
  ),
  ranked_topics AS (
    -- Rank topics within each channel by media count
    SELECT 
      *,
      CARDINALITY(media_ids) AS media_count,
      ROW_NUMBER() OVER (
        PARTITION BY channel_id 
        ORDER BY CARDINALITY(media_ids) DESC
      ) AS rank_in_channel
    FROM parsed_topics
  )
  -- Get top topic per channel, sorted by media count, limit 3
  SELECT 
    rt.channel_id,
    rt.channel_name,
    rt.topic_title,
    rt.topic_main_text,
    rt.topic_sub_topics,
    rt.media_ids AS media_message_ids,
    rt.media_count::INT
  FROM ranked_topics rt
  WHERE rt.rank_in_channel = 1
  ORDER BY rt.media_count DESC
  LIMIT 3;
END;
$$;

-- Grant access to the function
GRANT EXECUTE ON FUNCTION get_top_community_topics(DATE) TO anon, authenticated, service_role;

