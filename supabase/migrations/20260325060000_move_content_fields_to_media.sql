-- Move content-descriptive fields from competition_entries to media where they belong.
-- Drop redundant/derivable columns. Drop dead competition_content table.

-- 1. Add tools_used and additional_links to media (title and description already exist)
ALTER TABLE public.media
    ADD COLUMN IF NOT EXISTS tools_used TEXT[] DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS additional_links JSONB;

-- 2. Copy data from prize entries to their media records
UPDATE public.media m SET
    title = COALESCE(m.title, ce.title),
    description = COALESCE(m.description, ce.description),
    tools_used = CASE WHEN COALESCE(array_length(m.tools_used, 1), 0) = 0 THEN ce.tools_used ELSE m.tools_used END,
    additional_links = COALESCE(m.additional_links, ce.additional_links)
FROM public.competition_entries ce
WHERE ce.media_id = m.id
  AND ce.entry_type = 'prize';

-- 3. Drop views that reference columns being removed
DROP VIEW IF EXISTS public.submission_details CASCADE;
DROP VIEW IF EXISTS public.public_vote_counts CASCADE;

-- 3b. Drop RLS policies that reference entry_type column
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT policyname FROM pg_policies
             WHERE schemaname = 'public' AND tablename = 'competition_entries'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.competition_entries', r.policyname);
    END LOOP;
END;
$$;

-- 4. Drop redundant columns from competition_entries
ALTER TABLE public.competition_entries
    DROP COLUMN IF EXISTS title,
    DROP COLUMN IF EXISTS description,
    DROP COLUMN IF EXISTS tools_used,
    DROP COLUMN IF EXISTS additional_links,
    DROP COLUMN IF EXISTS thumbnail_url,
    DROP COLUMN IF EXISTS duration_seconds,
    DROP COLUMN IF EXISTS entry_type,
    DROP COLUMN IF EXISTS author_name,
    DROP COLUMN IF EXISTS channel_id;

-- 5. Drop dead table
DROP TABLE IF EXISTS public.competition_content;

-- 6. Recreate submission_details view - now JOINs media for content fields
CREATE VIEW public.submission_details AS
SELECT
    ce.id,
    ce.competition_id,
    m_auth.auth_user_id AS user_id,
    ce.media_id,
    ce.theme,
    med.url AS video_url,
    med.title,
    med.description,
    med.tools_used,
    med.additional_links,
    COALESCE(med.cloudflare_thumbnail_url, med.backup_thumbnail_url) AS thumbnail_url,
    ce.status,
    ce.admin_notes,
    ce.score,
    ce.vote_count,
    ce.winner,
    ce.submitted_at,
    ce.created_at,
    ce.updated_at,
    c.type AS competition_type,
    public.get_verified_vote_count(ce.id) AS verified_vote_count,
    public.get_verified_vote_count_with_judge_multiplier(ce.id, ce.competition_id) AS verified_weighted_vote_count,
    jsonb_build_object(
        'id', m_auth.auth_user_id,
        'discord_id', m_auth.member_id::TEXT,
        'discord_username', m_auth.username,
        'discord_discriminator', m_auth.discriminator,
        'display_name', COALESCE(m_auth.global_name, m_auth.username),
        'avatar_url', COALESCE(m_auth.stored_avatar_url, m_auth.avatar_url),
        'bio', m_auth.bio,
        'real_name', m_auth.real_name,
        'website_url', m_auth.website_url,
        'instagram_url', m_auth.instagram_url,
        'twitter_url', m_auth.twitter_url,
        'discord_account_created_at', m_auth.discord_created_at,
        'banodoco_owner', COALESCE(m_auth.banodoco_owner, FALSE)
    ) AS profile
FROM public.competition_entries ce
JOIN public.competitions c ON c.id = ce.competition_id
JOIN public.media med ON med.id = ce.media_id
LEFT JOIN public.members m_auth ON m_auth.member_id = ce.member_id
WHERE c.type = 'prize' AND ce.status <> 'rejected';

-- 7. Recreate public_vote_counts view
CREATE VIEW public.public_vote_counts AS
SELECT
    ce.id AS entry_id,
    ce.competition_id,
    med.title,
    m.auth_user_id AS creator_id,
    COALESCE(m.global_name, m.username) AS creator_name,
    ce.vote_count,
    public.get_vote_count_with_judge_multiplier(ce.id, ce.competition_id) AS weighted_vote_count,
    ce.status
FROM public.competition_entries ce
JOIN public.competitions c ON c.id = ce.competition_id
JOIN public.media med ON med.id = ce.media_id
LEFT JOIN public.members m ON m.member_id = ce.member_id
WHERE c.type = 'prize';

GRANT SELECT ON public.submission_details TO anon, authenticated;
GRANT SELECT ON public.public_vote_counts TO anon, authenticated;

-- 8. Drop the CHECK constraint for entry_type since column is gone
ALTER TABLE public.competition_entries DROP CONSTRAINT IF EXISTS chk_prize_requires_media;

-- 9. Recreate RLS on competition_entries using competitions.type instead of entry_type
ALTER TABLE public.competition_entries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Read visible entries" ON public.competition_entries FOR SELECT
    USING (
        status <> 'rejected'
        OR member_id IN (SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid())
        OR public.is_admin()
    );

CREATE POLICY "Users insert own prize entries" ON public.competition_entries FOR INSERT
    TO authenticated
    WITH CHECK (
        member_id IN (SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid())
        AND EXISTS (SELECT 1 FROM public.competitions c WHERE c.id = competition_id AND c.type = 'prize')
    );

CREATE POLICY "Users update own pending prize entries" ON public.competition_entries FOR UPDATE
    TO authenticated
    USING (
        member_id IN (SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid())
        AND EXISTS (SELECT 1 FROM public.competitions c WHERE c.id = competition_id AND c.type = 'prize')
        AND (status = 'submitted' OR public.is_admin())
    );

CREATE POLICY "Users delete own pending prize entries" ON public.competition_entries FOR DELETE
    TO authenticated
    USING (
        member_id IN (SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid())
        AND EXISTS (SELECT 1 FROM public.competitions c WHERE c.id = competition_id AND c.type = 'prize')
        AND (status = 'submitted' OR public.is_admin())
    );

GRANT SELECT ON public.competition_entries TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.competition_entries TO authenticated;
