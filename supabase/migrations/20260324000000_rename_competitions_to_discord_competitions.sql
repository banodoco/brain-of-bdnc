-- Clarify the Discord bot competition table name alongside ag_competitions.
DO $$
BEGIN
    IF to_regclass('public.competitions') IS NOT NULL
       AND to_regclass('public.discord_competitions') IS NULL THEN
        ALTER TABLE public.competitions RENAME TO discord_competitions;
    END IF;
END;
$$;

DO $$
BEGIN
    IF to_regclass('public.idx_competitions_guild_status') IS NOT NULL
       AND to_regclass('public.idx_discord_competitions_guild_status') IS NULL THEN
        ALTER INDEX public.idx_competitions_guild_status
            RENAME TO idx_discord_competitions_guild_status;
    END IF;
END;
$$;

UPDATE public.sync_status
SET table_name = 'discord_competitions'
WHERE table_name = 'competitions';
