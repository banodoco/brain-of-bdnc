-- Unify prize submissions and Discord competition entries into a single
-- competition_entries table while preserving the submissions API surface.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.submissions AS s
        LEFT JOIN public.members AS m
          ON m.auth_user_id = s.user_id
        WHERE m.member_id IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot unify competition entries: one or more submissions.user_id values do not map to members.auth_user_id';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.competition_entries AS ce
        LEFT JOIN public.members AS m
          ON m.member_id = ce.author_id
        WHERE m.member_id IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot unify competition entries: one or more competition_entries.author_id values do not map to members.member_id';
    END IF;
END;
$$;

DROP VIEW IF EXISTS public.fraud_detection_summary CASCADE;
DROP VIEW IF EXISTS public.votes_needing_review CASCADE;
DROP VIEW IF EXISTS public.votes_with_confidence CASCADE;
DROP VIEW IF EXISTS public.competition_leaderboard CASCADE;
DROP VIEW IF EXISTS public.admin_fraud_dashboard CASCADE;
DROP VIEW IF EXISTS public.submission_analytics_summary CASCADE;
DROP VIEW IF EXISTS public.submission_votes_with_judges CASCADE;
DROP VIEW IF EXISTS public.public_vote_counts CASCADE;
DROP VIEW IF EXISTS public.submission_details CASCADE;

DROP TRIGGER IF EXISTS trg_votes_prevent_self_voting ON public.votes;
DROP TRIGGER IF EXISTS trg_prevent_self_voting ON public.votes;
DROP TRIGGER IF EXISTS trg_votes_enforce_max_votes ON public.votes;
DROP TRIGGER IF EXISTS trg_enforce_max_votes ON public.votes;
DROP TRIGGER IF EXISTS trg_votes_update_submission_vote_count ON public.votes;
DROP TRIGGER IF EXISTS trg_update_vote_count ON public.votes;
DROP TRIGGER IF EXISTS trg_votes_capture_ip ON public.votes;
DROP TRIGGER IF EXISTS trg_capture_vote_ip ON public.votes;

DROP TRIGGER IF EXISTS trg_submissions_updated_at ON public.submissions;

DROP TRIGGER IF EXISTS trg_submission_analytics_updated_at ON public.submission_analytics;
DROP TRIGGER IF EXISTS trg_submission_analytics_capture_ip ON public.submission_analytics;
DROP TRIGGER IF EXISTS trg_capture_analytics_ip ON public.submission_analytics;

DROP FUNCTION IF EXISTS public.prevent_self_voting() CASCADE;
DROP FUNCTION IF EXISTS public.enforce_max_votes() CASCADE;
DROP FUNCTION IF EXISTS public.update_submission_vote_count() CASCADE;
DROP FUNCTION IF EXISTS public.calculate_vote_confidence(UUID) CASCADE;
DROP FUNCTION IF EXISTS public.get_verified_vote_count(UUID) CASCADE;
DROP FUNCTION IF EXISTS public.get_vote_count_with_judge_multiplier(UUID, UUID) CASCADE;
DROP FUNCTION IF EXISTS public.get_verified_vote_count_with_judge_multiplier(UUID, UUID) CASCADE;

DROP POLICY IF EXISTS "Public read visible submissions" ON public.submissions;
DROP POLICY IF EXISTS "Users insert own submissions" ON public.submissions;
DROP POLICY IF EXISTS "Users update own pending submissions" ON public.submissions;
DROP POLICY IF EXISTS "Users delete own pending submissions" ON public.submissions;

DROP POLICY IF EXISTS "Service role can access all competition_entries" ON public.competition_entries;

ALTER TABLE public.votes
    DROP CONSTRAINT IF EXISTS ag_votes_submission_id_fkey,
    DROP CONSTRAINT IF EXISTS votes_submission_id_fkey;

ALTER TABLE public.submission_analytics
    DROP CONSTRAINT IF EXISTS ag_submission_analytics_submission_id_fkey,
    DROP CONSTRAINT IF EXISTS submission_analytics_submission_id_fkey;

CREATE TABLE public.competition_entries_new (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    competition_id UUID NOT NULL
        REFERENCES public.competitions(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL
        REFERENCES public.members(member_id),
    entry_type TEXT NOT NULL
        CHECK (entry_type IN ('prize', 'community')),

    media_id UUID
        REFERENCES public.media(id) ON DELETE SET NULL,
    message_id BIGINT
        REFERENCES public.discord_messages(message_id) ON DELETE SET NULL,

    title TEXT,
    description TEXT,
    theme TEXT,
    tools_used TEXT[] DEFAULT '{}'::TEXT[],
    thumbnail_url TEXT,
    duration_seconds INTEGER,
    additional_links JSONB,
    status TEXT DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'under_review', 'approved', 'rejected', 'winner', 'finalist')),
    admin_notes TEXT,
    score NUMERIC(10, 2),
    vote_count INTEGER DEFAULT 0,
    winner BOOLEAN DEFAULT FALSE,
    submitted_at TIMESTAMPTZ DEFAULT NOW(),

    channel_id BIGINT,
    author_name TEXT,
    entry_number INTEGER,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_competition_entries_prize_invariants
        CHECK (
            entry_type <> 'prize'
            OR (
                media_id IS NOT NULL
                AND title IS NOT NULL
                AND description IS NOT NULL
                AND tools_used IS NOT NULL
                AND status IS NOT NULL
                AND vote_count IS NOT NULL
                AND winner IS NOT NULL
                AND submitted_at IS NOT NULL
            )
        )
);

INSERT INTO public.competition_entries_new (
    id,
    competition_id,
    member_id,
    entry_type,
    media_id,
    title,
    description,
    theme,
    tools_used,
    thumbnail_url,
    duration_seconds,
    additional_links,
    status,
    admin_notes,
    score,
    vote_count,
    winner,
    submitted_at,
    created_at,
    updated_at
)
SELECT
    s.id,
    s.competition_id,
    m.member_id,
    'prize',
    s.media_id,
    s.title,
    s.description,
    s.theme,
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
    s.updated_at
FROM public.submissions AS s
JOIN public.members AS m
  ON m.auth_user_id = s.user_id;

INSERT INTO public.competition_entries_new (
    id,
    competition_id,
    member_id,
    entry_type,
    message_id,
    channel_id,
    author_name,
    entry_number,
    created_at
)
SELECT
    gen_random_uuid(),
    ce.competition_id,
    ce.author_id,
    'community',
    ce.message_id,
    ce.channel_id,
    ce.author_name,
    ce.entry_number,
    ce.created_at
FROM public.competition_entries AS ce;

DROP TABLE public.submissions CASCADE;
DROP TABLE public.competition_entries CASCADE;

ALTER TABLE public.competition_entries_new
    RENAME TO competition_entries;

CREATE INDEX idx_competition_entries_competition_id
    ON public.competition_entries(competition_id);

CREATE INDEX idx_competition_entries_member_id
    ON public.competition_entries(member_id);

CREATE INDEX idx_competition_entries_entry_type
    ON public.competition_entries(entry_type);

CREATE INDEX idx_competition_entries_prize_status
    ON public.competition_entries(status)
    WHERE entry_type = 'prize';

CREATE INDEX idx_competition_entries_competition_message
    ON public.competition_entries(competition_id, message_id)
    WHERE message_id IS NOT NULL;

CREATE UNIQUE INDEX idx_competition_entries_competition_member_prize
    ON public.competition_entries(competition_id, member_id)
    WHERE entry_type = 'prize';

CREATE UNIQUE INDEX idx_competition_entries_competition_message_key
    ON public.competition_entries(competition_id, message_id)
    WHERE message_id IS NOT NULL;

ALTER TABLE public.votes
    ADD CONSTRAINT votes_submission_id_fkey
    FOREIGN KEY (submission_id)
    REFERENCES public.competition_entries(id)
    ON DELETE CASCADE;

ALTER TABLE public.submission_analytics
    ADD CONSTRAINT submission_analytics_submission_id_fkey
    FOREIGN KEY (submission_id)
    REFERENCES public.competition_entries(id)
    ON DELETE CASCADE;

UPDATE public.competition_entries AS ce
SET vote_count = vote_totals.vote_count
FROM (
    SELECT v.submission_id, COUNT(*)::INTEGER AS vote_count
    FROM public.votes AS v
    GROUP BY v.submission_id
) AS vote_totals
WHERE ce.id = vote_totals.submission_id;

UPDATE public.competition_entries
SET vote_count = 0
WHERE entry_type = 'prize'
  AND vote_count IS DISTINCT FROM 0
  AND NOT EXISTS (
      SELECT 1
      FROM public.votes AS v
      WHERE v.submission_id = public.competition_entries.id
  );

CREATE OR REPLACE FUNCTION public.prevent_self_voting()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_entry_member_id BIGINT;
    v_voter_member_id BIGINT;
BEGIN
    SELECT ce.member_id
    INTO v_entry_member_id
    FROM public.competition_entries AS ce
    WHERE ce.id = NEW.submission_id;

    SELECT m.member_id
    INTO v_voter_member_id
    FROM public.members AS m
    WHERE m.auth_user_id = NEW.user_id;

    IF v_entry_member_id IS NOT NULL
       AND v_voter_member_id IS NOT NULL
       AND v_entry_member_id = v_voter_member_id THEN
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

    UPDATE public.competition_entries
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
        COUNT(DISTINCT ce.member_id)
    INTO
        total_user_votes,
        unique_creators_voted_for
    FROM public.votes AS v2
    JOIN public.competition_entries AS ce
      ON ce.id = v2.submission_id
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
        FROM public.competition_entries
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
    LEFT JOIN public.members AS m
      ON m.auth_user_id = v.user_id
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
        FROM public.competition_entries
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
    LEFT JOIN public.members AS m
      ON m.auth_user_id = v.user_id
    WHERE v.submission_id = p_submission_id;

    RETURN COALESCE(v_weighted_count, 0);
END;
$$;

CREATE TRIGGER trg_competition_entries_updated_at
    BEFORE UPDATE ON public.competition_entries
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER trg_votes_prevent_self_voting
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_self_voting();

CREATE TRIGGER trg_votes_enforce_max_votes
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_max_votes();

CREATE TRIGGER trg_votes_update_submission_vote_count
    AFTER INSERT OR DELETE ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.update_submission_vote_count();

CREATE TRIGGER trg_votes_capture_ip
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.capture_vote_ip();

CREATE TRIGGER trg_submission_analytics_updated_at
    BEFORE UPDATE ON public.submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER trg_submission_analytics_capture_ip
    BEFORE INSERT ON public.submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION public.capture_analytics_ip();

CREATE VIEW public.submissions AS
SELECT
    ce.id,
    ce.competition_id,
    m.auth_user_id AS user_id,
    ce.media_id,
    ce.theme,
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
    ce.updated_at
FROM public.competition_entries AS ce
JOIN public.members AS m
  ON m.member_id = ce.member_id
WHERE ce.entry_type = 'prize'
  AND (
      ce.status <> 'rejected'
      OR m.auth_user_id = auth.uid()
      OR public.is_admin()
  );

CREATE OR REPLACE FUNCTION public.submissions_view_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    v_member_id BIGINT;
    v_id UUID;
BEGIN
    SELECT m.member_id
    INTO v_member_id
    FROM public.members AS m
    WHERE m.auth_user_id = NEW.user_id;

    IF v_member_id IS NULL THEN
        RAISE EXCEPTION 'No member found for user_id %', NEW.user_id;
    END IF;

    v_id := COALESCE(NEW.id, gen_random_uuid());

    INSERT INTO public.competition_entries (
        id,
        competition_id,
        member_id,
        entry_type,
        media_id,
        title,
        description,
        theme,
        tools_used,
        thumbnail_url,
        duration_seconds,
        additional_links,
        status,
        admin_notes,
        score,
        vote_count,
        winner,
        submitted_at
    )
    VALUES (
        v_id,
        NEW.competition_id,
        v_member_id,
        'prize',
        NEW.media_id,
        NEW.title,
        NEW.description,
        NEW.theme,
        COALESCE(NEW.tools_used, '{}'::TEXT[]),
        NEW.thumbnail_url,
        NEW.duration_seconds,
        NEW.additional_links,
        COALESCE(NEW.status, 'submitted'),
        NEW.admin_notes,
        NEW.score,
        COALESCE(NEW.vote_count, 0),
        COALESCE(NEW.winner, FALSE),
        COALESCE(NEW.submitted_at, NOW())
    );

    NEW.id := v_id;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.submissions_view_update()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    v_member_id BIGINT;
BEGIN
    SELECT m.member_id
    INTO v_member_id
    FROM public.members AS m
    WHERE m.auth_user_id = NEW.user_id;

    IF v_member_id IS NULL THEN
        RAISE EXCEPTION 'No member found for user_id %', NEW.user_id;
    END IF;

    UPDATE public.competition_entries
    SET
        competition_id = NEW.competition_id,
        member_id = v_member_id,
        media_id = NEW.media_id,
        title = NEW.title,
        description = NEW.description,
        theme = NEW.theme,
        tools_used = NEW.tools_used,
        thumbnail_url = NEW.thumbnail_url,
        duration_seconds = NEW.duration_seconds,
        additional_links = NEW.additional_links,
        status = NEW.status,
        admin_notes = NEW.admin_notes,
        score = NEW.score,
        vote_count = NEW.vote_count,
        winner = NEW.winner,
        submitted_at = NEW.submitted_at,
        updated_at = NOW()
    WHERE id = OLD.id
      AND entry_type = 'prize';

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.submissions_view_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    DELETE FROM public.competition_entries
    WHERE id = OLD.id
      AND entry_type = 'prize';

    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_submissions_view_insert
    INSTEAD OF INSERT ON public.submissions
    FOR EACH ROW
    EXECUTE FUNCTION public.submissions_view_insert();

CREATE TRIGGER trg_submissions_view_update
    INSTEAD OF UPDATE ON public.submissions
    FOR EACH ROW
    EXECUTE FUNCTION public.submissions_view_update();

CREATE TRIGGER trg_submissions_view_delete
    INSTEAD OF DELETE ON public.submissions
    FOR EACH ROW
    EXECUTE FUNCTION public.submissions_view_delete();

CREATE VIEW public.submission_details AS
SELECT
    ce.id,
    ce.competition_id,
    m.auth_user_id AS user_id,
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
FROM public.competition_entries AS ce
JOIN public.members AS m
  ON m.member_id = ce.member_id
JOIN public.media AS med
  ON med.id = ce.media_id
WHERE ce.entry_type = 'prize'
  AND ce.status <> 'rejected';

CREATE VIEW public.public_vote_counts AS
SELECT
    ce.id AS submission_id,
    ce.competition_id,
    ce.title,
    m.auth_user_id AS creator_id,
    COALESCE(m.global_name, m.username) AS creator_name,
    public.get_verified_vote_count(ce.id) AS vote_count,
    public.get_verified_vote_count_with_judge_multiplier(ce.id, ce.competition_id) AS weighted_vote_count,
    ce.status
FROM public.competition_entries AS ce
JOIN public.members AS m
  ON m.member_id = ce.member_id
WHERE ce.entry_type = 'prize'
  AND ce.status <> 'rejected'
ORDER BY weighted_vote_count DESC;

CREATE VIEW public.submission_votes_with_judges AS
SELECT
    ce.id AS submission_id,
    ce.title,
    ce.competition_id,
    COUNT(v.id) AS total_votes,
    COUNT(v.id) FILTER (WHERE NOT COALESCE(voter.banodoco_owner, FALSE)) AS regular_votes,
    COUNT(v.id) FILTER (WHERE COALESCE(voter.banodoco_owner, FALSE)) AS judge_votes,
    COALESCE((c.settings->>'judge_multiplier')::NUMERIC, 1) AS judge_multiplier,
    public.get_vote_count_with_judge_multiplier(ce.id, ce.competition_id) AS weighted_vote_count,
    public.get_verified_vote_count_with_judge_multiplier(ce.id, ce.competition_id) AS verified_weighted_vote_count,
    public.get_verified_vote_count(ce.id) AS verified_votes_no_multiplier,
    ce.status
FROM public.competition_entries AS ce
LEFT JOIN public.votes AS v
  ON v.submission_id = ce.id
LEFT JOIN public.members AS voter
  ON voter.auth_user_id = v.user_id
LEFT JOIN public.competitions AS c
  ON c.id = ce.competition_id
WHERE ce.entry_type = 'prize'
  AND ce.status <> 'rejected'
GROUP BY ce.id, ce.title, ce.competition_id, c.settings, ce.status
ORDER BY verified_weighted_vote_count DESC;

CREATE VIEW public.submission_analytics_summary AS
SELECT
    ce.id AS submission_id,
    ce.title,
    owner.auth_user_id AS submission_owner_id,
    COUNT(DISTINCT sa.session_id) AS total_views,
    COUNT(DISTINCT sa.user_id) FILTER (WHERE sa.user_id IS NOT NULL) AS registered_user_views,
    COUNT(DISTINCT sa.session_id) FILTER (WHERE sa.user_id IS NULL) AS anonymous_views,
    AVG(sa.view_duration_seconds) AS avg_view_duration_seconds,
    COUNT(*) FILTER (WHERE sa.video_played = TRUE) AS video_play_count,
    AVG(sa.video_play_duration_seconds) FILTER (WHERE sa.video_played = TRUE) AS avg_video_watch_duration,
    COUNT(*) FILTER (WHERE sa.video_completed = TRUE) AS video_completion_count
FROM public.competition_entries AS ce
JOIN public.members AS owner
  ON owner.member_id = ce.member_id
LEFT JOIN public.submission_analytics AS sa
  ON sa.submission_id = ce.id
WHERE ce.entry_type = 'prize'
  AND (
      public.is_admin()
      OR owner.auth_user_id = auth.uid()
  )
GROUP BY ce.id, ce.title, owner.auth_user_id;

CREATE VIEW public.admin_fraud_dashboard AS
SELECT
    v.id AS vote_id,
    v.submission_id,
    ce.title AS submission_title,
    owner.auth_user_id AS submission_owner_id,
    v.user_id AS voter_id,
    COALESCE(voter.global_name, voter.username) AS voter_name,
    voter.username AS discord_username,
    v.created_at AS voted_at,
    v.vote_duration_ms,
    ROUND(EXTRACT(EPOCH FROM (v.created_at - voter.discord_created_at)) / 3600.0, 1) AS voter_account_age_hours,
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
JOIN public.competition_entries AS ce
  ON ce.id = v.submission_id
JOIN public.members AS owner
  ON owner.member_id = ce.member_id
LEFT JOIN public.members AS voter
  ON voter.auth_user_id = v.user_id
LEFT JOIN public.vote_reviews AS vr
  ON vr.vote_id = v.id
WHERE ce.entry_type = 'prize'
  AND public.is_admin()
ORDER BY v.created_at DESC;

CREATE VIEW public.votes_with_confidence AS
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

CREATE VIEW public.votes_needing_review AS
SELECT *
FROM public.votes_with_confidence
WHERE confidence_score < 60
ORDER BY confidence_score ASC, voted_at DESC;

CREATE VIEW public.competition_leaderboard AS
SELECT
    ce.id AS submission_id,
    ce.title,
    owner.auth_user_id AS creator_id,
    COALESCE(owner.global_name, owner.username) AS creator_name,
    ce.vote_count AS raw_votes,
    svj.verified_weighted_vote_count AS weighted_vote_count,
    public.get_verified_vote_count(ce.id) AS verified_votes,
    RANK() OVER (ORDER BY ce.vote_count DESC) AS raw_rank,
    RANK() OVER (ORDER BY svj.verified_weighted_vote_count DESC) AS weighted_rank,
    CASE
        WHEN ABS(
            RANK() OVER (ORDER BY ce.vote_count DESC)
            - RANK() OVER (ORDER BY svj.verified_weighted_vote_count DESC)
        ) >= 3 THEN TRUE
        ELSE FALSE
    END AS ranking_discrepancy,
    CASE
        WHEN ce.vote_count > 0 THEN ROUND((svj.verified_weighted_vote_count / ce.vote_count::NUMERIC) * 100, 1)
        ELSE 0
    END AS avg_confidence_score
FROM public.competition_entries AS ce
JOIN public.members AS owner
  ON owner.member_id = ce.member_id
LEFT JOIN public.submission_votes_with_judges AS svj
  ON svj.submission_id = ce.id
WHERE ce.entry_type = 'prize'
  AND ce.status <> 'rejected'
ORDER BY weighted_rank ASC;

CREATE VIEW public.fraud_detection_summary AS
SELECT
    COUNT(*) FILTER (WHERE users_from_same_ip >= 5) AS high_risk_ips,
    COUNT(DISTINCT voter_id) FILTER (WHERE confidence_score < 60) AS suspicious_users,
    COUNT(*) FILTER (WHERE votes_from_same_ip_for_submission >= 5) AS suspicious_time_clusters,
    COUNT(*) FILTER (WHERE voter_account_age_hours IS NOT NULL AND voter_account_age_hours < 24) AS votes_from_brand_new_accounts,
    COUNT(DISTINCT ip_hash) FILTER (WHERE confidence_score < 40) AS suspicious_signup_clusters,
    COUNT(DISTINCT submission_id) FILTER (WHERE voter_account_age_hours IS NOT NULL AND voter_account_age_hours < 24) AS submissions_with_new_account_votes,
    COUNT(DISTINCT submission_id) FILTER (WHERE confidence_score < 60) AS submissions_with_suspicious_activity
FROM public.admin_fraud_dashboard;

ALTER TABLE public.competition_entries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Read visible entries" ON public.competition_entries
    FOR SELECT
    TO anon, authenticated
    USING (
        (entry_type = 'prize' AND status <> 'rejected')
        OR member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
        OR public.is_admin()
    );

CREATE POLICY "Users insert own prize entries" ON public.competition_entries
    FOR INSERT
    TO authenticated
    WITH CHECK (
        entry_type = 'prize'
        AND member_id IN (
            SELECT m.member_id
            FROM public.members AS m
            WHERE m.auth_user_id = auth.uid()
        )
    );

CREATE POLICY "Users update own pending prize entries" ON public.competition_entries
    FOR UPDATE
    TO authenticated
    USING (
        (
            entry_type = 'prize'
            AND member_id IN (
                SELECT m.member_id
                FROM public.members AS m
                WHERE m.auth_user_id = auth.uid()
            )
            AND status = 'submitted'
        )
        OR public.is_admin()
    )
    WITH CHECK (
        (
            entry_type = 'prize'
            AND member_id IN (
                SELECT m.member_id
                FROM public.members AS m
                WHERE m.auth_user_id = auth.uid()
            )
            AND status = 'submitted'
        )
        OR public.is_admin()
    );

CREATE POLICY "Users delete own pending prize entries" ON public.competition_entries
    FOR DELETE
    TO authenticated
    USING (
        (
            entry_type = 'prize'
            AND member_id IN (
                SELECT m.member_id
                FROM public.members AS m
                WHERE m.auth_user_id = auth.uid()
            )
            AND status = 'submitted'
        )
        OR public.is_admin()
    );

GRANT EXECUTE ON FUNCTION public.get_verified_vote_count(UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_vote_count_with_judge_multiplier(UUID, UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_verified_vote_count_with_judge_multiplier(UUID, UUID) TO anon, authenticated;

GRANT SELECT ON public.competition_entries TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.competition_entries TO authenticated;

GRANT SELECT ON public.submissions TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.submissions TO authenticated;

GRANT SELECT ON public.submission_details TO anon, authenticated;
GRANT SELECT ON public.public_vote_counts TO anon, authenticated;
GRANT SELECT ON public.submission_votes_with_judges TO anon, authenticated;
GRANT SELECT ON public.submission_analytics_summary TO authenticated;
GRANT SELECT ON public.admin_fraud_dashboard TO authenticated;
GRANT SELECT ON public.votes_with_confidence TO authenticated;
GRANT SELECT ON public.votes_needing_review TO authenticated;
GRANT SELECT ON public.competition_leaderboard TO authenticated;
GRANT SELECT ON public.fraud_detection_summary TO authenticated;
