# Implementation Plan: Tweet Thread Reply Support + Always Return Tweet URL

## Overview
The bot posts tweets via `post_tweet()` in `src/features/sharing/subfeatures/social_poster.py`, which is called from `Sharer.send_tweet()` and `Sharer.finalize_sharing()` in `src/features/sharing/sharer.py`. The admin-chat `share_to_social` tool (`src/features/admin_chat/tools.py`) invokes `finalize_sharing()` but that method returns `None`, so the admin LLM never sees the tweet URL. Tweets are always standalone — there is no plumbing to set `in_reply_to_tweet_id` on the underlying tweepy `create_tweet` call.

Goals:
1. Let callers post a tweet as a **reply to an existing tweet** (thread reply), all the way from `post_tweet()` up through `finalize_sharing()` and the admin-chat tool.
2. Make the tweet URL a **first-class return value** of every layer so the admin-chat tool (and any future caller) always surfaces it.

Constraints:
- Keep changes surgical to the sharing pipeline. Do not restructure `finalize_sharing`.
- The thread-reply path must still pass through the same dedupe / DB record / announcement / first-share notification flow — a reply is still a shared post.
- Twitter v2 API: `client_v2.create_tweet(..., in_reply_to_tweet_id=<str>)` is the native hook.

## Main Phase

### Step 1: Plumb `in_reply_to_tweet_id` through `post_tweet` (`src/features/sharing/subfeatures/social_poster.py`)
**Scope:** Small
1. **Add** an optional `in_reply_to_tweet_id: Optional[str] = None` parameter to `post_tweet()` at `src/features/sharing/subfeatures/social_poster.py:366`.
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
   Tweepy accepts `None` as "not a reply" so no branching is needed.
3. **Log** the reply target when set so failures are debuggable: one extra `logger.info` line noting `in_reply_to_tweet_id` before the create call.
4. **Return value already matches** — `post_tweet` already returns `{'url', 'id'}`; no change needed.

### Step 2: Accept and forward reply target from `Sharer.send_tweet` and `finalize_sharing` (`src/features/sharing/sharer.py`)
**Scope:** Medium
1. **Extend `send_tweet`** signature at `sharer.py:127` with `in_reply_to_tweet_id: Optional[str] = None`. Forward to `post_tweet` at `sharer.py:173`. `send_tweet` already returns `(success, tweet_url)` so the URL is surfaced — no change to return shape.
2. **Extend `finalize_sharing`** at `sharer.py:260` with `in_reply_to_tweet_id: Optional[str] = None`. Forward it to the `post_tweet(...)` call at `sharer.py:359-364`.
3. **Change `finalize_sharing` to return the result**. Currently it returns `None`. Introduce a small result dict:
   ```python
   # success path
   return {"success": True, "tweet_url": tweet_url, "tweet_id": tweet_id, "message_id": message_id}
   # failure paths (no attachments / dedupe / post_tweet None / exception)
   return {"success": False, "error": "<reason>", "message_id": message_id}
   ```
   Add early-return dicts at each existing `return` / abort point (lines ~269, 273, 281, 298, 399, 415). Leave the `finally` cleanup block intact.
4. **Audit other callers of `finalize_sharing`** (grep `finalize_sharing(`): they currently discard the return value (e.g., `asyncio.create_task(self.finalize_sharing(...))` at `sharer.py:231, 256`). No behavior change needed — ignoring a dict is fine — but double-check nothing asserts the old `None` return.

### Step 3: Surface URL and add reply param in admin-chat tool (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Add `reply_to_tweet`** property to the `share_to_social` tool schema at `tools.py:182-199`:
   ```json
   "reply_to_tweet": {
     "type": "string",
     "description": "Optional. Tweet ID or full tweet URL (https://twitter.com/<user>/status/<id>). If set, the new tweet is posted as a reply, forming a thread."
   }
   ```
   Update the tool `description` to mention thread-reply support.
2. **Parse it in `execute_share_to_social`** at `tools.py:989`. Accept either a raw ID or a URL; extract the trailing numeric ID (regex `status/(\d+)` or fallback to the param if it is all digits). Reject obvious garbage with a clear error.
3. **Pass** the parsed `in_reply_to_tweet_id` into `sharer.finalize_sharing(...)` at `tools.py:1054`.
4. **Await the return value** of `finalize_sharing` (it is already `await`-ed) and use it to build the tool response at `tools.py:1062`:
   ```python
   if result and result.get("success"):
       return {
           "success": True,
           "tweet_url": result["tweet_url"],
           "tweet_id": result["tweet_id"],
           "message": f"Posted tweet: {result['tweet_url']}"
                      + (" (reply in thread)" if in_reply_to_tweet_id else ""),
       }
   return {"success": False, "error": (result or {}).get("error", "Sharing failed")}
   ```
5. **Update the agent prompt** at `agent.py:46` and `agent.py:84` to mention the new `reply_to_tweet` parameter and that the tool returns a `tweet_url` the model should cite.

### Step 4: Fix the generic `twitter.com/user/...` URL (`src/features/sharing/subfeatures/social_poster.py`)
**Scope:** Small
1. **Replace** the hardcoded `user` placeholder at `social_poster.py:434` with the authenticated screen name. Call `api_v1.verify_credentials()` once (cached in a module-level `_cached_screen_name`) and build `https://twitter.com/{screen_name}/status/{tweet_id}`. Fall back to `user` if the lookup fails so the success path is not blocked.
2. This makes the URL clickable/shareable and matters now that it is the tool's primary return value.

### Step 5: Tests & manual verification (`tests/` if present, else scripts)
**Scope:** Small
1. **Look** for existing sharing tests (`grep -r "post_tweet\|finalize_sharing" tests/`). If tests exist, add unit coverage with a mocked `post_tweet`:
   - `finalize_sharing` returns `{"success": True, "tweet_url": ...}` on success.
   - `finalize_sharing` forwards `in_reply_to_tweet_id` to `post_tweet`.
   - `execute_share_to_social` parses a full tweet URL down to the numeric ID and returns `tweet_url` in its response.
2. If no test harness covers this module, add a minimal `tests/test_social_poster_reply.py` that monkeypatches `tweepy.Client` and asserts `create_tweet` is called with `in_reply_to_tweet_id="12345"`.
3. **Manual smoke (info-only):** run `scripts/test_social_picks.py` (already untracked in the repo) or an equivalent dry-run path against a staging account — post one standalone tweet, then post a reply to it, confirm the thread renders and both URLs come back.

## Execution Order
1. Step 1 (`post_tweet` param) — no callers break because the new arg is optional.
2. Step 2 (sharer plumbing + return dict) — only after Step 1 lands.
3. Step 4 (URL fix) — independent of 2, can land in parallel, but do it before Step 3 so the admin-chat tool surfaces a real URL.
4. Step 3 (admin-chat tool wiring) — depends on Steps 2 and 4.
5. Step 5 (tests) — last, after behavior is stable.

## Validation Order
1. `python -m pytest tests/ -k "share or tweet or social"` (or the nearest subset).
2. Targeted import smoke: `python -c "from src.features.sharing.subfeatures.social_poster import post_tweet; from src.features.sharing.sharer import Sharer"` to catch signature typos.
3. Full test suite if the targeted subset passes.
4. Manual end-to-end against a staging Twitter account (info-only).
