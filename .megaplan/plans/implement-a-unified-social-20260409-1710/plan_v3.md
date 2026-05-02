# Implementation Plan: Unified Social Publishing Queue

## Overview
The open flags do not indicate that the plan is aimed at the wrong subsystem or the wrong root cause. The repository evidence still points to the sharing stack as the correct integration point: outbound publishing is split across admin chat, summary and reaction flows, deletion still assumes legacy `shared_posts`, and `main.py` loads `SharingCog` through direct instantiation rather than the module-level `setup()` helper. The remaining gaps are narrower and concrete: the queue worker must be wired to the runtime `SharingCog` lifecycle that actually runs in `main.py`, and first-share notification and delete flows must carry a canonical `social_publications` identifier so one Discord message can safely map to multiple `post|reply|retweet` records.

This revision keeps the original goal intact: preserve immediate X posting, add scheduled queueing for `post|reply|retweet`, track durable success and failure, and keep the integration provider-based for future multi-platform work. Scope stays narrow to what the feature requires now: one live provider, canonical publication records, route resolution, scheduler wiring on the real load path, and a delete flow that targets a single publication record instead of guessing from legacy tweet identity.

## Phase 1: Foundation — Canonical Records, Claiming, and Request Shapes

### Step 1: Add canonical publication storage and atomic claim SQL (`sql/social_publications.sql`, `src/common/db_handler.py`)
**Scope:** Large
1. Check in a SQL artifact that creates `social_publications` as the canonical outbound table and `social_channel_routes` as the route table, instead of extending `shared_posts` for queue state.
2. Store enough canonical state in `social_publications` to cover current and planned behavior: publication id, guild/channel/message/user context, source kind, platform, action, route key, request payload, target post ref, scheduled time, status, attempt count, retry metadata, last error, published provider ref and url, delete support, and lifecycle timestamps.
3. Define a DB-atomic claim function in the same SQL artifact and call it via Supabase RPC from `DatabaseHandler`; implement claim semantics in SQL with row locking so due jobs cannot be double-claimed by overlapping workers or after restarts.
4. Extend `DatabaseHandler` with queue-facing helpers such as `create_social_publication`, `claim_due_social_publications`, `mark_social_publication_processing`, `mark_social_publication_succeeded`, `mark_social_publication_failed`, `mark_social_publication_cancelled`, `get_social_publication_by_id`, `get_social_publications_for_message`, and `list_social_publications`.
5. Narrow `shared_posts` compatibility explicitly: do not treat it as canonical and do not write one legacy row for every `post|reply|retweet` outcome. Keep it only for legacy consumers that still need a single tweet-like record, and drive duplicate and delete decisions from `social_publications` instead.

### Step 2: Define a source-aware normalized publish and delete contract (`src/features/sharing/`)
**Scope:** Medium
1. Introduce typed request and result objects such as `SocialPublishRequest`, `SocialPublishResult`, `PublicationSourceContext`, and a small delete-target shape under `src/features/sharing/`.
2. Make the normalized publish request carry both execution data and caller metadata: message, channel, guild, user, platform, action, scheduled time, target post id or url, route override, text and media hints, and `source_kind` such as `admin_chat`, `reaction_bridge`, `summary`, or `reaction_auto`.
3. Add explicit per-request policy fields so caller-specific behavior survives the rewrite without leaking into provider code: `duplicate_policy`, `text_only`, `announce_policy`, `first_share_notification_policy`, `legacy_shared_post_policy`, and optional moderation or consent metadata.
4. Make the publish result return the canonical `publication_id` alongside provider-facing values such as `tweet_id` and `tweet_url`, so downstream notification and deletion flows can target one publication record even when one Discord message has multiple publications.
5. Document first-release scope in the contract: X is the only live provider in this change, but the service boundary, DB shape, and delete identity remain platform-neutral.

## Phase 2: Core Integration — Unified Service and X Provider

### Step 3: Introduce `SocialPublishService` and make X the first provider (`src/features/sharing/social_publish_service.py`, `src/features/sharing/subfeatures/social_poster.py`, `src/features/sharing/sharer.py`)
**Scope:** Large
1. Add a `SocialPublishService` with two explicit entrypoints: `publish_now(request)` for immediate execution and `enqueue(request)` for scheduled work.
2. Wrap the existing X implementation behind a provider interface with operations like `publish(request)`, `delete(publication)`, and `normalize_target_ref(...)`, instead of letting callers invoke `post_tweet()` or `delete_tweet()` directly.
3. Expand the X provider to support `post`, `reply`, and `retweet` publication actions. Keep delete support for authored `post` and `reply` publications, and explicitly mark `retweet` publications as not delete-supported so the system does not send them through the standard tweet-delete path by mistake.
4. Move duplicate detection out of `Sharer._successfully_shared` and into canonical DB lookups that are both action-aware and caller-aware. Preserve current intent: primary non-reply posts can dedupe where existing callers rely on it, replies do not, and the consent bridge does not inherit admin duplicate suppression accidentally.
5. Route completion through one service-level success path that persists canonical status, performs any narrow `shared_posts` compatibility write, and emits a result payload that includes `publication_id`, provider refs, delete support, and the data needed for announcements or first-share DMs.
6. Keep media download, title extraction, and caption generation in `Sharer`, but refactor `Sharer` into request-building and orchestration code that hands a normalized request to the service instead of calling provider code directly.

### Step 4: Rewire current callers without collapsing their distinct behavior (`src/features/admin_chat/tools.py`, `src/features/reacting/subfeatures/tweet_sharer_bridge.py`, `src/features/sharing/sharer.py`)
**Scope:** Large
1. Refactor `execute_share_to_social()` in `src/features/admin_chat/tools.py` to build a `SocialPublishRequest` and call the service. Preserve the current reply behavior by keeping `text_only=True` as the default when `reply_to_tweet` is supplied unless the caller explicitly overrides it.
2. Expand the admin tool contract from immediate Twitter-only posting to the minimal operator surface this feature needs: `schedule_for`, `action`, `target_post`, and optional route or platform override, while still returning concrete `tweet_url` and `tweet_id` for unscheduled X publishes.
3. Keep `tweet_sharer_bridge.py` responsible for moderation, consent persistence, reactor DMs, and admin alerts. Replace only the final publish call with `publish_now()` using `source_kind='reaction_bridge'` and bridge-specific policy fields so those side effects remain intact.
4. Rewire summary-triggered sharing and any other `finalize_sharing()` callers onto the same service path. Do not force summary or reaction flows through the admin-chat parameter surface; each caller should build the same normalized execution object from its own context.
5. Move first-share notification triggering onto the unified completion path and pass the canonical `publication_id` into that notification call, rather than reconstructing delete identity later from only `discord_message_id` and `tweet_id`.

## Phase 3: Scheduling, Deletion, and Operator Visibility

### Step 5: Add routing resolution and scheduled worker lifecycle on the real load path (`src/common/server_config.py`, `src/features/sharing/sharing_cog.py`, `main.py`)
**Scope:** Medium
1. Add a route resolution helper that layers on top of `ServerConfig.is_feature_enabled()`: require `sharing_enabled`, then resolve exact channel route, parent channel route, then guild default route.
2. Allow explicit route or platform override only where the admin tool provides one; ordinary reaction and summary flows should use resolved routing automatically.
3. Implement the scheduled worker in `SharingCog` using `discord.ext.tasks.loop`, with readiness gating in `before_loop` and lifecycle management in `cog_load` and `cog_unload`.
4. Make the plan explicit about the active runtime path: because `main.py` instantiates `SharingCog(bot, bot.db_handler)` directly and then calls `await bot.add_cog(...)`, worker startup must hang off `SharingCog` lifecycle hooks or equivalent constructor-owned state that runs on that path. Do not rely only on the module-level `setup()` helper for startup behavior.
5. Keep the module-level `setup()` helper behavior aligned with the direct-instantiation path only as compatibility, so extension loading and direct `main.py` loading do not diverge.
6. Make the worker claim due jobs via the new RPC-backed atomic helper, execute them through `SocialPublishService`, apply bounded retries for transient provider failures, and persist durable terminal states with publication id, platform, action, and provider error in logs.
7. Normalize scheduled times to UTC before persistence and keep natural-language time parsing out of scope unless the repo already has a parser worth reusing.

### Step 6: Unify deletion, notification identity, and admin observability (`src/features/sharing/subfeatures/notify_user.py`, `src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium
1. Replace direct `social_poster.delete_tweet()` usage in `notify_user.py` with service or provider deletion that starts from the canonical `social_publications` record loaded by `publication_id`.
2. Update `send_post_share_notification()` and `PostShareNotificationView` to accept and store the canonical `publication_id`; keep `tweet_id` and `tweet_url` only for display and operator context.
3. Make the delete button resolve one canonical publication record, validate that it belongs to the expected Discord user and is still within the delete window, then branch on delete support. If the publication is a `retweet` or another unsupported action, return a clear unsupported result instead of calling the normal tweet-delete API blindly.
4. After successful deletion, update canonical publication state first and only then apply any narrow `shared_posts` compatibility update needed by remaining legacy consumers.
5. Extend the admin and operator read surface so staff can inspect queued, processing, failed, cancelled, and succeeded publication records and route configuration without relying on `shared_posts` alone.
6. Update tool descriptions and prompt text that still describe a Twitter-only immediate path so operators can both schedule work and inspect failure state.

## Phase 4: Verification

### Step 7: Add focused tests and smoke checks (`tests/`)
**Scope:** Medium
1. Add DB and helper tests for canonical publication creation, action-aware duplicate lookup, narrowed `shared_posts` compatibility, route fallback, atomic claim behavior, and `publication_id` lookup for delete flows.
2. Add service tests for `publish_now()`, `enqueue()`, success and failure transitions, retry classification, and delete support branching between authored tweets and retweets.
3. Add caller-path tests proving admin chat, summary-triggered sharing, and reaction-consent publishing all reach the same service entrypoint while preserving their existing caller-specific defaults and side effects. Include an explicit test for admin replies defaulting to `text_only=True`.
4. Add lifecycle coverage for the actual scheduler startup path: verify that the `SharingCog` worker starts and stops through `cog_load` and `cog_unload` when the cog is added the same way `main.py` adds it, rather than only through the extension `setup()` helper.
5. Add notification and delete-flow tests proving the DM view carries `publication_id`, deletes the intended canonical publication, rejects unsupported `retweet` deletion, and does not confuse multiple publications for the same Discord message.
6. Finish with manual X smoke tests for immediate post, scheduled post, scheduled reply, scheduled retweet, failed publish visibility, and the current first-share delete path on a delete-supported primary X publication.

## Execution Order
1. Land the SQL artifact and `DatabaseHandler` helpers first, including the RPC claim primitive and the narrowed `shared_posts` compatibility rules.
2. Add the normalized request and result types next so `publication_id` exists before service and notification rewiring begins.
3. Introduce `SocialPublishService` and the X provider boundary before rewiring callers.
4. Rewire admin chat, summary-triggered sharing, reaction-consent publishing, and first-share notification payloads onto the unified service path.
5. Add route resolution and the scheduled worker on `SharingCog` lifecycle hooks used by the direct `main.py` load path.
6. Finish by moving delete and admin observability code onto canonical publication records, then run targeted tests and smoke checks.

## Validation Order
1. Start with unit tests for request normalization, duplicate policy, route fallback, narrowed compatibility writes, atomic claim semantics, and `publication_id`-based delete lookup.
2. Test the immediate admin-chat path with the X provider mocked, including reply `text_only` defaults and concrete `tweet_url` and `tweet_id` responses.
3. Test summary-triggered and reaction-consent flows to confirm they preserve moderation, consent, DM, and announcement behavior while using the unified service.
4. Test scheduler execution, retries, terminal failure recording, and worker startup and shutdown through the real `SharingCog` add-cog lifecycle used in `main.py`.
5. End with manual X smoke tests for immediate post, scheduled post, scheduled reply, scheduled retweet, failed publish visibility, and delete compatibility for a delete-supported X post.
