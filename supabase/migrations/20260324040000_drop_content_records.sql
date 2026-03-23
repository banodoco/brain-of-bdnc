DROP VIEW IF EXISTS public.ag_submission_details;

DROP TRIGGER IF EXISTS trg_ag_submissions_sync_content_record ON public.ag_submissions;
DROP TRIGGER IF EXISTS trg_ag_submissions_delete_content_record ON public.ag_submissions;

DROP FUNCTION IF EXISTS public.ag_sync_submission_content_record();
DROP FUNCTION IF EXISTS public.ag_delete_submission_content_record();

UPDATE public.ag_submissions
SET content_record_id = NULL
WHERE content_record_id IS NOT NULL;

ALTER TABLE public.ag_submissions
    DROP CONSTRAINT IF EXISTS ag_submissions_content_record_id_fkey;

ALTER TABLE public.ag_submissions
    DROP COLUMN IF EXISTS content_record_id;

DROP TABLE IF EXISTS public.content_records;

CREATE VIEW public.ag_submission_details AS
SELECT
    s.id,
    s.competition_id,
    s.user_id,
    s.media_id,
    s.theme,
    s.video_url,
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
    public.ag_get_verified_vote_count(s.id) AS verified_vote_count,
    public.ag_get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS verified_weighted_vote_count,
    jsonb_build_object(
        'id', aui.auth_user_id,
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
FROM public.ag_submissions s
LEFT JOIN public.ag_user_identities aui ON aui.auth_user_id = s.user_id
LEFT JOIN public.members m ON m.member_id = aui.member_id
WHERE s.status <> 'rejected';

GRANT SELECT ON public.ag_submission_details TO anon, authenticated;
