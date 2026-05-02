# Execution Checklist

- [x] **T1:** Update tool schema and plumbing: (1) Add `media_urls` property to the `share_to_social` input schema in tools.py around line 205. (2) Append direct-posting documentation to the tool description at line 190. (3) Update the `execute_share_to_social` signature at line 1799 to accept `guild_id: Optional[int] = None`. (4) Update the dispatcher call at line 3957 to pass `guild_id=trusted_guild_id`. See plan Steps 1-2 for exact schema shape and wording.
  Executor notes: Updated `share_to_social` schema and plumbing in `src/features/admin_chat/tools.py`. Verified the description now documents direct-post mode at line 190, `media_urls` is defined as `type=array` with `items.type=string` at lines 206-211, `execute_share_to_social` accepts `guild_id: Optional[int] = None` at line 1810, and the dispatcher passes `guild_id=trusted_guild_id` at line 3969. Syntax verification passed with `python -m py_compile src/features/admin_chat/tools.py`.
  Files changed:
    - src/features/admin_chat/tools.py

- [x] **T2:** Implement direct-post branch in `execute_share_to_social` (plan Step 3). When neither `message_link` nor `message_id` is provided, enter direct mode. Key implementation points:

1. Extract `media_urls = params.get('media_urls') or []` near line 1807.
2. Replace the else-clause at line 1894-1895 with the direct-post branch:
   - Validate: retweet requires `target_post_id`, rejects `tweet_text`/`media_urls`; post/reply require `tweet_text`.
   - Resolve `guild_id` from the new parameter. Error if missing.
   - Download media via `sharer._download_media_from_url(url, 'direct', index)` for each URL. Fail if any download fails.
   - Construct `SocialPublishRequest` with sentinel IDs (`message_id=0, channel_id=0, user_id=0`), `user_details: {'direct_post': True}` in source_context metadata, disabled announce/first-share/legacy/duplicate policies.
   - If `scheduled_at`: call `social_publish_service.enqueue(request)` and return (do NOT clean up temp files).
   - If immediate: call `social_publish_service.publish_now(request)` in try/finally that cleans up temp files.
   - Skip `_announce_tweet_url` and first-share notification.
   - Return same response shape: `{success, message, tweet_url, tweet_id, publication_id, already_shared: False}`.
3. Existing message-based branch stays completely unchanged (just indented into the if-block). If `media_urls` provided alongside a message reference, ignore `media_urls`.

Critical details from plan: `user_details: {'direct_post': True}` satisfies XProvider guard at x_provider.py:31-33. `_build_tweet_caption` uses provided `tweet_text` as-is (social_poster.py:111-112). Target code size: under 80 new lines.
  Depends on: T1
  Executor notes: Implemented the direct-post branch in `src/features/admin_chat/tools.py`. Verified `media_urls = params.get('media_urls') or []` is extracted at line 1816. In the no-message branch, direct mode validates retweet restrictions, requires `guild_id` for direct posts, downloads each URL through `sharer._download_media_from_url`, constructs `SocialPublishRequest` with sentinel IDs at lines 1938-1941, sets `source_context.metadata['user_details']` to `{'direct_post': True}` at line 1958, and disables duplicate detection plus announce/first-share/legacy policies at lines 1950-1954. Scheduled direct posts preserve temp files via `preserve_downloads = True` at line 1968; immediate publishes clean up in `finally`. Verification passed with `python -m py_compile src/features/admin_chat/tools.py`, `python -m pytest tests/test_caller_paths.py -q`, and an inline direct-mode harness covering immediate publish, scheduled enqueue without cleanup, and retweet validation.
  Files changed:
    - src/features/admin_chat/tools.py

- [x] **T3:** Update the admin chat agent system prompt (plan Step 4). In `src/features/admin_chat/agent.py` around line 141, expand the `share_to_social` instruction to mention direct posting:

`- Share to social media with `share_to_social`, including scheduled posts/replies/retweets. Supports direct posting without a Discord message: provide tweet_text and optionally media_urls (e.g. Supabase video links). For retweets, provide action=retweet and target_post. Use the returned `publication_id` for canonical tracking, and if `already_shared` is true, report the existing `tweet_url`.`
  Depends on: T2
  Executor notes: Updated `src/features/admin_chat/agent.py` so the high-level `share_to_social` instruction at line 141 now mentions direct posting without a Discord message via `tweet_text` and optional `media_urls`, plus direct retweets via `action=retweet` and `target_post`. Also aligned the detailed tool synopsis at line 76 to the same behavior so the prompt is not self-contradictory. Verification passed with `python -m py_compile src/features/admin_chat/agent.py`, and text checks confirmed both prompt entries include `tweet_text`, `media_urls`, `publication_id`, `already_shared`, and direct retweet guidance.
  Files changed:
    - src/features/admin_chat/agent.py

- [x] **T4:** Run existing tests and validate (plan Step 5). Run `python -m pytest tests/ -x -q` and confirm all pass. Then manually trace the direct-post code path for correctness:
- `media_urls` + `tweet_text` -> downloads -> SocialPublishRequest -> publish_now/enqueue
- `tweet_text` alone (no media) -> text-only SocialPublishRequest -> publish_now
- `action=retweet` + `target_post` (no tweet_text, no media_urls) -> retweet via XProvider._retweet
- Missing `tweet_text` with `media_urls` for post action -> clear error
- `media_urls` alongside `message_link` -> message-based flow (media_urls ignored)

Write a short throwaway script that imports and calls `execute_share_to_social` with direct-mode params (mocked sharer/bot) to verify the branch is entered and validation works, run it, then delete it. If any test fails, read the error, fix the code, and re-run until all pass.
  Depends on: T3
  Executor notes: Full-suite verification passed with `python -m pytest tests/ -x -q`: 217 tests passed, 4 warnings, no test modifications needed. A throwaway script was created in `/tmp`, run successfully, and deleted afterward. The script manually traced the required direct-post scenarios: `media_urls` + `tweet_text` downloads into a direct request and reaches `publish_now`; scheduled direct media reaches `enqueue` and preserves temp files; `tweet_text` alone produces a text-only direct request and reaches `publish_now`; `action=retweet` + `target_post` produces a retweet request with no text/media; missing `tweet_text` with `media_urls` returns a clear error; and `media_urls` alongside `message_link` stays in the message-based flow with `media_urls` ignored.

## Watch Items

- XProvider.publish() guard at x_provider.py:31-33 requires truthy `user_details` — direct mode must set `{'direct_post': True}` in source_context metadata
- Scheduled direct posts must NOT clean up temp files (matches existing message-based scheduled behavior). Immediate posts MUST clean up in finally block.
- media_hints entries from `_download_media_from_url` already include the original `url` field — this preserves the source URL in stored payloads for future re-download
- message_id=0, channel_id=0, user_id=0 are sentinel values — verify no FK constraints in social_publications table reject them
- route_key is recommended but not required in direct mode — without it, route resolution uses guild_id + channel_id=0 which falls through to guild default
- When media_urls is provided alongside message_link/message_id, the message-based flow takes precedence and media_urls is silently ignored
- Do NOT make admin-chat-classification debt worse — anthropic client initialization timing is fragile
- Do NOT make social-channel-routes debt worse — route management tooling gap is pre-existing
- Do NOT make db-handler-social-publications debt worse — keep to existing access patterns

## Sense Checks

- **SC1** (T1): Does the new `media_urls` schema property use type=array with items.type=string, and is the tool description updated to document direct-post mode?
  Executor note: Confirmed in `src/features/admin_chat/tools.py` that `media_urls` uses `type=array` with `items.type=string`, and the `share_to_social` description explicitly documents direct-post mode with `media_urls + tweet_text`.

- **SC2** (T2): Does the direct-post branch correctly construct SocialPublishRequest with `user_details: {'direct_post': True}` in source_context.metadata, and does it disable announce/first-share/legacy/duplicate policies?
  Executor note: Confirmed in `src/features/admin_chat/tools.py` that the direct-post branch builds `SocialPublishRequest` with `source_context.metadata['user_details'] = {'direct_post': True}` and disables duplicate detection plus announce/first-share/legacy policies. The inline harness also verified the request shape and cleanup behavior.

- **SC3** (T3): Does the updated agent prompt mention both `tweet_text` and `media_urls` as parameters for direct posting, and does it mention direct retweet support?
  Executor note: Confirmed in `src/features/admin_chat/agent.py` that the updated prompt mentions direct posting with `tweet_text` and optional `media_urls`, and explicitly documents retweets via `action=retweet` plus `target_post`.

- **SC4** (T4): Do all existing tests pass without modification, and has the direct-post code path been traced for the five scenarios listed in the task?
  Executor note: Confirmed the full suite passed without modification (`217 passed`), and the throwaway direct-post script traced all required scenarios: direct media publish, scheduled enqueue, text-only direct post, direct retweet request, missing `tweet_text` validation, and message-link precedence with `media_urls` ignored.

## Meta

This is a focused 4-task execution: schema+plumbing, core implementation, prompt update, then validation. The core risk is getting the SocialPublishRequest construction right for direct mode — the sentinel IDs (0) and the `user_details` stub must satisfy downstream guards. The executor should read `x_provider.py:31-33` and `social_poster.py:111-112` before writing T2 to confirm the plan's assumptions about those guards still hold. The direct-post branch should be kept compact (under 80 lines) and structurally parallel to the existing message-based branch to minimize review surface. The `_download_media_from_url` method is already battle-tested in tweet_sharer_bridge.py — reuse it identically. For T2, the cleanest approach is to turn the existing `if message_link: ... elif message_id: ... else: error` block into `if message_link: ... elif message_id: ... else: [direct mode]` — the else-clause replaces a one-line error with the new branch, and the rest of the message-based flow stays in its original position inside the try block.
