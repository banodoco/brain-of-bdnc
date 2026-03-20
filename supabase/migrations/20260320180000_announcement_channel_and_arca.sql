-- Add missing channel columns and seed Arca Gidan.

ALTER TABLE server_config
    ADD COLUMN IF NOT EXISTS announcement_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS rules_channel_id BIGINT;

-- Seed BNDC hardcoded values into server_config
UPDATE server_config
SET announcement_channel_id = 1246615722164224141,
    rules_channel_id = 1138515622582562947
WHERE guild_id = (SELECT guild_id FROM server_config WHERE guild_name = 'BNDC' LIMIT 1)
  AND announcement_channel_id IS NULL;

-- Seed Arca Gidan as archive + reactions guild
INSERT INTO server_config (
    guild_id, guild_name, enabled, write_enabled,
    default_logging, default_archiving, default_summarising,
    default_reactions, default_sharing,
    speaker_management_enabled, monitor_all_channels,
    community_name
) VALUES (
    1431366141380395290, 'Arca Gidan', TRUE, TRUE,
    FALSE, TRUE, FALSE,
    TRUE, FALSE,
    FALSE, TRUE,
    'Arca Gidan'
) ON CONFLICT (guild_id) DO UPDATE SET
    enabled = TRUE,
    write_enabled = TRUE,
    default_archiving = TRUE,
    default_reactions = TRUE,
    monitor_all_channels = TRUE;
