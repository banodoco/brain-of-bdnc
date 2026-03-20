-- Follow-up multi-server fixes:
-- - guild-scoped speaker mute state
-- - explicit per-guild speaker management enablement
-- - monitor-all / monitored-channel config for summaries
-- - safer server feature defaults for newly added guilds

ALTER TABLE server_config
    ALTER COLUMN default_logging SET DEFAULT FALSE,
    ALTER COLUMN default_archiving SET DEFAULT FALSE,
    ALTER COLUMN default_summarising SET DEFAULT FALSE,
    ALTER COLUMN default_reactions SET DEFAULT FALSE,
    ALTER COLUMN default_sharing SET DEFAULT FALSE;

ALTER TABLE server_config
    ADD COLUMN IF NOT EXISTS speaker_management_enabled BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS monitor_all_channels BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS monitored_channel_ids BIGINT[];

ALTER TABLE guild_members
    ADD COLUMN IF NOT EXISTS speaker_muted BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_guild_members_guild_muted ON guild_members(guild_id, speaker_muted);

CREATE OR REPLACE VIEW member_guild_profile AS
SELECT
    gm.guild_id,
    gm.member_id,
    COALESCE(gm.server_nick, dm.global_name, dm.username) AS display_name,
    gm.server_nick,
    dm.global_name,
    dm.username,
    dm.avatar_url,
    dm.stored_avatar_url,
    gm.guild_join_date,
    gm.role_ids AS guild_role_ids,
    dm.twitter_handle,
    dm.allow_content_sharing,
    dm.include_in_updates,
    NOT COALESCE(gm.speaker_muted, FALSE) AS is_speaker,
    COALESCE(gm.speaker_muted, FALSE) AS speaker_muted,
    dm.first_shared_at
FROM guild_members gm
JOIN discord_members dm ON gm.member_id = dm.member_id;
