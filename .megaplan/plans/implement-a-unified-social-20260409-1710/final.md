# Execution Checklist

- [x] **T1:** Create SQL artifact and DatabaseHandler helpers for canonical publication storage.

**Files to create/modify:**
- Create `sql/social_publications.sql` — DDL for `social_publications` table (publication_id UUID PK, guild_id, channel_id, message_id, user_id, source_kind, platform, action [post|reply|retweet], route_key, request_payload JSONB, target_post_ref, scheduled_at timestamptz, status [queued|processing|succeeded|failed|cancelled], attempt_count, retry_after timestamptz, last_error text, provider_ref, provider_url, delete_supported bool, created_at, updated_at, completed_at, deleted_at) and `social_channel_routes` table (id, guild_id, channel_id, platform, route_config JSONB, enabled bool, timestamps). Include the `claim_due_social_publications` Postgres function that atomically selects and updates due rows (status='queued', scheduled_at <= now()) to status='processing' using `FOR UPDATE SKIP LOCKED`, returning claimed rows.
- Modify `src/common/db_handler.py` — Add methods using **fluent Supabase client** (NOT raw SQL) for all CRUD: `create_social_publication()`, `get_social_publication_by_id()`, `get_social_publications_for_message()`, `list_social_publications()`, `mark_social_publication_processing()`, `mark_social_publication_succeeded()`, `mark_social_publication_failed()`, `mark_social_publication_cancelled()`. Add one RPC-only method: `claim_due_social_publications()` using `self.supabase.rpc('claim_due_social_publications', {...}).execute()`. Do NOT use raw SQL via execute_query for this subsystem.

**Settled decisions:**
- Fluent client for CRUD, RPC only for atomic claim (settled decision `db-access-pattern`)
- `shared_posts` stays for legacy consumers; do not write one legacy row per post|reply|retweet outcome — only write a legacy row for primary non-reply posts where existing callers depend on it
- The SQL file is a checked-in artifact; deploying it to Supabase is an ops step (see watch items)

**DB method signatures should mirror existing patterns** — see `record_shared_post()` at db_handler.py:883 for style reference (guild_id gating via `_gate_check`, try/except with logging, return bool or Optional[Dict]).
  Executor notes: No code changes were needed on this rework pass. The route wiring fix did not alter the SQL artifact or the fluent-client DB helper pattern, and the full suite still covers the canonical storage and claim flow without introducing raw-SQL CRUD regressions.
  Reviewer verdict: Pass. Canonical table DDL, route table DDL, and the atomic claim function are present, and `DatabaseHandler` uses fluent Supabase CRUD plus RPC-only claiming as required.
  Evidence files:
    - sql/social_publications.sql
    - src/common/db_handler.py

- [x] **T2:** Define typed request/result contracts and source context under `src/features/sharing/`.

**Files to create:**
- Create `src/features/sharing/models.py` — Define dataclasses or typed dicts for:
  - `SocialPublishRequest`: message_id, channel_id, guild_id, user_id, platform, action (post|reply|retweet), scheduled_at, target_post_ref, route_override, text, media_hints, source_kind (admin_chat|reaction_bridge|summary|reaction_auto), duplicate_policy, text_only, announce_policy, first_share_notification_policy, legacy_shared_post_policy, and optional moderation/consent metadata
  - `SocialPublishResult`: publication_id, success, tweet_id, tweet_url, provider_ref, provider_url, delete_supported, already_shared, error
  - `PublicationSourceContext`: source_kind, caller-specific metadata

**Design notes:**
- The result MUST return `publication_id` alongside `tweet_id`/`tweet_url` so downstream notification and delete flows can target one record
- Per-request policy fields let caller-specific behavior survive without leaking into provider code
- Use Python dataclasses with sensible defaults; keep it simple
- X is the only live provider but contracts must be platform-neutral
  Depends on: T1
  Executor notes: No code changes were needed on this rework pass. The normalized request/result contracts remain intact, including publication_id and the caller-policy fields, and the full suite still exercises those contracts through caller and service tests.
  Reviewer verdict: Pass. Normalized request/result models include `publication_id`, source context, and the caller policy fields required by the plan.
  Evidence files:
    - src/features/sharing/models.py

- [x] **T3:** Introduce SocialPublishService with publish_now/enqueue entrypoints and wrap X behind a provider interface.

**Files to create/modify:**
- Create `src/features/sharing/social_publish_service.py` — Central service with:
  - `publish_now(request: SocialPublishRequest) -> SocialPublishResult`: Immediate execution path. Creates canonical publication record (status='processing'), delegates to provider, updates to succeeded/failed, performs legacy shared_posts compatibility write for primary non-reply posts, returns result with publication_id.
  - `enqueue(request: SocialPublishRequest) -> SocialPublishResult`: Creates canonical record with status='queued' and scheduled_at, returns result with publication_id (no provider call yet).
  - `execute_publication(publication_id)`: Internal method used by scheduler to execute a claimed publication.
  - `delete_publication(publication_id)`: Loads canonical record, validates delete_supported, delegates to provider.
  - Success path: persist canonical status → optional legacy shared_posts write → return result with publication_id + provider refs + delete_supported + announcement data.

- Create `src/features/sharing/providers/__init__.py` and `src/features/sharing/providers/x_provider.py` — Provider interface and X implementation:
  - Interface: `publish(request)`, `delete(publication)`, `normalize_target_ref(...)`
  - X provider wraps existing `social_poster.post_tweet()` and `social_poster.delete_tweet()` 
  - Supports actions: post, reply, retweet. Delete supported for post and reply only; retweet explicitly marked delete_supported=False.

- Modify `src/features/sharing/sharer.py` — Refactor `Sharer` to be request-building and orchestration code:
  - Keep media download, title extraction, caption generation in Sharer
  - `finalize_sharing()` builds a `SocialPublishRequest` and hands it to `SocialPublishService.publish_now()` instead of calling `post_tweet()` directly
  - Move duplicate detection from in-memory `_successfully_shared` set to canonical DB lookups that are action-aware and caller-aware (primary non-reply posts dedupe, replies don't, reaction bridge doesn't inherit admin duplicate suppression)
  - `send_tweet()` similarly routes through the service

**Critical behavioral preservation:**
- Admin replies default to text_only=True (callers-2 flag)
- Reaction bridge does NOT inherit duplicate suppression (callers-3 flag)
- First-share notification now receives publication_id from result
  Depends on: T2
  Executor notes: No new provider-surface changes were required beyond the route preparation now applied before publication creation. The provider boundary remains unchanged, retweets are still non-deletable, and the full suite passes with the service still handling success and failure transitions correctly.
  Reviewer verdict: Pass. The unified service/provider boundary is in place, retweets are explicitly non-deletable, and legacy `shared_posts` compatibility is narrowed to post actions.
  Evidence files:
    - src/features/sharing/social_publish_service.py
    - src/features/sharing/providers/__init__.py
    - src/features/sharing/providers/x_provider.py

- [x] **T4:** Rewire all callers to use the unified service while preserving their distinct behavior.

**Files to modify:**

1. `src/features/admin_chat/tools.py` (execute_share_to_social around line 1027):
   - Build a SocialPublishRequest with source_kind='admin_chat'
   - Add new parameters: schedule_for (ISO-8601 UTC), action (post|reply|retweet), target_post (tweet ID or URL), platform override
   - For immediate publishes (no schedule_for): call service.publish_now(), return tweet_url/tweet_id as before
   - For scheduled publishes: call service.enqueue(), return publication_id and scheduled status
   - Preserve: text_only=True default when reply_to_tweet is supplied
   - Update tool schema/description to reflect new parameters

2. `src/features/reacting/subfeatures/tweet_sharer_bridge.py` (around line 97):
   - Replace final publish call with service.publish_now() using source_kind='reaction_bridge'
   - Keep ALL moderation, consent persistence, reactor DMs, and admin alerts in the bridge
   - Only replace the actual tweet posting call, not the surrounding moderation/notification logic
   - Set bridge-specific policy fields (no duplicate suppression, etc.)

3. `src/features/sharing/sharer.py`:
   - Summary-triggered sharing (initiate_sharing_process_from_summary around line 236) routes through same service path
   - finalize_sharing() already rewired in T3, but verify all callers pass correct source_kind

4. `src/features/sharing/subfeatures/notify_user.py` (send_post_share_notification around line 168):
   - Update to accept and pass publication_id alongside tweet_id/tweet_url
   - PostShareNotificationView constructor (line 24) accepts publication_id
   - First-share notification triggered on unified completion path with publication_id

**Do NOT force all callers through the admin-chat parameter surface** — each caller builds its own SocialPublishRequest from its own context.
  Depends on: T3
  Executor notes: No additional caller-behavior changes were required beyond revalidating that admin reply text_only defaults, summary publishing, reaction-bridge side effects, and publication_id propagation still hold after the route fix. The full suite confirms those caller paths remain stable.
  Reviewer verdict: Pass. Admin chat, summary-triggered sharing, and reaction-consent publishing all reach the shared service while preserving caller-specific behavior.
  Evidence files:
    - src/features/admin_chat/tools.py
    - src/features/sharing/sharer.py
    - src/features/reacting/subfeatures/tweet_sharer_bridge.py
    - src/features/sharing/subfeatures/notify_user.py

- [x] **T5:** Add route resolution and scheduled worker lifecycle on the real load path.

**Files to create/modify:**

1. `src/common/server_config.py` — Add route resolution helper:
   - `resolve_social_route(guild_id, channel_id, platform)`: Requires sharing_enabled via is_feature_enabled(), then resolves: exact channel route → parent channel route → guild default route
   - For v1, guild-default routing is the only active path (settled decision `v1-routing-scope`); channel-specific routes can be seeded via direct DB inserts
   - Explicit route/platform override only where admin tool provides one

2. `src/features/sharing/sharing_cog.py` — Add scheduled worker:
   - Use `discord.ext.tasks.loop` pattern (consistent with repo conventions)
   - Worker runs on interval (e.g., 30s), calls db_handler.claim_due_social_publications(), executes each through SocialPublishService.execute_publication()
   - **Critical: main.py instantiates SharingCog directly** (`SharingCog(bot, bot.db_handler)` then `await bot.add_cog(...)`) — worker startup MUST use cog_load/cog_unload lifecycle hooks, NOT rely on module-level setup() alone
   - before_loop: wait_until_ready() gating
   - cog_load: start the loop
   - cog_unload: cancel/stop the loop
   - Keep setup() aligned with direct-instantiation path for compatibility
   - Bounded retries for transient provider failures; persist terminal states with publication_id, platform, action, and provider error in logs
   - Normalize scheduled times to UTC before persistence; no natural-language time parsing

3. `main.py` — Wire SocialPublishService into SharingCog:
   - SharingCog needs access to SocialPublishService (constructed with db_handler and X provider)
   - Ensure the service instance is accessible to admin tools and sharer via bot or cog reference
  Depends on: T4
  Executor notes: Fixed the returned review issue by resolving routes on the live publish_now/enqueue path before any canonical row is created, enforcing the sharing_enabled gate, failing cleanly when no route exists, and persisting the selected route on the canonical publication via route_key plus normalized route_override payload data. Added explicit admin route override support through share_to_social route_key input, verified the exact bug with a throwaway repro script, and re-ran targeted plus full tests to confirm live path routing now works.
  Files changed:
    - src/features/sharing/social_publish_service.py
    - src/features/admin_chat/tools.py
  Reviewer verdict: Pass. The rework gap was actually fixed: live publish/enqueue now resolve routes before row creation, the scheduler uses `SharingCog` lifecycle hooks, and `main.py` wires a shared `SocialPublishService` onto the active runtime path.
  Evidence files:
    - src/common/server_config.py
    - src/features/sharing/social_publish_service.py
    - src/features/sharing/sharing_cog.py
    - main.py

- [x] **T6:** Unify deletion, notification identity, and admin observability.

**Files to modify:**

1. `src/features/sharing/subfeatures/notify_user.py`:
   - Replace direct `social_poster.delete_tweet()` usage with service/provider deletion starting from canonical social_publications record loaded by publication_id
   - PostShareNotificationView stores publication_id; keeps tweet_id/tweet_url for display only
   - Delete button: load canonical record by publication_id → validate user ownership and delete window → check delete_supported → if retweet or unsupported action, return clear error message → else delete via provider → update canonical state first → then optional legacy shared_posts update
   - On timeout (6 hours): disable button as before

2. `src/features/admin_chat/tools.py`:
   - **Add `social_publications` and `social_channel_routes` to QUERYABLE_TABLES** (around line 66 and line 1637) — settled decision `admin-query-whitelist`
   - Add admin tools or expand existing tool to inspect publication records: queued, processing, failed, cancelled, succeeded states
   - Update share_to_social tool description to reflect scheduling capability

3. `src/features/admin_chat/agent.py`:
   - **Update system prompt table list** (around line 38) to include social_publications and social_channel_routes
   - Update tool descriptions that still describe Twitter-only immediate path
   - Document that operators can schedule work and inspect failure state

**This task addresses settled decisions:**
- `admin-query-whitelist`: explicit file paths and line numbers for QUERYABLE_TABLES updates
- Ensures admin agent can query new tables
  Depends on: T5
  Executor notes: No code changes were needed on this rework pass. The publication_id-based delete flow and admin query visibility remain intact, and the full suite still covers canonical delete targeting plus the admin surfaces introduced earlier.
  Reviewer verdict: Pass. Deletion now targets canonical publication rows by `publication_id`, and the admin query/operator surfaces expose the new tables and scheduling language.
  Evidence files:
    - src/features/sharing/subfeatures/notify_user.py
    - src/features/admin_chat/tools.py
    - src/features/admin_chat/agent.py

- [x] **T7:** Add focused tests and run verification.

**Test location:** Create `tests/` directory and test files. This repo has no existing test infrastructure (no pytest.ini, no tests/ dir except one script at scripts/test_social_picks.py), so:

1. Create `tests/test_social_publications.py`:
   - DB helper tests: canonical publication creation, action-aware duplicate lookup, narrowed shared_posts compatibility, route fallback, publication_id lookup for delete flows
   - Mock Supabase client for unit tests

2. Create `tests/test_social_publish_service.py`:
   - Service tests: publish_now(), enqueue(), success/failure transitions, retry classification, delete support branching (post/reply deletable, retweet not)
   - Mock X provider

3. Create `tests/test_caller_paths.py`:
   - Caller-path tests: admin chat, summary-triggered, reaction-consent all reach same service entrypoint
   - Admin replies default text_only=True
   - Reaction bridge preserves moderation/consent side effects

4. Create `tests/test_scheduler.py`:
   - Lifecycle: SharingCog worker starts/stops through cog_load/cog_unload
   - Claim → execute → terminal state flow

5. Create `tests/test_notification_delete.py`:
   - DM view carries publication_id
   - Deletes intended canonical publication
   - Rejects unsupported retweet deletion
   - Handles multiple publications for same Discord message

6. Write and run a throwaway smoke-check script that verifies:
   - SocialPublishRequest can be constructed with all required fields
   - SocialPublishService.publish_now() and .enqueue() paths work with mocked provider
   - Delete flow correctly branches on delete_supported
   Then delete the script.

7. Run all tests: `python -m pytest tests/ -v`

**Manual smoke tests** (documented for operator, not automated):
- Immediate post, scheduled post, scheduled reply, scheduled retweet
- Failed publish visibility in admin query
- First-share delete path on delete-supported primary X publication
  Depends on: T6
  Executor notes: Extended the tests to cover the returned routing gap directly: route resolution persistence on immediate and queued publications, clean rejection when sharing is disabled or no route is configured, and admin route override propagation into scheduled requests. The repro script passed, the focused test modules passed, and the full suite now passes at 18/18.
  Files changed:
    - tests/test_social_publish_service.py
    - tests/test_caller_paths.py
  Reviewer verdict: Pass. Focused tests exist for DB helpers, service behavior, caller paths, scheduler lifecycle, and publication-id-based deletion; `python -m pytest tests/ -v` passed 18/18.
  Evidence files:
    - tests/test_social_publications.py
    - tests/test_social_publish_service.py
    - tests/test_caller_paths.py
    - tests/test_scheduler.py
    - tests/test_notification_delete.py

## Watch Items

- [DEBT] db-access-pattern: Use fluent Supabase client for all CRUD on social_publications. RPC ONLY for claim_due_social_publications. Do NOT introduce raw SQL via execute_query. Settled decision.
- [DEBT] supabase-rpc-prerequisite: The claim_due_social_publications SQL function must be deployed to Supabase dashboard BEFORE the scheduler can run. This is a first-time RPC usage in the codebase. Check sql/social_publications.sql for the function definition and deploy it manually.
- [DEBT] route-management: social_channel_routes table ships empty. V1 uses guild-default routing only. Channel-specific routes require manual DB inserts. No admin CRUD commands for routes in this release.
- [BEHAVIORAL] admin-reply-text-only: execute_share_to_social must preserve text_only=True default when reply_to_tweet is supplied. Regression here means replies reattach parent media.
- [BEHAVIORAL] reaction-bridge-no-duplicate-suppression: The reaction_bridge source_kind must NOT inherit admin-chat duplicate suppression. Duplicate policy must be caller-aware.
- [BEHAVIORAL] first-share-notification: send_post_share_notification must receive publication_id from the service result. Delete button resolves one canonical record, not inferred from discord_message_id + tweet_id.
- [BEHAVIORAL] retweet-delete-guard: Retweet publications must be marked delete_supported=False. Delete flow must return clear unsupported message instead of calling tweet-delete API.
- [INTEGRATION] main.py-load-path: SharingCog is loaded via direct instantiation in main.py (SharingCog(bot, bot.db_handler) + add_cog), NOT via extension setup(). Worker lifecycle must use cog_load/cog_unload hooks that fire on this path.
- [INTEGRATION] admin-query-whitelist: Add social_publications and social_channel_routes to QUERYABLE_TABLES in tools.py (~line 66, ~line 1637) and update agent.py system prompt table list (~line 38). Missing this makes new tables invisible to admin agent.
- [SCOPE] shared_posts-compatibility: Do NOT write one legacy shared_posts row per post|reply|retweet outcome. Only write legacy row for primary non-reply posts where existing callers depend on it. Drive duplicate/delete decisions from social_publications.
- [SCOPE] no-natural-language-time-parsing: Scheduled times are ISO-8601/UTC only. No natural-language parsing unless the repo already has a parser.

## Sense Checks

- **SC1** (T1): Does the SQL artifact include both the social_publications table DDL and the claim_due_social_publications Postgres function with FOR UPDATE SKIP LOCKED? Are all DatabaseHandler methods using fluent client (not raw SQL) except the single RPC claim method?
  Executor note: Revalidated unchanged canonical storage behavior through the passing suite; the route fix did not touch the SQL artifact or the fluent-client/RPC split for social_publications.
  Verdict: Confirmed. SQL includes the canonical tables plus `FOR UPDATE SKIP LOCKED` claim function, and `DatabaseHandler` keeps fluent CRUD with RPC-only claiming.

- **SC2** (T2): Does SocialPublishResult include publication_id as a first-class field? Do the per-request policy fields (duplicate_policy, text_only, announce_policy, first_share_notification_policy, legacy_shared_post_policy) exist so caller-specific behavior doesn't leak into provider code?
  Executor note: Revalidated that publication_id and the caller-policy fields remain first-class on the normalized request/result models; the route fix did not change those contracts.
  Verdict: Confirmed. `SocialPublishResult` exposes `publication_id`, and the request model includes the caller-policy fields called out in the plan.

- **SC3** (T3): Does the X provider explicitly set delete_supported=False for retweet actions? Does duplicate detection use canonical DB lookups (not in-memory _successfully_shared set) and is it both action-aware and caller-aware?
  Executor note: Revalidated that retweets remain delete_supported=False and duplicate checks still stay canonical, action-aware, and caller-aware; the route fix did not regress that path.
  Verdict: Confirmed. Retweets are returned with `delete_supported=False`, and duplicate lookup is canonical, action-aware, and caller-aware.

- **SC4** (T4): Does the admin tool still default text_only=True for replies? Does the reaction bridge only replace the publish call while keeping ALL moderation, consent, DM, and admin alert logic intact? Does notify_user now accept and store publication_id?
  Executor note: Revalidated that admin replies still default text_only=True, the reaction bridge still only swaps the publish step, and notification flows still carry publication_id after the routing changes.
  Verdict: Confirmed. Admin replies still default to `text_only=True`, the reaction bridge preserves its side effects, and notification flows carry `publication_id`.

- **SC5** (T5): Does the scheduled worker start/stop from cog_load/cog_unload (not just setup())? Does it use discord.ext.tasks.loop with wait_until_ready gating? Does main.py wire the SocialPublishService instance so it's accessible to both the cog worker and admin tools?
  Executor note: Confirmed the scheduler lifecycle and shared service wiring remain in place, and the live publish/enqueue path now also resolves routes through server_config before canonical publication creation.
  Verdict: Confirmed. Scheduler lifecycle is tied to `cog_load`/`cog_unload`, and `main.py` wires a shared service instance onto the direct cog-add path.

- **SC6** (T6): Are social_publications and social_channel_routes added to QUERYABLE_TABLES at both locations in tools.py AND to the agent.py system prompt? Does the delete button load by publication_id and correctly reject retweet deletion with a clear message?
  Executor note: Revalidated that social_publications and social_channel_routes remain exposed to the admin query surface and that publication_id-based delete still rejects retweet deletion with the clear unsupported message.
  Verdict: Confirmed. Admin query surfaces include `social_publications` and `social_channel_routes`, and delete flow resolves by `publication_id` with clear retweet rejection.

- **SC7** (T7): Do tests cover: (1) atomic claim semantics, (2) caller-specific defaults (admin text_only, reaction no-dedupe), (3) scheduler lifecycle via cog_load path, (4) publication_id-based delete with retweet rejection, (5) multiple publications per Discord message? Do all tests pass?
  Executor note: Expanded test coverage for the routing rework and reran the full suite successfully; tests now cover route persistence, disabled/unrouted failure handling, caller defaults, scheduler lifecycle, publication_id-based delete, and multiple-publication cases with 18/18 passing.
  Verdict: Confirmed. The test suite covers the required scheduler, caller-default, publication-id delete, multiple-publication, and route-integration cases, and it currently passes.

## Meta

**Execution guidance:**

This is a large, multi-phase feature. The dependency chain is strictly linear (T1→T2→T3→T4→T5→T6→T7) because each layer builds on the previous. Do not skip ahead.

**Key gotchas:**

1. **RPC is new to this codebase.** The `claim_due_social_publications` Postgres function must be deployed to Supabase before the scheduler works. The SQL file is a checked-in artifact but deploying it is manual. Flag this to the operator after T1.

2. **main.py loads SharingCog directly** — not via `bot.load_extension()`. This means `setup()` is NOT the active path. The worker loop MUST attach to `cog_load`/`cog_unload` lifecycle hooks. Verify by reading main.py lines ~137-143.

3. **Three distinct caller paths must be preserved:** admin chat (text_only=True for replies, duplicate suppression), reaction bridge (moderation + consent + DMs, NO duplicate suppression), and summary-triggered (opt-out check). Each builds its own SocialPublishRequest — do NOT force them through a single parameter surface.

4. **shared_posts compatibility is narrow:** Only write a legacy row for primary non-reply posts. Do NOT write one row per post|reply|retweet. The canonical source of truth is social_publications.

5. **The admin query whitelist has TWO locations** in tools.py (~line 66 definition, ~line 1637 usage) plus the agent.py system prompt (~line 38). Missing any one makes the new tables invisible.

6. **No existing test infrastructure.** You'll need to create the tests/ directory and likely add pytest to requirements. Keep tests focused — mock Supabase and Discord, test the service logic and caller wiring.

7. **Route management is out of scope for v1.** Create the social_channel_routes table but don't build admin CRUD. Guild-default routing only.
