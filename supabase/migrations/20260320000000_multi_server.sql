-- Multi-server support: new tables, new columns, views
-- All additive. Nothing breaks existing BNDC functionality.

-- ============================================================
-- 1. server_config — per-server settings and identity
-- ============================================================
CREATE TABLE IF NOT EXISTS server_config (
    guild_id       BIGINT PRIMARY KEY,
    guild_name     TEXT,
    enabled        BOOLEAN DEFAULT TRUE,

    -- Feature defaults (channels inherit unless overridden)
    default_logging      BOOLEAN DEFAULT FALSE,
    default_archiving    BOOLEAN DEFAULT FALSE,
    default_summarising  BOOLEAN DEFAULT FALSE,
    default_reactions    BOOLEAN DEFAULT FALSE,
    default_sharing      BOOLEAN DEFAULT FALSE,

    -- Identity
    community_name        TEXT,
    community_description TEXT,
    community_demonym     TEXT,

    admin_user_id   BIGINT,
    twitter_account TEXT,
    solana_wallet   TEXT,

    -- Channel IDs
    summary_channel_id     BIGINT,
    top_gens_channel_id    BIGINT,
    art_channel_id         BIGINT,
    gate_channel_id        BIGINT,
    intro_channel_id       BIGINT,
    welcome_channel_id     BIGINT,
    grants_channel_id      BIGINT,
    moderation_channel_id  BIGINT,
    openmuse_channel_id    BIGINT,

    -- Role IDs
    speaker_role_id        BIGINT,
    approver_role_id       BIGINT,
    super_approver_role_id BIGINT,
    no_sharing_role_id     BIGINT,

    -- Config
    curator_ids              BIGINT[],
    reaction_watchlist       JSONB,
    message_linker_channels  BIGINT[],
    speaker_management_enabled BOOLEAN DEFAULT FALSE,
    monitor_all_channels       BOOLEAN DEFAULT FALSE,
    monitored_channel_ids      BIGINT[],

    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE server_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow service role full access on server_config"
    ON server_config FOR ALL
    USING (true) WITH CHECK (true);

-- ============================================================
-- 2. server_content — per-server prompts and posts
-- ============================================================
CREATE TABLE IF NOT EXISTS server_content (
    guild_id     BIGINT NOT NULL REFERENCES server_config(guild_id),
    content_key  TEXT NOT NULL,
    content      TEXT,
    content_type TEXT CHECK (content_type IN ('post', 'prompt', 'config')),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_by   BIGINT,
    PRIMARY KEY (guild_id, content_key)
);

ALTER TABLE server_content ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow service role full access on server_content"
    ON server_content FOR ALL
    USING (true) WITH CHECK (true);

-- ============================================================
-- 3. guild_members — guild-scoped member data
-- ============================================================
CREATE TABLE IF NOT EXISTS guild_members (
    guild_id        BIGINT NOT NULL,
    member_id       BIGINT NOT NULL,
    server_nick     TEXT,
    guild_join_date TIMESTAMPTZ,
    role_ids        JSONB,
    speaker_muted   BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (guild_id, member_id)
);

ALTER TABLE guild_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow service role full access on guild_members"
    ON guild_members FOR ALL
    USING (true) WITH CHECK (true);

-- ============================================================
-- 4. New columns on existing tables (all nullable)
-- ============================================================

-- discord_channels
ALTER TABLE discord_channels
    ADD COLUMN IF NOT EXISTS guild_id            BIGINT,
    ADD COLUMN IF NOT EXISTS channel_type        TEXT,
    ADD COLUMN IF NOT EXISTS parent_id           BIGINT,
    ADD COLUMN IF NOT EXISTS logging_enabled     BOOLEAN,
    ADD COLUMN IF NOT EXISTS archiving_enabled   BOOLEAN,
    ADD COLUMN IF NOT EXISTS summarising_enabled BOOLEAN,
    ADD COLUMN IF NOT EXISTS reactions_enabled   BOOLEAN,
    ADD COLUMN IF NOT EXISTS sharing_enabled     BOOLEAN;

-- discord_messages
ALTER TABLE discord_messages
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- daily_summaries
ALTER TABLE daily_summaries
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- shared_posts
ALTER TABLE shared_posts
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- pending_intros
ALTER TABLE pending_intros
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- discord_reactions
ALTER TABLE discord_reactions
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- discord_reaction_log
ALTER TABLE discord_reaction_log
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- ============================================================
-- 5. Indexes for guild_id on high-volume tables
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_messages_guild_id ON discord_messages(guild_id);
CREATE INDEX IF NOT EXISTS idx_channels_guild_id ON discord_channels(guild_id);
CREATE INDEX IF NOT EXISTS idx_reactions_guild_id ON discord_reactions(guild_id);
CREATE INDEX IF NOT EXISTS idx_reaction_log_guild_id ON discord_reaction_log(guild_id);
CREATE INDEX IF NOT EXISTS idx_summaries_guild_id ON daily_summaries(guild_id);
CREATE INDEX IF NOT EXISTS idx_shared_posts_guild_id ON shared_posts(guild_id);
CREATE INDEX IF NOT EXISTS idx_pending_intros_guild_id ON pending_intros(guild_id);
CREATE INDEX IF NOT EXISTS idx_guild_members_guild_muted ON guild_members(guild_id, speaker_muted);

-- ============================================================
-- 6. channel_effective_config view
--    Resolves feature flags: channel -> parent -> server default -> fallback
-- ============================================================
CREATE OR REPLACE VIEW channel_effective_config AS
SELECT
    c.channel_id,
    c.channel_name,
    c.guild_id,
    c.channel_type,
    c.parent_id,
    c.category_id,
    c.nsfw,
    c.speaker_mode,
    -- Resolve each feature: channel override -> parent override -> server default -> true
    COALESCE(c.logging_enabled,     p.logging_enabled,     sc.default_logging,     TRUE)  AS logging_enabled,
    COALESCE(c.archiving_enabled,   p.archiving_enabled,   sc.default_archiving,   TRUE)  AS archiving_enabled,
    COALESCE(c.summarising_enabled, p.summarising_enabled, sc.default_summarising, FALSE) AS summarising_enabled,
    COALESCE(c.reactions_enabled,   p.reactions_enabled,   sc.default_reactions,   TRUE)  AS reactions_enabled,
    COALESCE(c.sharing_enabled,     p.sharing_enabled,     sc.default_sharing,     FALSE) AS sharing_enabled
FROM discord_channels c
LEFT JOIN discord_channels p ON c.parent_id = p.channel_id
LEFT JOIN server_config sc ON c.guild_id = sc.guild_id;

-- ============================================================
-- 7. member_guild_profile view
--    Joins guild_members + discord_members for guild-aware display names
-- ============================================================
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
