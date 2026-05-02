# Implementation Plan: Unified Social Publishing Queue

## Overview
The repo is targeting the right subsystem, but the previous plan was too loose about the actual root causes. Outbound publishing is currently split across admin chat in `src/features/admin_chat/tools.py`, summary/reaction-driven sharing in `src/features/sharing/sharer.py`, and the consent bridge in `src/features/reacting/subfeatures/tweet_sharer_bridge.py`, while persistence and deletion still depend on the single-row `shared_posts` model in `src/common/db_handler.py` and `src/features/sharing/subfeatures/notify_user.py`. No flag suggests abandoning the unified queue/service direction; the correction is to make `social_publications` the canonical source of truth, keep `shared_posts` only as narrow compatibility where legacy UI still needs it, carry caller/source context explicitly so admin, summary, and consent flows can share execution without losing their distinct side effects, and define queue claiming as a DB-atomic primitive instead of a Python select/update helper.

## Phase 1: Canonical Data Model and Request Contract

### Step 1: Add canonical publication storage and atomic claim SQL (`sql/social_publications.sql`, `src/common/db_handler.py`)
**Scope:** Large
1. Check in a SQL artifact that creates a canonical `social_publications` table for all outbound work and a `social_channel_routes` table for per-channel routing, instead of overloading `shared_posts`.
2. Store enough state in `social_publications` to cover current and planned behavior: publication id, guild/channel/message/user context, source kind, platform, action (`post|reply|retweet`), route key, request payload, target post ref, scheduled time, status (`queued|processing|succeeded|failed|cancelled`), attempt count, retry metadata, last error, published ref/url, delete support, and lifecycle timestamps.
3. Define a DB-atomic claim function in the same SQL artifact and call it via Supabase RPC from `DatabaseHandler`; implement claim semantics in SQL using row locking (for example `FOR UPDATE SKIP LOCKED`) so due jobs cannot be double-claimed by multiple pollers or after restarts.
4. Extend `DatabaseHandler` with queue-facing helpers such as `create_social_publication`, `claim_due_social_publications`, `mark_social_publication_processing`, `mark_social_publication_succeeded`, `mark_social_publication_failed`, `get_social_publications_for_message`, and `list_social_publications`.
5. Narrow `shared_posts` compatibility explicitly: do not treat it as canonical and do not blindly write one row for every `post|reply|retweet` outcome. Keep it only for remaining legacy consumers that still need a single tweet-like record, and drive duplicate/delete decisions from canonical publication data instead.

### Step 2: Define a source-aware normalized publish request (`src/features/sharing/`)
**Scope:** Medium
1. Introduce typed request/result objects such as `SocialPublishRequest`, `SocialPublishResult`, and `PublicationSourceContext` under `src/features/sharing/`.
2. Make the normalized request carry both execution data and source metadata: message/channel/guild/user ids, platform, action, schedule time, target post id/url, route override, text/media hints, and `source_kind` such as `admin_chat`, `reaction_bridge`, `summary`, or `reaction_auto`.
3. Add explicit per-request policy fields so caller-specific behavior survives the rewrite without leaking into provider code: `duplicate_policy`, `text_only`, `announce_policy`, `first_share_notification_policy`, `legacy_shared_post_policy`, and optional moderation/consent metadata.
4. Document first-release scope in the contract: X is the only live provider in this change, but the service boundary and DB shape stay platform-neutral.

## Phase 2: Unified Service and X Provider

### Step 3: Introduce `SocialPublishService` and make X the first provider (`src/features/sharing/social_publish_service.py`, `src/features/sharing/subfeatures/social_poster.py`, `src/features/sharing/sharer.py`)
**Scope:** Large
1. Add a `SocialPublishService` with two explicit entrypoints: `publish_now(request)` for immediate execution and `enqueue(request)` for scheduled work.
2. Wrap the existing X/Twitter implementation behind a provider interface with operations like `publish(request)`, `delete(publication)`, and `normalize_target_ref(...)`, instead of letting callers invoke `post_tweet()` or `delete_tweet()` directly.
3. Expand the X provider to support `post`, `reply`, and `retweet` publication actions. Keep delete support for authored `post` and `reply` publications, and explicitly mark `retweet` publications as not delete-supported in the current UX so the system does not route them through the normal tweet-delete path by mistake.
4. Move duplicate detection out of `Sharer._successfully_shared` and into canonical DB lookups that are both action-aware and caller-aware. Preserve current intent: primary non-reply posts can dedupe where existing callers rely on it, replies do not, and the consent bridge does not inherit the admin path’s duplicate suppression accidentally.
5. Keep media download/title/caption generation in `Sharer`, but refactor `Sharer` into request-building/orchestration code that hands a normalized request to the service instead of calling provider code directly.

### Step 4: Rewire all current callers without collapsing their distinct behavior (`src/features/admin_chat/tools.py`, `src/features/reacting/subfeatures/tweet_sharer_bridge.py`, `src/features/sharing/sharer.py`)
**Scope:** Large
1. Refactor `execute_share_to_social()` in `src/features/admin_chat/tools.py` to build a `SocialPublishRequest` and call the service. Preserve its existing reply behavior by keeping `text_only=True` as the default when `reply_to_tweet` is supplied unless the caller explicitly overrides it.
2. Expand the admin tool contract from immediate Twitter-only posting to the minimal operator surface this feature actually needs: `schedule_for`, `action`, `target_post`, and optional route/platform override, while still returning concrete `tweet_url` and `tweet_id` for unscheduled X publishes.
3. Keep `tweet_sharer_bridge.py` responsible for moderation, consent persistence, reactor DMs, and admin alerts. Replace only the final `send_tweet()` call with `publish_now()` using `source_kind='reaction_bridge'` and bridge-specific policy fields so those side effects remain intact.
4. Rewire summary-triggered sharing and any other `finalize_sharing()` callers onto the same service path. Do not force summary or reaction flows through the admin-chat parameter surface; each caller should build the same normalized execution object from its own context.
5. Route common post-success work through one service-level completion path so announcements, first-share notifications, compatibility writes, and canonical status updates happen once after successful execution instead of being duplicated across callers.

## Phase 3: Routing, Scheduling, Deletion, and Operator Visibility

### Step 5: Add routing resolution and scheduled worker lifecycle (`src/common/server_config.py`, `src/features/sharing/sharing_cog.py`)
**Scope:** Medium
1. Add a route resolution helper that layers on top of `ServerConfig.is_feature_enabled()`: require `sharing_enabled`, then resolve exact channel route, parent channel route, then guild default route.
2. Allow explicit route/platform override only where the admin tool provides one; ordinary reaction and summary flows should use resolved routing automatically.
3. Implement a scheduled worker in `SharingCog` using `discord.ext.tasks.loop`, with `before_loop` readiness gating, startup wiring in cog setup, and cleanup in `cog_unload` so the worker behaves like the repo’s other polling loops.
4. Make the worker claim due jobs via the new RPC-backed atomic helper, execute them through `SocialPublishService`, apply bounded retries for transient provider failures, and persist durable terminal states with publication id/platform/action/error in logs.
5. Normalize scheduled times to UTC before persistence and keep natural-language time parsing out of scope unless the repo already has a parser worth reusing.

### Step 6: Unify deletion/notification compatibility and admin observability (`src/features/sharing/subfeatures/notify_user.py`, `src/features/admin_chat/tools.py`)
**Scope:** Medium
1. Replace direct `social_poster.delete_tweet()` usage in `notify_user.py` with service/provider deletion that starts from the canonical publication record and only updates `shared_posts` as compatibility state afterward.
2. Keep the existing first-share delete UX only for delete-supported X publications. If the record is a `retweet` or another non-delete-supported action, return a clear unsupported result instead of calling the normal tweet-delete API blindly.
3. Extend the admin/operator read surface so staff can inspect queued, failed, processing, and succeeded publications without relying on `shared_posts` alone. The smallest acceptable version is a focused read tool or expanded trusted query surface for `social_publications` and route tables.
4. Update tool descriptions and prompt text that still describe a Twitter-only immediate path so operators can both schedule work and inspect failure state.

## Phase 4: Verification

### Step 7: Add focused tests and smoke checks (`tests/`)
**Scope:** Medium
1. Add DB/helper tests for canonical publication creation, action-aware duplicate lookup, narrowed `shared_posts` compatibility, route fallback, and RPC-backed claim behavior.
2. Add service tests for `publish_now()`, `enqueue()`, success/failure transitions, retry classification, and delete support branching between authored tweets and retweets.
3. Add caller-path tests proving admin chat, summary-triggered sharing, and reaction-consent publishing all reach the same service entrypoint while preserving their existing caller-specific defaults and side effects. Include an explicit test for admin replies defaulting to `text_only=True`.
4. Finish with manual X smoke tests for immediate post, scheduled post, scheduled reply, scheduled retweet, failed publish visibility, and the current first-share delete path on a delete-supported primary X publication.

## Execution Order
1. Land the SQL artifact and `DatabaseHandler` helpers first, including the RPC claim primitive and the narrowed `shared_posts` compatibility rules.
2. Add the normalized request/result types and `SocialPublishService` before rewiring any callers.
3. Rewire admin chat, summary-triggered sharing, and reaction-consent publishing onto the service while preserving each caller’s source-specific policies.
4. Add routing resolution, the scheduled worker, and `SharingCog` lifecycle hooks once canonical state transitions are stable.
5. Finish by moving delete/notification code and admin observability onto canonical publication records, then run targeted tests and smoke checks.

## Validation Order
1. Start with unit tests for request normalization, duplicate policy, route fallback, narrowed compatibility writes, and atomic claim semantics.
2. Test the immediate admin-chat path with the X provider mocked, including reply `text_only` defaults and concrete `tweet_url`/`tweet_id` responses.
3. Test summary-triggered and reaction-consent flows to confirm they preserve moderation, consent, DM, and announcement behavior while using the unified service.
4. Test scheduled worker execution, retries, and durable terminal failure recording with due queued publications.
5. End with manual X smoke tests for immediate post, scheduled post, scheduled reply, scheduled retweet, failed publish visibility, and delete compatibility for a delete-supported X post.
