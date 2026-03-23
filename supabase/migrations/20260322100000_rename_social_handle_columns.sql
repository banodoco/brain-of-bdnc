-- Rename twitter_handle -> twitter_url and reddit_handle -> reddit_url
-- for consistency with website_url, instagram_url naming convention.
-- Both columns accept handles (@user) or full URLs — the consuming code
-- already handles both formats.

ALTER TABLE discord_members RENAME COLUMN twitter_handle TO twitter_url;
ALTER TABLE discord_members RENAME COLUMN reddit_handle TO reddit_url;

-- Drop and recreate views that reference the renamed columns
-- (CREATE OR REPLACE cannot rename view columns)

DROP VIEW IF EXISTS member_guild_profile;
CREATE VIEW member_guild_profile AS
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
    dm.twitter_url,
    dm.allow_content_sharing,
    dm.include_in_updates,
    NOT COALESCE(gm.speaker_muted, FALSE) AS is_speaker,
    COALESCE(gm.speaker_muted, FALSE) AS speaker_muted,
    dm.first_shared_at
FROM guild_members gm
JOIN discord_members dm ON gm.member_id = dm.member_id;

-- Recreate ag_profiles view (was aliasing twitter_handle AS twitter_url, now direct)
CREATE OR REPLACE VIEW ag_profiles AS
SELECT
    aui.auth_user_id AS id,
    dm.member_id::TEXT AS discord_id,
    dm.username AS discord_username,
    dm.discriminator AS discord_discriminator,
    COALESCE(dm.global_name, dm.username) AS display_name,
    COALESCE(dm.stored_avatar_url, dm.avatar_url) AS avatar_url,
    CASE
        WHEN auth.uid() = aui.auth_user_id THEN aui.email
        ELSE NULL
    END AS email,
    dm.bio,
    dm.real_name,
    dm.website_url,
    dm.instagram_url,
    dm.twitter_url,
    dm.discord_created_at AS discord_account_created_at,
    COALESCE(dm.banodoco_owner, FALSE) AS banodoco_owner,
    aui.created_at,
    GREATEST(aui.updated_at, dm.updated_at) AS updated_at
FROM ag_user_identities aui
JOIN discord_members dm ON dm.member_id = aui.member_id;

-- Recreate ag_update_profile to use twitter_url directly
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

    UPDATE discord_members dm SET
        bio = CASE
            WHEN p_profile ? 'bio' THEN NULLIF(BTRIM(p_profile->>'bio'), '')
            ELSE dm.bio
        END,
        real_name = CASE
            WHEN p_profile ? 'real_name' THEN NULLIF(BTRIM(p_profile->>'real_name'), '')
            ELSE dm.real_name
        END,
        website_url = CASE
            WHEN p_profile ? 'website_url' THEN NULLIF(BTRIM(p_profile->>'website_url'), '')
            ELSE dm.website_url
        END,
        instagram_url = CASE
            WHEN p_profile ? 'instagram_url' THEN NULLIF(BTRIM(p_profile->>'instagram_url'), '')
            ELSE dm.instagram_url
        END,
        twitter_url = CASE
            WHEN p_profile ? 'twitter_url' THEN NULLIF(BTRIM(p_profile->>'twitter_url'), '')
            ELSE dm.twitter_url
        END,
        stored_avatar_url = CASE
            WHEN p_profile ? 'avatar_url' THEN NULLIF(BTRIM(p_profile->>'avatar_url'), '')
            ELSE dm.stored_avatar_url
        END,
        updated_at = NOW()
    WHERE dm.member_id = v_member_id;
END;
$$;

-- ag_submission_details: the view column was already named twitter_url
-- (via jsonb key), so CREATE OR REPLACE works here
CREATE OR REPLACE VIEW ag_submission_details AS
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
        'discord_id', dm.member_id::TEXT,
        'discord_username', dm.username,
        'discord_discriminator', dm.discriminator,
        'display_name', COALESCE(dm.global_name, dm.username),
        'avatar_url', COALESCE(dm.stored_avatar_url, dm.avatar_url),
        'bio', dm.bio,
        'real_name', dm.real_name,
        'website_url', dm.website_url,
        'instagram_url', dm.instagram_url,
        'twitter_url', dm.twitter_url,
        'discord_account_created_at', dm.discord_created_at,
        'banodoco_owner', COALESCE(dm.banodoco_owner, FALSE)
    ) AS profile
FROM ag_submissions s
LEFT JOIN ag_user_identities aui ON aui.auth_user_id = s.user_id
LEFT JOIN discord_members dm ON dm.member_id = aui.member_id
WHERE s.status <> 'rejected';
