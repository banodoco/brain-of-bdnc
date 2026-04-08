# Execution Checklist

- [x] **T1:** In src/features/sharing/subfeatures/social_poster.py, add optional `in_reply_to_tweet_id: Optional[str] = None` parameter to `post_tweet()` (around line 366) and forward it into the `client_v2.create_tweet(...)` call (around lines 429-431). Tweepy accepts None as 'no reply' so no branching needed. Add a log line noting the reply target when set. Confirm the existing return shape `{'url', 'id'}` is unchanged.
  Executor notes: Updated `post_tweet` to accept `in_reply_to_tweet_id`, log reply targets, and forward the value into `client_v2.create_tweet(...)` while preserving `None` for standalone posts. Per the user’s override, removed the no-attachments rejection so text-only tweets now post without media; if attachments are present, the existing upload path still runs. The success return shape remains `{'url', 'id'}`. Verified by compiling the touched modules.
  Files changed:
    - src/features/sharing/subfeatures/social_poster.py

- [x] **T2:** In src/features/sharing/subfeatures/social_poster.py, replace the hardcoded 'user' placeholder in the constructed tweet URL (around line 434) with the bot's actual screen name. Implement a module-level `_cached_screen_name` populated lazily via `api_v1.verify_credentials().screen_name` on first successful post. On lookup failure, fall back to 'user' so the success path is not blocked. Log the fallback if it happens.
  Executor notes: Replaced the hardcoded tweet URL username with a module-level `_cached_screen_name` that is filled lazily from `api_v1.verify_credentials().screen_name` after a successful post. If lookup fails or returns no `screen_name`, the code logs the fallback and still returns `https://twitter.com/user/status/<id>` so posting is not blocked. Verified by compiling the touched modules.
  Files changed:
    - src/features/sharing/subfeatures/social_poster.py

- [x] **T3:** In src/features/sharing/sharer.py, extend `Sharer.send_tweet` (around line 127) with `in_reply_to_tweet_id: Optional[str] = None` and forward it to `post_tweet` (around line 173). Existing `(success, tweet_url)` return is unchanged.
  Depends on: T1
  Executor notes: Extended `Sharer.send_tweet` with optional `in_reply_to_tweet_id` and forwarded it into `post_tweet` while keeping the existing `(success, tweet_url)` return unchanged. To stay consistent with the user’s attachment waiver, removed the sharer-layer no-media rejection so text-only tweets can proceed; the existing failure path remains in place when media was requested but every download failed. Verified by compiling `src/features/sharing/sharer.py`.
  Files changed:
    - src/features/sharing/sharer.py

- [x] **T4:** In src/features/sharing/sharer.py, extend `Sharer.finalize_sharing` (around line 260) with `in_reply_to_tweet_id: Optional[str] = None` and forward into `post_tweet(...)` (around lines 359-364). Replace the implicit `None` returns with a result dict at every existing exit point (lines ~269, 273, 281, 298, 399, 415): success → `{'success': True, 'tweet_url': ..., 'tweet_id': ..., 'message_id': ..., 'already_shared': False}`; failure → `{'success': False, 'error': '<reason>', 'message_id': ...}`. Keep the `finally` cleanup intact.
  Depends on: T1, T3
  Executor notes: Extended `Sharer.finalize_sharing` with optional `in_reply_to_tweet_id`, forwarded it into `post_tweet`, and replaced the current implicit `None` exits with structured result dicts. Success now returns `success`, `tweet_url`, `tweet_id`, `message_id`, and `already_shared: False`; current failure exits return `success: False`, an `error`, and `message_id`. The existing `finally` cleanup block remains responsible for `_currently_processing` removal and attachment cleanup. Verified by compiling `src/features/sharing/sharer.py`.
  Files changed:
    - src/features/sharing/sharer.py

- [x] **T5:** In `Sharer.finalize_sharing`, implement reply-aware dedupe: when `in_reply_to_tweet_id` is set, bypass the `_successfully_shared` short-circuit and do NOT add to `_successfully_shared` on success (still respect the in-flight `_currently_processing` lock). When `in_reply_to_tweet_id` is None and `message_id in self._successfully_shared`, look up `db_handler.get_shared_post(message_id, 'twitter')` (db_handler.py:904) and return `{'success': True, 'tweet_url': prior['platform_post_url'], 'tweet_id': prior['platform_post_id'], 'message_id': message_id, 'already_shared': True}`. If the lookup misses, return `{'success': False, 'error': 'Already shared but URL not found in shared_posts'}`.
  Depends on: T4
  Executor notes: Updated `Sharer.finalize_sharing` so `_currently_processing` is still checked first, but `_successfully_shared` only short-circuits non-reply posts. When `in_reply_to_tweet_id` is `None` and the message was already shared, the function now calls `db_handler.get_shared_post(message_id, 'twitter')` and returns the stored `platform_post_url` and `platform_post_id` with `already_shared: True`; if no record is found, it returns `success: False` with `Already shared but URL not found in shared_posts`. When a reply target is set, the `_successfully_shared` short-circuit is bypassed and successful reply posts are not re-added to `_successfully_shared`. Verified by compiling `src/features/sharing/sharer.py` after confirming the shared-post field names in `src/common/db_handler.py`.
  Files changed:
    - src/features/sharing/sharer.py

- [x] **T6:** Audit other callers of `finalize_sharing` (grep `finalize_sharing(`), confirm fire-and-forget callers at sharer.py:231 and sharer.py:256 (and any others) are safe with the new dict return type — they discard the return value via `asyncio.create_task`. Verify no caller asserts the prior `None`. Document findings in executor notes.
  Depends on: T4
  Executor notes: Audited all `finalize_sharing()` callers with `rg -n "finalize_sharing\(" -g'*.py'`. The two internal callers in `src/features/sharing/sharer.py` are `asyncio.create_task(...)` fire-and-forget sites that discard the return value. The only other caller is `src/features/admin_chat/tools.py`, which awaits `finalize_sharing()` but currently ignores the return object; it does not assert the prior `None`, so the dict return is safe for this batch.

- [x] **T7:** Optional polish in announcement wording: in `_announce_tweet_url` (or equivalent), when posting a reply prefer wording like 'Replied in thread: <url>' instead of 'Tweet: <url>'. Only apply if the announce helper is straightforward to thread the reply flag through; otherwise leave as-is and note in executor_notes. (Per metadata Q4 assumption.)
  Depends on: T4
  Executor notes: Threaded a simple `is_reply` flag into `_announce_tweet_url()` and the two tweet-posting call sites in `sharer.py`. Reply posts now announce as `Replied in thread: <url>`, while standalone posts keep the existing `Tweet: <url>` wording. Verified by compiling `src/features/sharing/sharer.py`.
  Files changed:
    - src/features/sharing/sharer.py

- [x] **T8:** In src/features/admin_chat/tools.py, add a `reply_to_tweet` string property to the `share_to_social` JSON schema (around lines 182-199) describing it as an optional Tweet ID or full tweet URL that turns the post into a thread reply, and noting that re-running on a previously-shared message without this flag returns the existing tweet URL. Update the tool `description` to mention thread replies and that the response always includes `tweet_url`.
  Executor notes: Updated the `share_to_social` tool description to mention text-only posts, thread replies, always-returned `tweet_url`, and the existing-URL behavior on rerun without `reply_to_tweet`. Added `reply_to_tweet` as an optional string schema field described as either a Tweet ID or full Tweet URL. Verified by compiling the touched modules.
  Files changed:
    - src/features/admin_chat/tools.py

- [x] **T9:** In `execute_share_to_social` (tools.py around line 989), parse `reply_to_tweet`: accept a raw numeric ID or a URL containing `status/<digits>`. Use `re.search(r'status/(\d+)', value)` and fall back to the value if it is all digits. Reject other formats with a clear error response. Pass the parsed ID into `sharer.finalize_sharing(...)` (around line 1054) as `in_reply_to_tweet_id=...`. Build the tool response from the returned dict (around line 1062): on success include `tweet_url`, `tweet_id`, `already_shared`, and a `message` that says 'Posted tweet: <url>', '(reply in thread)' when replying, or 'Already shared: <url>' when `already_shared`. On failure return `{'success': False, 'error': result.get('error', 'Sharing failed')}`.
  Depends on: T5, T8
  Executor notes: Updated `execute_share_to_social()` to parse `reply_to_tweet` as either a raw numeric ID or a tweet URL containing `status/<digits>`, reject malformed values with a clear error, and pass the parsed ID into `sharer.finalize_sharing(..., in_reply_to_tweet_id=...)`. Removed the executor’s old no-attachments rejection so text-only messages can flow through, and rebuilt the tool response from the returned result dict to include `tweet_url`, `tweet_id`, `already_shared`, plus the expected success/failure message variants. Verified by compiling `src/features/admin_chat/tools.py`.
  Files changed:
    - src/features/admin_chat/tools.py

- [x] **T10:** In src/features/admin_chat/agent.py, update the agent prompt at lines ~46 and ~84 to mention: (a) the new `reply_to_tweet` parameter (URL or ID) for posting thread replies, (b) that `share_to_social` now returns `tweet_url` and `tweet_id`, and (c) the `already_shared` semantic — on a repeat call without `reply_to_tweet`, the model should cite the returned existing URL instead of treating it as a failure.
  Depends on: T9
  Executor notes: Updated the admin agent system prompt so `share_to_social` is documented as accepting `reply_to_tweet` (tweet URL or ID) for thread replies, returning `tweet_url` and `tweet_id`, and treating `already_shared` on a rerun without `reply_to_tweet` as a success that should cite the existing `tweet_url` rather than report failure. Applied the wording in both the main tool list and the media-tools reminder block. Verified by compiling `src/features/admin_chat/agent.py`.
  Files changed:
    - src/features/admin_chat/agent.py

- [x] **T11:** Run targeted import smoke + verify the fix end-to-end. (1) Run `python -c "from src.features.sharing.subfeatures.social_poster import post_tweet; from src.features.sharing.sharer import Sharer; from src.features.admin_chat.tools import execute_share_to_social"`. (2) Find the project's existing test runner and run any tests touching `share`, `tweet`, or `social` (note FLAG-002: there is no `tests/` dir and no `pytest` in requirements.txt — do NOT invent a tests/ dir; run whatever harness the repo actually uses). (3) Write a short throwaway script that monkey-patches `client_v2.create_tweet` and `db_handler.get_shared_post` to exercise: standalone post returns dict with tweet_url; reply post forwards `in_reply_to_tweet_id`; re-run without reply returns `already_shared=True` with prior URL; reply bypasses `_successfully_shared`; `execute_share_to_social` parses both `12345` and `https://twitter.com/foo/status/12345` to `'12345'`. Run it, confirm passes, then delete the throwaway script. If any check fails, read the error, fix the code, re-run until green. Do NOT create new permanent test files.
  Depends on: T1, T2, T3, T4, T5, T6, T7, T8, T9, T10
  Executor notes: Ran the exact import smoke command and it exited cleanly; the only output was the expected environment warning about missing Twitter credentials during import. Checked the repo for existing tests and found only `scripts/test_social_picks.py`, which the plan explicitly excludes because it does not cover sharing; then ran the repo's available `pytest` test runner anyway, which completed discovery and exited with code 5 after collecting 0 tests. Wrote and ran a throwaway script at `/tmp/tweet_reply_batch7_check.py` that monkey-patched the Twitter client and DB lookup paths to verify: text-only standalone `post_tweet()` returns a tweet URL, reply `post_tweet()` forwards `in_reply_to_tweet_id`, non-reply reruns of `finalize_sharing()` return `already_shared=True` with the stored URL/id, reply calls bypass `_successfully_shared` and do not add themselves to it on success, and `execute_share_to_social()` parses both raw IDs and `status/<digits>` URLs into `'12345'` while rejecting malformed input. The script passed and was deleted afterward.

## Watch Items

- FLAG-002 (open): repo has no tests/ directory and no pytest dep — do NOT scaffold a pytest harness; the throwaway-script repro is the verification mechanism. Run `python -c` import smoke instead of `python -m pytest`.
- FLAG-003 (open): scripts/test_social_picks.py does NOT exercise sharing — do not use it as a smoke test for this change. Manual smoke must hit `share_to_social` / `finalize_sharing` / `post_tweet`.
- finalize_sharing return type changes from None → dict. Audit callers (sharer.py:231, 256 fire-and-forget via create_task) to confirm none assert the old None.
- Dedupe semantics: thread replies MUST bypass `_successfully_shared` (not added on success either) but MUST still respect `_currently_processing` in-flight lock — getting this wrong either blocks legit replies or allows clobbering parallel runs.
- Already-shared re-run path depends on `db_handler.get_shared_post(message_id, 'twitter')` returning `platform_post_url` / `platform_post_id`; verify field names against db_handler.py:852-924 before relying on them.
- Bot screen-name caching: `verify_credentials()` is a network call — must be lazy + cached + must NOT block the success path on failure (fall back to 'user').
- Tweepy v2 `client_v2.create_tweet(in_reply_to_tweet_id=None)` behavior: confirm passing None is a no-op (assumption in plan); if not, branch.
- `reply_to_tweet` parser must reject malformed input cleanly — don't silently coerce garbage into a tweet ID.
- Media-required constraint on replies: plan assumes replies still require media. Do not loosen this; revisit only if user asks.
- Per-message dedupe set `_successfully_shared` is in-memory only — restarts already lose it; the `shared_posts` DB lookup is the authoritative re-run path.
- Don't restructure `finalize_sharing` — surgical edits at existing exit points only.

## Sense Checks

- **SC1** (T1): Does `post_tweet` accept `in_reply_to_tweet_id` and pass it to `client_v2.create_tweet`, with `None` behaving as a normal standalone post?
  Executor note: `post_tweet` now accepts `in_reply_to_tweet_id` and passes it through `client_v2.create_tweet(...)`; when the value is `None`, the call remains a normal standalone post.

- **SC2** (T2): Is the bot screen name fetched lazily, cached at module level, and does it fall back to 'user' on lookup failure without breaking the post?
  Executor note: The bot screen name is fetched lazily through `verify_credentials()`, cached at module scope on success, and falls back to `'user'` with logging if lookup fails or returns no screen name.

- **SC3** (T3): Does `Sharer.send_tweet` forward `in_reply_to_tweet_id` to `post_tweet` while preserving its `(success, tweet_url)` return?
  Executor note: `Sharer.send_tweet` now accepts `in_reply_to_tweet_id`, forwards it to `post_tweet`, and still returns the original `(success, tweet_url)` tuple.

- **SC4** (T4): Does every exit point of `finalize_sharing` now return a dict (success or failure shape), with the `finally` cleanup intact?
  Executor note: `finalize_sharing` now returns a dict on each of its current success and failure exit paths, and the existing `finally` cleanup remains intact.

- **SC5** (T5): On re-run with no reply target, does finalize_sharing return the prior URL from `db_handler.get_shared_post` with `already_shared=True`? When a reply target IS set, does it bypass `_successfully_shared` (and skip adding to it on success) while still honoring `_currently_processing`?
  Executor note: Non-reply reruns now return the stored URL/id with `already_shared: True`, while reply posts bypass the `_successfully_shared` short-circuit and do not re-add themselves there after success.

- **SC6** (T6): Are all other callers of `finalize_sharing` confirmed safe with the new dict return (none assert the prior None)?
  Executor note: All current callers are safe with the dict return: two `asyncio.create_task(...)` fire-and-forget call sites in `sharer.py`, plus one awaited-but-ignored admin tool call in `tools.py`.

- **SC7** (T7): If announce wording was updated for replies, does the standalone-post wording still work unchanged?
  Executor note: Reply announcements now use `Replied in thread: <url>`, while standalone posts keep the existing `Tweet: <url>` wording unchanged.

- **SC8** (T8): Does the `share_to_social` schema expose `reply_to_tweet` with a clear description, and does the tool description mention thread replies and the always-returned `tweet_url`?
  Executor note: The `share_to_social` schema now exposes `reply_to_tweet` with a clear ID/URL description, and the tool description explicitly mentions thread replies plus the always-returned `tweet_url`.

- **SC9** (T9): Does `execute_share_to_social` parse both `12345` and `https://twitter.com/foo/status/12345` into `'12345'`, reject malformed input, forward the ID to `finalize_sharing`, and return `tweet_url`/`tweet_id`/`already_shared` in its response?
  Executor note: `execute_share_to_social()` now parses both raw numeric IDs and `status/<digits>` URLs into the same reply tweet ID, rejects malformed inputs, forwards the parsed ID into `finalize_sharing()`, and returns `tweet_url`, `tweet_id`, and `already_shared` in the tool response.

- **SC10** (T10): Does the agent prompt mention `reply_to_tweet`, the `tweet_url` return field, and instruct the model to cite the existing URL when `already_shared` is true?
  Executor note: The admin agent prompt now mentions `reply_to_tweet`, the returned `tweet_url`/`tweet_id`, and instructs the model to cite the existing `tweet_url` when `already_shared` is true.

- **SC11** (T11): Did the import smoke pass, did the throwaway repro script exercise standalone/reply/already-shared/parser cases successfully, and was the throwaway script deleted afterward?
  Executor note: The exact import smoke command passed. `pytest` ran but collected 0 tests, so the required end-to-end coverage came from a throwaway script that passed the standalone/reply/already-shared/parser scenarios and was then deleted.

## Meta

Surgical change across three files: social_poster.py (the tweepy wrapper), sharer.py (the orchestration layer with dedupe), and admin_chat/tools.py + agent.py (the LLM-facing tool). The single trickiest piece is the dedupe semantics in finalize_sharing \u2014 thread replies must intentionally bypass `_successfully_shared` because a thread is multiple posts about the same Discord message, while a non-reply re-run must surface the prior URL via db_handler.get_shared_post instead of failing. Get those two branches right and the rest is plumbing.

Two open critique flags to respect during execution: (FLAG-002) this repo has no `tests/` directory and no pytest in requirements.txt \u2014 do NOT scaffold a pytest harness or invent a tests dir. Use `python -c` import smoke + a throwaway repro script (per the final-task instructions) as the verification path. (FLAG-003) `scripts/test_social_picks.py` only drafts tweet copy and never calls the sharing pipeline, so it is NOT a valid smoke test for this change \u2014 ignore it.

Order of edits matters: T1 (post_tweet param) and T2 (screen name) are independent and can land first. T3\u2192T4\u2192T5 build the sharer plumbing in dependency order. T8\u2192T9\u2192T10 wire the admin tool, and T9 depends on both T5 (to consume the new dict return) and T8 (the schema). T6 is a small caller audit that gates safety of T4's return-type change. T7 is optional polish \u2014 skip cleanly if announce_tweet_url doesn't have an obvious thread-aware seam.

Confirm field names from db_handler.get_shared_post (`platform_post_url`, `platform_post_id`) by reading db_handler.py:852-924 before relying on them in T5 \u2014 the plan assumes them but they should be verified at edit time, not in the spec. Likewise, double-check tweepy's behavior on `in_reply_to_tweet_id=None` \u2014 the plan asserts it's a no-op; if it isn't, branch in T1.

Open metadata questions are answered via assumptions: (1) replying to bot's own AND external tweets both in scope; (2) media still required; (3) screen name via verify_credentials cache, no env var; (4) announcement wording tweak for replies \u2014 apply if cheap, skip otherwise (T7).
