-- Consolidate legacy OpenMuse profiles into discord_members + openmuse_profiles.
-- This migration is additive and keeps legacy profiles/media/assets writes working
-- until the application cutover is deployed.

CREATE TABLE IF NOT EXISTS openmuse_profiles (
    member_id BIGINT PRIMARY KEY REFERENCES discord_members(member_id) ON DELETE CASCADE,
    username TEXT NOT NULL UNIQUE,
    discord_connected BOOLEAN NOT NULL DEFAULT FALSE,
    links TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    background_image_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- username already has a unique index from the UNIQUE constraint

CREATE INDEX IF NOT EXISTS idx_openmuse_profiles_updated_at
    ON openmuse_profiles(updated_at DESC);

ALTER TABLE openmuse_profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read openmuse_profiles" ON openmuse_profiles;
CREATE POLICY "Public read openmuse_profiles"
    ON openmuse_profiles FOR SELECT
    USING (true);

DROP POLICY IF EXISTS "Service write openmuse_profiles" ON openmuse_profiles;
CREATE POLICY "Service write openmuse_profiles"
    ON openmuse_profiles FOR ALL
    USING (true)
    WITH CHECK (true);

GRANT SELECT ON openmuse_profiles TO anon, authenticated;

DROP TRIGGER IF EXISTS trg_openmuse_profiles_updated_at ON openmuse_profiles;
CREATE TRIGGER trg_openmuse_profiles_updated_at
    BEFORE UPDATE ON openmuse_profiles
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE OR REPLACE FUNCTION public.sync_legacy_profile_to_canonical()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_member_id BIGINT;
BEGIN
    IF NEW.discord_user_id IS NULL OR NEW.discord_user_id !~ '^[0-9]+$' THEN
        RETURN NEW;
    END IF;

    v_member_id := NEW.discord_user_id::BIGINT;

    INSERT INTO discord_members (
        member_id,
        username,
        global_name,
        avatar_url,
        bio,
        real_name
    )
    VALUES (
        v_member_id,
        COALESCE(NULLIF(NEW.discord_username, ''), NULLIF(NEW.username, ''), 'discord-user-' || NEW.discord_user_id),
        COALESCE(NULLIF(NEW.display_name, ''), NULLIF(NEW.discord_username, ''), NULLIF(NEW.username, '')),
        NULLIF(NEW.avatar_url, ''),
        NULLIF(NEW.description, ''),
        NULLIF(NEW.real_name, '')
    )
    ON CONFLICT (member_id) DO UPDATE
    SET
        username = COALESCE(EXCLUDED.username, discord_members.username),
        global_name = COALESCE(EXCLUDED.global_name, discord_members.global_name),
        avatar_url = COALESCE(EXCLUDED.avatar_url, discord_members.avatar_url),
        bio = CASE
            WHEN discord_members.bio IS NULL THEN EXCLUDED.bio
            WHEN TG_OP = 'UPDATE'
                AND OLD.description IS NOT DISTINCT FROM discord_members.bio
            THEN COALESCE(EXCLUDED.bio, discord_members.bio)
            ELSE discord_members.bio
        END,
        real_name = CASE
            WHEN discord_members.real_name IS NULL THEN EXCLUDED.real_name
            WHEN TG_OP = 'UPDATE'
                AND OLD.real_name IS NOT DISTINCT FROM discord_members.real_name
            THEN COALESCE(EXCLUDED.real_name, discord_members.real_name)
            ELSE discord_members.real_name
        END,
        updated_at = NOW();

    INSERT INTO openmuse_profiles (
        member_id,
        username,
        discord_connected,
        links,
        background_image_url,
        created_at,
        updated_at
    )
    VALUES (
        v_member_id,
        COALESCE(NULLIF(NEW.username, ''), COALESCE(NULLIF(NEW.discord_username, ''), 'discord-user-' || NEW.discord_user_id)),
        COALESCE(NEW.discord_connected, FALSE),
        COALESCE(NEW.links, ARRAY[]::TEXT[]),
        NULLIF(NEW.background_image_url, ''),
        COALESCE(NEW.created_at, NOW()),
        NOW()
    )
    ON CONFLICT (member_id) DO UPDATE
    SET
        username = COALESCE(EXCLUDED.username, openmuse_profiles.username),
        discord_connected = openmuse_profiles.discord_connected OR COALESCE(EXCLUDED.discord_connected, FALSE),
        links = CASE
            WHEN COALESCE(array_length(EXCLUDED.links, 1), 0) > 0 THEN EXCLUDED.links
            ELSE openmuse_profiles.links
        END,
        background_image_url = COALESCE(EXCLUDED.background_image_url, openmuse_profiles.background_image_url),
        updated_at = NOW();

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.sync_legacy_profile_fk_to_member_id()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_member_id BIGINT;
BEGIN
    IF NEW.member_id IS NOT NULL OR NEW.user_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT p.discord_user_id::BIGINT
    INTO v_member_id
    FROM profiles p
    WHERE p.id = NEW.user_id
      AND p.discord_user_id IS NOT NULL
      AND p.discord_user_id ~ '^[0-9]+$'
    LIMIT 1;

    IF v_member_id IS NOT NULL THEN
        NEW.member_id := v_member_id;
    END IF;

    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF to_regclass('public.profiles') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_profiles_sync_to_openmuse_canonical ON public.profiles';
        EXECUTE '
            CREATE TRIGGER trg_profiles_sync_to_openmuse_canonical
            AFTER INSERT OR UPDATE ON public.profiles
            FOR EACH ROW
            EXECUTE FUNCTION public.sync_legacy_profile_to_canonical()
        ';
    END IF;

    IF to_regclass('public.media') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.media ADD COLUMN IF NOT EXISTS member_id BIGINT';
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_media_member_id ON public.media(member_id)';

        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'fk_media_member_id'
              AND conrelid = 'public.media'::regclass
        ) THEN
            EXECUTE '
                ALTER TABLE public.media
                ADD CONSTRAINT fk_media_member_id
                FOREIGN KEY (member_id) REFERENCES public.discord_members(member_id)
                ON DELETE SET NULL
            ';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'media'
              AND column_name = 'user_id'
        ) THEN
            EXECUTE 'DROP TRIGGER IF EXISTS trg_media_sync_member_id ON public.media';
            EXECUTE '
                CREATE TRIGGER trg_media_sync_member_id
                BEFORE INSERT OR UPDATE OF user_id, member_id ON public.media
                FOR EACH ROW
                EXECUTE FUNCTION public.sync_legacy_profile_fk_to_member_id()
            ';
        END IF;
    END IF;

    IF to_regclass('public.assets') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.assets ADD COLUMN IF NOT EXISTS member_id BIGINT';
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_assets_member_id ON public.assets(member_id)';

        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'fk_assets_member_id'
              AND conrelid = 'public.assets'::regclass
        ) THEN
            EXECUTE '
                ALTER TABLE public.assets
                ADD CONSTRAINT fk_assets_member_id
                FOREIGN KEY (member_id) REFERENCES public.discord_members(member_id)
                ON DELETE SET NULL
            ';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'assets'
              AND column_name = 'user_id'
        ) THEN
            EXECUTE 'DROP TRIGGER IF EXISTS trg_assets_sync_member_id ON public.assets';
            EXECUTE '
                CREATE TRIGGER trg_assets_sync_member_id
                BEFORE INSERT OR UPDATE OF user_id, member_id ON public.assets
                FOR EACH ROW
                EXECUTE FUNCTION public.sync_legacy_profile_fk_to_member_id()
            ';
        END IF;
    END IF;
END;
$$;

DO $$
BEGIN
    IF to_regclass('public.profiles') IS NOT NULL THEN
        INSERT INTO discord_members (
            member_id,
            username,
            global_name,
            avatar_url,
            bio,
            real_name
        )
        SELECT
            p.discord_user_id::BIGINT,
            COALESCE(NULLIF(p.discord_username, ''), NULLIF(p.username, ''), 'discord-user-' || p.discord_user_id),
            COALESCE(NULLIF(p.display_name, ''), NULLIF(p.discord_username, ''), NULLIF(p.username, '')),
            NULLIF(p.avatar_url, ''),
            NULLIF(p.description, ''),
            NULLIF(p.real_name, '')
        FROM profiles p
        WHERE p.discord_user_id IS NOT NULL
          AND p.discord_user_id ~ '^[0-9]+$'
        ON CONFLICT (member_id) DO UPDATE
        SET
            username = COALESCE(EXCLUDED.username, discord_members.username),
            global_name = COALESCE(EXCLUDED.global_name, discord_members.global_name),
            avatar_url = COALESCE(EXCLUDED.avatar_url, discord_members.avatar_url),
            bio = COALESCE(discord_members.bio, EXCLUDED.bio),
            real_name = COALESCE(discord_members.real_name, EXCLUDED.real_name),
            updated_at = NOW();

        INSERT INTO openmuse_profiles (
            member_id,
            username,
            discord_connected,
            links,
            background_image_url,
            created_at,
            updated_at
        )
        SELECT
            p.discord_user_id::BIGINT,
            COALESCE(NULLIF(p.username, ''), COALESCE(NULLIF(p.discord_username, ''), 'discord-user-' || p.discord_user_id)),
            COALESCE(p.discord_connected, FALSE),
            COALESCE(p.links, ARRAY[]::TEXT[]),
            NULLIF(p.background_image_url, ''),
            COALESCE(p.created_at, NOW()),
            NOW()
        FROM profiles p
        WHERE p.discord_user_id IS NOT NULL
          AND p.discord_user_id ~ '^[0-9]+$'
        ON CONFLICT (member_id) DO UPDATE
        SET
            username = COALESCE(EXCLUDED.username, openmuse_profiles.username),
            discord_connected = openmuse_profiles.discord_connected OR COALESCE(EXCLUDED.discord_connected, FALSE),
            links = CASE
                WHEN COALESCE(array_length(EXCLUDED.links, 1), 0) > 0 THEN EXCLUDED.links
                ELSE openmuse_profiles.links
            END,
            background_image_url = COALESCE(EXCLUDED.background_image_url, openmuse_profiles.background_image_url),
            updated_at = NOW();
    END IF;

    IF to_regclass('public.media') IS NOT NULL
       AND to_regclass('public.profiles') IS NOT NULL THEN
        UPDATE media m
        SET member_id = p.discord_user_id::BIGINT
        FROM profiles p
        WHERE m.user_id = p.id
          AND m.member_id IS NULL
          AND p.discord_user_id IS NOT NULL
          AND p.discord_user_id ~ '^[0-9]+$';
    END IF;

    IF to_regclass('public.assets') IS NOT NULL
       AND to_regclass('public.profiles') IS NOT NULL THEN
        UPDATE assets a
        SET member_id = p.discord_user_id::BIGINT
        FROM profiles p
        WHERE a.user_id = p.id
          AND a.member_id IS NULL
          AND p.discord_user_id IS NOT NULL
          AND p.discord_user_id ~ '^[0-9]+$';
    END IF;
END;
$$;

CREATE OR REPLACE VIEW openmuse_profile_view AS
SELECT
    op.member_id::TEXT AS discord_user_id,
    op.member_id,
    op.username,
    dm.username AS discord_username,
    COALESCE(dm.global_name, dm.username) AS display_name,
    COALESCE(dm.stored_avatar_url, dm.avatar_url) AS avatar_url,
    dm.bio AS description,
    dm.real_name,
    op.links,
    op.background_image_url,
    op.discord_connected,
    op.created_at,
    op.updated_at
FROM openmuse_profiles op
JOIN discord_members dm ON dm.member_id = op.member_id;

GRANT SELECT ON openmuse_profile_view TO anon, authenticated;
