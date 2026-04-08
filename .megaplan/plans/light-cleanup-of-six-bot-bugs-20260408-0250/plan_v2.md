# Implementation Plan: Light Cleanup of Six Bot Bugs

## Overview
The user has provided the full bug list and answers in the notes — replacing the earlier scaffold with a concrete plan. All six bugs are localized small fixes in the Discord bot (`brain-of-bndc`). Land them as **one bundled commit**, leave the unrelated working-tree changes (`.megaplan/schemas/*.json`, `src/features/summarising/summariser.py`, new `scripts/`) untouched.

Confirmed file evidence after reading the code:
- Bug 1 (DM context): `src/features/admin_chat/admin_chat_cog.py:246-287` — only the `else:` (guild) branch builds `replied_to`/`recent_messages`; the `if is_dm:` branch only sets source/guild metadata.
- Bug 2 (5xx crash): `admin_chat_cog.py:333` and `:340` are the bare `channel.send` calls; `:348-350` is the catch-all that echoes raw `str(e)` to chat.
- Bug 3 (dead Zapier): `social_poster.py` defines `post_to_instagram_via_zapier`/`post_to_tiktok_via_zapier`/`post_to_youtube_via_zapier` and imports requests/the env vars (lines 26-39, 521-595). `sharer.py:23-25` imports them, `sharer.py:404-434` calls all three. `tools.py:181` and `tools.py:1044` advertise IG/TikTok/YouTube in the tool description and success message. `agent.py:46` tells the model the same.
- Bug 4 (ForumChannel.fetch_message): `tools.py:1018-1022` does `bot.get_channel(channel_id)` then `channel.fetch_message(...)` with no thread resolution.
- Bug 5 (bad column hints): `agent.py:38` mentions `discord_messages` and `shared_posts` as queryable. `tools.py:406` shows the model `author_name` as an example filter and `tools.py:410` shows `-created_at` as an order example. Real schemas: `storage_handler.py:112-130` shows `discord_messages` has `author_id`/`created_at` (no `author_name`); `db_handler.py:885-893` shows `shared_posts` has `shared_at` (no `created_at`).
- Bug 6 (deque race): `storage_handler.py:69-150` — `store_messages_to_supabase(messages)` iterates `messages` in the transform loop (line 89) and again in the batch slicing loop (line 139). If the caller passes a live deque (collected concurrently), iteration can raise `RuntimeError: deque mutated during iteration`. Fix at the function entry by snapshotting to a list.

Constraints:
- Light robustness, minimal direct fixes, no refactors or defensive wrappers beyond what each bug requires.
- One bundled commit. No new files. No new dependencies.

## Main Phase

### Step 1: Fix DM context — mirror reply/history into the DM branch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Edit** the `if is_dm:` block at `admin_chat_cog.py:247-253` to also build `replied_to` (from `message.reference.resolved`) and `recent_messages` (from `message.channel.history(limit=10)`), exactly mirroring the `else:` branch at lines 268-287. `discord.DMChannel` supports `.history()` the same way.
2. **Surface the anchor more loudly** to the model: include a short explicit marker in the channel_context (e.g. set `channel_context["replied_to_anchor_note"] = "USER IS REPLYING TO THIS MESSAGE — treat it as the primary referent."` whenever `replied_to` is set, in both branches).
3. **Refactor minimally** by extracting the reply+history block into a small local helper inside `on_message` (or a private method on the cog) so DM and guild branches share one path — only if the duplication is awkward; otherwise duplicate inline. Keep the change tight.

### Step 2: Fix 5xx send crash — retry + neutral error copy (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Wrap** each `await message.channel.send(...)` at `admin_chat_cog.py:333` and `:340` in a tiny inline retry: on `discord.HTTPException` with `status >= 500`, sleep `0.5s` then retry, then `1.5s` then retry; after 2 retries re-raise. Keep it inline (no new helper unless both call sites are awkward — if so, define a single local `async def _send_with_retry(channel, content, reference)` inside `on_message`).
2. **Replace** the catch-all at `admin_chat_cog.py:348-350` so it logs with `exc_info=True` (already does) but sends a **neutral** user-facing message — e.g. `"Sorry, something went wrong on my side. Try again in a moment."` — no `str(e)` interpolation. The send of the neutral message itself should be wrapped in a `try/except` so a follow-up send failure cannot recurse into the same handler.

### Step 3: Remove dead Zapier IG/TikTok/YouTube destinations (`src/features/sharing/subfeatures/social_poster.py`, `src/features/sharing/sharer.py`, `src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Medium
1. **Delete** the three Zapier functions in `social_poster.py:521-595` (`post_to_instagram_via_zapier`, `post_to_tiktok_via_zapier`, `post_to_youtube_via_zapier`).
2. **Delete** the Zapier env-var reads and warnings in `social_poster.py:26-39` (`ZAPIER_TIKTOK_BUFFER_URL`, `ZAPIER_INSTAGRAM_URL`, `ZAPIER_YOUTUBE_URL` and the three `if not …: logger.warning(…)` lines). Remove the `import requests` line at `social_poster.py:7` only if grep confirms it has no remaining users in this file (Twitter path uses tweepy, not requests).
3. **Delete** the imports and call sites in `sharer.py`:
   - Remove `post_to_instagram_via_zapier`, `post_to_tiktok_via_zapier`, `post_to_youtube_via_zapier` from the import block at `sharer.py:23-25`.
   - Remove the three call blocks at `sharer.py:404-412` (Instagram), `:414-422` (TikTok), `:424-434` (YouTube).
4. **Update** the operator-facing strings in `admin_chat`:
   - `tools.py:181` — change description from `"(Twitter, Instagram, TikTok, YouTube)"` to `"(Twitter)"`.
   - `tools.py:1044` — change `"Will post to Twitter/Instagram/TikTok/YouTube."` to `"Will post to Twitter."`.
   - `agent.py:46` — change `"share to Twitter/Instagram/TikTok/YouTube"` to `"share to Twitter"`.
5. **Grep** the repo for any remaining occurrences of `ZAPIER_INSTAGRAM_URL`, `ZAPIER_TIKTOK_BUFFER_URL`, `ZAPIER_YOUTUBE_URL`, `post_to_instagram_via_zapier`, `post_to_tiktok_via_zapier`, `post_to_youtube_via_zapier`, and the literal substrings `Instagram`, `TikTok`, `YouTube` in the sharing/admin_chat surface area to confirm nothing dead remains. (Twitter stays.)

### Step 4: Fix `share_to_social` ForumChannel resolution (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Edit** `tools.py:1017-1022`. After `channel = bot.get_channel(channel_id)`, if `isinstance(channel, discord.ForumChannel)` (or more generally if `channel` lacks `fetch_message`), resolve to the thread before calling `fetch_message`:
   ```python
   channel = bot.get_channel(channel_id)
   if channel is None:
       return {"success": False, "error": f"Could not find channel {channel_id}"}
   if isinstance(channel, discord.ForumChannel) or not hasattr(channel, 'fetch_message'):
       # ForumChannel posts ARE threads — resolve via guild/bot
       thread = None
       guild = channel.guild if hasattr(channel, 'guild') else None
       if guild:
           thread = guild.get_thread(int(message_id))
       if thread is None:
           try:
               thread = await bot.fetch_channel(int(message_id))
           except Exception as e:
               return {"success": False, "error": f"Could not resolve forum thread for message {message_id}: {e}"}
       channel = thread
   message = await channel.fetch_message(int(message_id))
   ```
2. **Keep** the rest of the function (downstream `sharer.finalize_sharing` call) unchanged.

### Step 5: Fix `query_table` schema hints (`src/features/admin_chat/tools.py`, `src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Edit** `tools.py:406` to remove the bogus `author_name` example. Replace with an example using a real column, e.g. `{"reaction_count": "gte.5", "author_id": "123456789"}`.
2. **Edit** `tools.py:410` to use a real default-orderable column. `discord_messages` has `created_at`, but `shared_posts` uses `shared_at`. Change the example to one that's universally safe, e.g. `'-reaction_count'`, or note that order columns are table-specific.
3. **Add** a one-line schema-cheatsheet to `agent.py:38` (the `query_table` line) listing the actual primary timestamp/author columns per table the model is most likely to touch:
   - `discord_messages`: `author_id`, `created_at`, `reaction_count`
   - `shared_posts`: `discord_user_id`, `shared_at`, `platform`
   - (only the columns the model is realistically going to filter/order on — keep it short)
4. **Verify** by grepping `tools.py` for other places that reference `author_name`/`created_at` against `discord_messages` or `shared_posts`. The earlier grep showed `tools.py:526`, `:531`, `:928`, `:938`, `:942` use `msg.get('author_name', ...)`. **Read** those callsites — they read from result rows assembled elsewhere (likely from local DB tools that *do* return `author_name`), so they're not the bug. Confirm and leave alone.

### Step 6: Fix deque race in storage handler (`src/common/storage_handler.py`)
**Scope:** Small
1. **Edit** `store_messages_to_supabase` at `storage_handler.py:69`. Immediately after the `if not messages: return 0` guard, snapshot the input:
   ```python
   messages = list(messages)
   ```
   This freezes the iteration source so the two downstream loops (transform at line 89, batching at line 139) cannot trip on a concurrent mutation of the original deque.
2. **Do not** broaden the type hint or change the public signature.

### Step 7: Validate (`python -m py_compile`, targeted imports, behavioural reasoning checks)
**Scope:** Small
1. **Syntax check** every touched file:
   ```
   python -m py_compile src/features/admin_chat/admin_chat_cog.py src/features/admin_chat/tools.py src/features/admin_chat/agent.py src/features/sharing/sharer.py src/features/sharing/subfeatures/social_poster.py src/common/storage_handler.py
   ```
2. **Import check** each touched module from the repo root:
   ```
   python -c "import src.features.admin_chat.admin_chat_cog, src.features.admin_chat.tools, src.features.admin_chat.agent, src.features.sharing.sharer, src.features.sharing.subfeatures.social_poster, src.common.storage_handler"
   ```
3. **Grep verification** for Bug 3 cleanup completeness:
   - `rg -n "ZAPIER_(INSTAGRAM|TIKTOK|YOUTUBE)" src/`
   - `rg -n "post_to_(instagram|tiktok|youtube)_via_zapier" src/`
   - `rg -n "Instagram|TikTok|YouTube" src/features/sharing src/features/admin_chat`
   All three should return zero hits in the touched code paths (Twitter remains).
4. **Bug-1 verification (DM context)** — static reasoning required by user:
   - Re-read `admin_chat_cog.py:247-287` after the edit and confirm the DM branch produces a `channel_context` dict whose keys (`replied_to`, `recent_messages`) are populated identically to the guild branch when the equivalent inputs are present. Concretely: with a DM message that has `message.reference.resolved` set, the resulting `channel_context["replied_to"]` must be a non-None dict with `message_id`, `author`, `content`. With prior DM history present, `channel_context["recent_messages"]` must be a non-empty list of formatted strings.
   - Confirm the anchor note key is set whenever `replied_to` is set, and that it appears in both DM and guild paths.
5. **Bug-2 verification (5xx crash)** — static reasoning required by user:
   - Re-read the retry block and confirm: (a) every `channel.send` inside the response loop goes through the retry path; (b) the retry only swallows `discord.HTTPException` with `status >= 500`, all other exceptions still propagate; (c) after retries are exhausted the exception is re-raised so the cog catch-all can log it; (d) the catch-all at `:348-350` no longer interpolates `str(e)` into the user-facing message, and (e) the neutral-message send is itself protected from raising back into the handler.
6. **Bug-6 verification (deque race)** — confirm the snapshot line is the very first statement after the guards in `store_messages_to_supabase` and that no later code path re-references the original `messages` argument under a different name.
7. **Test discovery** — run `rg -l "admin_chat_cog|store_messages_to_supabase|social_poster|share_to_social|query_table|storage_handler" tests scripts 2>/dev/null` to find any existing coverage. If anything turns up, run it. (Repo currently has no `tests/` directory and only `scripts/test_social_picks.py`, which is unrelated — confirm by running it as a sanity check that nothing imports broke.)
8. **Manual smoke (info only, user-side)** — flag to the user that runtime confirmation in dev Discord is the only way to fully prove Bugs 1, 2, and 4 in production-like conditions; list the exact reproductions to run.

## Execution Order
1. Step 1 (DM context).
2. Step 2 (5xx retry + neutral copy) — same file as Step 1, do them together so the file is opened once.
3. Step 6 (deque snapshot) — one-line, isolated, do early to bank an easy win.
4. Step 3 (dead Zapier removal) — touches the most files; do as a contiguous block to keep the diff coherent.
5. Step 4 (ForumChannel resolution).
6. Step 5 (query_table schema hints).
7. Step 7 (validation).
8. Single bundled commit covering all six bugs.

## Validation Order
1. `py_compile` on all touched files (cheapest).
2. Import-check the six modules from the repo root.
3. Grep sweep for residual Zapier IG/TikTok/YouTube references.
4. Static re-read of Bugs 1, 2, 6 against the criteria above.
5. Run any existing tests touching the changed modules; otherwise run `scripts/test_social_picks.py` as a smoke that imports still resolve in the broader sharing path.
6. Hand off Bugs 1/2/4 to the user for runtime smoke in dev Discord.
