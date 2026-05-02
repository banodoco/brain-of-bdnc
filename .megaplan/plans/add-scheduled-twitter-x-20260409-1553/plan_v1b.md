# Implementation Plan: Scheduled Twitter/X Queue

## Overview
Repository inspection shows three usable building blocks already in place: the admin share tool in `src/features/admin_chat/tools.py:181-208` and `src/features/admin_chat/tools.py:1027-1153`, the main tweet/reply pipeline in `src/features/sharing/sharer.py:260-556` and `src/features/sharing/subfeatures/social_poster.py:386-540`, and a DB-backed polling pattern in `src/features/competition/competition_cog.py:154-350`. The simplest path is to add a DB-backed scheduling layer on top of those pieces instead of building a new media pipeline or UI.

The main structural constraint is that `shared_posts` in `src/common/db_handler.py:883-957` is a publication log for the current one-off share flow, not a queue or full history table. It cannot safely model pending jobs, retweets, or multiple scheduled posts over time. This feature needs a separate queue table and a small scheduler that reuses `Sharer.finalize_sharing()` for tweet/reply jobs where that behavior is already correct.

No prior clarification artifact exists. The questions below would change scope; the plan proceeds with the assumptions listed separately.

## Phase 1: Queue Foundation

### Step 1: Add a DB-backed scheduled-post queue (`supabase/migrations/`, `src/common/db_handler.py`)
**Scope:** Medium
1. **Create** a new `scheduled_social_posts` table in a checked-in SQL migration, even though the repo currently lacks a committed `supabase/` directory. This feature needs a versioned schema artifact instead of hiding the change in docs.
2. **Model** the queue around explicit state instead of overloading `shared_posts`:
   ```sql
   id, guild_id, status, post_type, publish_at,
   source_message_id, source_channel_id,
   tweet_text, text_only,
   reply_to_tweet_id, retweet_tweet_id,
   created_by_user_id,
   published_tweet_id, published_tweet_url,
   last_error, attempt_count,
   created_at, updated_at, published_at
   ```
   Use statuses like `queued`, `publishing`, `published`, `failed`, `cancelled` and types like `tweet`, `reply`, `retweet`.
3. **Add** DB helpers in `src/common/db_handler.py` alongside the existing competition helpers at `src/common/db_handler.py:1776-1905`: create queue row, list rows, cancel row, retry row, fetch due rows, atomically claim one due row, and mark it `published` or `failed`.
4. **Prefer** DB state transitions over in-memory locks for scheduler safety. Claim rows by moving `queued -> publishing` before any API call so the bot does not double-post across poll cycles or restarts.
5. **Validate early** with a cheap DB smoke path: insert a queue row, list it, claim it once, and confirm a second claim attempt does nothing.

### Step 2: Add admin queue-management tools (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Add** dedicated admin tools instead of relying on raw table writes: `queue_social_post`, `list_scheduled_social_posts`, `cancel_scheduled_social_post`, and `retry_scheduled_social_post` in `src/features/admin_chat/tools.py:77-533` and `src/features/admin_chat/tools.py:1812-1875`.
2. **Normalize** inputs at enqueue time so the scheduler does not parse free-form strings later:
   - Discord source: `message_id` or `message_link`
   - Reply target: bare tweet ID extracted from the current `reply_to_tweet` parser at `src/features/admin_chat/tools.py:1039-1049`
   - Retweet target: same URL-or-ID normalization
3. **Keep** queue writes on the dedicated tools, but add `scheduled_social_posts` to `QUERYABLE_TABLES` for inspection/debugging only.
4. **Update** the admin agent prompt in `src/features/admin_chat/agent.py:25-98` so the LLM knows when to queue, list, cancel, or retry instead of posting immediately.
5. **Cheap validation:** exercise tool parsing and enqueue/list/cancel flows with mocked DB helpers before any background scheduler is turned on.

## Phase 2: Publisher Integration

### Step 3: Add the background scheduler in sharing (`src/features/sharing/sharing_cog.py`, patterned after `src/features/competition/competition_cog.py:154-350`)
**Scope:** Medium
1. **Extend** `SharingCog` in `src/features/sharing/sharing_cog.py:11-24` with `on_ready` startup wiring plus a `@tasks.loop(minutes=1)` poller, following the same shape as the competition scheduler.
2. **Poll** for due rows, claim them through the new DB helper, and hand each claimed row to a small queue executor function. Keep the poller thin; put per-job branching in a helper module if `sharing_cog.py` starts to bloat.
3. **Recover** cleanly after restart by treating overdue `queued` rows as immediately eligible on the next pass. If `publishing` rows can be stranded by crashes, add a stale-publish timeout and explicit recovery path instead of leaving them stuck forever.
4. **Log** queue row IDs, source message IDs, and resulting tweet IDs/URLs so scheduled publishes can be traced in existing logs.

### Step 4: Reuse the existing share pipeline for tweet/reply jobs, add a narrow retweet branch (`src/features/sharing/sharer.py`, `src/features/sharing/subfeatures/social_poster.py`)
**Scope:** Medium
1. **Dispatch `tweet` and `reply` jobs** through `Sharer.finalize_sharing()` in `src/features/sharing/sharer.py:260-464` so queued posts keep the current media download, opt-out checks, announcement behavior, and first-share notification behavior.
2. **Audit dedupe semantics** before wiring queue jobs straight through. `Sharer.finalize_sharing()` currently short-circuits already-shared non-reply posts via `_successfully_shared` and `shared_posts` at `src/features/sharing/sharer.py:280-295` and `src/features/sharing/sharer.py:428-431`. If product scope allows multiple scheduled standalone posts from the same Discord source, add an explicit queue-only override; do not bypass the sharer by dropping to `post_tweet()` unless reuse is proven impossible.
3. **Add** a native retweet helper next to `post_tweet()` / `delete_tweet()` in `src/features/sharing/subfeatures/social_poster.py:386-540`, and route `retweet` jobs through that path instead of forcing them through `finalize_sharing()`.
4. **Persist** per-job outcomes back onto `scheduled_social_posts` regardless of job type. For tweet/reply jobs, keep `shared_posts` as the existing immediate-share log if that behavior is still desired, but treat the new queue table as the scheduler source of truth.
5. **Cheap validation:** run the queue executor against stubs for `Sharer.finalize_sharing()` and the Tweepy client to confirm `tweet`, `reply`, and `retweet` rows reach the correct branch and final status.

## Phase 3: Verification

### Step 5: Add a lightweight regression harness the repo will actually run (`scripts/` or `tests/`)
**Scope:** Small
1. **Add** the lightest persistent harness that matches repo conventions. Because the repo currently has scripts like `scripts/test_social_picks.py` but no committed `tests/` tree, prefer either a focused stdlib test module or a deterministic script such as `scripts/test_scheduled_social_posts.py`.
2. **Cover** the core yes/no cases:
   - queue row create/list/cancel/retry transitions
   - due-row claim idempotency
   - scheduled `tweet` job reuses the sharer path and stores tweet URL/ID
   - scheduled `reply` job forwards the normalized parent tweet ID
   - scheduled `retweet` job calls the retweet branch and stores success/failure
   - existing immediate `share_to_social` behavior still works after the new queue code lands
3. **Finish** with a staging smoke run against a non-production X account: one scheduled tweet, one scheduled reply, and one scheduled retweet.

## Execution Order
1. Land the queue schema and DB helpers before touching admin tools or the scheduler.
2. Wire admin enqueue/list/cancel/retry tools before enabling the background publisher so rows can be inspected manually.
3. Reuse the sharer for scheduled tweet/reply jobs before adding the retweet-only branch.
4. Add the regression harness before doing the staging publish smoke.

## Validation Order
1. Start with parser and DB-helper smoke checks.
2. Validate claim/publish state transitions with stubbed sharer/Twitter clients.
3. Re-run the immediate `share_to_social` path to catch regressions.
4. End with staging tweet/reply/retweet publishing.
