-- Arca Gidan -> shared Banodoco Supabase consolidation
-- Adds:
-- - private UUID<->Discord bridge for AG auth users
-- - AG competition/voting/analytics schema with ag_ prefix
-- - AG-facing profile/submission views
-- - content_records layer so AG submissions become shared content records
-- - Discord OAuth signup trigger and profile-picture storage policies

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- 1. Shared content records for cross-app linkage
-- ============================================================
CREATE TABLE IF NOT EXISTS content_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_app TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_id UUID NOT NULL,
    author_auth_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    author_member_id BIGINT REFERENCES discord_members(member_id) ON DELETE SET NULL,
    title TEXT,
    description TEXT,
    primary_url TEXT,
    thumbnail_url TEXT,
    content_type TEXT NOT NULL DEFAULT 'video'
        CHECK (content_type IN ('video', 'image', 'audio', 'link', 'mixed')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_app, source_table, source_id)
);

ALTER TABLE content_records ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public can read content_records" ON content_records;
CREATE POLICY "Public can read content_records"
    ON content_records FOR SELECT
    USING (true);

CREATE INDEX IF NOT EXISTS idx_content_records_author_auth_user_id
    ON content_records(author_auth_user_id);
CREATE INDEX IF NOT EXISTS idx_content_records_author_member_id
    ON content_records(author_member_id);

DROP TRIGGER IF EXISTS trg_content_records_updated_at ON content_records;
CREATE TRIGGER trg_content_records_updated_at
    BEFORE UPDATE ON content_records
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================
-- 2. Extend discord_members with AG-managed public fields only
--    Identity linkage lives in ag_user_identities to avoid exposing
--    auth UUIDs/emails through existing public discord_members reads.
-- ============================================================
ALTER TABLE discord_members
    ADD COLUMN IF NOT EXISTS bio TEXT,
    ADD COLUMN IF NOT EXISTS real_name TEXT,
    ADD COLUMN IF NOT EXISTS website_url TEXT,
    ADD COLUMN IF NOT EXISTS instagram_url TEXT,
    ADD COLUMN IF NOT EXISTS banodoco_owner BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_discord_members_banodoco_owner
    ON discord_members(banodoco_owner)
    WHERE banodoco_owner = TRUE;

-- ============================================================
-- 3. Private AG identity bridge
-- ============================================================
CREATE TABLE IF NOT EXISTS ag_user_identities (
    auth_user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    member_id BIGINT NOT NULL UNIQUE REFERENCES discord_members(member_id) ON DELETE CASCADE,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE ag_user_identities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view own AG identity" ON ag_user_identities;
CREATE POLICY "Users can view own AG identity"
    ON ag_user_identities FOR SELECT
    TO authenticated
    USING (auth.uid() = auth_user_id);

DROP POLICY IF EXISTS "Users can update own AG identity" ON ag_user_identities;
CREATE POLICY "Users can update own AG identity"
    ON ag_user_identities FOR UPDATE
    TO authenticated
    USING (auth.uid() = auth_user_id)
    WITH CHECK (auth.uid() = auth_user_id);

DROP TRIGGER IF EXISTS trg_ag_user_identities_updated_at ON ag_user_identities;
CREATE TRIGGER trg_ag_user_identities_updated_at
    BEFORE UPDATE ON ag_user_identities
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================
-- 4. AG helper functions and Discord OAuth signup bridge
-- ============================================================
CREATE OR REPLACE FUNCTION ag_get_banodoco_owner_ids()
RETURNS BIGINT[] AS $$
BEGIN
    RETURN ARRAY[
        983731695691907102,
        666750555472920626,
        455300561034543105,
        492106319327789066,
        819379946878795786,
        843483141074976788,
        306765844359282688,
        282238411945934849,
        751600364129615882,
        288798368799653888,
        236457647824568320,
        186218000343367680,
        1006234707375173722,
        1013388614203359252,
        211685818622803970,
        703444869624102937,
        706657865229664266,
        1033424534520463507,
        756701878011691029,
        148527827157909504,
        216819983643181056,
        268736844118753280,
        1167191535922724934,
        1129774333003309116,
        385120143107424256,
        460527000793120769,
        422902797093306368,
        269745532639903744,
        649759357290020865,
        690307386145374448,
        627140525916422145,
        461142774599778306,
        352094797730676748,
        350433941922119680,
        184092236676333568,
        254874349159448576,
        250118360711430144,
        695251738273513502,
        614602980943069214,
        1059592307579564162,
        942872661250437130,
        1074404980737450065,
        123694023910227968,
        423349485712834561,
        879714655356997692,
        1083233555188031629,
        1090035586582196326,
        103595801015488512,
        217213209831145474,
        972157669408395334,
        242210988370165760,
        906982447151984720,
        994554775980998696,
        240965201241964544,
        689521911918886953,
        679317699448274973,
        1058233905293053983,
        310003312936091650,
        809491434344284240,
        968570531047690270,
        283755228740976661,
        606012921935429632,
        323115728146399234,
        946684593912815616,
        1061984538332516392,
        1049226579001233429,
        145754764414550017,
        217815920981049355,
        421446598703317015,
        240166269607870464,
        474070326192504842,
        843663462100893727,
        978367254431408142,
        739880702421500066,
        981143322116628490,
        1051782690946424842,
        1078770090050326680,
        983060020516249670,
        273227187862110218,
        273403593845899265,
        825444296689451039,
        525933180130426891,
        1027796031267684454,
        177807077912215552,
        371754999304290304,
        544126527852380190,
        857355125172469801,
        712453331959808002,
        222006941491265536,
        1099257978277867542,
        771193439399444490,
        137009159173308416,
        160047720931786752,
        994005407016161350,
        396817315997417493,
        189142875613822976,
        691480985056837644,
        228118453062467585,
        179206814720720896,
        369506666473062403,
        264527454285070348,
        498650277860081684,
        875595585045012502,
        136060502697705472,
        919470327506030593,
        610121728664010753,
        412292838114459648,
        155631980749389824,
        380911583074975744,
        822313193791029248,
        314183563719475200,
        854762326212345956,
        816673722093928449,
        237937096717762560,
        230186969588695041,
        168373586812207104,
        499629400254447616,
        830738807811866624,
        439838009001639936,
        374866446976548881,
        1098816459850915861,
        308348673031667712,
        926222468388098068,
        240980243211354113,
        239433028537942017,
        464322697418244096,
        141501701847777280,
        750242555408547940,
        714528038112591962,
        372525087120949258,
        1251563614104457299,
        210800518883311618,
        257217392298426380,
        673139523500113933,
        518920257121943552,
        1013923910821093517,
        88822364468412416,
        1079392509479899217,
        497770368853999628,
        305473730715320321,
        301463647895683072,
        627665325701201930,
        210245371002093570,
        348248198210387970,
        823564252748709918,
        199568892345974785,
        234537784533057536,
        433293460053950466,
        217051700085784577,
        378668580822188033,
        665773783902191620,
        176766258430083074,
        738104484114464889,
        947112250198618162,
        233733151879331843,
        133784166977372160,
        809159895593123852,
        692109544339406870,
        681356078914469966,
        1171374421739577367,
        256155058620727306,
        807028745344385034,
        294680499769835520,
        1035550271838887996,
        594573289293217803,
        246063417750716416,
        391075181642252289,
        372902354397429772,
        407614876815589376,
        586961856271351833,
        827942076288860160,
        743301749086879808,
        602172463367061524,
        552281744460742657,
        702949481997402132,
        653324948957429801,
        272911326010015745,
        454824938529095680,
        1011311690102603878,
        623923865864765452,
        1090239761874157639,
        688343645644259328,
        439811659729469441,
        318630871747788800,
        430030959874932737
    ]::BIGINT[];
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION ag_is_banodoco_owner(p_member_id BIGINT)
RETURNS BOOLEAN AS $$
BEGIN
    IF p_member_id IS NULL THEN
        RETURN FALSE;
    END IF;
    RETURN p_member_id = ANY(ag_get_banodoco_owner_ids());
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION ag_apply_banodoco_owner_flag()
RETURNS TRIGGER AS $$
BEGIN
    NEW.banodoco_owner := ag_is_banodoco_owner(NEW.member_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_discord_members_ag_owner_flag ON discord_members;
CREATE TRIGGER trg_discord_members_ag_owner_flag
    BEFORE INSERT OR UPDATE OF member_id ON discord_members
    FOR EACH ROW
    EXECUTE FUNCTION ag_apply_banodoco_owner_flag();

UPDATE discord_members
SET banodoco_owner = ag_is_banodoco_owner(member_id)
WHERE COALESCE(banodoco_owner, FALSE) IS DISTINCT FROM ag_is_banodoco_owner(member_id);

CREATE OR REPLACE FUNCTION ag_extract_discord_created_at(p_discord_id TEXT)
RETURNS TIMESTAMPTZ AS $$
DECLARE
    v_snowflake NUMERIC;
BEGIN
    IF p_discord_id IS NULL OR p_discord_id !~ '^[0-9]+$' THEN
        RETURN NULL;
    END IF;

    v_snowflake := p_discord_id::NUMERIC;
    RETURN to_timestamp(((v_snowflake / 4194304)::BIGINT + 1420070400000) / 1000.0);
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION ag_link_auth_identity(
    p_auth_user_id UUID,
    p_member_id BIGINT,
    p_email TEXT DEFAULT NULL
)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO ag_user_identities (auth_user_id, member_id, email)
    VALUES (p_auth_user_id, p_member_id, p_email)
    ON CONFLICT (auth_user_id) DO UPDATE
    SET
        member_id = EXCLUDED.member_id,
        email = COALESCE(EXCLUDED.email, ag_user_identities.email),
        updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
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

    INSERT INTO discord_members (
        member_id,
        username,
        global_name,
        avatar_url,
        discriminator,
        discord_created_at,
        banodoco_owner
    )
    VALUES (
        discord_id_bigint,
        username_value,
        display_name_value,
        avatar_url_value,
        discriminator_value,
        ag_extract_discord_created_at(discord_id_text),
        ag_is_banodoco_owner(discord_id_bigint)
    )
    ON CONFLICT (member_id) DO UPDATE
    SET
        username = COALESCE(EXCLUDED.username, discord_members.username),
        global_name = COALESCE(EXCLUDED.global_name, discord_members.global_name),
        avatar_url = COALESCE(EXCLUDED.avatar_url, discord_members.avatar_url),
        discriminator = COALESCE(EXCLUDED.discriminator, discord_members.discriminator),
        discord_created_at = COALESCE(discord_members.discord_created_at, EXCLUDED.discord_created_at),
        banodoco_owner = ag_is_banodoco_owner(discord_members.member_id),
        updated_at = NOW();

    PERFORM ag_link_auth_identity(NEW.id, discord_id_bigint, NEW.email);

    RETURN NEW;
EXCEPTION
    WHEN unique_violation THEN
        RETURN NEW;
    WHEN foreign_key_violation THEN
        RAISE WARNING 'AG handle_new_user foreign key failure for user %: %', NEW.id, SQLERRM;
        RETURN NEW;
    WHEN OTHERS THEN
        RAISE WARNING 'AG handle_new_user failed for user % (%): %', NEW.id, SQLSTATE, SQLERRM;
        RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();

CREATE OR REPLACE VIEW ag_profiles AS
SELECT
    aui.auth_user_id AS id,
    dm.member_id::TEXT AS discord_id,
    dm.username AS discord_username,
    dm.discriminator AS discord_discriminator,
    COALESCE(dm.global_name, dm.username) AS display_name,
    COALESCE(dm.stored_avatar_url, dm.avatar_url) AS avatar_url,
    CASE
        WHEN auth.uid() = aui.auth_user_id THEN aui.email
        ELSE NULL
    END AS email,
    dm.bio,
    dm.real_name,
    dm.website_url,
    dm.instagram_url,
    dm.twitter_url,
    dm.discord_created_at AS discord_account_created_at,
    COALESCE(dm.banodoco_owner, FALSE) AS banodoco_owner,
    aui.created_at,
    GREATEST(aui.updated_at, dm.updated_at) AS updated_at
FROM ag_user_identities aui
JOIN discord_members dm ON dm.member_id = aui.member_id;

GRANT SELECT ON ag_profiles TO anon, authenticated;

CREATE OR REPLACE FUNCTION ag_update_profile(p_profile JSONB DEFAULT '{}'::jsonb)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_auth_user_id UUID;
BEGIN
    v_auth_user_id := auth.uid();

    IF v_auth_user_id IS NULL THEN
        RAISE EXCEPTION 'Authentication required';
    END IF;

    UPDATE discord_members dm
    SET
        bio = CASE
            WHEN p_profile ? 'bio' THEN NULLIF(BTRIM(p_profile->>'bio'), '')
            ELSE dm.bio
        END,
        real_name = CASE
            WHEN p_profile ? 'real_name' THEN NULLIF(BTRIM(p_profile->>'real_name'), '')
            ELSE dm.real_name
        END,
        website_url = CASE
            WHEN p_profile ? 'website_url' THEN NULLIF(BTRIM(p_profile->>'website_url'), '')
            ELSE dm.website_url
        END,
        instagram_url = CASE
            WHEN p_profile ? 'instagram_url' THEN NULLIF(BTRIM(p_profile->>'instagram_url'), '')
            ELSE dm.instagram_url
        END,
        twitter_url = CASE
            WHEN p_profile ? 'twitter_url' THEN NULLIF(BTRIM(p_profile->>'twitter_url'), '')
            ELSE dm.twitter_url
        END,
        stored_avatar_url = CASE
            WHEN p_profile ? 'avatar_url' THEN NULLIF(BTRIM(p_profile->>'avatar_url'), '')
            ELSE dm.stored_avatar_url
        END,
        updated_at = NOW()
    FROM ag_user_identities aui
    WHERE aui.auth_user_id = v_auth_user_id
      AND aui.member_id = dm.member_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No Arca Gidan profile is linked to auth user %', v_auth_user_id;
    END IF;
END;
$$ LANGUAGE plpgsql;

GRANT EXECUTE ON FUNCTION ag_update_profile(JSONB) TO authenticated;
GRANT EXECUTE ON FUNCTION ag_link_auth_identity(UUID, BIGINT, TEXT) TO service_role;

-- ============================================================
-- 5. AG schema
-- ============================================================
CREATE TABLE IF NOT EXISTS ag_competitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    start_date TIMESTAMPTZ NOT NULL,
    end_date TIMESTAMPTZ NOT NULL,
    submission_start TIMESTAMPTZ NOT NULL,
    submission_end TIMESTAMPTZ NOT NULL,
    voting_start TIMESTAMPTZ,
    voting_end TIMESTAMPTZ,
    results_announced_at TIMESTAMPTZ,
    themes_announced_at TIMESTAMPTZ,
    theme TEXT,
    themes JSONB NOT NULL DEFAULT '[]'::jsonb,
    prizes JSONB,
    rules TEXT,
    settings JSONB NOT NULL DEFAULT '{"max_votes": 5, "judge_multiplier": 1}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ag_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    competition_id UUID NOT NULL REFERENCES ag_competitions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    content_record_id UUID UNIQUE REFERENCES content_records(id) ON DELETE SET NULL,
    theme TEXT,
    video_url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tools_used TEXT[] NOT NULL DEFAULT '{}',
    thumbnail_url TEXT,
    duration_seconds INTEGER,
    additional_links JSONB,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'under_review', 'approved', 'rejected', 'winner', 'finalist')),
    admin_notes TEXT,
    score NUMERIC(10, 2),
    vote_count INTEGER NOT NULL DEFAULT 0,
    winner BOOLEAN NOT NULL DEFAULT FALSE,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (competition_id, user_id)
);

CREATE TABLE IF NOT EXISTS ag_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    submission_id UUID NOT NULL REFERENCES ag_submissions(id) ON DELETE CASCADE,
    competition_id UUID NOT NULL REFERENCES ag_competitions(id) ON DELETE CASCADE,
    ip_hash TEXT,
    user_agent TEXT,
    vote_duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, submission_id)
);

CREATE TABLE IF NOT EXISTS ag_submission_analytics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id UUID NOT NULL REFERENCES ag_submissions(id) ON DELETE CASCADE,
    user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    competition_id UUID NOT NULL REFERENCES ag_competitions(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    view_duration_seconds INTEGER NOT NULL DEFAULT 0,
    video_played BOOLEAN NOT NULL DEFAULT FALSE,
    video_play_duration_seconds INTEGER NOT NULL DEFAULT 0,
    video_completed BOOLEAN NOT NULL DEFAULT FALSE,
    device_type TEXT,
    referrer TEXT,
    ip_hash TEXT,
    user_agent TEXT,
    total_view_duration_seconds INTEGER NOT NULL DEFAULT 0,
    visit_count INTEGER NOT NULL DEFAULT 1,
    last_viewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, submission_id)
);

CREATE TABLE IF NOT EXISTS ag_vote_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vote_id UUID NOT NULL UNIQUE REFERENCES ag_votes(id) ON DELETE CASCADE,
    reviewed_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    is_legitimate BOOLEAN NOT NULL,
    review_notes TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ag_fraud_detection_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    config_key TEXT NOT NULL UNIQUE,
    config_value JSONB NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by UUID REFERENCES auth.users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ag_admin_users (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    granted_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ag_competitions_active
    ON ag_competitions(is_active)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_ag_submissions_competition_id
    ON ag_submissions(competition_id);
CREATE INDEX IF NOT EXISTS idx_ag_submissions_user_id
    ON ag_submissions(user_id);
CREATE INDEX IF NOT EXISTS idx_ag_submissions_status
    ON ag_submissions(status);
CREATE INDEX IF NOT EXISTS idx_ag_votes_competition_id
    ON ag_votes(competition_id);
CREATE INDEX IF NOT EXISTS idx_ag_votes_submission_id
    ON ag_votes(submission_id);
CREATE INDEX IF NOT EXISTS idx_ag_votes_user_id
    ON ag_votes(user_id);
CREATE INDEX IF NOT EXISTS idx_ag_votes_ip_hash
    ON ag_votes(ip_hash);
CREATE INDEX IF NOT EXISTS idx_ag_submission_analytics_competition_id
    ON ag_submission_analytics(competition_id);
CREATE INDEX IF NOT EXISTS idx_ag_submission_analytics_submission_id
    ON ag_submission_analytics(submission_id);
CREATE INDEX IF NOT EXISTS idx_ag_submission_analytics_user_id
    ON ag_submission_analytics(user_id);
CREATE INDEX IF NOT EXISTS idx_ag_submission_analytics_ip_hash
    ON ag_submission_analytics(ip_hash);

DROP TRIGGER IF EXISTS trg_ag_competitions_updated_at ON ag_competitions;
CREATE TRIGGER trg_ag_competitions_updated_at
    BEFORE UPDATE ON ag_competitions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_ag_submissions_updated_at ON ag_submissions;
CREATE TRIGGER trg_ag_submissions_updated_at
    BEFORE UPDATE ON ag_submissions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_ag_submission_analytics_updated_at ON ag_submission_analytics;
CREATE TRIGGER trg_ag_submission_analytics_updated_at
    BEFORE UPDATE ON ag_submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

ALTER TABLE ag_competitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ag_submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ag_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE ag_submission_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE ag_vote_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE ag_fraud_detection_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE ag_admin_users ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION ag_is_admin(check_user_id UUID DEFAULT NULL)
RETURNS BOOLEAN
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF check_user_id IS NULL THEN
        check_user_id := auth.uid();
    END IF;

    RETURN EXISTS (
        SELECT 1
        FROM ag_admin_users
        WHERE user_id = check_user_id
    );
END;
$$ LANGUAGE plpgsql STABLE;

GRANT EXECUTE ON FUNCTION ag_is_admin(UUID) TO anon, authenticated;

CREATE OR REPLACE FUNCTION ag_get_fraud_config(p_key TEXT)
RETURNS JSONB AS $$
    SELECT config_value
    FROM ag_fraud_detection_config
    WHERE config_key = p_key;
$$ LANGUAGE sql STABLE;

INSERT INTO ag_fraud_detection_config (config_key, config_value, description)
VALUES
    (
        'account_age_thresholds',
        '{
          "very_new_hours": 1,
          "new_hours": 24,
          "recent_hours": 168,
          "very_new_penalty": 40,
          "new_penalty": 25,
          "recent_penalty": 10
        }'::jsonb,
        'Account age thresholds and penalties'
    ),
    (
        'vote_speed_thresholds',
        '{
          "instant_ms": 3000,
          "quick_ms": 10000,
          "instant_penalty": 30,
          "quick_penalty": 15
        }'::jsonb,
        'Vote speed thresholds and penalties'
    ),
    (
        'ip_sharing_thresholds',
        '{
          "high_risk_count": 5,
          "medium_risk_count": 3,
          "high_risk_penalty": 20,
          "medium_risk_penalty": 10
        }'::jsonb,
        'IP sharing detection thresholds'
    ),
    (
        'voting_pattern_thresholds',
        '{
          "min_votes_to_check": 3,
          "single_creator_penalty": 15,
          "two_creators_penalty": 8
        }'::jsonb,
        'Coordinated voting pattern detection'
    ),
    (
        'user_agent_penalty',
        '{
          "missing_penalty": 10
        }'::jsonb,
        'Missing user agent penalty'
    )
ON CONFLICT (config_key) DO NOTHING;

CREATE OR REPLACE FUNCTION ag_hash_ip_address(ip_text TEXT)
RETURNS TEXT AS $$
BEGIN
    RETURN encode(digest(ip_text::bytea, 'sha256'), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION ag_capture_vote_ip()
RETURNS TRIGGER AS $$
BEGIN
    BEGIN
        NEW.ip_hash := ag_hash_ip_address(
            COALESCE(
                current_setting('request.headers', TRUE)::jsonb->>'x-real-ip',
                current_setting('request.headers', TRUE)::jsonb->>'x-forwarded-for',
                'unknown'
            )
        );
    EXCEPTION WHEN OTHERS THEN
        NEW.ip_hash := ag_hash_ip_address('unknown');
    END;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ag_capture_analytics_ip()
RETURNS TRIGGER AS $$
BEGIN
    BEGIN
        NEW.ip_hash := ag_hash_ip_address(
            COALESCE(
                current_setting('request.headers', TRUE)::jsonb->>'x-real-ip',
                current_setting('request.headers', TRUE)::jsonb->>'x-forwarded-for',
                'unknown'
            )
        );
    EXCEPTION WHEN OTHERS THEN
        NEW.ip_hash := ag_hash_ip_address('unknown');
    END;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ag_votes_capture_ip ON ag_votes;
CREATE TRIGGER trg_ag_votes_capture_ip
    BEFORE INSERT ON ag_votes
    FOR EACH ROW
    EXECUTE FUNCTION ag_capture_vote_ip();

DROP TRIGGER IF EXISTS trg_ag_submission_analytics_capture_ip ON ag_submission_analytics;
CREATE TRIGGER trg_ag_submission_analytics_capture_ip
    BEFORE INSERT ON ag_submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION ag_capture_analytics_ip();

CREATE OR REPLACE FUNCTION ag_sync_submission_content_record()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_content_record_id UUID;
    v_member_id BIGINT;
BEGIN
    IF NEW.id IS NULL THEN
        NEW.id := gen_random_uuid();
    END IF;

    SELECT member_id
    INTO v_member_id
    FROM ag_user_identities
    WHERE auth_user_id = NEW.user_id;

    INSERT INTO content_records (
        source_app,
        source_table,
        source_id,
        author_auth_user_id,
        author_member_id,
        title,
        description,
        primary_url,
        thumbnail_url,
        content_type,
        metadata
    )
    VALUES (
        'arca-gidan',
        'ag_submissions',
        NEW.id,
        NEW.user_id,
        v_member_id,
        NEW.title,
        NEW.description,
        NEW.video_url,
        NEW.thumbnail_url,
        'video',
        jsonb_build_object(
            'competition_id', NEW.competition_id,
            'theme', NEW.theme,
            'tools_used', NEW.tools_used,
            'status', NEW.status,
            'additional_links', COALESCE(NEW.additional_links, 'null'::jsonb),
            'winner', NEW.winner,
            'duration_seconds', NEW.duration_seconds
        )
    )
    ON CONFLICT (source_app, source_table, source_id) DO UPDATE
    SET
        author_auth_user_id = EXCLUDED.author_auth_user_id,
        author_member_id = EXCLUDED.author_member_id,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        primary_url = EXCLUDED.primary_url,
        thumbnail_url = EXCLUDED.thumbnail_url,
        content_type = EXCLUDED.content_type,
        metadata = EXCLUDED.metadata,
        updated_at = NOW()
    RETURNING id INTO v_content_record_id;

    NEW.content_record_id := v_content_record_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ag_delete_submission_content_record()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    DELETE FROM content_records
    WHERE id = OLD.content_record_id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ag_submissions_sync_content_record ON ag_submissions;
CREATE TRIGGER trg_ag_submissions_sync_content_record
    BEFORE INSERT OR UPDATE ON ag_submissions
    FOR EACH ROW
    EXECUTE FUNCTION ag_sync_submission_content_record();

DROP TRIGGER IF EXISTS trg_ag_submissions_delete_content_record ON ag_submissions;
CREATE TRIGGER trg_ag_submissions_delete_content_record
    AFTER DELETE ON ag_submissions
    FOR EACH ROW
    EXECUTE FUNCTION ag_delete_submission_content_record();

CREATE OR REPLACE FUNCTION ag_prevent_self_voting()
RETURNS TRIGGER AS $$
DECLARE
    v_submission_owner UUID;
BEGIN
    SELECT user_id
    INTO v_submission_owner
    FROM ag_submissions
    WHERE id = NEW.submission_id;

    IF v_submission_owner = NEW.user_id THEN
        RAISE EXCEPTION 'You cannot vote for your own submission';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ag_enforce_max_votes()
RETURNS TRIGGER AS $$
DECLARE
    v_max_votes INTEGER;
    v_vote_count INTEGER;
BEGIN
    SELECT COALESCE((settings->>'max_votes')::INTEGER, 5)
    INTO v_max_votes
    FROM ag_competitions
    WHERE id = NEW.competition_id;

    SELECT COUNT(*)
    INTO v_vote_count
    FROM ag_votes
    WHERE user_id = NEW.user_id
      AND competition_id = NEW.competition_id;

    IF v_vote_count >= v_max_votes THEN
        RAISE EXCEPTION 'Maximum of % votes allowed per competition', v_max_votes;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ag_update_submission_vote_count()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_submission_id UUID;
BEGIN
    v_submission_id := COALESCE(NEW.submission_id, OLD.submission_id);

    UPDATE ag_submissions
    SET vote_count = (
        SELECT COUNT(*)
        FROM ag_votes
        WHERE submission_id = v_submission_id
    )
    WHERE id = v_submission_id;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ag_votes_prevent_self_voting ON ag_votes;
CREATE TRIGGER trg_ag_votes_prevent_self_voting
    BEFORE INSERT ON ag_votes
    FOR EACH ROW
    EXECUTE FUNCTION ag_prevent_self_voting();

DROP TRIGGER IF EXISTS trg_ag_votes_enforce_max_votes ON ag_votes;
CREATE TRIGGER trg_ag_votes_enforce_max_votes
    BEFORE INSERT ON ag_votes
    FOR EACH ROW
    EXECUTE FUNCTION ag_enforce_max_votes();

DROP TRIGGER IF EXISTS trg_ag_votes_update_submission_vote_count ON ag_votes;
CREATE TRIGGER trg_ag_votes_update_submission_vote_count
    AFTER INSERT OR DELETE ON ag_votes
    FOR EACH ROW
    EXECUTE FUNCTION ag_update_submission_vote_count();

CREATE OR REPLACE FUNCTION ag_calculate_vote_confidence(p_vote_id UUID)
RETURNS INTEGER AS $$
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
    account_config := ag_get_fraud_config('account_age_thresholds');
    speed_config := ag_get_fraud_config('vote_speed_thresholds');
    ip_config := ag_get_fraud_config('ip_sharing_thresholds');
    pattern_config := ag_get_fraud_config('voting_pattern_thresholds');
    ua_config := ag_get_fraud_config('user_agent_penalty');

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
    FROM ag_votes
    WHERE id = p_vote_id;

    IF v_user_id IS NULL THEN
        RETURN 0;
    END IF;

    SELECT EXTRACT(EPOCH FROM (v_created_at - dm.discord_created_at)) / 3600.0
    INTO account_age_hours
    FROM ag_user_identities aui
    JOIN discord_members dm ON dm.member_id = aui.member_id
    WHERE aui.auth_user_id = v_user_id;

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
        FROM ag_votes
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
    FROM ag_votes v2
    JOIN ag_submissions s ON s.id = v2.submission_id
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
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION ag_get_verified_vote_count(p_submission_id UUID)
RETURNS BIGINT
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COUNT(v.id) FILTER (
        WHERE ag_calculate_vote_confidence(v.id) >= 40
           OR EXISTS (
               SELECT 1
               FROM ag_vote_reviews vr
               WHERE vr.vote_id = v.id
                 AND vr.is_legitimate = TRUE
           )
    )
    FROM ag_votes v
    WHERE v.submission_id = p_submission_id
      AND NOT EXISTS (
          SELECT 1
          FROM ag_vote_reviews vr
          WHERE vr.vote_id = v.id
            AND vr.is_legitimate = FALSE
      );
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION ag_get_vote_count_with_judge_multiplier(
    p_submission_id UUID,
    p_competition_id UUID DEFAULT NULL
)
RETURNS NUMERIC
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_competition_id UUID;
    v_judge_multiplier NUMERIC;
    v_weighted_count NUMERIC;
BEGIN
    IF p_competition_id IS NULL THEN
        SELECT competition_id
        INTO v_competition_id
        FROM ag_submissions
        WHERE id = p_submission_id;
    ELSE
        v_competition_id := p_competition_id;
    END IF;

    SELECT COALESCE((settings->>'judge_multiplier')::NUMERIC, 1)
    INTO v_judge_multiplier
    FROM ag_competitions
    WHERE id = v_competition_id;

    SELECT
        COUNT(v.id) FILTER (WHERE NOT COALESCE(dm.banodoco_owner, FALSE))
        + (
            COUNT(v.id) FILTER (WHERE COALESCE(dm.banodoco_owner, FALSE))
            * v_judge_multiplier
        )
    INTO v_weighted_count
    FROM ag_votes v
    LEFT JOIN ag_user_identities aui ON aui.auth_user_id = v.user_id
    LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
    WHERE v.submission_id = p_submission_id;

    RETURN COALESCE(v_weighted_count, 0);
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION ag_get_verified_vote_count_with_judge_multiplier(
    p_submission_id UUID,
    p_competition_id UUID DEFAULT NULL
)
RETURNS NUMERIC
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_competition_id UUID;
    v_judge_multiplier NUMERIC;
    v_weighted_count NUMERIC;
BEGIN
    IF p_competition_id IS NULL THEN
        SELECT competition_id
        INTO v_competition_id
        FROM ag_submissions
        WHERE id = p_submission_id;
    ELSE
        v_competition_id := p_competition_id;
    END IF;

    SELECT COALESCE((settings->>'judge_multiplier')::NUMERIC, 1)
    INTO v_judge_multiplier
    FROM ag_competitions
    WHERE id = v_competition_id;

    SELECT
        COUNT(v.id) FILTER (
            WHERE NOT COALESCE(dm.banodoco_owner, FALSE)
              AND (
                  ag_calculate_vote_confidence(v.id) >= 40
                  OR EXISTS (
                      SELECT 1
                      FROM ag_vote_reviews vr
                      WHERE vr.vote_id = v.id
                        AND vr.is_legitimate = TRUE
                  )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM ag_vote_reviews vr
                  WHERE vr.vote_id = v.id
                    AND vr.is_legitimate = FALSE
              )
        )
        + (
            COUNT(v.id) FILTER (
                WHERE COALESCE(dm.banodoco_owner, FALSE)
                  AND (
                      ag_calculate_vote_confidence(v.id) >= 40
                      OR EXISTS (
                          SELECT 1
                          FROM ag_vote_reviews vr
                          WHERE vr.vote_id = v.id
                            AND vr.is_legitimate = TRUE
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ag_vote_reviews vr
                      WHERE vr.vote_id = v.id
                        AND vr.is_legitimate = FALSE
                  )
            ) * v_judge_multiplier
        )
    INTO v_weighted_count
    FROM ag_votes v
    LEFT JOIN ag_user_identities aui ON aui.auth_user_id = v.user_id
    LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
    WHERE v.submission_id = p_submission_id;

    RETURN COALESCE(v_weighted_count, 0);
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION ag_update_fraud_config(
    p_config_key TEXT,
    p_new_value JSONB
)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT ag_is_admin() THEN
        RAISE EXCEPTION 'Only admins can update fraud detection config';
    END IF;

    UPDATE ag_fraud_detection_config
    SET
        config_value = p_new_value,
        updated_at = NOW(),
        updated_by = auth.uid()
    WHERE config_key = p_config_key;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ag_add_admin(target_user_id UUID)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT ag_is_admin() THEN
        RAISE EXCEPTION 'Only admins can grant admin access';
    END IF;

    INSERT INTO ag_admin_users (user_id, granted_by)
    VALUES (target_user_id, auth.uid())
    ON CONFLICT (user_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ag_remove_admin(target_user_id UUID)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT ag_is_admin() THEN
        RAISE EXCEPTION 'Only admins can revoke admin access';
    END IF;

    DELETE FROM ag_admin_users
    WHERE user_id = target_user_id;
END;
$$ LANGUAGE plpgsql;

GRANT EXECUTE ON FUNCTION ag_get_verified_vote_count(UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION ag_get_vote_count_with_judge_multiplier(UUID, UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION ag_get_verified_vote_count_with_judge_multiplier(UUID, UUID) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION ag_update_fraud_config(TEXT, JSONB) TO authenticated;
GRANT EXECUTE ON FUNCTION ag_add_admin(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION ag_remove_admin(UUID) TO authenticated;

CREATE OR REPLACE VIEW ag_submission_details AS
SELECT
    s.id,
    s.competition_id,
    s.user_id,
    s.content_record_id,
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
    ag_get_verified_vote_count(s.id) AS verified_vote_count,
    ag_get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS verified_weighted_vote_count,
    jsonb_build_object(
        'id', aui.auth_user_id,
        'discord_id', dm.member_id::TEXT,
        'discord_username', dm.username,
        'discord_discriminator', dm.discriminator,
        'display_name', COALESCE(dm.global_name, dm.username),
        'avatar_url', COALESCE(dm.stored_avatar_url, dm.avatar_url),
        'bio', dm.bio,
        'real_name', dm.real_name,
        'website_url', dm.website_url,
        'instagram_url', dm.instagram_url,
        'twitter_url', dm.twitter_url,
        'discord_account_created_at', dm.discord_created_at,
        'banodoco_owner', COALESCE(dm.banodoco_owner, FALSE)
    ) AS profile
FROM ag_submissions s
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = s.user_id
LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
WHERE s.status <> 'rejected';

CREATE OR REPLACE VIEW ag_public_vote_counts AS
SELECT
    s.id AS submission_id,
    s.competition_id,
    s.title,
    s.user_id AS creator_id,
    COALESCE(dm.global_name, dm.username) AS creator_name,
    ag_get_verified_vote_count(s.id) AS vote_count,
    ag_get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS weighted_vote_count,
    s.status
FROM ag_submissions s
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = s.user_id
LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
WHERE s.status <> 'rejected'
ORDER BY weighted_vote_count DESC;

CREATE OR REPLACE VIEW ag_submission_votes_with_judges AS
SELECT
    s.id AS submission_id,
    s.title,
    s.competition_id,
    COUNT(v.id) AS total_votes,
    COUNT(v.id) FILTER (WHERE NOT COALESCE(dm.banodoco_owner, FALSE)) AS regular_votes,
    COUNT(v.id) FILTER (WHERE COALESCE(dm.banodoco_owner, FALSE)) AS judge_votes,
    COALESCE((c.settings->>'judge_multiplier')::NUMERIC, 1) AS judge_multiplier,
    ag_get_vote_count_with_judge_multiplier(s.id, s.competition_id) AS weighted_vote_count,
    ag_get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS verified_weighted_vote_count,
    ag_get_verified_vote_count(s.id) AS verified_votes_no_multiplier,
    s.status
FROM ag_submissions s
LEFT JOIN ag_votes v ON v.submission_id = s.id
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = v.user_id
LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
LEFT JOIN ag_competitions c ON c.id = s.competition_id
WHERE s.status <> 'rejected'
GROUP BY s.id, s.title, s.competition_id, c.settings, s.status
ORDER BY verified_weighted_vote_count DESC;

CREATE OR REPLACE VIEW ag_submission_analytics_summary AS
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
FROM ag_submissions s
LEFT JOIN ag_submission_analytics sa ON s.id = sa.submission_id
WHERE ag_is_admin() OR s.user_id = auth.uid()
GROUP BY s.id, s.title, s.user_id;

CREATE OR REPLACE VIEW ag_admin_fraud_dashboard AS
SELECT
    v.id AS vote_id,
    v.submission_id,
    s.title AS submission_title,
    s.user_id AS submission_owner_id,
    v.user_id AS voter_id,
    COALESCE(dm.global_name, dm.username) AS voter_name,
    dm.username AS discord_username,
    v.created_at AS voted_at,
    v.vote_duration_ms,
    ROUND(EXTRACT(EPOCH FROM (v.created_at - dm.discord_created_at)) / 3600.0, 1) AS voter_account_age_hours,
    ag_calculate_vote_confidence(v.id) AS confidence_score,
    CASE
        WHEN ag_calculate_vote_confidence(v.id) >= 80 THEN 'HIGH'
        WHEN ag_calculate_vote_confidence(v.id) >= 60 THEN 'MEDIUM'
        WHEN ag_calculate_vote_confidence(v.id) >= 40 THEN 'LOW'
        ELSE 'VERY_LOW'
    END AS confidence_level,
    v.ip_hash,
    v.user_agent,
    (
        SELECT COUNT(DISTINCT v2.user_id)
        FROM ag_votes v2
        WHERE v2.ip_hash = v.ip_hash
    ) AS users_from_same_ip,
    (
        SELECT COUNT(*)
        FROM ag_votes v2
        WHERE v2.ip_hash = v.ip_hash
          AND v2.submission_id = v.submission_id
    ) AS votes_from_same_ip_for_submission,
    vr.is_legitimate AS manually_reviewed,
    vr.reviewed_by,
    vr.review_notes,
    vr.reviewed_at
FROM ag_votes v
JOIN ag_submissions s ON s.id = v.submission_id
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = v.user_id
LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
LEFT JOIN ag_vote_reviews vr ON vr.vote_id = v.id
WHERE ag_is_admin()
ORDER BY v.created_at DESC;

CREATE OR REPLACE VIEW ag_votes_with_confidence AS
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
FROM ag_admin_fraud_dashboard;

CREATE OR REPLACE VIEW ag_votes_needing_review AS
SELECT *
FROM ag_votes_with_confidence
WHERE confidence_score < 60
ORDER BY confidence_score ASC, voted_at DESC;

CREATE OR REPLACE VIEW ag_competition_leaderboard AS
SELECT
    s.id AS submission_id,
    s.title,
    s.user_id AS creator_id,
    COALESCE(dm.global_name, dm.username) AS creator_name,
    s.vote_count AS raw_votes,
    svj.verified_weighted_vote_count AS weighted_vote_count,
    ag_get_verified_vote_count(s.id) AS verified_votes,
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
FROM ag_submissions s
LEFT JOIN ag_submission_votes_with_judges svj ON svj.submission_id = s.id
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = s.user_id
LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
WHERE s.status <> 'rejected'
ORDER BY weighted_rank ASC;

CREATE OR REPLACE VIEW ag_fraud_detection_summary AS
SELECT
    COUNT(*) FILTER (WHERE users_from_same_ip >= 5) AS high_risk_ips,
    COUNT(DISTINCT voter_id) FILTER (WHERE confidence_score < 60) AS suspicious_users,
    COUNT(*) FILTER (WHERE votes_from_same_ip_for_submission >= 5) AS suspicious_time_clusters,
    COUNT(*) FILTER (WHERE voter_account_age_hours IS NOT NULL AND voter_account_age_hours < 24) AS votes_from_brand_new_accounts,
    COUNT(DISTINCT ip_hash) FILTER (WHERE confidence_score < 40) AS suspicious_signup_clusters,
    COUNT(DISTINCT submission_id) FILTER (WHERE voter_account_age_hours IS NOT NULL AND voter_account_age_hours < 24) AS submissions_with_new_account_votes,
    COUNT(DISTINCT submission_id) FILTER (WHERE confidence_score < 60) AS submissions_with_suspicious_activity
FROM ag_admin_fraud_dashboard;

GRANT SELECT ON ag_submission_details TO anon, authenticated;
GRANT SELECT ON ag_public_vote_counts TO anon, authenticated;
GRANT SELECT ON ag_submission_votes_with_judges TO anon, authenticated;
GRANT SELECT ON ag_submission_analytics_summary TO authenticated;
GRANT SELECT ON ag_admin_fraud_dashboard TO authenticated;
GRANT SELECT ON ag_votes_with_confidence TO authenticated;
GRANT SELECT ON ag_votes_needing_review TO authenticated;
GRANT SELECT ON ag_competition_leaderboard TO authenticated;
GRANT SELECT ON ag_fraud_detection_summary TO authenticated;

DROP POLICY IF EXISTS "Public can read AG competitions" ON ag_competitions;
CREATE POLICY "Public can read AG competitions"
    ON ag_competitions FOR SELECT
    TO anon, authenticated
    USING (true);

DROP POLICY IF EXISTS "Admins can mutate AG competitions" ON ag_competitions;
CREATE POLICY "Admins can mutate AG competitions"
    ON ag_competitions FOR ALL
    TO authenticated
    USING (ag_is_admin())
    WITH CHECK (ag_is_admin());

DROP POLICY IF EXISTS "Public can read visible AG submissions" ON ag_submissions;
CREATE POLICY "Public can read visible AG submissions"
    ON ag_submissions FOR SELECT
    TO anon, authenticated
    USING (status <> 'rejected' OR user_id = auth.uid() OR ag_is_admin());

DROP POLICY IF EXISTS "Users can insert own AG submissions" ON ag_submissions;
CREATE POLICY "Users can insert own AG submissions"
    ON ag_submissions FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can update own pending AG submissions" ON ag_submissions;
CREATE POLICY "Users can update own pending AG submissions"
    ON ag_submissions FOR UPDATE
    TO authenticated
    USING ((auth.uid() = user_id AND status = 'submitted') OR ag_is_admin())
    WITH CHECK ((auth.uid() = user_id AND status = 'submitted') OR ag_is_admin());

DROP POLICY IF EXISTS "Users can delete own pending AG submissions" ON ag_submissions;
CREATE POLICY "Users can delete own pending AG submissions"
    ON ag_submissions FOR DELETE
    TO authenticated
    USING ((auth.uid() = user_id AND status = 'submitted') OR ag_is_admin());

DROP POLICY IF EXISTS "Users can read own AG votes" ON ag_votes;
CREATE POLICY "Users can read own AG votes"
    ON ag_votes FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id OR ag_is_admin());

DROP POLICY IF EXISTS "Users can insert own AG votes" ON ag_votes;
CREATE POLICY "Users can insert own AG votes"
    ON ag_votes FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can delete own AG votes" ON ag_votes;
CREATE POLICY "Users can delete own AG votes"
    ON ag_votes FOR DELETE
    TO authenticated
    USING (auth.uid() = user_id OR ag_is_admin());

DROP POLICY IF EXISTS "Anyone can insert AG analytics" ON ag_submission_analytics;
CREATE POLICY "Anyone can insert AG analytics"
    ON ag_submission_analytics FOR INSERT
    TO anon, authenticated
    WITH CHECK (true);

DROP POLICY IF EXISTS "Anyone can update AG analytics" ON ag_submission_analytics;
CREATE POLICY "Anyone can update AG analytics"
    ON ag_submission_analytics FOR UPDATE
    TO anon, authenticated
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "Anyone can read AG analytics" ON ag_submission_analytics;
CREATE POLICY "Anyone can read AG analytics"
    ON ag_submission_analytics FOR SELECT
    TO anon, authenticated
    USING (true);

DROP POLICY IF EXISTS "Admins can manage AG vote reviews" ON ag_vote_reviews;
CREATE POLICY "Admins can manage AG vote reviews"
    ON ag_vote_reviews FOR ALL
    TO authenticated
    USING (ag_is_admin())
    WITH CHECK (ag_is_admin());

DROP POLICY IF EXISTS "Admins can read AG admin users" ON ag_admin_users;
CREATE POLICY "Admins can read AG admin users"
    ON ag_admin_users FOR SELECT
    TO authenticated
    USING (ag_is_admin());

DROP POLICY IF EXISTS "Admins can mutate AG admin users" ON ag_admin_users;
CREATE POLICY "Admins can mutate AG admin users"
    ON ag_admin_users FOR ALL
    TO authenticated
    USING (ag_is_admin())
    WITH CHECK (ag_is_admin());

DROP POLICY IF EXISTS "Admins can read AG fraud config" ON ag_fraud_detection_config;
CREATE POLICY "Admins can read AG fraud config"
    ON ag_fraud_detection_config FOR SELECT
    TO authenticated
    USING (ag_is_admin());

DROP POLICY IF EXISTS "Admins can mutate AG fraud config" ON ag_fraud_detection_config;
CREATE POLICY "Admins can mutate AG fraud config"
    ON ag_fraud_detection_config FOR ALL
    TO authenticated
    USING (ag_is_admin())
    WITH CHECK (ag_is_admin());

GRANT SELECT ON ag_competitions TO anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ag_submissions TO authenticated;
GRANT SELECT, INSERT, DELETE ON ag_votes TO authenticated;
GRANT SELECT, INSERT, UPDATE ON ag_submission_analytics TO anon, authenticated;
GRANT SELECT ON ag_vote_reviews TO authenticated;
GRANT SELECT ON ag_admin_users TO authenticated;
GRANT SELECT ON ag_fraud_detection_config TO authenticated;

-- ============================================================
-- 6. Profile picture bucket on shared instance
-- ============================================================
INSERT INTO storage.buckets (id, name, public)
VALUES ('profile-pictures', 'profile-pictures', TRUE)
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "AG users can upload own profile pictures" ON storage.objects;
CREATE POLICY "AG users can upload own profile pictures"
    ON storage.objects FOR INSERT
    TO authenticated
    WITH CHECK (
        bucket_id = 'profile-pictures'
        AND auth.uid()::TEXT = (storage.foldername(name))[1]
    );

DROP POLICY IF EXISTS "AG users can update own profile pictures" ON storage.objects;
CREATE POLICY "AG users can update own profile pictures"
    ON storage.objects FOR UPDATE
    TO authenticated
    USING (
        bucket_id = 'profile-pictures'
        AND auth.uid()::TEXT = (storage.foldername(name))[1]
    )
    WITH CHECK (
        bucket_id = 'profile-pictures'
        AND auth.uid()::TEXT = (storage.foldername(name))[1]
    );

DROP POLICY IF EXISTS "AG users can delete own profile pictures" ON storage.objects;
CREATE POLICY "AG users can delete own profile pictures"
    ON storage.objects FOR DELETE
    TO authenticated
    USING (
        bucket_id = 'profile-pictures'
        AND auth.uid()::TEXT = (storage.foldername(name))[1]
    );

DROP POLICY IF EXISTS "Public can read AG profile pictures" ON storage.objects;
CREATE POLICY "Public can read AG profile pictures"
    ON storage.objects FOR SELECT
    TO anon, authenticated
    USING (bucket_id = 'profile-pictures');
