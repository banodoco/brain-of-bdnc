-- Clean up any residual discord_messages rows that still missed guild_id backfill.

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
