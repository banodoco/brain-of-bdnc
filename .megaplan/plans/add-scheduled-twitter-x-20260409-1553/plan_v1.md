# Implementation Plan: Scheduled Twitter/X Posting

## Overview

The BNDC Discord bot already has a fully functional Twitter posting pipeline: `social_poster.post_tweet()` handles media upload + tweet creation, `Sharer.finalize_sharing()` orchestrates the full flow (download media, generate captions, post, record to DB, announce), and the admin chat agent can trigger posts via `share_to_social`. There's also a standalone script (`scripts/send_social_picks.py`) that generates daily social picks from the summary and DMs them to admin.

**Goal:** Let the admin queue tweets (with custom copy, media, reply/retweet targets, and scheduling times) and have them publish automatically on a daily cadence, reusing the existing sharing pipeline.

**Architecture approach:** Add a `scheduled_posts` Supabase table as the queue, a new admin chat tool (`schedule_tweet`) to manage the queue, and a `@tasks.loop` scheduler in the sharing cog that publishes due posts. Keep it simple — the admin is the only user, and the existing `share_to_social` + `finalize_sharing` flow already handles all the hard parts (media download, caption building, thread replies, DB recording, announcements).

## Phase 1: Database & Queue Foundation

### Step 1: Create `scheduled_posts` Supabase table
**Scope:** Small
1. **Create** a new Supabase migration for the `scheduled_posts` table with columns:
   - `id` (bigint, auto-increment PK)
   - `tweet_text` (text, nullable — custom copy; if null, auto-generate)
   - `message_link` (text, nullable — Discord message link for media source)
   - `message_id` (bigint, nullable — Discord message ID, alternative to link)
   - `channel_id` (bigint, nullable — resolved channel ID for the message)
   - `media_urls` (jsonb, nullable — direct media URLs when no Discord message source)
   - `reply_to_tweet_id` (text, nullable — for thread replies)
   - `quote_tweet_url` (text, nullable — for quote tweets)
   - `scheduled_for` (timestamptz, not null — when to publish)
   - `status` (text, default 'pending' — enum: pending, posting, posted, failed)
   - `tweet_url` (text, nullable — populated after posting)
   - `tweet_id` (text, nullable — populated after posting)
   - `error` (text, nullable — populated on failure)
   - `created_at` (timestamptz, default now())
   - `guild_id` (bigint, nullable)
   - `text_only` (boolean, default false)
2. **Add** an index on `(status, scheduled_for)` for efficient polling.

### Step 2: Add DB helper methods (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** `create_scheduled_post(...)` — insert a new row, return the created record.
2. **Add** `get_due_scheduled_posts(...)` — select rows where `status = 'pending'` and `scheduled_for <= now()`, ordered by `scheduled_for ASC`.
3. **Add** `update_scheduled_post_status(id, status, tweet_url=None, tweet_id=None, error=None)` — update a row's status and result fields.
4. **Add** `get_pending_scheduled_posts(...)` — select all pending posts ordered by `scheduled_for ASC` (for listing the queue).
5. **Add** `delete_scheduled_post(id)` — delete a pending post from the queue.

## Phase 2: Scheduler Loop

### Step 3: Add scheduled post publisher to `SharingCog` (`src/features/sharing/sharing_cog.py`)
**Scope:** Medium
1. **Add** a `@tasks.loop(minutes=1)` task `_publish_scheduled_posts` that:
   - Calls `db_handler.get_due_scheduled_posts()`.
   - For each due post, sets status to `'posting'`, then calls into the existing sharing pipeline.
   - If the scheduled post has a `message_link`/`message_id` + `channel_id`: call `sharer.finalize_sharing(...)` with `tweet_text`, `in_reply_to_tweet_id`, `text_only` from the scheduled row — this is exactly what `execute_share_to_social` does today.
   - If the scheduled post has `media_urls` but no Discord message: call `sharer.send_tweet(...)` directly with the media URLs.
   - If the scheduled post is text-only with no message source: call `social_poster.post_tweet()` directly with the tweet text and empty attachments.
   - On success: update status to `'posted'` with `tweet_url` and `tweet_id`.
   - On failure: update status to `'failed'` with error message. DM admin about the failure.
2. **Add** `before_loop` with `await self.bot.wait_until_ready()`.
3. **Start** the loop in `__init__` and cancel in `cog_unload()`.

### Step 4: Resolve message context for scheduled posts (`src/features/sharing/sharing_cog.py`)
**Scope:** Small
1. **Add** helper `_resolve_scheduled_post_channel(post)` that parses `message_link` to extract `channel_id` and `message_id` (reusing the same link-parsing logic from `execute_share_to_social` in `tools.py:1060-1076`), or uses stored `channel_id`/`message_id` directly.
2. **Handle** forum channels / threads the same way `execute_share_to_social` does (`tools.py:1083-1101`).

## Phase 3: Admin Interface

### Step 5: Add `schedule_tweet` admin chat tool (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** tool definition to `TOOLS` list:
   - `name`: `"schedule_tweet"`
   - Parameters: `message_link`, `message_id`, `tweet_text`, `reply_to_tweet`, `text_only`, `scheduled_for` (ISO datetime or relative like "tomorrow 2pm"), `quote_tweet_url`
   - If `scheduled_for` is omitted, default to next available slot in the posting schedule (or "next daily run").
2. **Add** `execute_schedule_tweet(bot, sharer, params)` function that:
   - Validates tweet text length (<=280 chars).
   - Resolves `message_link` to `channel_id` + `message_id` if provided.
   - Parses `scheduled_for` (support ISO format and relative dates like "tomorrow 3pm EST").
   - Calls `db_handler.create_scheduled_post(...)`.
   - Returns confirmation with scheduled time and queue position.
3. **Wire** the tool in the executor dispatch (`tools.py:~1866`).

### Step 6: Add `list_scheduled` and `cancel_scheduled` admin chat tools (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** `list_scheduled` tool — calls `db_handler.get_pending_scheduled_posts()`, formats as a numbered list with time, preview of tweet text, and status.
2. **Add** `cancel_scheduled` tool — takes `id`, calls `db_handler.delete_scheduled_post(id)`, returns confirmation.
3. **Wire** both in the executor dispatch.

### Step 7: Add `QUERYABLE_TABLES` entry (`src/features/admin_chat/tools.py:66`)
**Scope:** Tiny
1. **Add** `'scheduled_posts'` to the `QUERYABLE_TABLES` set so the admin can query it directly.

## Phase 4: Validation & Polish

### Step 8: Test the end-to-end flow
**Scope:** Medium
1. **Verify** scheduling a tweet via admin DM (schedule_tweet tool).
2. **Verify** the scheduler picks it up and posts it at the right time.
3. **Verify** listing and cancelling scheduled posts works.
4. **Verify** thread replies work (schedule a reply to an existing tweet).
5. **Verify** text-only posts work (no media source).
6. **Verify** failure handling — bad message link, expired media URL, Twitter API error.

## Execution Order
1. Create the DB table first (Step 1) — everything depends on it.
2. Add DB helpers (Step 2) — needed by both scheduler and admin tools.
3. Build the scheduler loop (Steps 3-4) — this is the core engine.
4. Build the admin tools (Steps 5-7) — this is how posts get queued.
5. End-to-end testing (Step 8).

## Validation Order
1. After Step 2: Verify DB methods work by inserting/querying a test row.
2. After Step 4: Manually insert a due post in Supabase and verify the scheduler picks it up and posts.
3. After Step 7: Test the full flow through admin DM commands.
