# Implementation Plan: moderation_decisions table + reaction-removal instrumentation

## Overview

Goal: every time a reaction disappears from a Discord message, write a row to a new Supabase table `moderation_decisions` with full context (reactor, emoji, message/channel/guild IDs, message author, message content snippet, timestamp) and a classification of who removed it and why.

Repository shape:
- `src/features/reacting/reactor_cog.py` owns raw reaction add/remove listeners. The restricted-emoji auto-remove lives at lines 342–371. `on_raw_reaction_remove` at 410–504 already delegates logging to LoggerCog.
- `src/features/logging/logger_cog.py` `_update_reaction` (55–85) writes to `discord_reactions` (current state) and `discord_reaction_log` (pure history of add/remove events). Neither table carries "who removed" or "why".
- `src/common/db_handler.py` exposes Supabase write helpers (`add_reaction`, `remove_reaction`, `log_reaction_event` at 626–697), each gated by `_gate_check(guild_id)`.
- Admin-DM pattern: `src/common/base_bot.py:176-183` uses `int(os.getenv("ADMIN_USER_ID"))` → `fetch_user` → `create_dm` → `dm.send(...)`.
- Migrations are staged under `.migrations_staging/` for manual hand-off into the workspace-level `supabase/migrations/` repo (see `.migrations_staging/README.md`).

Key design decisions:
- New table (not a view). `discord_reaction_log` lacks classification, reason, message author, content snippet. A view can't synthesize those.
- Classification is tagged at the write site, not inferred later. Bot-initiated removals (restricted-emoji branch, future clear calls) register the (message_id, user_id, emoji) tuple in a short-lived in-memory set *before* calling `message.remove_reaction`; the subsequent `on_raw_reaction_remove` reads and consumes that marker.
- `on_raw_reaction_remove` without a bot marker → `user_self_removal`. Discord gateway events do not identify a moderator actor for single-reaction removals, so we do not invent a `moderator_removal` classification for that path. We do cover clear-all (`bot_clear_all`) and clear-emoji (`bot_clear_emoji`) via their dedicated listeners, which are always bot/mod-initiated.
- No backfill (forward-only). `discord_reaction_log` can optionally derive coarse history later, but that's out of scope.

Constraints: no feature flags, no new config surfaces, ADMIN_USER_ID reused as-is, migration file staged (not auto-applied).

## Main Phase

### Step 1: Stage the `moderation_decisions` migration (`.migrations_staging/20260419110000_create_moderation_decisions.sql`)
**Scope:** Small
1. **Create** a new staged migration mirroring the existing file naming (`YYYYMMDDhhmmss_*.sql`) with columns:
   - `id bigserial primary key`
   - `message_id bigint not null`, `channel_id bigint`, `guild_id bigint not null`
   - `reactor_user_id bigint not null`, `reactor_name text`
   - `emoji text not null`
   - `message_author_id bigint`, `message_author_name text`
   - `message_content_snippet text` (truncated to 200 chars at write site)
   - `classification text not null check (classification in ('bot_auto_restricted','user_self_removal','bot_clear_all','bot_clear_emoji'))`
   - `reason text` (e.g. `flag`, `political`, `religious` for `bot_auto_restricted`; null otherwise)
   - `is_suspicious boolean not null default false`
   - `removed_at timestamptz not null default now()`
   - Index on `(guild_id, removed_at desc)` and `(message_id)`.
2. **Update** `.migrations_staging/README.md` with an entry for the new file and destination path.

### Step 2: Add `record_moderation_decision` to `DatabaseHandler` (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** a single method next to `log_reaction_event` (around `src/common/db_handler.py:675`):
   ```python
   def record_moderation_decision(self, *, message_id, channel_id, guild_id,
                                  reactor_user_id, reactor_name, emoji,
                                  message_author_id, message_author_name,
                                  message_content_snippet, classification,
                                  reason=None, is_suspicious=False) -> bool
   ```
   Gate via `_gate_check(guild_id)`, use `self.storage_handler.supabase_client.table('moderation_decisions').insert(...)` following the exact pattern at `src/common/db_handler.py:685-697`.
2. **Truncate** `message_content_snippet` to 200 chars at the call site (not here) — this helper just forwards values.

### Step 3: Instrument the restricted-emoji branch and single-reaction removals (`src/features/reacting/reactor_cog.py`)
**Scope:** Medium
1. **Add** an in-memory pending-removals set on the cog in `__init__` (after line 31):
   ```python
   self._bot_initiated_removals = {}  # key: (message_id, user_id, emoji_str) -> (classification, reason, ts)
   ```
   Prune entries older than ~60s opportunistically on each add/remove to bound memory.
2. **Introduce** a small classifier for restricted emoji that returns the category string (`'flag' | 'political' | 'religious'`). Refactor `_is_restricted_emoji` at `src/features/reacting/reactor_cog.py:70-87` into `_classify_restricted_emoji(emoji_str) -> Optional[str]`; the boolean callers become `_classify_restricted_emoji(...) is not None`.
3. **Modify** the restricted-emoji branch at `src/features/reacting/reactor_cog.py:340-371`:
   - Before `await message.remove_reaction(emoji, user)`, register the pending-removal marker `(message.id, user.id, str(emoji)) -> ('bot_auto_restricted', category, now)`.
   - Immediately after (success path), call `self.bot.db_handler.record_moderation_decision(...)` with classification `'bot_auto_restricted'`, reason = category, `is_suspicious=False`, passing reactor name (`user.name`), message author fields (`message.author.id`, `message.author.name`), and `message.content[:200]`. This guarantees the decision is logged even if the later `on_raw_reaction_remove` fires out of order or the user isn't in cache.
4. **Modify** `on_raw_reaction_remove` at `src/features/reacting/reactor_cog.py:410-504`:
   - After resolving `user`, `channel`, `message`, `emoji_str`, consult `self._bot_initiated_removals`: if the key is present, pop it and **skip** writing a second decision (already recorded in the restricted branch).
   - Otherwise classify as `'user_self_removal'`. Compute `is_suspicious = (emoji_str in {'🤮','👎','😭'})`. Call `record_moderation_decision` with `reason=None`.
   - When `is_suspicious`, fire-and-forget DM the admin using the `src/common/base_bot.py:176-183` pattern inline (small async helper at module level or on the cog). Keep it under 10 lines; do not add a new config surface.
5. **Add** two new listeners at the bottom of the cog:
   ```python
   @commands.Cog.listener()
   async def on_raw_reaction_clear(self, payload): ...
   @commands.Cog.listener()
   async def on_raw_reaction_clear_emoji(self, payload): ...
   ```
   Fetch channel + message (reuse the fetch block from `on_raw_reaction_remove`; factor a `_fetch_message(payload)` helper if duplication gets ugly), then write a single `moderation_decisions` row per event with classification `'bot_clear_all'` / `'bot_clear_emoji'` and `reactor_user_id = 0` (no per-reactor info available in clear events), `emoji` = `'*'` for clear-all or `str(payload.emoji)` for clear-emoji.

### Step 4: Tests (`tests/test_moderation_decisions.py`)
**Scope:** Medium
1. **Create** a new pytest module using `conftest.load_module_from_repo` to load `src/features/reacting/reactor_cog.py` and the db handler with a mocked Supabase client (pattern used by existing tests — look for similar fixtures in `tests/test_social_publications.py`).
2. **Cover**:
   - Restricted-emoji add → `record_moderation_decision` called once with `classification='bot_auto_restricted'` and reason in `{'flag','political','religious'}`; pending-removal marker registered; subsequent `on_raw_reaction_remove` for the same tuple is a no-op for moderation_decisions.
   - Non-restricted reaction add → no moderation_decision; `on_raw_reaction_remove` writes `classification='user_self_removal'`, `is_suspicious=False`.
   - Suspicious emoji self-removal (🤮) → decision with `is_suspicious=True` and admin DM attempted.
   - `on_raw_reaction_clear` → one `bot_clear_all` decision; `on_raw_reaction_clear_emoji` → one `bot_clear_emoji` decision with the emoji populated.
   - Classifier helper: flags, political, religious codepoints each map to the right category.
3. **Use** dependency injection or monkeypatching on `self.bot.db_handler.record_moderation_decision` and `self.bot.fetch_user` so tests don't hit Supabase or Discord.

### Step 5: Validate and document
**Scope:** Small
1. **Run** the new test module: `pytest tests/test_moderation_decisions.py -q`.
2. **Run** the whole suite to catch regressions in reactor/logger paths: `pytest -q`.
3. **Note** in the PR description that the migration file must be copied from `.migrations_staging/` to `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` before the code is deployed, and that the handler will silently drop writes (via `_gate_check`) if the table doesn't exist yet — i.e., deploy order is migration-first.

## Execution Order

1. Migration SQL (Step 1) — reviewable artifact, no runtime impact.
2. `record_moderation_decision` helper (Step 2) — pure addition, safe.
3. Reactor cog instrumentation (Step 3) — the behavior change.
4. Tests (Step 4) — lock in the behavior.
5. Validate (Step 5).

## Validation Order

1. Targeted unit test module first (`pytest tests/test_moderation_decisions.py`).
2. Full suite second (`pytest -q`).
3. Manual smoke in dev: react with a flag emoji → row written with `bot_auto_restricted`; react + unreact with 🤮 → admin DM; react + unreact with 👍 → row written, no DM.
