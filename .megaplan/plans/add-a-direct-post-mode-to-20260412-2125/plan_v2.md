# Implementation Plan: Direct Post Mode for share_to_social

## Overview

The `share_to_social` tool (`src/features/admin_chat/tools.py:189`) currently requires a Discord message reference (`message_link` or `message_id`) as its entry point. All media, user context, and guild/channel metadata are derived from the fetched Discord message. We need to add an alternative "direct post" path that accepts `tweet_text` and optionally `media_urls` (list of external URLs), bypassing the Discord message entirely.

**Key existing infrastructure:**
- `sharer._download_media_from_url(url, message_id, item_index)` (`sharer.py:84`) already downloads arbitrary URLs to temp files with the exact dict shape needed for `media_hints`
- `SocialPublishRequest` (`models.py:22`) requires `message_id`, `channel_id`, `guild_id`, `user_id` — in direct mode these must be synthesized from admin context
- Route resolution (`_prepare_request_for_delivery`) uses `guild_id` + `channel_id` to find a social route; with `route_key` override this is bypassed
- `XProvider.publish()` (`x_provider.py:31-33`) guards on `metadata.get('user_details')` being truthy before calling `post_tweet()` — direct mode must supply a non-empty `user_details` dict
- `trusted_guild_id` is available in the `execute_tool` dispatcher (`tools.py:3923`) but not currently passed to `execute_share_to_social`

**Approach:** When neither `message_link` nor `message_id` is provided, enter direct-post mode. Validate inputs based on action (post/reply need `tweet_text`; retweet needs only `target_post`). Download any `media_urls` via `_download_media_from_url`. Construct a `SocialPublishRequest` with sentinel IDs (`message_id=0`, `channel_id=0`) and a minimal `user_details` stub to satisfy the XProvider guard. Feed through the existing publish pipeline. Skip Discord-specific policies (announce, first-share notification, legacy shared post). For scheduled posts, match the existing behavior: leave temp files in place for the executor, and additionally preserve the original URLs in `media_hints` for future re-download support.

## Main Phase

### Step 1: Update tool schema (`src/features/admin_chat/tools.py:189-238`)
**Scope:** Small
1. **Add** `media_urls` property to the `share_to_social` input schema (after `tweet_text`, around line 205):
   ```json
   "media_urls": {
       "type": "array",
       "items": {"type": "string"},
       "description": "Direct media URLs to attach (e.g. Supabase video links). Use instead of message_link/message_id for external media. Requires tweet_text for post/reply actions."
   }
   ```
2. **Update** the tool description string at line 190 to append: `" Also supports direct posting without a Discord message: provide tweet_text (and optionally media_urls) without message_link/message_id. Direct retweets need only action=retweet plus target_post."`

### Step 2: Pass guild context to execute_share_to_social (`src/features/admin_chat/tools.py:1799, 3956-3957`)
**Scope:** Small
1. **Update** the `execute_share_to_social` signature (line 1799) to accept `guild_id: Optional[int] = None`:
   ```python
   async def execute_share_to_social(
       bot: discord.Client,
       sharer,
       params: Dict[str, Any],
       guild_id: Optional[int] = None,
   ) -> Dict[str, Any]:
   ```
2. **Update** the dispatcher call at line 3957 to pass `trusted_guild_id`:
   ```python
   return await execute_share_to_social(bot, sharer, trusted_tool_input, guild_id=trusted_guild_id)
   ```

### Step 3: Implement direct-post branch in execute_share_to_social (`src/features/admin_chat/tools.py:1879-2081`)
**Scope:** Medium

The entry condition for direct mode is: neither `message_link` nor `message_id` is provided. Replace the current block at lines 1879-1895 with a two-way branch.

1. **Extract** `media_urls` from params near line 1807:
   ```python
   media_urls = params.get('media_urls') or []
   ```

2. **Direct-post branch** (when `not message_link and not message_id`):
   - **Validate action-specific requirements:**
     - `action == 'retweet'`: require `target_post_id`, reject if `tweet_text` or `media_urls` provided
     - `action in ('post', 'reply')`: require `tweet_text` (since there's no message to derive text from)
   - **Resolve guild_id** from the passed-in `guild_id` parameter (from dispatcher). Return error if missing.
   - **Download media** (if `media_urls` non-empty): iterate and call `sharer._download_media_from_url(url, 'direct', index)` for each. Fail if any download fails. Each result already has `url`, `filename`, `content_type`, `local_path` — the `url` field preserves the original URL for future re-download support.
   - **Construct `SocialPublishRequest`:**
     ```python
     request = SocialPublishRequest(
         message_id=0,
         channel_id=0,
         guild_id=guild_id,
         user_id=0,
         platform=platform,
         action=action,
         scheduled_at=scheduled_at,
         target_post_ref=target_post_id,
         route_override=route_override,
         text=tweet_text if action != 'retweet' else None,
         media_hints=downloaded_attachments,
         source_kind='admin_chat',
         text_only=text_only,
         duplicate_policy={'check_existing': False},
         announce_policy={'enabled': False},
         first_share_notification_policy={'enabled': False},
         legacy_shared_post_policy={'enabled': False},
         source_context=PublicationSourceContext(
             source_kind='admin_chat',
             metadata={
                 'user_details': {'direct_post': True},
                 'guild_id': guild_id,
             },
         ),
     )
     ```
     - **Key:** `user_details: {'direct_post': True}` satisfies the `XProvider.publish()` guard at `x_provider.py:31-33`. When `tweet_text` is provided as `request.text`, `_build_tweet_caption()` (`social_poster.py:111-112`) uses it as-is without looking at `user_details` fields.
   - **Publish/enqueue** using the same conditional as the existing flow:
     - If `scheduled_at`: call `social_publish_service.enqueue(request)` and return. Do NOT clean up temp files (same behavior as existing message-based scheduled flow — temp files persist for the executor).
     - If immediate: call `social_publish_service.publish_now(request)` in a try/finally that cleans up temp files.
   - **Skip** `_announce_tweet_url` and first-share notification (no Discord user/message).
   - **Return** the same response shape: `{success, message, tweet_url, tweet_id, publication_id, already_shared: False}`.

3. **Message-based branch** (when `message_link` or `message_id` is provided): existing flow, completely unchanged. If `media_urls` is also provided alongside a message reference, ignore `media_urls` (message attachments take precedence — avoids ambiguity).

4. **Update error message** at the bottom of the branch (old line 1895): `"Provide message_link, message_id, or tweet_text/media_urls for direct posting"`

5. **Temp file cleanup:** for immediate direct posts, mirror the existing `finally` cleanup block at lines 2030-2035. For scheduled direct posts, do NOT clean up (matches existing behavior — see note below).

   > **Note on scheduled media persistence:** The existing message-based scheduled flow has the same fragility: it downloads Discord attachments to temp, serializes `local_path` into `request_payload`, and leaves files for the deferred executor. Direct mode matches this behavior. Each `media_hints` entry already includes the original `url` field (from `_download_media_from_url`), preserving the source URL in the stored payload for a future re-download mechanism if needed. This is a pre-existing limitation, not a new regression.

### Step 4: Update admin chat system prompt (`src/features/admin_chat/agent.py:141`)
**Scope:** Small
1. **Update** the `share_to_social` instruction in the agent system prompt to mention direct posting capability. Around line 141, expand to:
   ```
   - Share to social media with `share_to_social`, including scheduled posts/replies/retweets. Supports direct posting without a Discord message: provide tweet_text and optionally media_urls (e.g. Supabase video links). For retweets, provide action=retweet and target_post. Use the returned `publication_id` for canonical tracking, and if `already_shared` is true, report the existing `tweet_url`.
   ```

### Step 5: Validate (`tests/`)
**Scope:** Small
1. **Run** existing tests to confirm no regressions: `python -m pytest tests/ -x -q`
2. **Check** that the tool schema is valid JSON (no syntax errors from the edit).
3. **Manually trace** the direct-post code path to verify:
   - `media_urls` + `tweet_text` → downloads → SocialPublishRequest → publish_now/enqueue
   - `tweet_text` alone (no media) → text-only SocialPublishRequest → publish_now
   - `action=retweet` + `target_post` (no tweet_text, no media_urls) → retweet via XProvider._retweet
   - Missing `tweet_text` with `media_urls` for post action → clear error
   - `media_urls` alongside `message_link` → message-based flow (media_urls ignored)

## Execution Order
1. Steps 1-2 first (schema + plumbing) — no behavioral change yet.
2. Step 3 (core implementation) — the actual new code path.
3. Step 4 (prompt update) — so the admin chat LLM can discover the feature.
4. Step 5 (validation) — run tests and verify.

## Validation Order
1. Run existing test suite first to establish baseline.
2. After implementation, re-run tests to confirm no regressions.
3. Review the direct-post path for correct SocialPublishRequest construction and XProvider compatibility.
