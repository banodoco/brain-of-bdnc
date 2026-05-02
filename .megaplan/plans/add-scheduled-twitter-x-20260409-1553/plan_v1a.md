# Implementation Plan: Scheduled X/Twitter Queueing

## Overview
The repo already has a manual X posting path that should be reused instead of rebuilt: `src/features/admin_chat/tools.py:1027` calls `Sharer.finalize_sharing()` in `src/features/sharing/sharer.py:260`, which in turn calls `post_tweet()` in `src/features/sharing/subfeatures/social_poster.py:386`. That path already supports custom tweet text and thread replies, and it records published posts in `shared_posts` via `src/common/db_handler.py:883`. What is missing is a persisted queue, a scheduler, queue-management tools for admin, and direct retweet support.

The simplest durable approach is:
1. Keep queued media-backed posts anchored to existing Discord messages so the current sharing pipeline can still fetch attachments and respect current author metadata.
2. Add a new Supabase-backed queue table instead of overloading `shared_posts`, because `shared_posts` is publication history, not an unpublished work queue, and it cannot represent retweets or text-only scheduled items cleanly.
3. Run scheduling inside the bot with the same `discord.ext.tasks` pattern already used elsewhere, but make queue claiming DB-atomic so duplicate Railway instances do not double-post.

## Phase 1: Queue Foundation

### Step 1: Lock the queue contract around the existing share flow (`src/features/admin_chat/tools.py:1027`, `src/features/sharing/sharer.py:260`, `src/features/sharing/subfeatures/social_poster.py:386`)
**Scope:** Small
1. **Trace** the current manual share path and document the exact branches that can be reused unchanged for scheduled original tweets and scheduled replies.
2. **Define** a minimal queue item model with `action_type` (`post`, `reply`, `retweet`), `status`, `scheduled_for`, optional Discord source message/channel, custom tweet text, `text_only`, external target tweet ID/URL, optional dependency on another queued item, publish result fields, and lease/attempt metadata.
3. **Reject** reusing `shared_posts` for the queue, because it only represents already-published content keyed to `discord_message_id` and would make retweets, cancellations, and retries awkward.

### Step 2: Add persisted queue storage and atomic state transitions (`src/common/db_handler.py`, new checked-in SQL schema artifact)
**Scope:** Medium
1. **Create** a new schema artifact for `scheduled_social_posts` with fields for queue payload, status, publish attempts, lease ownership/timestamps, and published tweet metadata.
2. **Add** `DatabaseHandler` helpers for `create_scheduled_social_post`, `list_scheduled_social_posts`, `cancel_scheduled_social_post`, `claim_due_scheduled_social_posts`, `mark_scheduled_social_post_published`, and `mark_scheduled_social_post_failed`.
3. **Make** claim/lease transitions atomic so only one bot instance can move a row from `queued` to `publishing`; this matters because the repo already documents duplicate Railway deployment risk in `docs/preventing-duplicate-deployments.md`.
4. **Keep** retry behavior explicit: failed rows remain visible and retryable instead of being silently retried forever.

## Phase 2: Admin Queue Ingress

### Step 3: Extend admin tooling to queue, inspect, and cancel posts (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Extend** the social tool surface so admin can schedule the same payloads they can already post immediately. The lowest-friction option is to add an optional `schedule_for` parameter to `share_to_social` and return queue metadata instead of publishing immediately when that field is present.
2. **Add** companion tools for `list_scheduled_social_posts` and `cancel_scheduled_social_post` so the queue is manageable without touching the database manually.
3. **Validate** payload shapes at tool-entry time:
   - `post`: allow Discord-backed media posts or text-only posts.
   - `reply`: require either a live target tweet ID/URL or a queued-parent reference.
   - `retweet`: require a live target tweet ID/URL.
4. **Update** the admin agent prompt so it knows when to queue versus publish-now and how to describe queued replies/retweets back to the admin.

### Step 4: Add a scheduled publish dispatcher that reuses `Sharer` where possible (`src/features/sharing/sharer.py`, `src/features/sharing/subfeatures/social_poster.py`, optional new helper module under `src/features/sharing/subfeatures/`)
**Scope:** Medium
1. **Introduce** one queue-execution entrypoint that reads a queued row and routes by `action_type`.
2. **Reuse** `Sharer.finalize_sharing()` for scheduled `post` and `reply` items that originate from Discord messages so attachment download, tweet text handling, author lookup, and `shared_posts` recording stay consistent with the manual path.
3. **Add** direct X retweet support in `social_poster.py` for `retweet`, because the current pipeline only knows how to create and delete tweets.
4. **Resolve** reply parents before publish. If a reply points to another queued item, require that parent item to be published first and then use its published tweet ID.
5. **Record** publish outcomes back into the queue row and continue writing to `shared_posts` for Discord-backed items so existing deletion/audit flows still work.

## Phase 3: Scheduler And Verification

### Step 5: Add the background scheduler to the sharing feature (`src/features/sharing/sharing_cog.py`, following the task-loop pattern in `src/features/summarising/summariser_cog.py:27`)
**Scope:** Medium
1. **Start** a bounded `tasks.loop` in `SharingCog` that wakes up frequently enough to catch due posts and claims ready queue rows from Supabase.
2. **Process** claimed rows serially or in a very small batch through the new dispatcher, with clear logging for queue ID, action type, publish result, and retry state.
3. **Recover** safely from crashes by reclaiming stale `publishing` leases after a timeout instead of reposting immediately.
4. **Preserve** the current manual `share_to_social` behavior; scheduled posting is an addition, not a rewrite.

### Step 6: Prove the change with focused automated coverage and one real smoke path (`tests/` or equivalent new test module, optional smoke script under `scripts/`)
**Scope:** Small
1. **Add** focused tests for queue payload validation, queue claim/idempotency rules, reply-parent resolution, and retweet dispatch.
2. **Mock** X/Tweepy calls so scheduler behavior and queue state transitions can be verified without hitting the live API.
3. **Add** one manual smoke path for a sandbox account: queue a normal post, a reply, and a retweet; confirm they publish once, update queue state correctly, and produce stored result URLs/IDs.

## Execution Order
1. Define the queue schema and DB helpers first so every caller writes the same payload contract.
2. Add admin queue ingress and pure validation tests before wiring the background loop.
3. Implement the publish dispatcher next, reusing `Sharer.finalize_sharing()` for posts/replies and adding the minimal new retweet helper.
4. Enable the scheduler only after queue creation and publish execution both work in direct invocation.

## Validation Order
1. Start with queue payload and DB state-transition tests.
2. Run dispatcher tests with mocked `Sharer`/Tweepy clients.
3. Finish with a manual smoke test against a sandbox X account to confirm no duplicate publishes and correct queue status updates.
