-- Banodoco schema redesign:
-- - remove ag_ prefixes from AG tables, views, and functions
-- - unify ag_competitions + discord_competitions into competitions
-- - absorb ag_user_identities into members.auth_user_id
-- - require submissions.media_id
-- - move competition linkage to unified competition_id columns + competition_content join table

ALTER TABLE public.members
    ADD COLUMN IF NOT EXISTS auth_user_id UUID;

UPDATE public.members AS m
SET auth_user_id = aui.auth_user_id
FROM public.ag_user_identities AS aui
WHERE aui.member_id = m.member_id
  AND m.auth_user_id IS DISTINCT FROM aui.auth_user_id;

ALTER TABLE public.members
    DROP CONSTRAINT IF EXISTS members_auth_user_id_fkey;

ALTER TABLE public.members
    ADD CONSTRAINT members_auth_user_id_fkey
    FOREIGN KEY (auth_user_id)
    REFERENCES auth.users(id)
    ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_members_auth_user_id
    ON public.members(auth_user_id)
    WHERE auth_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.competitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL CHECK (type IN ('prize', 'community')),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    description TEXT,
    start_date TIMESTAMPTZ,
    end_date TIMESTAMPTZ,
    submission_start TIMESTAMPTZ,
    submission_end TIMESTAMPTZ,
    voting_start TIMESTAMPTZ,
    voting_end TIMESTAMPTZ,
    results_announced_at TIMESTAMPTZ,
    themes_announced_at TIMESTAMPTZ,
    theme TEXT,
    themes JSONB DEFAULT '[]'::jsonb,
    prizes JSONB,
    rules TEXT,
    settings JSONB DEFAULT '{}'::jsonb,
    is_active BOOLEAN DEFAULT FALSE,
    guild_id BIGINT REFERENCES public.server_config(guild_id),
    channel_id BIGINT,
    voting_channel_id BIGINT,
    voting_hours INTEGER,
    min_join_weeks INTEGER,
    voting_header TEXT,
    status TEXT DEFAULT 'setup',
    voting_started_at TIMESTAMPTZ,
    questions_thread_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_competitions_prize_slug
    ON public.competitions(slug)
    WHERE type = 'prize';

CREATE UNIQUE INDEX IF NOT EXISTS idx_competitions_guild_slug
    ON public.competitions(guild_id, slug)
    WHERE guild_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_competitions_type
    ON public.competitions(type);

CREATE INDEX IF NOT EXISTS idx_competitions_active
    ON public.competitions(is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_competitions_guild_status
    ON public.competitions(guild_id, status)
    WHERE guild_id IS NOT NULL;

INSERT INTO public.competitions (
    id,
    type,
    name,
    slug,
    description,
    start_date,
    end_date,
    submission_start,
    submission_end,
    voting_start,
    voting_end,
    results_announced_at,
    themes_announced_at,
    theme,
    themes,
    prizes,
    rules,
    settings,
    is_active,
    created_at,
    updated_at
)
SELECT
    ac.id,
    'prize',
    ac.name,
    ac.slug,
    ac.description,
    ac.start_date,
    ac.end_date,
    ac.submission_start,
    ac.submission_end,
    ac.voting_start,
    ac.voting_end,
    ac.results_announced_at,
    ac.themes_announced_at,
    ac.theme,
    ac.themes,
    ac.prizes,
    ac.rules,
    ac.settings,
    ac.is_active,
    ac.created_at,
    ac.updated_at
FROM public.ag_competitions AS ac
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.competitions (
    type,
    name,
    slug,
    guild_id,
    channel_id,
    voting_channel_id,
    voting_hours,
    min_join_weeks,
    voting_header,
    status,
    voting_start,
    voting_started_at,
    voting_end,
    questions_thread_id,
    created_at,
    updated_at
)
SELECT
    'community',
    dc.name,
    dc.slug,
    dc.guild_id,
    dc.channel_id,
    dc.voting_channel_id,
    dc.voting_hours,
    dc.min_join_weeks,
    dc.voting_header,
    dc.status,
    dc.voting_starts_at,
    dc.voting_started_at,
    dc.voting_ends_at,
    dc.questions_thread_id,
    dc.created_at,
    COALESCE(dc.voting_started_at, dc.voting_ends_at, dc.created_at)
FROM public.discord_competitions AS dc
ON CONFLICT DO NOTHING;

WITH inserted_submission_media AS (
    INSERT INTO public.media (
        id,
        member_id,
        url,
        type,
        classification,
        created_at,
        metadata
    )
    SELECT
        gen_random_uuid(),
        m.member_id,
        s.video_url,
        'external_video',
        'submission',
        s.created_at,
        jsonb_build_object('submission_id', s.id)
    FROM public.ag_submissions AS s
    JOIN public.members AS m
      ON m.auth_user_id = s.user_id
    WHERE s.video_url IS NOT NULL
      AND s.media_id IS NULL
    RETURNING id, metadata
)
UPDATE public.ag_submissions AS s
SET media_id = ism.id
FROM inserted_submission_media AS ism
WHERE (ism.metadata->>'submission_id')::UUID = s.id;

DROP VIEW IF EXISTS public.ag_fraud_detection_summary CASCADE;
DROP VIEW IF EXISTS public.ag_votes_needing_review CASCADE;
DROP VIEW IF EXISTS public.ag_votes_with_confidence CASCADE;
DROP VIEW IF EXISTS public.ag_admin_fraud_dashboard CASCADE;
DROP VIEW IF EXISTS public.ag_submission_analytics_summary CASCADE;
DROP VIEW IF EXISTS public.ag_submission_votes_with_judges CASCADE;
DROP VIEW IF EXISTS public.ag_competition_leaderboard CASCADE;
DROP VIEW IF EXISTS public.ag_public_vote_counts CASCADE;
DROP VIEW IF EXISTS public.ag_submission_details CASCADE;
DROP VIEW IF EXISTS public.ag_profiles CASCADE;

DROP TRIGGER IF EXISTS trg_ag_competitions_updated_at ON public.ag_competitions;
DROP TRIGGER IF EXISTS trg_ag_submissions_updated_at ON public.ag_submissions;
DROP TRIGGER IF EXISTS trg_ag_submission_analytics_updated_at ON public.ag_submission_analytics;
DROP TRIGGER IF EXISTS trg_ag_votes_capture_ip ON public.ag_votes;
DROP TRIGGER IF EXISTS trg_ag_submission_analytics_capture_ip ON public.ag_submission_analytics;
DROP TRIGGER IF EXISTS trg_ag_votes_prevent_self_voting ON public.ag_votes;
DROP TRIGGER IF EXISTS trg_ag_votes_enforce_max_votes ON public.ag_votes;
DROP TRIGGER IF EXISTS trg_ag_votes_update_submission_vote_count ON public.ag_votes;
DROP TRIGGER IF EXISTS trg_ag_user_identities_updated_at ON public.ag_user_identities;
DROP TRIGGER IF EXISTS trg_discord_members_ag_owner_flag ON public.members;

-- Drop ALL RLS policies on ag_ tables before dropping functions they reference
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT policyname, tablename
        FROM pg_policies
        WHERE schemaname = 'public'
          AND (tablename LIKE 'ag_%' OR policyname LIKE '%AG %' OR policyname LIKE '%ag_%')
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', r.policyname, r.tablename);
    END LOOP;
END;
$$;

DROP FUNCTION IF EXISTS public.ag_update_profile(JSONB);
DROP FUNCTION IF EXISTS public.ag_is_admin(UUID);
DROP FUNCTION IF EXISTS public.ag_get_fraud_config(TEXT);
DROP FUNCTION IF EXISTS public.ag_prevent_self_voting();
DROP FUNCTION IF EXISTS public.ag_enforce_max_votes();
DROP FUNCTION IF EXISTS public.ag_update_submission_vote_count();
DROP FUNCTION IF EXISTS public.ag_calculate_vote_confidence(UUID);
DROP FUNCTION IF EXISTS public.ag_get_verified_vote_count(UUID);
DROP FUNCTION IF EXISTS public.ag_get_vote_count_with_judge_multiplier(UUID, UUID);
DROP FUNCTION IF EXISTS public.ag_get_verified_vote_count_with_judge_multiplier(UUID, UUID);
DROP FUNCTION IF EXISTS public.ag_update_fraud_config(TEXT, JSONB);
DROP FUNCTION IF EXISTS public.ag_add_admin(UUID);
DROP FUNCTION IF EXISTS public.ag_remove_admin(UUID);
DROP FUNCTION IF EXISTS public.ag_link_auth_identity(UUID, BIGINT, TEXT);
DROP FUNCTION IF EXISTS public.ag_sync_submission_content_record();
DROP FUNCTION IF EXISTS public.ag_delete_submission_content_record();

ALTER FUNCTION public.ag_get_banodoco_owner_ids() RENAME TO get_banodoco_owner_ids;
ALTER FUNCTION public.ag_is_banodoco_owner(BIGINT) RENAME TO is_banodoco_owner;
ALTER FUNCTION public.ag_apply_banodoco_owner_flag() RENAME TO apply_banodoco_owner_flag;
ALTER FUNCTION public.ag_extract_discord_created_at(TEXT) RENAME TO extract_discord_created_at;
ALTER FUNCTION public.ag_hash_ip_address(TEXT) RENAME TO hash_ip_address;
ALTER FUNCTION public.ag_capture_vote_ip() RENAME TO capture_vote_ip;
ALTER FUNCTION public.ag_capture_analytics_ip() RENAME TO capture_analytics_ip;

ALTER TABLE public.ag_submissions RENAME TO submissions;
ALTER TABLE public.ag_votes RENAME TO votes;
ALTER TABLE public.ag_submission_analytics RENAME TO submission_analytics;
ALTER TABLE public.ag_admin_users RENAME TO admin_users;
ALTER TABLE public.ag_fraud_detection_config RENAME TO fraud_config;
ALTER TABLE public.ag_vote_reviews RENAME TO vote_reviews;

ALTER TABLE public.submissions
    DROP CONSTRAINT IF EXISTS ag_submissions_competition_id_fkey;

ALTER TABLE public.submissions
    DROP CONSTRAINT IF EXISTS ag_submissions_media_id_fkey;

ALTER TABLE public.submissions
    ADD CONSTRAINT submissions_competition_id_fkey
    FOREIGN KEY (competition_id)
    REFERENCES public.competitions(id)
    ON DELETE CASCADE;

ALTER TABLE public.submissions
    ADD CONSTRAINT submissions_media_id_fkey
    FOREIGN KEY (media_id)
    REFERENCES public.media(id);

ALTER TABLE public.submissions
    ALTER COLUMN media_id SET NOT NULL;

ALTER TABLE public.competition_entries
    ADD COLUMN IF NOT EXISTS competition_id UUID;

UPDATE public.competition_entries AS ce
SET competition_id = c.id
FROM public.competitions AS c
WHERE c.type = 'community'
  AND c.guild_id = ce.guild_id
  AND c.slug = ce.competition_slug;

ALTER TABLE public.competition_entries
    ALTER COLUMN competition_id SET NOT NULL;

ALTER TABLE public.competition_entries
    ADD CONSTRAINT competition_entries_competition_id_fkey
    FOREIGN KEY (competition_id)
    REFERENCES public.competitions(id)
    ON DELETE CASCADE;

ALTER TABLE public.competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_guild_slug_fkey;

ALTER TABLE public.competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_guild_id_fkey;

ALTER TABLE public.competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_guild_slug_message_key;

ALTER TABLE public.competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_competition_slug_fkey;

ALTER TABLE public.competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_competition_slug_message_id_key;

DROP INDEX IF EXISTS public.idx_competition_entries_guild_slug;
DROP INDEX IF EXISTS public.idx_competition_entries_slug;

ALTER TABLE public.competition_entries
    DROP COLUMN IF EXISTS competition_slug;

ALTER TABLE public.competition_entries
    DROP COLUMN IF EXISTS guild_id;

ALTER TABLE public.competition_entries
    ADD CONSTRAINT competition_entries_competition_message_key
    UNIQUE (competition_id, message_id);

CREATE INDEX IF NOT EXISTS idx_competition_entries_competition_id
    ON public.competition_entries(competition_id);

ALTER TABLE public.media
    ADD COLUMN IF NOT EXISTS competition_id UUID;

UPDATE public.media
SET competition_id = ag_competition_id
WHERE ag_competition_id IS NOT NULL
  AND competition_id IS NULL;

UPDATE public.media AS m
SET competition_id = c.id
FROM public.competitions AS c
WHERE c.type = 'community'
  AND c.guild_id = m.competition_guild_id
  AND c.slug = m.competition_slug
  AND m.competition_guild_id IS NOT NULL
  AND m.competition_slug IS NOT NULL;

ALTER TABLE public.media
    DROP CONSTRAINT IF EXISTS media_competition_id_fkey;

ALTER TABLE public.media
    ADD CONSTRAINT media_competition_id_fkey
    FOREIGN KEY (competition_id)
    REFERENCES public.competitions(id)
    ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_media_competition_id
    ON public.media(competition_id)
    WHERE competition_id IS NOT NULL;

ALTER TABLE public.media
    DROP CONSTRAINT IF EXISTS media_ag_competition_id_fkey;

ALTER TABLE public.media
    DROP CONSTRAINT IF EXISTS fk_media_discord_competition;

ALTER TABLE public.media
    DROP CONSTRAINT IF EXISTS chk_media_discord_competition_pair;

DROP INDEX IF EXISTS public.idx_media_ag_competition_id;
DROP INDEX IF EXISTS public.idx_media_discord_competition;

ALTER TABLE public.media
    DROP COLUMN IF EXISTS ag_competition_id,
    DROP COLUMN IF EXISTS competition_guild_id,
    DROP COLUMN IF EXISTS competition_slug;

ALTER TABLE public.assets
    ADD COLUMN IF NOT EXISTS competition_id UUID;

UPDATE public.assets
SET competition_id = ag_competition_id
WHERE ag_competition_id IS NOT NULL
  AND competition_id IS NULL;

UPDATE public.assets AS a
SET competition_id = c.id
FROM public.competitions AS c
WHERE c.type = 'community'
  AND c.guild_id = a.competition_guild_id
  AND c.slug = a.competition_slug
  AND a.competition_guild_id IS NOT NULL
  AND a.competition_slug IS NOT NULL;

ALTER TABLE public.assets
    DROP CONSTRAINT IF EXISTS assets_competition_id_fkey;

ALTER TABLE public.assets
    ADD CONSTRAINT assets_competition_id_fkey
    FOREIGN KEY (competition_id)
    REFERENCES public.competitions(id)
    ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_assets_competition_id
    ON public.assets(competition_id)
    WHERE competition_id IS NOT NULL;

ALTER TABLE public.assets
    DROP CONSTRAINT IF EXISTS assets_ag_competition_id_fkey;

ALTER TABLE public.assets
    DROP CONSTRAINT IF EXISTS fk_assets_discord_competition;

ALTER TABLE public.assets
    DROP CONSTRAINT IF EXISTS chk_assets_discord_competition_pair;

DROP INDEX IF EXISTS public.idx_assets_ag_competition_id;
DROP INDEX IF EXISTS public.idx_assets_discord_competition;

ALTER TABLE public.assets
    DROP COLUMN IF EXISTS ag_competition_id,
    DROP COLUMN IF EXISTS competition_guild_id,
    DROP COLUMN IF EXISTS competition_slug;

CREATE TABLE IF NOT EXISTS public.competition_content (
    competition_id UUID NOT NULL REFERENCES public.competitions(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL CHECK (content_type IN ('media', 'asset')),
    content_id UUID NOT NULL,
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (competition_id, content_type, content_id)
);

CREATE INDEX IF NOT EXISTS idx_competition_content_content
    ON public.competition_content(content_type, content_id);

CREATE OR REPLACE FUNCTION public.is_admin(check_user_id UUID DEFAULT NULL)
RETURNS BOOLEAN
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    IF check_user_id IS NULL THEN
        check_user_id := auth.uid();
    END IF;

    RETURN EXISTS (
        SELECT 1
        FROM public.admin_users
        WHERE user_id = check_user_id
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.get_fraud_config(p_key TEXT)
RETURNS JSONB
LANGUAGE sql
STABLE
AS $$
    SELECT config_value
    FROM public.fraud_config
    WHERE config_key = p_key;
$$;

CREATE OR REPLACE FUNCTION public.update_profile(p_profile JSONB DEFAULT '{}'::jsonb)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
DECLARE
    v_member_id BIGINT;
BEGIN
    SELECT member_id
    INTO v_member_id
    FROM public.members
    WHERE auth_user_id = auth.uid();

    IF v_member_id IS NULL THEN
        RAISE EXCEPTION 'No linked member profile';
    END IF;

    UPDATE public.members AS m
    SET
        bio = CASE
            WHEN p_profile ? 'bio' THEN NULLIF(BTRIM(p_profile->>'bio'), '')
            ELSE m.bio
        END,
        real_name = CASE
            WHEN p_profile ? 'real_name' THEN NULLIF(BTRIM(p_profile->>'real_name'), '')
            ELSE m.real_name
        END,
        website_url = CASE
            WHEN p_profile ? 'website_url' THEN NULLIF(BTRIM(p_profile->>'website_url'), '')
            ELSE m.website_url
        END,
        instagram_url = CASE
            WHEN p_profile ? 'instagram_url' THEN NULLIF(BTRIM(p_profile->>'instagram_url'), '')
            ELSE m.instagram_url
        END,
        twitter_url = CASE
            WHEN p_profile ? 'twitter_url' THEN NULLIF(BTRIM(p_profile->>'twitter_url'), '')
            ELSE m.twitter_url
        END,
        stored_avatar_url = CASE
            WHEN p_profile ? 'avatar_url' THEN NULLIF(BTRIM(p_profile->>'avatar_url'), '')
            ELSE m.stored_avatar_url
        END,
        updated_at = NOW()
    WHERE m.member_id = v_member_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.prevent_self_voting()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_submission_owner UUID;
BEGIN
    SELECT user_id
    INTO v_submission_owner
    FROM public.submissions
    WHERE id = NEW.submission_id;

    IF v_submission_owner = NEW.user_id THEN
        RAISE EXCEPTION 'You cannot vote for your own submission';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.enforce_max_votes()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_max_votes INTEGER;
    v_vote_count INTEGER;
BEGIN
    SELECT COALESCE((settings->>'max_votes')::INTEGER, 5)
    INTO v_max_votes
    FROM public.competitions
    WHERE id = NEW.competition_id;

    SELECT COUNT(*)
    INTO v_vote_count
    FROM public.votes
    WHERE user_id = NEW.user_id
      AND competition_id = NEW.competition_id;

    IF v_vote_count >= v_max_votes THEN
        RAISE EXCEPTION 'Maximum of % votes allowed per competition', v_max_votes;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_submission_vote_count()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
DECLARE
    v_submission_id UUID;
BEGIN
    v_submission_id := COALESCE(NEW.submission_id, OLD.submission_id);

    UPDATE public.submissions
    SET vote_count = (
        SELECT COUNT(*)
        FROM public.votes
        WHERE submission_id = v_submission_id
    )
    WHERE id = v_submission_id;

    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE OR REPLACE FUNCTION public.calculate_vote_confidence(p_vote_id UUID)
RETURNS INTEGER
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    confidence_score INTEGER := 100;
    v_user_id UUID;
    v_created_at TIMESTAMPTZ;
    v_vote_duration_ms INTEGER;
    v_ip_hash TEXT;
    v_user_agent TEXT;
    v_submission_id UUID;
    v_competition_id UUID;
    account_age_hours NUMERIC;
    votes_from_same_ip INTEGER;
    total_user_votes INTEGER;
    unique_creators_voted_for INTEGER;
    account_config JSONB;
    speed_config JSONB;
    ip_config JSONB;
    pattern_config JSONB;
    ua_config JSONB;
BEGIN
    account_config := public.get_fraud_config('account_age_thresholds');
    speed_config := public.get_fraud_config('vote_speed_thresholds');
    ip_config := public.get_fraud_config('ip_sharing_thresholds');
    pattern_config := public.get_fraud_config('voting_pattern_thresholds');
    ua_config := public.get_fraud_config('user_agent_penalty');

    SELECT
        user_id,
        created_at,
        vote_duration_ms,
        ip_hash,
        user_agent,
        submission_id,
        competition_id
    INTO
        v_user_id,
        v_created_at,
        v_vote_duration_ms,
        v_ip_hash,
        v_user_agent,
        v_submission_id,
        v_competition_id
    FROM public.votes
    WHERE id = p_vote_id;

    IF v_user_id IS NULL THEN
        RETURN 0;
    END IF;

    SELECT EXTRACT(EPOCH FROM (v_created_at - m.discord_created_at)) / 3600.0
    INTO account_age_hours
    FROM public.members AS m
    WHERE m.auth_user_id = v_user_id;

    IF account_age_hours IS NOT NULL THEN
        IF account_age_hours < (account_config->>'very_new_hours')::NUMERIC THEN
            confidence_score := confidence_score - (account_config->>'very_new_penalty')::INTEGER;
        ELSIF account_age_hours < (account_config->>'new_hours')::NUMERIC THEN
            confidence_score := confidence_score - (account_config->>'new_penalty')::INTEGER;
        ELSIF account_age_hours < (account_config->>'recent_hours')::NUMERIC THEN
            confidence_score := confidence_score - (account_config->>'recent_penalty')::INTEGER;
        END IF;
    END IF;

    IF v_vote_duration_ms IS NOT NULL THEN
        IF v_vote_duration_ms < (speed_config->>'instant_ms')::INTEGER THEN
            confidence_score := confidence_score - (speed_config->>'instant_penalty')::INTEGER;
        ELSIF v_vote_duration_ms < (speed_config->>'quick_ms')::INTEGER THEN
            confidence_score := confidence_score - (speed_config->>'quick_penalty')::INTEGER;
        END IF;
    END IF;

    IF v_ip_hash IS NOT NULL THEN
        SELECT COUNT(DISTINCT user_id)
        INTO votes_from_same_ip
        FROM public.votes
        WHERE ip_hash = v_ip_hash
          AND submission_id = v_submission_id;

        IF votes_from_same_ip >= (ip_config->>'high_risk_count')::INTEGER THEN
            confidence_score := confidence_score - (ip_config->>'high_risk_penalty')::INTEGER;
        ELSIF votes_from_same_ip >= (ip_config->>'medium_risk_count')::INTEGER THEN
            confidence_score := confidence_score - (ip_config->>'medium_risk_penalty')::INTEGER;
        END IF;
    END IF;

    SELECT
        COUNT(DISTINCT v2.submission_id),
        COUNT(DISTINCT s.user_id)
    INTO
        total_user_votes,
        unique_creators_voted_for
    FROM public.votes AS v2
    JOIN public.submissions AS s ON s.id = v2.submission_id
    WHERE v2.user_id = v_user_id
      AND v2.competition_id = v_competition_id;

    IF total_user_votes >= (pattern_config->>'min_votes_to_check')::INTEGER THEN
        IF unique_creators_voted_for = 1 THEN
            confidence_score := confidence_score - (pattern_config->>'single_creator_penalty')::INTEGER;
        ELSIF unique_creators_voted_for = 2 THEN
            confidence_score := confidence_score - (pattern_config->>'two_creators_penalty')::INTEGER;
        END IF;
    END IF;

    IF v_user_agent IS NULL OR v_user_agent = 'unknown' THEN
        confidence_score := confidence_score - (ua_config->>'missing_penalty')::INTEGER;
    END IF;

    IF confidence_score < 0 THEN
        confidence_score := 0;
    END IF;

    RETURN confidence_score;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_verified_vote_count(p_submission_id UUID)
RETURNS BIGINT
SECURITY DEFINER
SET search_path = public
LANGUAGE sql
STABLE
AS $$
    SELECT COUNT(v.id) FILTER (
        WHERE public.calculate_vote_confidence(v.id) >= 40
           OR EXISTS (
               SELECT 1
               FROM public.vote_reviews AS vr
               WHERE vr.vote_id = v.id
                 AND vr.is_legitimate = TRUE
           )
    )
    FROM public.votes AS v
    WHERE v.submission_id = p_submission_id
      AND NOT EXISTS (
          SELECT 1
          FROM public.vote_reviews AS vr
          WHERE vr.vote_id = v.id
            AND vr.is_legitimate = FALSE
      );
$$;

CREATE OR REPLACE FUNCTION public.get_vote_count_with_judge_multiplier(
    p_submission_id UUID,
    p_competition_id UUID DEFAULT NULL
)
RETURNS NUMERIC
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_competition_id UUID;
    v_judge_multiplier NUMERIC;
    v_weighted_count NUMERIC;
BEGIN
    IF p_competition_id IS NULL THEN
        SELECT competition_id
        INTO v_competition_id
        FROM public.submissions
        WHERE id = p_submission_id;
    ELSE
        v_competition_id := p_competition_id;
    END IF;

    SELECT COALESCE((settings->>'judge_multiplier')::NUMERIC, 1)
    INTO v_judge_multiplier
    FROM public.competitions
    WHERE id = v_competition_id;

    SELECT
        COUNT(v.id) FILTER (WHERE NOT COALESCE(m.banodoco_owner, FALSE))
        + (
            COUNT(v.id) FILTER (WHERE COALESCE(m.banodoco_owner, FALSE))
            * v_judge_multiplier
        )
    INTO v_weighted_count
    FROM public.votes AS v
    LEFT JOIN public.members AS m ON m.auth_user_id = v.user_id
    WHERE v.submission_id = p_submission_id;

    RETURN COALESCE(v_weighted_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION public.get_verified_vote_count_with_judge_multiplier(
    p_submission_id UUID,
    p_competition_id UUID DEFAULT NULL
)
RETURNS NUMERIC
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_competition_id UUID;
    v_judge_multiplier NUMERIC;
    v_weighted_count NUMERIC;
BEGIN
    IF p_competition_id IS NULL THEN
        SELECT competition_id
        INTO v_competition_id
        FROM public.submissions
        WHERE id = p_submission_id;
    ELSE
        v_competition_id := p_competition_id;
    END IF;

    SELECT COALESCE((settings->>'judge_multiplier')::NUMERIC, 1)
    INTO v_judge_multiplier
    FROM public.competitions
    WHERE id = v_competition_id;

    SELECT
        COUNT(v.id) FILTER (
            WHERE NOT COALESCE(m.banodoco_owner, FALSE)
              AND (
                  public.calculate_vote_confidence(v.id) >= 40
                  OR EXISTS (
                      SELECT 1
                      FROM public.vote_reviews AS vr
                      WHERE vr.vote_id = v.id
                        AND vr.is_legitimate = TRUE
                  )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.vote_reviews AS vr
                  WHERE vr.vote_id = v.id
                    AND vr.is_legitimate = FALSE
              )
        )
        + (
            COUNT(v.id) FILTER (
                WHERE COALESCE(m.banodoco_owner, FALSE)
                  AND (
                      public.calculate_vote_confidence(v.id) >= 40
                      OR EXISTS (
                          SELECT 1
                          FROM public.vote_reviews AS vr
                          WHERE vr.vote_id = v.id
                            AND vr.is_legitimate = TRUE
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.vote_reviews AS vr
                      WHERE vr.vote_id = v.id
                        AND vr.is_legitimate = FALSE
                  )
            ) * v_judge_multiplier
        )
    INTO v_weighted_count
    FROM public.votes AS v
    LEFT JOIN public.members AS m ON m.auth_user_id = v.user_id
    WHERE v.submission_id = p_submission_id;

    RETURN COALESCE(v_weighted_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION public.update_fraud_config(
    p_config_key TEXT,
    p_new_value JSONB
)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT public.is_admin() THEN
        RAISE EXCEPTION 'Only admins can update fraud detection config';
    END IF;

    UPDATE public.fraud_config
    SET
        config_value = p_new_value,
        updated_at = NOW(),
        updated_by = auth.uid()
    WHERE config_key = p_config_key;
END;
$$;

CREATE OR REPLACE FUNCTION public.add_admin(target_user_id UUID)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT public.is_admin() THEN
        RAISE EXCEPTION 'Only admins can grant admin access';
    END IF;

    INSERT INTO public.admin_users (user_id, granted_by)
    VALUES (target_user_id, auth.uid())
    ON CONFLICT (user_id) DO NOTHING;
END;
$$;

CREATE OR REPLACE FUNCTION public.remove_admin(target_user_id UUID)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT public.is_admin() THEN
        RAISE EXCEPTION 'Only admins can revoke admin access';
    END IF;

    DELETE FROM public.admin_users
    WHERE user_id = target_user_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
DECLARE
    discord_data JSONB;
    discord_id_text TEXT;
    discord_id_bigint BIGINT;
    username_value TEXT;
    display_name_value TEXT;
    avatar_url_value TEXT;
    discriminator_value TEXT;
BEGIN
    discord_data := COALESCE(NEW.raw_user_meta_data, '{}'::jsonb);

    discord_id_text := COALESCE(
        discord_data->>'sub',
        discord_data->>'id',
        discord_data->>'user_id',
        discord_data->>'provider_id'
    );

    IF discord_id_text IS NULL OR discord_id_text !~ '^[0-9]+$' THEN
        RETURN NEW;
    END IF;

    discord_id_bigint := discord_id_text::BIGINT;

    username_value := COALESCE(
        NULLIF(discord_data->>'username', ''),
        NULLIF(discord_data->>'preferred_username', ''),
        NULLIF(discord_data->>'full_name', ''),
        NULLIF(discord_data->>'name', ''),
        'discord-user-' || discord_id_text
    );

    display_name_value := COALESCE(
        NULLIF(discord_data->'custom_claims'->>'global_name', ''),
        NULLIF(discord_data->>'global_name', ''),
        NULLIF(discord_data->>'full_name', ''),
        NULLIF(discord_data->>'name', ''),
        NULLIF(discord_data->>'preferred_username', ''),
        NULLIF(discord_data->>'username', ''),
        username_value
    );

    avatar_url_value := COALESCE(
        NULLIF(discord_data->>'avatar_url', ''),
        NULLIF(discord_data->>'picture', ''),
        CASE
            WHEN NULLIF(discord_data->>'avatar', '') IS NOT NULL
                 AND discord_id_text IS NOT NULL
            THEN 'https://cdn.discordapp.com/avatars/' || discord_id_text || '/' || (discord_data->>'avatar') || '.png'
            ELSE NULL
        END
    );

    discriminator_value := NULLIF(discord_data->>'discriminator', '');

    INSERT INTO public.members (
        member_id,
        username,
        global_name,
        avatar_url,
        discriminator,
        discord_created_at,
        banodoco_owner,
        auth_user_id
    )
    VALUES (
        discord_id_bigint,
        username_value,
        display_name_value,
        avatar_url_value,
        discriminator_value,
        public.extract_discord_created_at(discord_id_text),
        public.is_banodoco_owner(discord_id_bigint),
        NEW.id
    )
    ON CONFLICT (member_id) DO UPDATE
    SET
        username = COALESCE(EXCLUDED.username, public.members.username),
        global_name = COALESCE(EXCLUDED.global_name, public.members.global_name),
        avatar_url = COALESCE(EXCLUDED.avatar_url, public.members.avatar_url),
        discriminator = COALESCE(EXCLUDED.discriminator, public.members.discriminator),
        discord_created_at = COALESCE(public.members.discord_created_at, EXCLUDED.discord_created_at),
        banodoco_owner = public.is_banodoco_owner(public.members.member_id),
        auth_user_id = COALESCE(public.members.auth_user_id, EXCLUDED.auth_user_id),
        updated_at = NOW();

    RETURN NEW;
EXCEPTION
    WHEN unique_violation THEN
        RETURN NEW;
    WHEN foreign_key_violation THEN
        RAISE WARNING 'handle_new_user foreign key failure for user %: %', NEW.id, SQLERRM;
        RETURN NEW;
    WHEN OTHERS THEN
        RAISE WARNING 'handle_new_user failed for user % (%): %', NEW.id, SQLSTATE, SQLERRM;
        RETURN NEW;
END;
$$;

CREATE OR REPLACE VIEW public.profiles AS
SELECT
    m.auth_user_id AS id,
    m.member_id::TEXT AS discord_id,
    m.username AS discord_username,
    m.discriminator AS discord_discriminator,
    COALESCE(m.global_name, m.username) AS display_name,
    COALESCE(m.stored_avatar_url, m.avatar_url) AS avatar_url,
    NULL::TEXT AS email,
    m.bio,
    m.real_name,
    m.website_url,
    m.instagram_url,
    m.twitter_url,
    m.discord_created_at AS discord_account_created_at,
    COALESCE(m.banodoco_owner, FALSE) AS banodoco_owner,
    m.created_at,
    m.updated_at
FROM public.members AS m
WHERE m.auth_user_id IS NOT NULL;

CREATE OR REPLACE VIEW public.submission_details AS
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
FROM public.submissions AS s
LEFT JOIN public.members AS m
  ON m.auth_user_id = s.user_id
WHERE s.status <> 'rejected';

CREATE OR REPLACE VIEW public.public_vote_counts AS
SELECT
    s.id AS submission_id,
    s.competition_id,
    s.title,
    s.user_id AS creator_id,
    COALESCE(m.global_name, m.username) AS creator_name,
    public.get_verified_vote_count(s.id) AS vote_count,
    public.get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS weighted_vote_count,
    s.status
FROM public.submissions AS s
LEFT JOIN public.members AS m
  ON m.auth_user_id = s.user_id
WHERE s.status <> 'rejected'
ORDER BY weighted_vote_count DESC;

CREATE OR REPLACE VIEW public.submission_votes_with_judges AS
SELECT
    s.id AS submission_id,
    s.title,
    s.competition_id,
    COUNT(v.id) AS total_votes,
    COUNT(v.id) FILTER (WHERE NOT COALESCE(m.banodoco_owner, FALSE)) AS regular_votes,
    COUNT(v.id) FILTER (WHERE COALESCE(m.banodoco_owner, FALSE)) AS judge_votes,
    COALESCE((c.settings->>'judge_multiplier')::NUMERIC, 1) AS judge_multiplier,
    public.get_vote_count_with_judge_multiplier(s.id, s.competition_id) AS weighted_vote_count,
    public.get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS verified_weighted_vote_count,
    public.get_verified_vote_count(s.id) AS verified_votes_no_multiplier,
    s.status
FROM public.submissions AS s
LEFT JOIN public.votes AS v ON v.submission_id = s.id
LEFT JOIN public.members AS m ON m.auth_user_id = v.user_id
LEFT JOIN public.competitions AS c ON c.id = s.competition_id
WHERE s.status <> 'rejected'
GROUP BY s.id, s.title, s.competition_id, c.settings, s.status
ORDER BY verified_weighted_vote_count DESC;

CREATE OR REPLACE VIEW public.submission_analytics_summary AS
SELECT
    s.id AS submission_id,
    s.title,
    s.user_id AS submission_owner_id,
    COUNT(DISTINCT sa.session_id) AS total_views,
    COUNT(DISTINCT sa.user_id) FILTER (WHERE sa.user_id IS NOT NULL) AS registered_user_views,
    COUNT(DISTINCT sa.session_id) FILTER (WHERE sa.user_id IS NULL) AS anonymous_views,
    AVG(sa.view_duration_seconds) AS avg_view_duration_seconds,
    COUNT(*) FILTER (WHERE sa.video_played = TRUE) AS video_play_count,
    AVG(sa.video_play_duration_seconds) FILTER (WHERE sa.video_played = TRUE) AS avg_video_watch_duration,
    COUNT(*) FILTER (WHERE sa.video_completed = TRUE) AS video_completion_count
FROM public.submissions AS s
LEFT JOIN public.submission_analytics AS sa ON sa.submission_id = s.id
WHERE public.is_admin() OR s.user_id = auth.uid()
GROUP BY s.id, s.title, s.user_id;

CREATE OR REPLACE VIEW public.admin_fraud_dashboard AS
SELECT
    v.id AS vote_id,
    v.submission_id,
    s.title AS submission_title,
    s.user_id AS submission_owner_id,
    v.user_id AS voter_id,
    COALESCE(m.global_name, m.username) AS voter_name,
    m.username AS discord_username,
    v.created_at AS voted_at,
    v.vote_duration_ms,
    ROUND(EXTRACT(EPOCH FROM (v.created_at - m.discord_created_at)) / 3600.0, 1) AS voter_account_age_hours,
    public.calculate_vote_confidence(v.id) AS confidence_score,
    CASE
        WHEN public.calculate_vote_confidence(v.id) >= 80 THEN 'HIGH'
        WHEN public.calculate_vote_confidence(v.id) >= 60 THEN 'MEDIUM'
        WHEN public.calculate_vote_confidence(v.id) >= 40 THEN 'LOW'
        ELSE 'VERY_LOW'
    END AS confidence_level,
    v.ip_hash,
    v.user_agent,
    (
        SELECT COUNT(DISTINCT v2.user_id)
        FROM public.votes AS v2
        WHERE v2.ip_hash = v.ip_hash
    ) AS users_from_same_ip,
    (
        SELECT COUNT(*)
        FROM public.votes AS v2
        WHERE v2.ip_hash = v.ip_hash
          AND v2.submission_id = v.submission_id
    ) AS votes_from_same_ip_for_submission,
    vr.is_legitimate AS manually_reviewed,
    vr.reviewed_by,
    vr.review_notes,
    vr.reviewed_at
FROM public.votes AS v
JOIN public.submissions AS s ON s.id = v.submission_id
LEFT JOIN public.members AS m ON m.auth_user_id = v.user_id
LEFT JOIN public.vote_reviews AS vr ON vr.vote_id = v.id
WHERE public.is_admin()
ORDER BY v.created_at DESC;

CREATE OR REPLACE VIEW public.votes_with_confidence AS
SELECT
    vote_id,
    voter_id AS user_id,
    voter_name AS display_name,
    discord_username,
    submission_id,
    submission_title,
    submission_owner_id,
    voted_at,
    vote_duration_ms,
    confidence_score,
    CASE
        WHEN confidence_score >= 80 THEN 'HIGH - Legitimate'
        WHEN confidence_score >= 60 THEN 'MEDIUM - Probably OK'
        WHEN confidence_score >= 40 THEN 'LOW - Suspicious'
        ELSE 'VERY LOW - Likely Fraud'
    END AS confidence_level,
    voter_account_age_hours AS account_age_hours,
    ROUND(voter_account_age_hours / 24.0, 1) AS account_age_days,
    ip_hash,
    user_agent
FROM public.admin_fraud_dashboard;

CREATE OR REPLACE VIEW public.votes_needing_review AS
SELECT *
FROM public.votes_with_confidence
WHERE confidence_score < 60
ORDER BY confidence_score ASC, voted_at DESC;

CREATE OR REPLACE VIEW public.competition_leaderboard AS
SELECT
    s.id AS submission_id,
    s.title,
    s.user_id AS creator_id,
    COALESCE(m.global_name, m.username) AS creator_name,
    s.vote_count AS raw_votes,
    svj.verified_weighted_vote_count AS weighted_vote_count,
    public.get_verified_vote_count(s.id) AS verified_votes,
    RANK() OVER (ORDER BY s.vote_count DESC) AS raw_rank,
    RANK() OVER (ORDER BY svj.verified_weighted_vote_count DESC) AS weighted_rank,
    CASE
        WHEN ABS(
            RANK() OVER (ORDER BY s.vote_count DESC)
            - RANK() OVER (ORDER BY svj.verified_weighted_vote_count DESC)
        ) >= 3 THEN TRUE
        ELSE FALSE
    END AS ranking_discrepancy,
    CASE
        WHEN s.vote_count > 0 THEN ROUND((svj.verified_weighted_vote_count / s.vote_count::NUMERIC) * 100, 1)
        ELSE 0
    END AS avg_confidence_score
FROM public.submissions AS s
LEFT JOIN public.submission_votes_with_judges AS svj ON svj.submission_id = s.id
LEFT JOIN public.members AS m ON m.auth_user_id = s.user_id
WHERE s.status <> 'rejected'
ORDER BY weighted_rank ASC;

CREATE OR REPLACE VIEW public.fraud_detection_summary AS
SELECT
    COUNT(*) FILTER (WHERE users_from_same_ip >= 5) AS high_risk_ips,
    COUNT(DISTINCT voter_id) FILTER (WHERE confidence_score < 60) AS suspicious_users,
    COUNT(*) FILTER (WHERE votes_from_same_ip_for_submission >= 5) AS suspicious_time_clusters,
    COUNT(*) FILTER (WHERE voter_account_age_hours IS NOT NULL AND voter_account_age_hours < 24) AS votes_from_brand_new_accounts,
    COUNT(DISTINCT ip_hash) FILTER (WHERE confidence_score < 40) AS suspicious_signup_clusters,
    COUNT(DISTINCT submission_id) FILTER (WHERE voter_account_age_hours IS NOT NULL AND voter_account_age_hours < 24) AS submissions_with_new_account_votes,
    COUNT(DISTINCT submission_id) FILTER (WHERE confidence_score < 60) AS submissions_with_suspicious_activity
FROM public.admin_fraud_dashboard;

DROP POLICY IF EXISTS "Users can view their own media" ON public.media;
DROP POLICY IF EXISTS "Users can insert their own media" ON public.media;
DROP POLICY IF EXISTS "Users can update their own media" ON public.media;
DROP POLICY IF EXISTS "Users can delete their own media" ON public.media;

CREATE POLICY "Users can view their own media" ON public.media
    FOR SELECT
    USING (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
        OR TRUE
    );

CREATE POLICY "Users can insert their own media" ON public.media
    FOR INSERT
    WITH CHECK (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

CREATE POLICY "Users can update their own media" ON public.media
    FOR UPDATE
    USING (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

CREATE POLICY "Users can delete their own media" ON public.media
    FOR DELETE
    USING (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "Users can view their own assets" ON public.assets;
DROP POLICY IF EXISTS "Users can insert their own assets" ON public.assets;
DROP POLICY IF EXISTS "Users can update their own assets" ON public.assets;
DROP POLICY IF EXISTS "Users can delete their own assets" ON public.assets;

CREATE POLICY "Users can view their own assets" ON public.assets
    FOR SELECT
    USING (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
        OR TRUE
    );

CREATE POLICY "Users can insert their own assets" ON public.assets
    FOR INSERT
    WITH CHECK (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

CREATE POLICY "Users can update their own assets" ON public.assets
    FOR UPDATE
    USING (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

CREATE POLICY "Users can delete their own assets" ON public.assets
    FOR DELETE
    USING (
        member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS "Users can update their own asset media status" ON public.asset_media;
DROP POLICY IF EXISTS "Users can view their own asset_media" ON public.asset_media;
DROP POLICY IF EXISTS "Users can delete their own asset_media" ON public.asset_media;

CREATE POLICY "Users can update their own asset media status" ON public.asset_media
    FOR UPDATE
    USING (
        EXISTS (
            SELECT 1
            FROM public.assets AS a
            JOIN public.members AS m ON m.member_id = a.member_id
            WHERE a.id = public.asset_media.asset_id
              AND m.auth_user_id = auth.uid()
        )
    );

CREATE POLICY "Users can view their own asset_media" ON public.asset_media
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM public.assets AS a
            JOIN public.members AS m ON m.member_id = a.member_id
            WHERE a.id = public.asset_media.asset_id
              AND m.auth_user_id = auth.uid()
        )
        OR TRUE
    );

CREATE POLICY "Users can delete their own asset_media" ON public.asset_media
    FOR DELETE
    USING (
        EXISTS (
            SELECT 1
            FROM public.assets AS a
            JOIN public.members AS m ON m.member_id = a.member_id
            WHERE a.id = public.asset_media.asset_id
              AND m.auth_user_id = auth.uid()
        )
    );

ALTER TABLE public.competitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.competition_content ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public can read AG competitions" ON public.competitions;
DROP POLICY IF EXISTS "Admins can mutate AG competitions" ON public.competitions;

CREATE POLICY "Public read competitions" ON public.competitions
    FOR SELECT
    TO anon, authenticated
    USING (true);

CREATE POLICY "Admins mutate competitions" ON public.competitions
    FOR ALL
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS "Public can read visible AG submissions" ON public.submissions;
DROP POLICY IF EXISTS "Users can insert own AG submissions" ON public.submissions;
DROP POLICY IF EXISTS "Users can update own pending AG submissions" ON public.submissions;
DROP POLICY IF EXISTS "Users can delete own pending AG submissions" ON public.submissions;

CREATE POLICY "Public read visible submissions" ON public.submissions
    FOR SELECT
    TO anon, authenticated
    USING (status <> 'rejected' OR user_id = auth.uid() OR public.is_admin());

CREATE POLICY "Users insert own submissions" ON public.submissions
    FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users update own pending submissions" ON public.submissions
    FOR UPDATE
    TO authenticated
    USING ((auth.uid() = user_id AND status = 'submitted') OR public.is_admin())
    WITH CHECK ((auth.uid() = user_id AND status = 'submitted') OR public.is_admin());

CREATE POLICY "Users delete own pending submissions" ON public.submissions
    FOR DELETE
    TO authenticated
    USING ((auth.uid() = user_id AND status = 'submitted') OR public.is_admin());

DROP POLICY IF EXISTS "Users can read own AG votes" ON public.votes;
DROP POLICY IF EXISTS "Users can insert own AG votes" ON public.votes;
DROP POLICY IF EXISTS "Users can delete own AG votes" ON public.votes;

CREATE POLICY "Users read own votes" ON public.votes
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id OR public.is_admin());

CREATE POLICY "Users insert own votes" ON public.votes
    FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users delete own votes" ON public.votes
    FOR DELETE
    TO authenticated
    USING (auth.uid() = user_id OR public.is_admin());

DROP POLICY IF EXISTS "Anyone can insert AG analytics" ON public.submission_analytics;
DROP POLICY IF EXISTS "Anyone can update AG analytics" ON public.submission_analytics;
DROP POLICY IF EXISTS "Anyone can read AG analytics" ON public.submission_analytics;

CREATE POLICY "Anyone insert analytics" ON public.submission_analytics
    FOR INSERT
    TO anon, authenticated
    WITH CHECK (true);

CREATE POLICY "Anyone update analytics" ON public.submission_analytics
    FOR UPDATE
    TO anon, authenticated
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Anyone read analytics" ON public.submission_analytics
    FOR SELECT
    TO anon, authenticated
    USING (true);

DROP POLICY IF EXISTS "Admins can manage AG vote reviews" ON public.vote_reviews;
DROP POLICY IF EXISTS "Admins can read AG admin users" ON public.admin_users;
DROP POLICY IF EXISTS "Admins can mutate AG admin users" ON public.admin_users;
DROP POLICY IF EXISTS "Admins can read AG fraud config" ON public.fraud_config;
DROP POLICY IF EXISTS "Admins can mutate AG fraud config" ON public.fraud_config;

CREATE POLICY "Admins manage vote_reviews" ON public.vote_reviews
    FOR ALL
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

CREATE POLICY "Admins read admin_users" ON public.admin_users
    FOR SELECT
    TO authenticated
    USING (public.is_admin());

CREATE POLICY "Admins mutate admin_users" ON public.admin_users
    FOR ALL
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

CREATE POLICY "Admins read fraud_config" ON public.fraud_config
    FOR SELECT
    TO authenticated
    USING (public.is_admin());

CREATE POLICY "Admins mutate fraud_config" ON public.fraud_config
    FOR ALL
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

CREATE POLICY "Public read competition_content" ON public.competition_content
    FOR SELECT
    USING (true);

CREATE POLICY "Service write competition_content" ON public.competition_content
    FOR ALL
    USING (true)
    WITH CHECK (true);

DROP TRIGGER IF EXISTS trg_ag_submission_analytics_updated_at ON public.submission_analytics;
DROP TRIGGER IF EXISTS trg_ag_votes_capture_ip ON public.votes;
DROP TRIGGER IF EXISTS trg_ag_submission_analytics_capture_ip ON public.submission_analytics;
DROP TRIGGER IF EXISTS trg_ag_votes_prevent_self_voting ON public.votes;
DROP TRIGGER IF EXISTS trg_ag_votes_enforce_max_votes ON public.votes;
DROP TRIGGER IF EXISTS trg_ag_votes_update_submission_vote_count ON public.votes;

CREATE TRIGGER trg_competitions_updated_at
    BEFORE UPDATE ON public.competitions
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER trg_submissions_updated_at
    BEFORE UPDATE ON public.submissions
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER trg_submission_analytics_updated_at
    BEFORE UPDATE ON public.submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER trg_prevent_self_voting
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_self_voting();

CREATE TRIGGER trg_enforce_max_votes
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_max_votes();

CREATE TRIGGER trg_update_vote_count
    AFTER INSERT OR DELETE ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.update_submission_vote_count();

CREATE TRIGGER trg_capture_vote_ip
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.capture_vote_ip();

CREATE TRIGGER trg_capture_analytics_ip
    BEFORE INSERT ON public.submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION public.capture_analytics_ip();

CREATE TRIGGER trg_members_owner_flag
    BEFORE INSERT OR UPDATE OF member_id ON public.members
    FOR EACH ROW
    EXECUTE FUNCTION public.apply_banodoco_owner_flag();

GRANT EXECUTE ON FUNCTION public.is_admin(UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_profile(JSONB) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_verified_vote_count(UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_vote_count_with_judge_multiplier(UUID, UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_verified_vote_count_with_judge_multiplier(UUID, UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_fraud_config(TEXT, JSONB) TO authenticated;
GRANT EXECUTE ON FUNCTION public.add_admin(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.remove_admin(UUID) TO authenticated;

GRANT SELECT ON public.competitions TO anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.submissions TO authenticated;
GRANT SELECT, INSERT, DELETE ON public.votes TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.submission_analytics TO anon, authenticated;
GRANT SELECT ON public.vote_reviews TO authenticated;
GRANT SELECT ON public.admin_users TO authenticated;
GRANT SELECT ON public.fraud_config TO authenticated;
GRANT SELECT ON public.profiles TO anon, authenticated;
GRANT SELECT ON public.submission_details TO anon, authenticated;
GRANT SELECT ON public.public_vote_counts TO anon, authenticated;
GRANT SELECT ON public.submission_votes_with_judges TO anon, authenticated;
GRANT SELECT ON public.submission_analytics_summary TO authenticated;
GRANT SELECT ON public.admin_fraud_dashboard TO authenticated;
GRANT SELECT ON public.votes_with_confidence TO authenticated;
GRANT SELECT ON public.votes_needing_review TO authenticated;
GRANT SELECT ON public.competition_leaderboard TO authenticated;
GRANT SELECT ON public.fraud_detection_summary TO authenticated;
GRANT SELECT ON public.competition_content TO anon, authenticated;

DROP TABLE IF EXISTS public.ag_user_identities CASCADE;
DROP TABLE IF EXISTS public.ag_competitions CASCADE;
DROP TABLE IF EXISTS public.discord_competitions CASCADE;
DROP TABLE IF EXISTS public.openmuse_profiles CASCADE;

UPDATE public.sync_status
SET table_name = 'competitions'
WHERE table_name IN ('ag_competitions', 'discord_competitions');

DELETE FROM public.sync_status
WHERE table_name IN ('ag_user_identities', 'openmuse_profiles');

ALTER INDEX IF EXISTS public.idx_ag_submissions_competition_id RENAME TO idx_submissions_competition_id;
ALTER INDEX IF EXISTS public.idx_ag_submissions_user_id RENAME TO idx_submissions_user_id;
ALTER INDEX IF EXISTS public.idx_ag_submissions_status RENAME TO idx_submissions_status;
ALTER INDEX IF EXISTS public.idx_ag_submissions_media_id RENAME TO idx_submissions_media_id;
ALTER INDEX IF EXISTS public.idx_ag_votes_competition_id RENAME TO idx_votes_competition_id;
ALTER INDEX IF EXISTS public.idx_ag_votes_submission_id RENAME TO idx_votes_submission_id;
ALTER INDEX IF EXISTS public.idx_ag_votes_user_id RENAME TO idx_votes_user_id;
ALTER INDEX IF EXISTS public.idx_ag_submission_analytics_competition_id RENAME TO idx_submission_analytics_competition_id;
ALTER INDEX IF EXISTS public.idx_ag_submission_analytics_submission_id RENAME TO idx_submission_analytics_submission_id;
ALTER INDEX IF EXISTS public.idx_ag_submission_analytics_user_id RENAME TO idx_submission_analytics_user_id;
