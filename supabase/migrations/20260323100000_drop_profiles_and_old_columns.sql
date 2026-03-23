-- Finalize OpenMuse profile consolidation after the application cutover is live.
-- This migration is intentionally strict: it aborts if member_id backfills are incomplete.

DO $$
DECLARE
    v_missing_media BIGINT := 0;
    v_missing_assets BIGINT := 0;
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

        -- Also backfill profiles with NULL discord_user_id via ag_user_identities bridge
        INSERT INTO openmuse_profiles (member_id, username, discord_connected, links, background_image_url, created_at, updated_at)
        SELECT
            aui.member_id,
            COALESCE(NULLIF(p.username, ''), 'discord-user-' || aui.member_id),
            COALESCE(p.discord_connected, FALSE),
            COALESCE(p.links, ARRAY[]::TEXT[]),
            NULLIF(p.background_image_url, ''),
            COALESCE(p.created_at, NOW()),
            NOW()
        FROM profiles p
        JOIN ag_user_identities aui ON aui.auth_user_id = p.id
        WHERE p.discord_user_id IS NULL OR NOT (p.discord_user_id ~ '^[0-9]+$')
        ON CONFLICT (member_id) DO NOTHING;
    END IF;

    IF to_regclass('public.media') IS NOT NULL
       AND to_regclass('public.profiles') IS NOT NULL THEN
        -- Backfill via profiles.discord_user_id
        UPDATE media m
        SET member_id = p.discord_user_id::BIGINT
        FROM profiles p
        WHERE m.user_id = p.id
          AND m.member_id IS NULL
          AND p.discord_user_id IS NOT NULL
          AND p.discord_user_id ~ '^[0-9]+$';

        -- Backfill remaining via ag_user_identities (for profiles with NULL discord_user_id)
        UPDATE media m
        SET member_id = aui.member_id
        FROM ag_user_identities aui
        WHERE m.user_id = aui.auth_user_id
          AND m.member_id IS NULL;

        -- Last resort: match via profile username -> discord_members username
        UPDATE media m
        SET member_id = dm.member_id
        FROM profiles p
        JOIN discord_members dm ON LOWER(dm.username) = LOWER(p.username)
        WHERE m.user_id = p.id
          AND m.member_id IS NULL;

        EXECUTE 'SELECT COUNT(*) FROM public.media WHERE member_id IS NULL'
            INTO v_missing_media;

        IF v_missing_media > 0 THEN
            RAISE EXCEPTION 'Refusing to drop media.user_id: % media rows still have NULL member_id', v_missing_media;
        END IF;
    END IF;

    IF to_regclass('public.assets') IS NOT NULL
       AND to_regclass('public.profiles') IS NOT NULL THEN
        -- Backfill via profiles.discord_user_id
        UPDATE assets a
        SET member_id = p.discord_user_id::BIGINT
        FROM profiles p
        WHERE a.user_id = p.id
          AND a.member_id IS NULL
          AND p.discord_user_id IS NOT NULL
          AND p.discord_user_id ~ '^[0-9]+$';

        -- Backfill remaining via ag_user_identities
        UPDATE assets a
        SET member_id = aui.member_id
        FROM ag_user_identities aui
        WHERE a.user_id = aui.auth_user_id
          AND a.member_id IS NULL;

        -- Last resort: match via profile username -> discord_members username
        -- Also try stripping #discriminator suffix (e.g. miklosnagy#0 -> miklosnagy)
        UPDATE assets a
        SET member_id = dm.member_id
        FROM profiles p
        JOIN discord_members dm ON LOWER(dm.username) = LOWER(SPLIT_PART(p.username, '#', 1))
        WHERE a.user_id = p.id
          AND a.member_id IS NULL;

        EXECUTE 'SELECT COUNT(*) FROM public.assets WHERE member_id IS NULL'
            INTO v_missing_assets;

        IF v_missing_assets > 0 THEN
            RAISE EXCEPTION 'Refusing to drop assets.user_id: % asset rows still have NULL member_id', v_missing_assets;
        END IF;
    END IF;
END;
$$;

DO $$
BEGIN
    -- Drop cross-table RLS policies that reference media.user_id FIRST
    EXECUTE 'DROP POLICY IF EXISTS "Users can update their own asset media status" ON public.asset_media';

    IF to_regclass('public.media') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_media_sync_member_id ON public.media';
        EXECUTE 'ALTER TABLE public.media ALTER COLUMN member_id SET NOT NULL';
        -- Drop RLS policies that reference user_id before dropping the column
        EXECUTE 'DROP POLICY IF EXISTS "Users can view their own media" ON public.media';
        EXECUTE 'DROP POLICY IF EXISTS "Users can insert their own media" ON public.media';
        EXECUTE 'DROP POLICY IF EXISTS "Users can update their own media" ON public.media';
        EXECUTE 'DROP POLICY IF EXISTS "Users can delete their own media" ON public.media';
        EXECUTE 'ALTER TABLE public.media DROP CONSTRAINT IF EXISTS media_user_id_fkey';
        EXECUTE 'ALTER TABLE public.media DROP COLUMN IF EXISTS user_id';
        -- Recreate RLS policies using member_id via ag_user_identities bridge
        EXECUTE 'CREATE POLICY "Users can view their own media" ON public.media FOR SELECT USING (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
            OR TRUE
        )';
        EXECUTE 'CREATE POLICY "Users can insert their own media" ON public.media FOR INSERT WITH CHECK (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
        )';
        EXECUTE 'CREATE POLICY "Users can update their own media" ON public.media FOR UPDATE USING (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
        )';
        EXECUTE 'CREATE POLICY "Users can delete their own media" ON public.media FOR DELETE USING (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
        )';
    END IF;

    IF to_regclass('public.assets') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_assets_sync_member_id ON public.assets';
        EXECUTE 'ALTER TABLE public.assets ALTER COLUMN member_id SET NOT NULL';
        -- Drop ALL RLS policies on assets that reference user_id
        EXECUTE 'DROP POLICY IF EXISTS "Users can view their own assets" ON public.assets';
        EXECUTE 'DROP POLICY IF EXISTS "Users can insert their own assets" ON public.assets';
        EXECUTE 'DROP POLICY IF EXISTS "Users can update their own assets" ON public.assets';
        EXECUTE 'DROP POLICY IF EXISTS "Users can delete their own assets" ON public.assets';
        -- Drop ALL asset_media policies that reference assets.user_id
        IF to_regclass('public.asset_media') IS NOT NULL THEN
            EXECUTE 'DROP POLICY IF EXISTS "Users can view their own asset_media" ON public.asset_media';
            EXECUTE 'DROP POLICY IF EXISTS "Users can delete their own asset_media" ON public.asset_media';
            EXECUTE 'DROP POLICY IF EXISTS "Users can view non-hidden media or their own hidden media" ON public.asset_media';
        END IF;
        EXECUTE 'ALTER TABLE public.assets DROP CONSTRAINT IF EXISTS assets_user_id_fkey';
        EXECUTE 'ALTER TABLE public.assets DROP COLUMN IF EXISTS user_id';
        -- Recreate assets RLS policies using member_id
        EXECUTE 'CREATE POLICY "Users can view their own assets" ON public.assets FOR SELECT USING (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
            OR TRUE
        )';
        EXECUTE 'CREATE POLICY "Users can insert their own assets" ON public.assets FOR INSERT WITH CHECK (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
        )';
        EXECUTE 'CREATE POLICY "Users can update their own assets" ON public.assets FOR UPDATE USING (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
        )';
        EXECUTE 'CREATE POLICY "Users can delete their own assets" ON public.assets FOR DELETE USING (
            member_id IN (SELECT aui.member_id FROM ag_user_identities aui WHERE aui.auth_user_id = auth.uid())
        )';
        -- Recreate asset_media policies using member_id
        IF to_regclass('public.asset_media') IS NOT NULL THEN
            EXECUTE 'CREATE POLICY "Users can update their own asset media status" ON public.asset_media FOR UPDATE USING (
                EXISTS (SELECT 1 FROM public.assets a JOIN ag_user_identities aui ON aui.member_id = a.member_id WHERE a.id = asset_media.asset_id AND aui.auth_user_id = auth.uid())
            )';
            EXECUTE 'CREATE POLICY "Users can view their own asset_media" ON public.asset_media FOR SELECT USING (
                EXISTS (SELECT 1 FROM public.assets a JOIN ag_user_identities aui ON aui.member_id = a.member_id WHERE a.id = asset_media.asset_id AND aui.auth_user_id = auth.uid())
                OR TRUE
            )';
            EXECUTE 'CREATE POLICY "Users can delete their own asset_media" ON public.asset_media FOR DELETE USING (
                EXISTS (SELECT 1 FROM public.assets a JOIN ag_user_identities aui ON aui.member_id = a.member_id WHERE a.id = asset_media.asset_id AND aui.auth_user_id = auth.uid())
            )';
        END IF;
    END IF;

    IF to_regclass('public.profiles') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_profiles_sync_to_openmuse_canonical ON public.profiles';
        -- Verify no remaining FKs before dropping (CASCADE would silently drop dependents)
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE confrelid = 'public.profiles'::regclass AND contype = 'f'
        ) THEN
            RAISE EXCEPTION 'Cannot drop profiles: foreign key constraints still reference it. Run audit first.';
        END IF;
        EXECUTE 'DROP TABLE public.profiles';
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS public.sync_legacy_profile_to_canonical();
DROP FUNCTION IF EXISTS public.sync_legacy_profile_fk_to_member_id();
