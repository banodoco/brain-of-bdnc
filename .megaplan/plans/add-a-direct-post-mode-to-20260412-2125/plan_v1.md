# Implementation Plan: Direct Post Mode for share_to_social

## Overview

The `share_to_social` tool (`src/features/admin_chat/tools.py:189`) currently requires a Discord message reference (`message_link` or `message_id`) as its entry point. All media, user context, and guild/channel metadata are derived from the fetched Discord message. We need to add an alternative "direct post" path that accepts `media_urls` (list of external URLs) and `tweet_text` directly, bypassing the Discord message entirely.

**Key existing infrastructure:**
- `sharer._download_media_from_url(url, message_id, item_index)` (sharer.py:84) already downloads arbitrary URLs to temp files with the exact dict shape needed for `media_hints`
- `SocialPublishRequest` (models.py:22) requires `message_id`, `channel_id`, `guild_id`, `user_id` — in direct mode these must be synthesized from admin context
- Route resolution (`_prepare_request_for_delivery`) uses `guild_id` + `channel_id` to find a social route; with `route_key` override this is bypassed
- `trusted_guild_id` is available in the `execute_tool` dispatcher (tools.py:3923) but not currently passed to `execute_share_to_social`

**Approach:** Add `media_urls` (list of strings) to the tool schema. When `media_urls` is provided without a message reference, enter the direct-post path: download each URL via `_download_media_from_url`, construct a `SocialPublishRequest` using the admin's guild context, and feed it through the existing publish pipeline. Skip duplicate detection, announce policy, first-share notification, and legacy shared post tracking (all Discord-message-specific).

## Main Phase

### Step 1: Update tool schema (`src/features/admin_chat/tools.py:189-238`)
**Scope:** Small
1. **Add** `media_urls` property to the `share_to_social` input schema:
   ```json
   "media_urls": {
       "type": "array",
       "items": {"type": "string"},
       "description": "Direct media URLs to post (e.g. Supabase video links). Use instead of message_link/message_id for external media. tweet_text is required when using media_urls."
   }
   ```
2. **Update** the tool description string to mention the direct-post mode: "Also supports direct posting with media_urls + tweet_text, bypassing Discord message lookup."

### Step 2: Pass guild context to execute_share_to_social (`src/features/admin_chat/tools.py:3956-3957`)
**Scope:** Small
1. **Update** the dispatcher call at line 3957 to pass `trusted_guild_id`:
   ```python
   return await execute_share_to_social(bot, sharer, trusted_tool_input, guild_id=trusted_guild_id)
   ```
2. **Update** the `execute_share_to_social` signature (line 1799) to accept `guild_id: Optional[int] = None`.

### Step 3: Implement direct-post branch in execute_share_to_social (`src/features/admin_chat/tools.py:1879-1895`)
**Scope:** Medium
1. **Extract** `media_urls` from params at the top of the function (near line 1807).
2. **After** the existing parameter validation block (around line 1878, after scheduled_at parsing), replace the current "Parse link or use direct ID" block (lines 1879-1895) with a three-way branch:
   - **If `media_urls` is provided** (direct-post mode):
     - Validate `tweet_text` is provided (required in direct mode, since there's no message to derive text from)
     - Validate `action != 'retweet'` or return error (retweet needs a target, not media)
     - Use the passed-in `guild_id` (from dispatcher) or return error if missing
     - Use a sentinel `message_id = 0` and `channel_id = 0` for the publication record
     - Download each URL via `sharer._download_media_from_url(url, 'direct', index)`
     - Collect results into `downloaded_attachments`; fail if any download fails
     - Skip duplicate detection (no real message_id to check)
     - Construct `SocialPublishRequest` with:
       - `message_id=0, channel_id=0, guild_id=guild_id, user_id=0`
       - `text=tweet_text`, `media_hints=downloaded_attachments`
       - `source_kind='admin_chat'`
       - `duplicate_policy={'check_existing': False}`
       - `announce_policy={'enabled': False}` (no Discord message to link back to)
       - `first_share_notification_policy={'enabled': False}` (no Discord user)
       - `legacy_shared_post_policy={'enabled': False}`
       - `source_context` with metadata containing just `{'guild_id': guild_id}`
       - All other fields (`action`, `target_post_ref`, `scheduled_at`, `route_override`, `text_only`, `platform`) from the existing param parsing
     - Route to the same publish/enqueue path as the existing flow
     - Return the same response shape (tweet_url, tweet_id, publication_id, success)
     - Skip `_announce_tweet_url` and first-share notification (no Discord user/message)
   - **Else if `message_link` or `message_id`** — existing flow (unchanged)
   - **Else** — existing error: "Provide either message_link, message_id, or media_urls"
3. **Ensure** temp file cleanup runs in the `finally` block for the direct-post path too (mirror the existing pattern at lines 2030-2035).

### Step 4: Update admin chat system prompt (`src/features/admin_chat/agent.py:141`)
**Scope:** Small
1. **Update** the share_to_social description in the agent system prompt to mention the direct-post capability, so the LLM knows it can use `media_urls` + `tweet_text` without a Discord message.

### Step 5: Validate (`tests/`)
**Scope:** Small
1. **Run** existing tests to confirm no regressions: `python -m pytest tests/ -x -q`
2. **Check** that the tool schema is valid JSON (no syntax errors from the edit).
3. **Manually trace** the direct-post code path to verify:
   - `media_urls` + `tweet_text` → downloads → SocialPublishRequest → publish_now/enqueue
   - Missing `tweet_text` with `media_urls` → clear error
   - Empty `media_urls` list → falls through to require message_link/message_id

## Execution Order
1. Steps 1-2 first (schema + plumbing) — no behavioral change yet.
2. Step 3 (core implementation) — the actual new code path.
3. Step 4 (prompt update) — so the admin chat LLM can discover the feature.
4. Step 5 (validation) — run tests and verify.

## Validation Order
1. Run existing test suite first to establish baseline.
2. After implementation, re-run tests to confirm no regressions.
3. Review the new code path for correctness of the SocialPublishRequest construction.
