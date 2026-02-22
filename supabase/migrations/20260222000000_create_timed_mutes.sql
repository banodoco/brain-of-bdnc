CREATE TABLE IF NOT EXISTS timed_mutes (
    member_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    mute_end_at TIMESTAMPTZ NOT NULL,
    reason TEXT,
    muted_by_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (member_id, guild_id)
);
