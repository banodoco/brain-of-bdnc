-- Make competitions guild-scoped so the same slug can exist in multiple guilds.
-- Bootstrap the legacy single-guild BNDC install into server_config/guild_id.

INSERT INTO server_config (guild_id, guild_name, enabled)
SELECT 1076117621407223829, 'BNDC', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
)
  AND NOT EXISTS (
    SELECT 1 FROM server_config
);

UPDATE discord_channels
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

UPDATE discord_messages dm
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE dm.guild_id IS NULL
  AND dm.channel_id = dc.channel_id
  AND dc.guild_id IS NOT NULL;

UPDATE discord_messages
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

-- 1. Add guild_id columns (nullable during backfill)
ALTER TABLE competitions
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

ALTER TABLE competition_entries
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- 2. Backfill competitions.guild_id from configured channels
UPDATE competitions c
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE c.guild_id IS NULL
  AND dc.channel_id = COALESCE(c.voting_channel_id, c.channel_id);

UPDATE competitions c
SET guild_id = dm.guild_id
FROM competition_entries ce
JOIN discord_messages dm ON dm.message_id = ce.message_id
WHERE c.guild_id IS NULL
  AND ce.competition_slug = c.slug
  AND dm.guild_id IS NOT NULL;

UPDATE competitions c
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE c.guild_id IS NULL
  AND dc.channel_id = c.questions_thread_id
  AND dc.guild_id IS NOT NULL;

UPDATE competitions c
SET guild_id = sc.guild_id
FROM server_config sc
WHERE c.guild_id IS NULL
  AND sc.guild_id = 1076117621407223829;

-- 3. Backfill competition_entries.guild_id from competitions first, then messages
UPDATE competition_entries ce
SET guild_id = c.guild_id
FROM competitions c
WHERE ce.guild_id IS NULL
  AND ce.competition_slug = c.slug
  AND c.guild_id IS NOT NULL;

UPDATE competition_entries ce
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE ce.guild_id IS NULL
  AND ce.message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE competition_entries ce
SET guild_id = sc.guild_id
FROM server_config sc
WHERE ce.guild_id IS NULL
  AND sc.guild_id = 1076117621407223829;

-- 4. Rebuild constraints for guild-scoped uniqueness / references
ALTER TABLE competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_competition_slug_fkey;

ALTER TABLE competition_entries
    DROP CONSTRAINT IF EXISTS competition_entries_competition_slug_message_id_key;

ALTER TABLE competitions
    DROP CONSTRAINT IF EXISTS competitions_pkey;

ALTER TABLE competitions
    ADD CONSTRAINT competitions_guild_id_fkey
    FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);

ALTER TABLE competition_entries
    ADD CONSTRAINT competition_entries_guild_id_fkey
    FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);

ALTER TABLE competitions
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE competition_entries
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE competitions
    ADD CONSTRAINT competitions_pkey PRIMARY KEY (guild_id, slug);

ALTER TABLE competition_entries
    ADD CONSTRAINT competition_entries_guild_slug_message_key
    UNIQUE (guild_id, competition_slug, message_id);

ALTER TABLE competition_entries
    ADD CONSTRAINT competition_entries_guild_slug_fkey
    FOREIGN KEY (guild_id, competition_slug)
    REFERENCES competitions(guild_id, slug)
    ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_competitions_guild_status
    ON competitions(guild_id, status);

CREATE INDEX IF NOT EXISTS idx_competition_entries_guild_slug
    ON competition_entries(guild_id, competition_slug);
