-- Enforce guild scoping on legacy backfilled tables and make events slugs guild-local.

BEGIN;

LOCK TABLE daily_summaries IN ACCESS EXCLUSIVE MODE;
LOCK TABLE shared_posts IN ACCESS EXCLUSIVE MODE;
LOCK TABLE pending_intros IN ACCESS EXCLUSIVE MODE;
LOCK TABLE discord_reactions IN ACCESS EXCLUSIVE MODE;
LOCK TABLE discord_reaction_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE events IN ACCESS EXCLUSIVE MODE;

UPDATE discord_messages dm
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE dm.guild_id IS NULL
  AND dm.channel_id = dc.channel_id
  AND dc.guild_id IS NOT NULL;

UPDATE discord_reactions dr
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE dr.guild_id IS NULL
  AND dr.message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE discord_reactions dr
SET guild_id = dc.guild_id
FROM discord_messages dm
JOIN discord_channels dc ON dc.channel_id = dm.channel_id
WHERE dr.guild_id IS NULL
  AND dr.message_id = dm.message_id
  AND dc.guild_id IS NOT NULL;

UPDATE discord_reaction_log drl
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE drl.guild_id IS NULL
  AND drl.message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE discord_reaction_log drl
SET guild_id = dc.guild_id
FROM discord_messages dm
JOIN discord_channels dc ON dc.channel_id = dm.channel_id
WHERE drl.guild_id IS NULL
  AND drl.message_id = dm.message_id
  AND dc.guild_id IS NOT NULL;

ALTER TABLE daily_summaries
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE shared_posts
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE pending_intros
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE discord_reactions
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE discord_reaction_log
    ALTER COLUMN guild_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'daily_summaries_guild_id_fkey'
    ) THEN
        ALTER TABLE daily_summaries
            ADD CONSTRAINT daily_summaries_guild_id_fkey
            FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'shared_posts_guild_id_fkey'
    ) THEN
        ALTER TABLE shared_posts
            ADD CONSTRAINT shared_posts_guild_id_fkey
            FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'pending_intros_guild_id_fkey'
    ) THEN
        ALTER TABLE pending_intros
            ADD CONSTRAINT pending_intros_guild_id_fkey
            FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'discord_reactions_guild_id_fkey'
    ) THEN
        ALTER TABLE discord_reactions
            ADD CONSTRAINT discord_reactions_guild_id_fkey
            FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'discord_reaction_log_guild_id_fkey'
    ) THEN
        ALTER TABLE discord_reaction_log
            ADD CONSTRAINT discord_reaction_log_guild_id_fkey
            FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);
    END IF;
END $$;

ALTER TABLE events
    DROP CONSTRAINT IF EXISTS events_slug_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'events_guild_slug_key'
    ) THEN
        ALTER TABLE events
            ADD CONSTRAINT events_guild_slug_key UNIQUE (guild_id, slug);
    END IF;
END $$;

COMMIT;
