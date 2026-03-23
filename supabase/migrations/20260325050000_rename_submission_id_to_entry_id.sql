-- Rename submission_id -> entry_id on votes and submission_analytics
-- to match the unified competition_entries table.

-- 1. Drop dependent views first
-- Drop all dependent views (CASCADE handles ordering)
DROP VIEW IF EXISTS public.submission_details CASCADE;
DROP VIEW IF EXISTS public.public_vote_counts CASCADE;
DROP VIEW IF EXISTS public.votes_with_confidence CASCADE;
DROP VIEW IF EXISTS public.votes_needing_review CASCADE;
DROP VIEW IF EXISTS public.admin_fraud_dashboard CASCADE;
DROP VIEW IF EXISTS public.fraud_detection_summary CASCADE;
DROP VIEW IF EXISTS public.submission_analytics_summary CASCADE;
DROP VIEW IF EXISTS public.submission_votes_with_judges CASCADE;

-- 2. Drop triggers that reference old column
DROP TRIGGER IF EXISTS trg_votes_capture_ip ON public.votes;
DROP TRIGGER IF EXISTS trg_votes_prevent_self_voting ON public.votes;
DROP TRIGGER IF EXISTS trg_votes_enforce_max_votes ON public.votes;
DROP TRIGGER IF EXISTS trg_votes_update_submission_vote_count ON public.votes;
DROP TRIGGER IF EXISTS trg_submission_analytics_capture_ip ON public.submission_analytics;

-- 3. Drop functions that reference old column
DROP FUNCTION IF EXISTS public.prevent_self_voting();
DROP FUNCTION IF EXISTS public.enforce_max_votes();
DROP FUNCTION IF EXISTS public.update_submission_vote_count();
DROP FUNCTION IF EXISTS public.capture_vote_ip();
DROP FUNCTION IF EXISTS public.capture_analytics_ip();
DROP FUNCTION IF EXISTS public.calculate_vote_confidence(UUID);
DROP FUNCTION IF EXISTS public.get_verified_vote_count(UUID);
DROP FUNCTION IF EXISTS public.get_vote_count_with_judge_multiplier(UUID, UUID);
DROP FUNCTION IF EXISTS public.get_verified_vote_count_with_judge_multiplier(UUID, UUID);

-- 4. Drop RLS policies on votes that reference old column
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT policyname, tablename FROM pg_policies
             WHERE schemaname = 'public' AND tablename IN ('votes', 'submission_analytics')
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', r.policyname, r.tablename);
    END LOOP;
END;
$$;

-- 5. Rename the columns
ALTER TABLE public.votes RENAME COLUMN submission_id TO entry_id;
ALTER TABLE public.submission_analytics RENAME COLUMN submission_id TO entry_id;

-- Also rename submission_analytics table to entry_analytics
ALTER TABLE public.submission_analytics RENAME TO entry_analytics;

-- Rename vote_reviews.vote_id stays (it references votes, not submissions)

-- 6. Rename indexes
ALTER INDEX IF EXISTS idx_votes_submission_id RENAME TO idx_votes_entry_id;
ALTER INDEX IF EXISTS idx_submission_analytics_submission_id RENAME TO idx_entry_analytics_entry_id;
ALTER INDEX IF EXISTS idx_submission_analytics_session_submission RENAME TO idx_entry_analytics_session_entry;

-- 7. Recreate functions with entry_id
CREATE OR REPLACE FUNCTION public.prevent_self_voting()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.competition_entries
        WHERE id = NEW.entry_id AND member_id = (
            SELECT m.member_id FROM public.members m WHERE m.auth_user_id = NEW.user_id
        )
    ) THEN
        RAISE EXCEPTION 'Cannot vote for your own entry';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.enforce_max_votes()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
DECLARE
    v_max INTEGER;
    v_current INTEGER;
BEGIN
    SELECT (c.settings->>'max_votes')::INTEGER INTO v_max
    FROM public.competitions c
    WHERE c.id = NEW.competition_id;

    IF v_max IS NOT NULL THEN
        SELECT COUNT(*) INTO v_current
        FROM public.votes
        WHERE user_id = NEW.user_id AND competition_id = NEW.competition_id;

        IF v_current >= v_max THEN
            RAISE EXCEPTION 'Maximum votes reached for this competition';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_submission_vote_count()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.competition_entries SET vote_count = vote_count + 1 WHERE id = NEW.entry_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE public.competition_entries SET vote_count = GREATEST(vote_count - 1, 0) WHERE id = OLD.entry_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.capture_vote_ip()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
BEGIN
    NEW.ip_hash := public.hash_ip_address(
        COALESCE(current_setting('request.headers', true)::json->>'x-forwarded-for', 'unknown')
    );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.capture_analytics_ip()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
BEGIN
    NEW.ip_hash := public.hash_ip_address(
        COALESCE(current_setting('request.headers', true)::json->>'x-forwarded-for', 'unknown')
    );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.calculate_vote_confidence(p_vote_id UUID)
RETURNS INTEGER
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
DECLARE
    v_score INTEGER := 100;
    v_vote RECORD;
    v_member RECORD;
    v_config RECORD;
BEGIN
    SELECT v.*, m.discord_created_at
    INTO v_vote
    FROM public.votes v
    LEFT JOIN public.members m ON m.auth_user_id = v.user_id
    WHERE v.id = p_vote_id;

    IF NOT FOUND THEN RETURN 0; END IF;

    -- Account age scoring
    SELECT config_value INTO v_config FROM public.fraud_config WHERE config_key = 'account_age_thresholds';
    IF v_config IS NOT NULL AND v_vote.discord_created_at IS NOT NULL THEN
        DECLARE v_age_days INTEGER := EXTRACT(DAY FROM NOW() - v_vote.discord_created_at)::INTEGER;
        BEGIN
            IF v_age_days < (v_config.config_value->>'very_new_days')::INTEGER THEN
                v_score := v_score - (v_config.config_value->>'very_new_penalty')::INTEGER;
            ELSIF v_age_days < (v_config.config_value->>'new_days')::INTEGER THEN
                v_score := v_score - (v_config.config_value->>'new_penalty')::INTEGER;
            END IF;
        END;
    END IF;

    -- Vote speed scoring
    SELECT config_value INTO v_config FROM public.fraud_config WHERE config_key = 'vote_speed_thresholds';
    IF v_config IS NOT NULL AND v_vote.vote_duration_ms IS NOT NULL THEN
        IF v_vote.vote_duration_ms < (v_config.config_value->>'suspicious_ms')::INTEGER THEN
            v_score := v_score - (v_config.config_value->>'suspicious_penalty')::INTEGER;
        ELSIF v_vote.vote_duration_ms < (v_config.config_value->>'fast_ms')::INTEGER THEN
            v_score := v_score - (v_config.config_value->>'fast_penalty')::INTEGER;
        END IF;
    END IF;

    -- IP sharing scoring
    SELECT config_value INTO v_config FROM public.fraud_config WHERE config_key = 'ip_sharing_thresholds';
    IF v_config IS NOT NULL AND v_vote.ip_hash IS NOT NULL THEN
        DECLARE v_ip_count INTEGER;
        BEGIN
            SELECT COUNT(DISTINCT user_id) INTO v_ip_count
            FROM public.votes
            WHERE ip_hash = v_vote.ip_hash AND competition_id = v_vote.competition_id;
            IF v_ip_count > (v_config.config_value->>'high_threshold')::INTEGER THEN
                v_score := v_score - (v_config.config_value->>'high_penalty')::INTEGER;
            ELSIF v_ip_count > (v_config.config_value->>'medium_threshold')::INTEGER THEN
                v_score := v_score - (v_config.config_value->>'medium_penalty')::INTEGER;
            END IF;
        END;
    END IF;

    RETURN GREATEST(v_score, 0);
END;
$$;

CREATE OR REPLACE FUNCTION public.get_verified_vote_count(p_entry_id UUID)
RETURNS BIGINT
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
DECLARE v_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM public.votes v
    LEFT JOIN public.vote_reviews vr ON vr.vote_id = v.id
    WHERE v.entry_id = p_entry_id
      AND (public.calculate_vote_confidence(v.id) >= 40 OR COALESCE(vr.is_legitimate, FALSE));
    RETURN v_count;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_vote_count_with_judge_multiplier(
    p_entry_id UUID,
    p_competition_id UUID DEFAULT NULL
)
RETURNS NUMERIC
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
DECLARE
    v_multiplier NUMERIC := 1;
    v_regular BIGINT := 0;
    v_judge BIGINT := 0;
BEGIN
    IF p_competition_id IS NOT NULL THEN
        SELECT COALESCE((c.settings->>'judge_multiplier')::NUMERIC, 1) INTO v_multiplier
        FROM public.competitions c WHERE c.id = p_competition_id;
    END IF;

    SELECT COUNT(*) INTO v_regular
    FROM public.votes v
    JOIN public.members m ON m.auth_user_id = v.user_id
    WHERE v.entry_id = p_entry_id AND NOT COALESCE(m.banodoco_owner, FALSE);

    SELECT COUNT(*) INTO v_judge
    FROM public.votes v
    JOIN public.members m ON m.auth_user_id = v.user_id
    WHERE v.entry_id = p_entry_id AND COALESCE(m.banodoco_owner, FALSE);

    RETURN v_regular + (v_judge * v_multiplier);
END;
$$;

CREATE OR REPLACE FUNCTION public.get_verified_vote_count_with_judge_multiplier(
    p_entry_id UUID,
    p_competition_id UUID DEFAULT NULL
)
RETURNS NUMERIC
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
DECLARE
    v_multiplier NUMERIC := 1;
    v_regular BIGINT := 0;
    v_judge BIGINT := 0;
BEGIN
    IF p_competition_id IS NOT NULL THEN
        SELECT COALESCE((c.settings->>'judge_multiplier')::NUMERIC, 1) INTO v_multiplier
        FROM public.competitions c WHERE c.id = p_competition_id;
    END IF;

    SELECT COUNT(*) INTO v_regular
    FROM public.votes v
    JOIN public.members m ON m.auth_user_id = v.user_id
    LEFT JOIN public.vote_reviews vr ON vr.vote_id = v.id
    WHERE v.entry_id = p_entry_id
      AND NOT COALESCE(m.banodoco_owner, FALSE)
      AND (public.calculate_vote_confidence(v.id) >= 40 OR COALESCE(vr.is_legitimate, FALSE));

    SELECT COUNT(*) INTO v_judge
    FROM public.votes v
    JOIN public.members m ON m.auth_user_id = v.user_id
    LEFT JOIN public.vote_reviews vr ON vr.vote_id = v.id
    WHERE v.entry_id = p_entry_id
      AND COALESCE(m.banodoco_owner, FALSE)
      AND (public.calculate_vote_confidence(v.id) >= 40 OR COALESCE(vr.is_legitimate, FALSE));

    RETURN v_regular + (v_judge * v_multiplier);
END;
$$;

-- 8. Recreate triggers
CREATE TRIGGER trg_votes_capture_ip
    BEFORE INSERT ON public.votes
    FOR EACH ROW EXECUTE FUNCTION public.capture_vote_ip();

CREATE TRIGGER trg_votes_prevent_self_voting
    BEFORE INSERT ON public.votes
    FOR EACH ROW EXECUTE FUNCTION public.prevent_self_voting();

CREATE TRIGGER trg_votes_enforce_max_votes
    BEFORE INSERT ON public.votes
    FOR EACH ROW EXECUTE FUNCTION public.enforce_max_votes();

CREATE TRIGGER trg_votes_update_entry_vote_count
    AFTER INSERT OR DELETE ON public.votes
    FOR EACH ROW EXECUTE FUNCTION public.update_submission_vote_count();

CREATE TRIGGER trg_entry_analytics_capture_ip
    BEFORE INSERT ON public.entry_analytics
    FOR EACH ROW EXECUTE FUNCTION public.capture_analytics_ip();

-- 9. Recreate RLS on votes
ALTER TABLE public.votes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users read own votes" ON public.votes FOR SELECT TO authenticated
    USING (user_id = auth.uid() OR public.is_admin());
CREATE POLICY "Users insert own votes" ON public.votes FOR INSERT TO authenticated
    WITH CHECK (user_id = auth.uid());
CREATE POLICY "Users delete own votes" ON public.votes FOR DELETE TO authenticated
    USING (user_id = auth.uid());

-- 10. Recreate RLS on entry_analytics
ALTER TABLE public.entry_analytics ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can insert analytics" ON public.entry_analytics FOR INSERT WITH CHECK (true);
CREATE POLICY "Anyone can update analytics" ON public.entry_analytics FOR UPDATE USING (true);
CREATE POLICY "Anyone can read analytics" ON public.entry_analytics FOR SELECT USING (true);

GRANT SELECT, INSERT, UPDATE ON public.entry_analytics TO anon, authenticated;

-- 11. Recreate views with entry_id
CREATE OR REPLACE VIEW public.submission_details AS
SELECT
    ce.id,
    ce.competition_id,
    m_auth.auth_user_id AS user_id,
    ce.media_id,
    ce.theme,
    med.url AS video_url,
    ce.title,
    ce.description,
    ce.tools_used,
    ce.thumbnail_url,
    ce.duration_seconds,
    ce.additional_links,
    ce.status,
    ce.admin_notes,
    ce.score,
    ce.vote_count,
    ce.winner,
    ce.submitted_at,
    ce.created_at,
    ce.updated_at,
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
JOIN public.media med ON med.id = ce.media_id
LEFT JOIN public.members m_auth ON m_auth.member_id = ce.member_id
WHERE ce.entry_type = 'prize' AND ce.status <> 'rejected';

CREATE OR REPLACE VIEW public.public_vote_counts AS
SELECT
    ce.id AS entry_id,
    ce.competition_id,
    ce.title,
    m.auth_user_id AS creator_id,
    COALESCE(m.global_name, m.username) AS creator_name,
    ce.vote_count,
    public.get_vote_count_with_judge_multiplier(ce.id, ce.competition_id) AS weighted_vote_count,
    ce.status
FROM public.competition_entries ce
LEFT JOIN public.members m ON m.member_id = ce.member_id
WHERE ce.entry_type = 'prize';

GRANT SELECT ON public.submission_details TO anon, authenticated;
GRANT SELECT ON public.public_vote_counts TO anon, authenticated;

-- 12. Update unique constraint on entry_analytics
ALTER TABLE public.entry_analytics DROP CONSTRAINT IF EXISTS submission_analytics_session_submission_key;
ALTER TABLE public.entry_analytics ADD CONSTRAINT entry_analytics_session_entry_key UNIQUE (session_id, entry_id);

-- 13. Update sync_status
UPDATE public.sync_status SET table_name = 'entry_analytics' WHERE table_name = 'submission_analytics';
