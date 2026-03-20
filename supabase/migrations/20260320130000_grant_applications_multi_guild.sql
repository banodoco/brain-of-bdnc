-- Make grant applications guild-scoped for multi-server safety.

ALTER TABLE grant_applications
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- First preference: the grant thread itself should exist as a discord channel/thread row.
UPDATE grant_applications ga
SET guild_id = dc.guild_id
FROM discord_channels dc
WHERE ga.guild_id IS NULL
  AND dc.channel_id = ga.thread_id
  AND dc.guild_id IS NOT NULL;

-- Fallback: infer from messages posted inside the grant thread.
UPDATE grant_applications ga
SET guild_id = dm.guild_id
FROM (
    SELECT channel_id, MAX(guild_id) AS guild_id
    FROM discord_messages
    WHERE guild_id IS NOT NULL
    GROUP BY channel_id
) dm
WHERE ga.guild_id IS NULL
  AND dm.channel_id = ga.thread_id
  AND dm.guild_id IS NOT NULL;

UPDATE grant_applications ga
SET guild_id = sc.guild_id
FROM server_config sc
WHERE ga.guild_id IS NULL
  AND sc.guild_id = 1076117621407223829;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM grant_applications
        WHERE guild_id IS NULL
    ) THEN
        RAISE EXCEPTION 'grant_applications guild_id backfill incomplete; manual cleanup required before enforcing NOT NULL';
    END IF;
END $$;

ALTER TABLE grant_applications
    ALTER COLUMN guild_id SET NOT NULL;

ALTER TABLE grant_applications
    ADD CONSTRAINT grant_applications_guild_id_fkey
        FOREIGN KEY (guild_id) REFERENCES server_config(guild_id);

CREATE INDEX IF NOT EXISTS idx_grant_applications_guild_id ON grant_applications(guild_id);
CREATE INDEX IF NOT EXISTS idx_grant_applications_guild_applicant ON grant_applications(guild_id, applicant_id);
CREATE INDEX IF NOT EXISTS idx_grant_applications_guild_status ON grant_applications(guild_id, status);
