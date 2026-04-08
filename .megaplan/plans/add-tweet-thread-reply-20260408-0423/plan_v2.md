# Implementation Plan: Tweet Thread Reply Support + Always Return Tweet URL

## Overview
The bot posts tweets via `post_tweet()` in `src/features/sharing/subfeatures/social_poster.py`, called from `Sharer.send_tweet()` and `Sharer.finalize_sharing()` in `src/features/sharing/sharer.py`. The admin-chat `share_to_social` tool (`src/features/admin_chat/tools.py`) invokes `finalize_sharing()`, which currently returns `None`, so the admin LLM never sees the tweet URL. Tweets are also always standalone — there is no plumbing for `in_reply_to_tweet_id`.

There is one important wrinkle: `finalize_sharing` short-circuits on dedupe (`_currently_processing` and `_successfully_shared`) before any tweet is posted, and the repo already persists prior tweet URLs in the `shared_posts` table via `db_handler.record_shared_post()` / `db_handler.get_shared_post()` (`src/common/db_handler.py:852-924`). So an admin re-running `share_to_social` for an already-shared message must still get a usable URL back — it should be looked up from `shared_posts`, not reported as a generic failure.

Goals:
1. Let any caller post a tweet as a reply to an existing tweet (thread reply), end-to-end.
2. Make the tweet URL a first-class return value of every layer, including for already-shared messages (looked up from `shared_posts`).

Constraints:
- Surgical changes to the sharing pipeline; do not restructure `finalize_sharing`.
- Thread replies still pass through the same dedupe / DB record / announcement / first-share notification flow.
- Twitter v2: `client_v2.create_tweet(..., in_reply_to_tweet_id=<str>)` is the native hook.
- Thread replies bypass the per-message dedupe set (a thread is *intentionally* multiple posts about the same message).

## Main Phase

### Step 1: Plumb `in_reply_to_tweet_id` through `post_tweet` (`src/features/sharing/subfeatures/social_poster.py`)
**Scope:** Small
1. **Add** an optional `in_reply_to_tweet_id: Optional[str] = None` parameter to `post_tweet()` at `social_poster.py:366`.
2. **Pass** it to the v2 client call at `social_poster.py:429-431`:
   ```python
   tweet = await loop.run_in_executor(None,
       lambda: client_v2.create_tweet(
           text=final_caption,
           media_ids=[media_id],
           in_reply_to_tweet_id=in_reply_to_tweet_id,
       )
   )
   ```
   Tweepy accepts `None` as "not a reply", so no branching.
3. **Log** the reply target before the create call when set.
4. **Return value already matches** — `post_tweet` already returns `{'url', 'id'}`.

### Step 2: Fix the generic `twitter.com/user/...` URL (`src/features/sharing/subfeatures/social_poster.py`)
**Scope:** Small
1. **Replace** the hardcoded `user` placeholder at `social_poster.py:434` with the authenticated screen name. Cache it in a module-level `_cached_screen_name` populated lazily via `api_v1.verify_credentials().screen_name` on first successful post. Fall back to `user` on lookup failure so the success path is not blocked.
2. This matters now that the URL is the tool's primary return value.

### Step 3: Plumb reply target + return-dict through `Sharer` (`src/features/sharing/sharer.py`)
**Scope:** Medium
1. **Extend `Sharer.send_tweet`** at `sharer.py:127` with `in_reply_to_tweet_id: Optional[str] = None`. Forward to `post_tweet` at `sharer.py:173`. Existing `(success, tweet_url)` return is unchanged.
2. **Extend `Sharer.finalize_sharing`** at `sharer.py:260` with `in_reply_to_tweet_id: Optional[str] = None`. Forward into `post_tweet(...)` at `sharer.py:359-364`.
3. **Bypass dedupe for replies.** A reply to a previously-posted tweet for the same Discord message is intentional, so when `in_reply_to_tweet_id` is set, do *not* check `_successfully_shared` (and do not add to it on success). Still respect `_currently_processing` (in-flight lock) to avoid clobbering parallel runs.
4. **Return a result dict** from `finalize_sharing` (replacing the implicit `None`):
   ```python
   {"success": True,  "tweet_url": ..., "tweet_id": ..., "message_id": ..., "already_shared": False}
   {"success": False, "error": "<reason>", "message_id": ...}
   ```
   Add returns at every existing exit point (`sharer.py:269`, `273`, `281`, `298`, `399`, `415`). Keep the `finally` cleanup intact.
5. **Re-runs of already-shared messages return the prior URL.** At the existing dedupe check (`sharer.py:271-273`), when `message_id in self._successfully_shared` *and* `in_reply_to_tweet_id is None`, look up `db_handler.get_shared_post(message_id, 'twitter')` (`db_handler.py:904`) and return:
   ```python
   {"success": True, "tweet_url": prior["platform_post_url"],
    "tweet_id": prior["platform_post_id"], "message_id": message_id,
    "already_shared": True}
   ```
   If the lookup misses (e.g., DB row was pruned), return `{"success": False, "error": "Already shared but URL not found in shared_posts"}`. This fixes FLAG-001: an admin re-running `share_to_social` always gets back a usable URL for messages the bot has already posted.
6. **Audit other callers** of `finalize_sharing` (grep `finalize_sharing(`): they currently fire-and-forget via `asyncio.create_task` (`sharer.py:231, 256`). Discarding a returned dict is safe — no behavior change needed, but verify nothing asserts the prior `None`.

### Step 4: Surface URL + add `reply_to_tweet` in admin-chat tool (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Add `reply_to_tweet`** to the `share_to_social` schema at `tools.py:182-199`:
   ```json
   "reply_to_tweet": {
     "type": "string",
     "description": "Optional. Tweet ID or full tweet URL. If set, the new tweet is posted as a reply, forming a thread. Re-running on a previously-shared message without this flag will return the existing tweet URL."
   }
   ```
   Update the tool `description` to mention thread replies and that the response always includes `tweet_url`.
2. **Parse `reply_to_tweet`** in `execute_share_to_social` at `tools.py:989`. Accept either a raw numeric ID or a URL; extract the ID with `re.search(r"status/(\d+)", value)` and fall back to the value when it is all digits. Reject anything else with a clear error.
3. **Pass** the parsed ID into `sharer.finalize_sharing(...)` at `tools.py:1054` as `in_reply_to_tweet_id=...`.
4. **Use the returned dict** to build the tool response at `tools.py:1062`:
   ```python
   if result and result.get("success"):
       msg = f"Posted tweet: {result['tweet_url']}"
       if in_reply_to_tweet_id:
           msg += " (reply in thread)"
       elif result.get("already_shared"):
           msg = f"Already shared: {result['tweet_url']}"
       return {
           "success": True,
           "tweet_url": result["tweet_url"],
           "tweet_id": result["tweet_id"],
           "already_shared": result.get("already_shared", False),
           "message": msg,
       }
   return {"success": False, "error": (result or {}).get("error", "Sharing failed")}
   ```
5. **Update the agent prompt** at `agent.py:46` and `agent.py:84`: mention the new `reply_to_tweet` parameter, the `tweet_url` field, and the `already_shared` semantic so the model cites the existing URL on repeat calls.

### Step 5: Tests + manual verification
**Scope:** Small
1. **Look** for existing sharing tests (`grep -r "post_tweet\|finalize_sharing" tests/`). Add unit coverage with mocked tweepy / mocked `post_tweet`:
   - `post_tweet` forwards `in_reply_to_tweet_id` to `client_v2.create_tweet`.
   - `finalize_sharing` returns `{"success": True, "tweet_url": ...}` on the happy path.
   - `finalize_sharing` re-run on an already-shared message (no reply target) returns `{"success": True, "already_shared": True, "tweet_url": <prior>}` from a stubbed `db_handler.get_shared_post`.
   - `finalize_sharing` with `in_reply_to_tweet_id` set bypasses the `_successfully_shared` short-circuit and calls `post_tweet` again.
   - `execute_share_to_social` parses both `12345` and `https://twitter.com/foo/status/12345` into `"12345"` and includes `tweet_url` in its response.
2. If no test harness covers this module, add `tests/test_social_poster_reply.py` with the above as the minimum bar.
3. **Manual smoke (info-only):** post a standalone tweet, then a reply, against a staging account; confirm both URLs are returned and the thread renders.

## Execution Order
1. Step 1 (`post_tweet` reply param) — additive, no callers break.
2. Step 2 (real screen name in URL) — independent of 1, can land in parallel.
3. Step 3 (sharer plumbing, dedupe bypass for replies, prior-URL lookup) — depends on Step 1.
4. Step 4 (admin-chat tool wiring) — depends on Steps 2 and 3.
5. Step 5 (tests) — last, after behavior is stable.

## Validation Order
1. Targeted import smoke: `python -c "from src.features.sharing.subfeatures.social_poster import post_tweet; from src.features.sharing.sharer import Sharer"`.
2. `python -m pytest tests/ -k "share or tweet or social"`.
3. Full test suite once the targeted subset passes.
4. Manual end-to-end against a staging Twitter account (info-only).
