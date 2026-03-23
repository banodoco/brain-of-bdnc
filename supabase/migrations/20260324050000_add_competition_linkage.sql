ALTER TABLE public.media
    ADD COLUMN IF NOT EXISTS ag_competition_id UUID,
    ADD COLUMN IF NOT EXISTS competition_guild_id BIGINT,
    ADD COLUMN IF NOT EXISTS competition_slug TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.media'::regclass
          AND conname = 'media_ag_competition_id_fkey'
    ) THEN
        ALTER TABLE public.media
            ADD CONSTRAINT media_ag_competition_id_fkey
            FOREIGN KEY (ag_competition_id)
            REFERENCES public.ag_competitions(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.media'::regclass
          AND conname = 'fk_media_discord_competition'
    ) THEN
        ALTER TABLE public.media
            ADD CONSTRAINT fk_media_discord_competition
            FOREIGN KEY (competition_guild_id, competition_slug)
            REFERENCES public.discord_competitions(guild_id, slug)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.media'::regclass
          AND conname = 'chk_media_discord_competition_pair'
    ) THEN
        ALTER TABLE public.media
            ADD CONSTRAINT chk_media_discord_competition_pair
            CHECK (
                (competition_guild_id IS NULL AND competition_slug IS NULL)
                OR (competition_guild_id IS NOT NULL AND competition_slug IS NOT NULL)
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_media_ag_competition_id
    ON public.media(ag_competition_id)
    WHERE ag_competition_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_discord_competition
    ON public.media(competition_guild_id, competition_slug)
    WHERE competition_guild_id IS NOT NULL;

ALTER TABLE public.assets
    ADD COLUMN IF NOT EXISTS ag_competition_id UUID,
    ADD COLUMN IF NOT EXISTS competition_guild_id BIGINT,
    ADD COLUMN IF NOT EXISTS competition_slug TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass
          AND conname = 'assets_ag_competition_id_fkey'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT assets_ag_competition_id_fkey
            FOREIGN KEY (ag_competition_id)
            REFERENCES public.ag_competitions(id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass
          AND conname = 'fk_assets_discord_competition'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT fk_assets_discord_competition
            FOREIGN KEY (competition_guild_id, competition_slug)
            REFERENCES public.discord_competitions(guild_id, slug)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.assets'::regclass
          AND conname = 'chk_assets_discord_competition_pair'
    ) THEN
        ALTER TABLE public.assets
            ADD CONSTRAINT chk_assets_discord_competition_pair
            CHECK (
                (competition_guild_id IS NULL AND competition_slug IS NULL)
                OR (competition_guild_id IS NOT NULL AND competition_slug IS NOT NULL)
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_assets_ag_competition_id
    ON public.assets(ag_competition_id)
    WHERE ag_competition_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_assets_discord_competition
    ON public.assets(competition_guild_id, competition_slug)
    WHERE competition_guild_id IS NOT NULL;
