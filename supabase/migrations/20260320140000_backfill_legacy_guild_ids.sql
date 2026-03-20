-- Backfill legacy guild_id columns added during multi-server rollout,
-- and make events/invite_codes guild-aware for admin tooling.

INSERT INTO server_config (guild_id, guild_name, enabled)
SELECT 1076117621407223829, 'BNDC', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
);

-- Existing guild-aware tables added by the multi-server migration.
UPDATE daily_summaries ds
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE ds.guild_id IS NULL
  AND ds.channel_id = dc.channel_id
  AND dc.guild_id IS NOT NULL;

UPDATE daily_summaries
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

UPDATE shared_posts sp
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE sp.guild_id IS NULL
  AND sp.discord_message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE shared_posts
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

UPDATE pending_intros pi
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE pi.guild_id IS NULL
  AND pi.message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE pending_intros pi
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE pi.guild_id IS NULL
  AND pi.channel_id = dc.channel_id
  AND dc.guild_id IS NOT NULL;

UPDATE pending_intros
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

UPDATE discord_reactions dr
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE dr.guild_id IS NULL
  AND dr.message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE discord_reactions
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

UPDATE discord_reaction_log drl
SET guild_id = dm.guild_id
FROM discord_messages dm
WHERE drl.guild_id IS NULL
  AND drl.message_id = dm.message_id
  AND dm.guild_id IS NOT NULL;

UPDATE discord_reaction_log
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

-- Events and invites are already treated as guild-scoped by admin_chat.
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

UPDATE events
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

ALTER TABLE invite_codes
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

UPDATE invite_codes ic
SET guild_id = e.guild_id
FROM events e
WHERE ic.guild_id IS NULL
  AND ic.event_id = e.id
  AND e.guild_id IS NOT NULL;

UPDATE invite_codes
SET guild_id = 1076117621407223829
WHERE guild_id IS NULL
  AND EXISTS (
      SELECT 1 FROM server_config WHERE guild_id = 1076117621407223829
  );

ALTER TABLE events
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE invite_codes
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE events
    ADD CONSTRAINT events_guild_id_fkey
    FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);

ALTER TABLE invite_codes
    ADD CONSTRAINT invite_codes_guild_id_fkey
    FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);

CREATE INDEX IF NOT EXISTS idx_events_guild_id ON events(guild_id);
CREATE INDEX IF NOT EXISTS idx_invite_codes_guild_id ON invite_codes(guild_id);
