# Implementation Plan: moderation_decisions table + reaction-removal instrumentation (revised)

## Overview

Goal: every time a reaction disappears from a Discord message, write a row to a new Supabase table `moderation_decisions` with full context (reactor, emoji, message/channel/guild IDs, message author, message content snippet, timestamp) and a classification of who removed it and why.

Repository shape:
- `src/features/reacting/reactor_cog.py` owns raw reaction add/remove listeners. The restricted-emoji auto-remove lives at lines 342–371.
- `src/features/curating/curator.py` calls `message.remove_reaction('❌', user)` at lines 296, 339, 460, 472 during rejection flows — these are bot-initiated and must also be marked.
- `src/features/logging/logger_cog.py` `_update_reaction` (55–85) writes to `discord_reactions` (current state) and `discord_reaction_log` (pure history). `on_raw_message_delete` at 140–158 soft-deletes the message row without clearing content or reactor data.
- `src/common/db_handler.py` exposes Supabase write helpers (`add_reaction`, `remove_reaction`, `log_reaction_event` at 626–697), each gated by `_gate_check(guild_id)`. `discord_reactions` retains `removed_at IS NULL` rows per active (message, user, emoji) — good for cascade enumeration.
- Admin-DM pattern: `src/common/base_bot.py:176-183` (ADMIN_USER_ID → fetch_user → create_dm → send).
- discord.py 2.6.3 `RawReactionClearEvent` carries only message/channel/guild IDs (no actor); `RawReactionClearEmojiEvent` adds `emoji` only. No bot code calls `clear_reactions()` / `clear_reaction()` anywhere in `src/`, so clear events are always moderator-initiated in practice.
- Migrations are staged under `.migrations_staging/` for manual hand-off into the workspace-level `supabase/migrations/` repo.

Key design decisions:
- New table (not a view). `discord_reaction_log` lacks classification/reason/message author/content snippet.
- Classification tagged at source. For any bot-initiated single-reaction removal (restricted-emoji branch **and** curator `❌` cleanup at 4 sites), the caller registers an in-memory marker on ReactorCog before invoking `message.remove_reaction`. The subsequent `on_raw_reaction_remove` consumes the marker and uses its stored classification/reason.
- `on_raw_reaction_remove` without a marker → `user_self_removal` (Discord gateway events do not identify a moderator actor for single-reaction removals). Suspicion is computed here only.
- `on_raw_reaction_clear` / `on_raw_reaction_clear_emoji` → classified as `moderator_cleared_all` / `moderator_cleared_emoji`. The clear-event payloads have no actor, but the repo has no bot clear-call paths and the Discord permission model requires Manage Messages, so moderator attribution is accurate (not bot). Enumerate active reactors via `discord_reactions` and write one row per cleared reaction.
- `on_raw_message_delete` cascade → classified as `message_deleted_cascade`. Enumerate active reactors via `discord_reactions` and write one row per reactor/emoji; snapshot message author/content from `discord_messages` before/independent of LoggerCog's soft-delete (which preserves content fields).
- No backfill (forward-only). No retention policy added.

Constraints: no feature flags, no new config surfaces, ADMIN_USER_ID reused as-is, migration file staged (not auto-applied).

## Main Phase

### Step 1: Stage the `moderation_decisions` migration (`.migrations_staging/20260419110000_create_moderation_decisions.sql`)
**Scope:** Small
1. **Create** a timestamp-prefixed file with columns:
   - `id bigserial primary key`
   - `message_id bigint not null`, `channel_id bigint`, `guild_id bigint not null`
   - `reactor_user_id bigint`, `reactor_name text` (nullable; cascade/clear paths may lack name, reactor_user_id may be null for clear-all when no active reactors recorded — but we only write when a reactor is known)
   - `emoji text not null`
   - `message_author_id bigint`, `message_author_name text`
   - `message_content_snippet text`
   - `classification text not null check (classification in ('bot_auto_restricted','bot_curator_reject','user_self_removal','moderator_cleared_all','moderator_cleared_emoji','message_deleted_cascade'))`
   - `reason text`
   - `is_suspicious boolean not null default false`
   - `removed_at timestamptz not null default now()`
   - Indexes: `(guild_id, removed_at desc)`, `(message_id)`, `(reactor_user_id, removed_at desc)` for per-user audits.
2. **Update** `.migrations_staging/README.md` with the new file entry and destination path.

### Step 2: Add DB helpers to `DatabaseHandler` (`src/common/db_handler.py`)
**Scope:** Small
1. **Add** `record_moderation_decision(*, message_id, channel_id, guild_id, reactor_user_id, reactor_name, emoji, message_author_id, message_author_name, message_content_snippet, classification, reason=None, is_suspicious=False) -> bool` near `log_reaction_event` (~line 675). Gate with `_gate_check(guild_id)`. Insert into `moderation_decisions` using the same try/except pattern at lines 685–697.
2. **Add** `get_active_reactors(message_id: int, emoji: Optional[str] = None) -> list[dict]` that returns `[{'user_id', 'emoji', 'guild_id'}]` from `discord_reactions` where `removed_at IS NULL` (optionally filtered by emoji). Used by clear-event and cascade paths.
3. **Add** `get_message_snapshot(message_id: int) -> Optional[dict]` returning `{'author_id', 'author_name', 'content', 'channel_id', 'guild_id'}` from `discord_messages`. Used by cascade path when the Discord `message` object is unavailable (already deleted).

### Step 3: ReactorCog: marker infrastructure and restricted-emoji branch (`src/features/reacting/reactor_cog.py`)
**Scope:** Medium
1. **Add** in `__init__` (after line 31):
   ```python
   self._bot_initiated_removals = {}  # (message_id, user_id, emoji_str) -> (classification, reason, monotonic_ts)
   ```
2. **Add** a public method on the cog:
   ```python
   def register_bot_removal(self, message_id, user_id, emoji_str, classification, reason=None):
       self._bot_initiated_removals[(message_id, user_id, emoji_str)] = (classification, reason, time.monotonic())
       self._prune_bot_removals()
   def _prune_bot_removals(self, ttl_seconds=60):
       now = time.monotonic()
       self._bot_initiated_removals = {k: v for k, v in self._bot_initiated_removals.items() if now - v[2] < ttl_seconds}
   ```
3. **Refactor** `_is_restricted_emoji` at lines 70–87 into `_classify_restricted_emoji(emoji_str) -> Optional[str]` returning `'flag' | 'political' | 'religious'` or `None`. Replace the single existing boolean caller (line 342) with `category = self._classify_restricted_emoji(emoji_str); if category is not None:`.
4. **Modify** the restricted-emoji branch at lines 340–371:
   - Before `await message.remove_reaction(emoji, user)`, call `self.register_bot_removal(message.id, user.id, emoji_str, 'bot_auto_restricted', category)`.
   - After successful removal, call `self.bot.db_handler.record_moderation_decision(...)` with `classification='bot_auto_restricted'`, `reason=category`, `is_suspicious=False`, reactor/author/content fields populated from the live `user` and `message` objects, `message_content_snippet=(message.content or '')[:200]`.

### Step 4: ReactorCog: `on_raw_reaction_remove` classification (`src/features/reacting/reactor_cog.py`)
**Scope:** Medium
1. **Modify** `on_raw_reaction_remove` at lines 410–504, after `user/channel/message/emoji_str` are resolved:
   - Consult `self._bot_initiated_removals`: if key `(message.id, user.id, emoji_str)` is present, pop it — **do not** write a second decision (the registering call site already wrote one).
   - Otherwise classify as `'user_self_removal'`, compute `is_suspicious = emoji_str in {'🤮', '👎', '😭'}`, call `record_moderation_decision(reason=None, is_suspicious=is_suspicious, ...)`.
   - When `is_suspicious`, fire-and-forget DM the admin using the `src/common/base_bot.py:176-183` pattern (inline, ≤10 lines, no new config). DM body: reactor mention, emoji, jump link (`message.jump_url`), 200-char snippet.

### Step 5: ReactorCog: clear and message-delete cascade listeners (`src/features/reacting/reactor_cog.py`)
**Scope:** Medium
1. **Add** `@commands.Cog.listener() async def on_raw_reaction_clear(self, payload):` — use `db_handler.get_active_reactors(payload.message_id)` to enumerate, fetch a message snapshot via `db_handler.get_message_snapshot(payload.message_id)` (fallback: skip fields that are unavailable), write one row per reactor with `classification='moderator_cleared_all'`, `reactor_user_id=row.user_id`, `reason=None`, `is_suspicious=False`.
2. **Add** `@commands.Cog.listener() async def on_raw_reaction_clear_emoji(self, payload):` — same pattern but pass `emoji=str(payload.emoji)` to `get_active_reactors` and use `classification='moderator_cleared_emoji'`.
3. **Add** `@commands.Cog.listener() async def on_raw_message_delete(self, payload):` — enumerate active reactors for `payload.message_id`, read message snapshot, write one row per reactor with `classification='message_deleted_cascade'`, `reason=None`, `is_suspicious=False`. Note: LoggerCog also listens for `on_raw_message_delete` and soft-deletes the message row; both listeners run independently. Soft-delete preserves `content`/`author_*` columns, so ordering is not critical, but to reduce races we read the snapshot synchronously at the top of this handler.
4. **Factor** the channel/message fetch block (currently duplicated in `on_raw_reaction_add` 284–316 and `on_raw_reaction_remove` 448–483) into a `_fetch_channel_and_message(payload)` helper only if the new listeners need it; the clear/cascade paths use DB snapshots, so no Discord fetch is required.

### Step 6: Mark curator bot-removal call sites (`src/features/curating/curator.py`)
**Scope:** Small
1. **At each of the 4 sites** (`curator.py:296, 339, 460, 472`) immediately before `await message.remove_reaction('❌', user)`, insert:
   ```python
   reactor_cog = self.bot.get_cog('ReactorCog')
   if reactor_cog:
       reactor_cog.register_bot_removal(message.id, user.id, '❌', 'bot_curator_reject', reason='curator_reject')
   ```
   (If the file already has a shared `self.bot` reference, reuse it; otherwise thread `bot` through the helper as-is — no refactor beyond the 4 insertions.)
2. **Do not** write the moderation_decision row directly from curator. The `on_raw_reaction_remove` consumer reads the marker and writes a single row with classification `'bot_curator_reject'`. This keeps write logic in one place.

   → Adjust Step 4 accordingly: when the marker is present, write the row using the marker's stored `(classification, reason)` instead of skipping. (The restricted-emoji branch in Step 3 is the only site that writes eagerly, because that branch has full `user`/`message` context in hand and may short-circuit downstream events; curator leaves the write to the consumer.)

### Step 7: Tests (`tests/test_moderation_decisions.py`)
**Scope:** Medium
1. **Create** a new pytest module using `conftest.load_module_from_repo` to load the cog and db handler with a mocked Supabase client. Monkeypatch `db_handler.record_moderation_decision`, `get_active_reactors`, `get_message_snapshot`, and `bot.fetch_user`.
2. **Cover**:
   - Restricted-emoji add (flag/political/religious) → one `bot_auto_restricted` decision with correct reason; marker registered; subsequent `on_raw_reaction_remove` is a no-op.
   - Curator `❌` removal path (simulated): marker registered as `bot_curator_reject` with `reason='curator_reject'`; `on_raw_reaction_remove` writes exactly one decision with that classification/reason; no user_self_removal row.
   - Plain non-restricted self-removal → `user_self_removal`, `is_suspicious=False`, no admin DM.
   - Suspicious self-removal (🤮, 👎, 😭) → `user_self_removal`, `is_suspicious=True`, admin DM attempted (`fetch_user` + `create_dm` + `send` called with message jump link).
   - `on_raw_reaction_clear` with 3 active reactors → 3 `moderator_cleared_all` rows.
   - `on_raw_reaction_clear_emoji` with 2 active reactors for the target emoji and 1 for another emoji → exactly 2 `moderator_cleared_emoji` rows for the target emoji.
   - `on_raw_message_delete` with 2 active reactors → 2 `message_deleted_cascade` rows, author/content populated from `get_message_snapshot`.
   - `_classify_restricted_emoji`: flag codepoints → `'flag'`, 🍉/🔻 → `'political'`, ✝/☪/🕉 → `'religious'`, 👍 → `None`.
   - Marker TTL: entries older than 60s are pruned.

### Step 8: Validate
**Scope:** Small
1. **Run** `pytest tests/test_moderation_decisions.py -q`.
2. **Run** `pytest -q` to catch regressions in reactor/logger/curator tests.
3. **Document** in the PR body: migration must be copied from `.migrations_staging/` to `/Users/peteromalley/Documents/banodoco-workspace/supabase/migrations/` before deploy; until then, inserts will error and be swallowed by try/except.

## Execution Order

1. Migration SQL (Step 1).
2. DB helpers (Step 2) — pure additions.
3. ReactorCog marker infra + restricted-emoji (Step 3).
4. ReactorCog `on_raw_reaction_remove` classifier (Step 4).
5. ReactorCog clear + cascade listeners (Step 5).
6. Curator call-site markers (Step 6) — depends on Step 3 (needs `register_bot_removal`).
7. Tests (Step 7).
8. Validate (Step 8).

## Validation Order

1. Targeted unit test module (`pytest tests/test_moderation_decisions.py`).
2. Full suite (`pytest -q`).
3. Manual smoke in dev: flag-emoji reaction → `bot_auto_restricted` row; admin rejection flow → `bot_curator_reject` row; 🤮 self-retract → admin DM + `user_self_removal` with `is_suspicious=true`; moderator right-click-clears all reactions → N `moderator_cleared_all` rows; message deletion with reactions → N `message_deleted_cascade` rows.
