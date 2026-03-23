-- Rename discord_members -> members.
-- discord_members is the canonical user/profile table; the name should reflect that.
-- Also drop openmuse_profiles and openmuse_profile_view (OpenMuse is discontinued).

-- 1. Drop openmuse artifacts (discontinued product)
DROP VIEW IF EXISTS openmuse_profile_view;
DROP TABLE IF EXISTS openmuse_profiles CASCADE;
DROP FUNCTION IF EXISTS sync_legacy_profile_to_canonical();
DROP FUNCTION IF EXISTS sync_legacy_profile_fk_to_member_id();

-- 2. Rename the table
ALTER TABLE discord_members RENAME TO members;

-- 3. Rename indexes to match new table name
ALTER INDEX IF EXISTS idx_discord_members_username RENAME TO idx_members_username;
ALTER INDEX IF EXISTS idx_discord_members_global_name RENAME TO idx_members_global_name;
ALTER INDEX IF EXISTS idx_discord_members_discord_created_at RENAME TO idx_members_discord_created_at;
ALTER INDEX IF EXISTS idx_discord_members_synced_at RENAME TO idx_members_synced_at;
ALTER INDEX IF EXISTS idx_discord_members_banodoco_owner RENAME TO idx_members_banodoco_owner;

-- 4. Rename triggers
ALTER TRIGGER update_discord_members_updated_at ON members RENAME TO update_members_updated_at;

-- 5. Recreate views that reference the old table name

-- member_guild_profile
CREATE OR REPLACE VIEW member_guild_profile AS
SELECT
    gm.guild_id,
    gm.member_id,
    COALESCE(gm.server_nick, m.global_name, m.username) AS display_name,
    gm.server_nick,
    m.global_name,
    m.username,
    m.avatar_url,
    m.stored_avatar_url,
    gm.guild_join_date,
    gm.role_ids AS guild_role_ids,
    m.twitter_url,
    m.allow_content_sharing,
    m.include_in_updates,
    NOT COALESCE(gm.speaker_muted, FALSE) AS is_speaker,
    COALESCE(gm.speaker_muted, FALSE) AS speaker_muted,
    m.first_shared_at
FROM guild_members gm
JOIN members m ON gm.member_id = m.member_id;

-- ag_profiles
DROP VIEW IF EXISTS ag_profiles CASCADE;
CREATE VIEW ag_profiles AS
SELECT
    aui.auth_user_id AS id,
    m.member_id::TEXT AS discord_id,
    m.username AS discord_username,
    m.discriminator AS discord_discriminator,
    COALESCE(m.global_name, m.username) AS display_name,
    COALESCE(m.stored_avatar_url, m.avatar_url) AS avatar_url,
    CASE
        WHEN auth.uid() = aui.auth_user_id THEN aui.email
        ELSE NULL
    END AS email,
    m.bio,
    m.real_name,
    m.website_url,
    m.instagram_url,
    m.twitter_url,
    m.discord_created_at AS discord_account_created_at,
    COALESCE(m.banodoco_owner, FALSE) AS banodoco_owner,
    aui.created_at,
    GREATEST(aui.updated_at, m.updated_at) AS updated_at
FROM ag_user_identities aui
JOIN members m ON m.member_id = aui.member_id;

-- ag_submission_details
DROP VIEW IF EXISTS ag_submission_details;
CREATE VIEW ag_submission_details AS
SELECT
    s.id,
    s.competition_id,
    s.user_id,
    s.content_record_id,
    s.theme,
    s.video_url,
    s.title,
    s.description,
    s.tools_used,
    s.thumbnail_url,
    s.duration_seconds,
    s.additional_links,
    s.status,
    s.admin_notes,
    s.score,
    s.vote_count,
    s.winner,
    s.submitted_at,
    s.created_at,
    s.updated_at,
    ag_get_verified_vote_count(s.id) AS verified_vote_count,
    ag_get_verified_vote_count_with_judge_multiplier(s.id, s.competition_id) AS verified_weighted_vote_count,
    jsonb_build_object(
        'id', aui.auth_user_id,
        'discord_id', m.member_id::TEXT,
        'discord_username', m.username,
        'discord_discriminator', m.discriminator,
        'display_name', COALESCE(m.global_name, m.username),
        'avatar_url', COALESCE(m.stored_avatar_url, m.avatar_url),
        'bio', m.bio,
        'real_name', m.real_name,
        'website_url', m.website_url,
        'instagram_url', m.instagram_url,
        'twitter_url', m.twitter_url,
        'discord_account_created_at', m.discord_created_at,
        'banodoco_owner', COALESCE(m.banodoco_owner, FALSE)
    ) AS profile
FROM ag_submissions s
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = s.user_id
LEFT JOIN members m ON m.member_id = aui.member_id
WHERE s.status <> 'rejected';

-- 6. Recreate ag_update_profile function
DROP FUNCTION IF EXISTS ag_update_profile(JSONB);
CREATE FUNCTION ag_update_profile(p_profile JSONB)
RETURNS VOID
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql AS $$
DECLARE
    v_auth_user_id UUID;
    v_member_id BIGINT;
BEGIN
    v_auth_user_id := auth.uid();
    IF v_auth_user_id IS NULL THEN
        RAISE EXCEPTION 'Not authenticated';
    END IF;

    SELECT member_id INTO v_member_id
    FROM ag_user_identities
    WHERE auth_user_id = v_auth_user_id;

    IF v_member_id IS NULL THEN
        RAISE EXCEPTION 'No linked Discord member';
    END IF;

    UPDATE members m SET
        bio = CASE
            WHEN p_profile ? 'bio' THEN NULLIF(BTRIM(p_profile->>'bio'), '')
            ELSE m.bio
        END,
        real_name = CASE
            WHEN p_profile ? 'real_name' THEN NULLIF(BTRIM(p_profile->>'real_name'), '')
            ELSE m.real_name
        END,
        website_url = CASE
            WHEN p_profile ? 'website_url' THEN NULLIF(BTRIM(p_profile->>'website_url'), '')
            ELSE m.website_url
        END,
        instagram_url = CASE
            WHEN p_profile ? 'instagram_url' THEN NULLIF(BTRIM(p_profile->>'instagram_url'), '')
            ELSE m.instagram_url
        END,
        twitter_url = CASE
            WHEN p_profile ? 'twitter_url' THEN NULLIF(BTRIM(p_profile->>'twitter_url'), '')
            ELSE m.twitter_url
        END,
        stored_avatar_url = CASE
            WHEN p_profile ? 'avatar_url' THEN NULLIF(BTRIM(p_profile->>'avatar_url'), '')
            ELSE m.stored_avatar_url
        END,
        updated_at = NOW()
    WHERE m.member_id = v_member_id;
END;
$$;

-- 7. Regrant view permissions
GRANT SELECT ON ag_profiles TO anon, authenticated;
GRANT SELECT ON ag_submission_details TO anon, authenticated;
GRANT SELECT ON member_guild_profile TO anon, authenticated;

-- 8. Update sync_status reference
UPDATE sync_status SET table_name = 'members' WHERE table_name = 'discord_members';
