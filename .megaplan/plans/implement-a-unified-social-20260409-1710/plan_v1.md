# Implementation Plan: Unified Social Publishing Queue

## Overview
Current outbound publishing is X-only and split across two paths: admin chat calls `share_to_social` into [`src/features/admin_chat/tools.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/admin_chat/tools.py#L1027) which then uses [`Sharer.finalize_sharing()`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/sharing/sharer.py#L260), while the reaction consent flow in [`tweet_sharer_bridge.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/reacting/subfeatures/tweet_sharer_bridge.py#L271) still calls `send_tweet()` directly. Persistence only records successful posts in [`shared_posts`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/common/db_handler.py#L883), so there is no durable queue, no scheduled execution, and no first-class failure tracking. The simplest durable fix is to centralize all outbound publishing behind one queue-backed service, keep X as the first platform adapter by wrapping [`social_poster.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/sharing/subfeatures/social_poster.py#L386), and preserve the current immediate-post UX by executing “publish now” through that same service.

## Phase 1: Foundation

### Step 1: Add a durable publication model and DB helpers
**Scope:** Medium
1. Add a new canonical table for outbound work, e.g. `social_publications`, instead of overloading `shared_posts`, because the current shape cannot represent multiple attempts, queued jobs, replies, retweets, or multi-platform fan-out cleanly.
2. Store at least: publication id, guild/channel/message/user context, platform, action (`post|reply|retweet`), route key, request payload, scheduled time, status (`queued|processing|succeeded|failed|cancelled`), attempt count, last error, published post id/url, and lifecycle timestamps.
3. Extend [`DatabaseHandler`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/common/db_handler.py#L883) with queue-oriented helpers such as `create_social_publication`, `claim_due_social_publications`, `mark_social_publication_succeeded`, `mark_social_publication_failed`, and `get_latest_successful_publication_for_message`.
4. Keep writing the legacy `shared_posts` success record on successful X publishes so the existing delete flow and duplicate lookup continue to work during the migration.
5. Since this repo does not currently contain a migrations directory, check in a small SQL migration artifact rather than relying on ad hoc dashboard edits.

### Step 2: Define routing/config resolution without a big config rewrite
**Scope:** Medium
1. Reuse the existing per-channel enablement model in [`ServerConfig.is_feature_enabled()`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/common/server_config.py#L119) and the parent-channel fallback already present there.
2. Add a small DB-backed routing layer for outbound targets, likely a `social_channel_routes`-style table plus a helper on `DatabaseHandler` or `ServerConfig`, with fallback order: exact channel, parent channel, guild default.
3. Model routes in a platform-neutral way: route key, enabled platforms, per-platform settings, and allowed actions. Ship one concrete route for X now, but avoid hard-coding `twitter` into the service contract.
4. Keep the existing `sharing_enabled` feature gate in [`ReactorCog`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/reacting/reactor_cog.py#L351) as the coarse on/off switch, and treat routing as the next-level decision after that gate passes.

## Phase 2: Core Integration

### Step 3: Introduce a unified publish service and make X the first adapter
**Scope:** Large
1. Add a `SocialPublishService` under `src/features/sharing/` that accepts a normalized publish request and exposes two entrypoints: `publish_now()` and `enqueue()`.
2. Convert [`social_poster.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/sharing/subfeatures/social_poster.py#L386) into the first provider implementation behind a small adapter interface such as `publish(request)`, `delete(published_ref)`, and `normalize_target_ref(...)`.
3. Extend the X adapter to handle three action types: normal tweet, reply tweet, and retweet/repost. Keep delete support intact for published tweet/reply records.
4. Move duplicate-detection and “already shared” lookups out of the in-memory-only `_successfully_shared` path in [`sharer.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/sharing/sharer.py#L280) and into DB-backed publication lookup so scheduled jobs and restarts behave correctly.
5. Keep media preparation and caption generation in `Sharer`, but make `Sharer` build a normalized request for the service instead of calling platform code directly.

### Step 4: Rewire all current callers onto the same service
**Scope:** Large
1. Refactor [`Sharer.finalize_sharing()`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/sharing/sharer.py#L260) so it becomes the main request-construction path for immediate and scheduled jobs rather than a Twitter-specific executor.
2. Remove the separate direct-post path in [`tweet_sharer_bridge.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/reacting/subfeatures/tweet_sharer_bridge.py#L271) and submit the same normalized request used by admin chat.
3. Expand the admin tool contract in [`tools.py`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/admin_chat/tools.py#L181) and [`execute_share_to_social()`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/admin_chat/tools.py#L1027) to accept scheduling and action parameters such as `schedule_for`, `action`, `target_post`, and optionally `route_key` or `platforms`.
4. Preserve the current immediate behavior when no schedule is supplied: the tool should still return a concrete `tweet_url`/`tweet_id` for X posts, not just a queued job id.
5. Route post-success side effects through the unified service as well: announcement messages, first-share notifications, and legacy `shared_posts` writes should happen after successful execution whether the job was immediate or queued.

## Phase 3: Scheduled Execution and Verification

### Step 5: Add a background worker using the repo’s existing polling pattern
**Scope:** Medium
1. Implement a background loop in [`SharingCog`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/sharing/sharing_cog.py) or a dedicated publishing cog, following the existing `discord.ext.tasks.loop` pattern already used by [`CompetitionCog`](/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc/src/features/competition/competition_cog.py#L318).
2. Poll for due queued publications, atomically claim them, execute them through `SocialPublishService`, and update status/attempt metadata.
3. Start with a simple retry model: bounded retries for transient provider failures, terminal failure for validation errors, and enough error detail for manual recovery.
4. Ensure the worker waits for bot readiness before running and is safe across restarts by relying on DB state rather than process memory.

### Step 6: Add focused tests and smoke checks
**Scope:** Medium
1. Create a small automated test suite for request normalization, route resolution, action parsing, queue claiming, duplicate handling, and success/failure transitions. Use stdlib `unittest` if you want zero new test dependencies.
2. Mock the X adapter so the service and worker can be tested without real credentials.
3. Add one narrow integration-style test for the admin tool path and one for the reaction-consent path to prove both reach the same service entrypoint.
4. Finish with manual smoke tests against a real X account: immediate post, scheduled post, scheduled reply, scheduled retweet, and failed publish visibility.

## Execution Order
1. Land the new publication table and DB helpers first.
2. Add the unified service and X adapter before touching call sites.
3. Rewire admin chat and reaction flows onto the service.
4. Add the background worker after the queue API and state transitions are stable.
5. Keep `shared_posts` compatibility until delete and duplicate lookup no longer depend on the old shape.

## Validation Order
1. Start with unit tests for request parsing, routing fallback, and publication state transitions.
2. Then test the immediate admin-chat path with the X adapter mocked.
3. Then test the scheduled worker path with due queued records.
4. End with manual X smoke tests for post, reply, retweet, and failure reporting.
