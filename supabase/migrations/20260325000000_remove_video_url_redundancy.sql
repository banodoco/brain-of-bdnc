-- Remove submissions.video_url redundancy.
-- media.url is the canonical URL; the submission_details view exposes it as video_url.

-- Step 1: Update submission_details view to source video_url from media.url
DROP VIEW IF EXISTS public.submission_details;
CREATE VIEW public.submission_details AS
SELECT
    s.id,
    s.competition_id,
    s.user_id,
    s.media_id,
    s.theme,
    med.url AS video_url,
    s.title,
    s.description,
    s.tools_used,
    s.thumbnail_url,
    s.duration_seconds,
    s.additional_links,
    s.status,
    s.admin_notes,
    s.score,
    s.vote_count,
    s.winner,
    s.submitted_at,
    s.created_at,
    s.updated_at,
    public.get_verified_vote_count(s.id) AS verified_vote_count,
    public.get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS verified_weighted_vote_count,
    jsonb_build_object(
        'id', m.auth_user_id,
        'discord_id', m.member_id::TEXT,
        'discord_username', m.username,
        'discord_discriminator', m.discriminator,
        'display_name', COALESCE(m.global_name, m.username),
        'avatar_url', COALESCE(m.stored_avatar_url, m.avatar_url),
        'bio', m.bio,
        'real_name', m.real_name,
        'website_url', m.website_url,
        'instagram_url', m.instagram_url,
        'twitter_url', m.twitter_url,
        'discord_account_created_at', m.discord_created_at,
        'banodoco_owner', COALESCE(m.banodoco_owner, FALSE)
    ) AS profile
FROM public.submissions s
JOIN public.media med ON med.id = s.media_id
LEFT JOIN public.members m ON m.auth_user_id = s.user_id
WHERE s.status <> 'rejected';

GRANT SELECT ON public.submission_details TO anon, authenticated;

-- Step 2: Drop the redundant column
ALTER TABLE public.submissions DROP COLUMN IF EXISTS video_url;
