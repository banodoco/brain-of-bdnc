# Implementation Plan: Open Bot Chat to Non-Admin Members with Access Controls

## Overview
The BNDC Discord bot currently has a single admin chat feature (`src/features/admin_chat/`) that only responds to one hardcoded `ADMIN_USER_ID`. The goal is to allow approved community members to DM or @mention the bot with access to a restricted (read-only) subset of tools, while admins retain full tool access.

**Current architecture:**
- `AdminChatCog` (`admin_chat_cog.py:99-107`) gates all access behind `_is_admin()`, rejecting everyone else
- `AdminCog` (`admin_cog.py:295-316`) has a **separate DM listener** that replies with robot sounds to all non-admin DMs — this must be coordinated with any access change
- `AdminChatAgent` (`agent.py`) passes all 16 `TOOLS` to Claude on every request
- `execute_tool()` (`tools.py:1388-1436`) dispatches without any permission check
- The `members` table has no access-control flag; `guild_members` table exists for per-guild member data
- `server_config` already stores per-guild `admin_user_id` (`multi_server.sql:24`), but the cog still reads from env var

**Key insertion points:**
- Tool list selection: `agent.py:214` (tools param to Claude API call)
- Tool execution enforcement: `tools.py:1388` (dispatch function)
- Access gate: `admin_chat_cog.py:105-107` (admin-only check)
- DM conflict: `admin_cog.py:295-316` (robot sounds fallback)
- Channel visibility: `tools.py:565-567` (live fetch), `tools.py:612-616` (DB safe_channels), `tools.py:743-759` (inspect_message)

## Step 1: Add `can_message_bot` to `guild_members` table
**Scope:** Small

The repo has already moved guild-scoped member data to `guild_members` (`multi_server.sql:84-93`). Placing the flag here aligns with the existing architecture and avoids a future migration.

1. **Create migration** `supabase/migrations/YYYYMMDD_add_can_message_bot.sql`:
   ```sql
   ALTER TABLE guild_members ADD COLUMN can_message_bot BOOLEAN DEFAULT FALSE;
   ```
2. **Validate** by running the migration locally.

## Step 2: Define tool permission tiers and enforce at executor (`tools.py`)
**Scope:** Small

1. **Add constants** near `QUERYABLE_TABLES` (`tools.py:20`):
   ```python
   # Tools available to all approved users (read-only + response)
   MEMBER_TOOLS = {
       'reply', 'end_turn',
       'find_messages', 'inspect_message',
       'get_active_channels', 'get_daily_summaries',
       'get_member_info', 'get_bot_status',
       'resolve_user',
   }

   # Tools restricted to admins only
   ADMIN_ONLY_TOOLS = {
       'send_message', 'edit_message', 'delete_message',
       'upload_file', 'share_to_social', 'search_logs',
       'query_table',
   }
   ```
2. **Add helper** `get_tools_for_role(is_admin: bool) -> list` that filters the `TOOLS` list to return only schemas for permitted tools.
3. **Add enforcement** to `execute_tool()` (`tools.py:1388`): accept an `allowed_tools: set` parameter. If the tool_name is not in `allowed_tools`, return `{"success": False, "error": "Permission denied"}` immediately. This is defense-in-depth against prompt injection.

### Sensitive operations rationale
- **Write tools** (`send_message`, `edit_message`, `delete_message`, `upload_file`): Modify server state or impersonate the bot.
- **`share_to_social`**: Publishes content to external platforms.
- **`search_logs`**: Exposes internal system logs with sensitive operational data.
- **`query_table`**: Removed from member tools entirely. It exposes raw DB access over 14 tables including `members.auth_user_id`, `grant_applications`, `invite_codes`, `pending_intros`, and `timed_mutes`. The purpose-built search tools (`find_messages`, `get_member_info`, `get_active_channels`, `get_daily_summaries`) already cover the read-only use cases members need without exposing arbitrary column access.

## Step 3: Add channel-visibility enforcement for non-admin users (`tools.py`)
**Scope:** Medium

The read-only tools currently operate with bot-level permissions: `find_messages` searches all non-NSFW channels (`tools.py:612-616`), the live path fetches any channel by ID (`tools.py:565-567`), and `inspect_message` fetches any message by ID (`tools.py:743-759`). For non-admin users, these must be scoped to channels the requesting user can actually see in Discord.

1. **Add** `requester_id: Optional[int]` parameter to `execute_tool()` (`tools.py:1388`). Admins pass `None` (unrestricted). Non-admins pass their Discord user ID.
2. **Build a visible-channel set** for non-admin requests. At tool execution time, resolve via the Discord API:
   ```python
   async def _get_visible_channel_ids(bot, guild_id: int, user_id: int) -> set[int]:
       """Return channel IDs the user has ViewChannel permission for."""
       guild = bot.get_guild(guild_id)
       member = guild.get_member(user_id) or await guild.fetch_member(user_id)
       return {
           ch.id for ch in guild.channels
           if ch.permissions_for(member).view_channel
       }
   ```
   Cache this per `(guild_id, user_id)` for ~60s to avoid repeated API calls.
3. **Enforce in `find_messages`** (`tools.py:556-628`):
   - **Live path** (`tools.py:565`): After fetching the channel, check `channel.id in visible_channels`. Reject with "You don't have access to that channel" if not.
   - **DB path** (`tools.py:612-628`): Intersect `safe_channels` with `visible_channels` so the query only searches channels the user can see.
4. **Enforce in `inspect_message`** (`tools.py:743-759`): After resolving the channel from the DB record, check `channel_id in visible_channels`. Reject if not visible.
5. **Thread through:** `execute_tool()` passes `visible_channels` (or `None` for admins) to the individual tool executors that need it (`execute_find_messages`, `execute_inspect_message`).

## Step 4: Resolve the DM handler conflict (`admin_cog.py`, `admin_chat_cog.py`)
**Scope:** Small

`AdminCog.on_message` (`admin_cog.py:295-316`) currently catches all non-admin DMs and replies with robot sounds. It already has a carve-out for admin DMs (lines 300-308). This must be extended to also skip approved members.

1. **Update** `AdminCog.on_message` (`admin_cog.py:299-308`) to check `can_message_bot` before falling through to robot sounds:
   ```python
   # Check if this DM should go to admin chat instead
   admin_user_id_str = os.getenv('ADMIN_USER_ID')
   if admin_user_id_str:
       try:
           admin_user_id = int(admin_user_id_str)
           if message.author.id == admin_user_id:
               return
       except ValueError:
           pass

   # Check if user has bot messaging enabled (let AdminChatCog handle it)
   if await self._can_user_message_bot(message.author.id):
       return

   # Reply with robot sounds to all other DMs
   ```
2. **Add** `_can_user_message_bot()` to `AdminCog` — a simple DB lookup on `guild_members.can_message_bot`. This can share the same caching approach as the AdminChatCog check (Step 5). Consider extracting to a shared helper in `src/common/` if duplication is unacceptable.
3. **Ordering guarantee:** Discord.py processes cog listeners in registration order. `AdminCog` is loaded before `AdminChatCog` (`main.py`), so the robot-sounds listener fires first. The fix in sub-step 1 ensures it yields to `AdminChatCog` for approved users.

## Step 5: Refactor the cog for member access — guild @mentions only for v1 (`admin_chat_cog.py`)
**Scope:** Medium

For v1, **non-admin users can only interact via @mentions in guild channels**, not via DMs. This eliminates the DM guild-ambiguity problem entirely — guild context is always available from the message, so the system prompt, community identity, and query scoping are unambiguous. Admin DMs continue to work as before.

1. **Replace** the admin-only gate (`admin_chat_cog.py:105-107`) with a two-tier check:
   ```python
   is_admin = self._is_admin(message.author.id)
   if not is_admin:
       # v1: non-admin access only via guild @mentions, not DMs
       if is_dm:
           return  # Silently ignore — robot sounds handled by AdminCog
       guild_id = message.guild.id
       can_message = await self._can_user_message_bot(message.author.id, guild_id)
       if not can_message:
           return  # Silently ignore
   ```
2. **Add** `_can_user_message_bot(user_id, guild_id)` method that queries `guild_members` for the `can_message_bot` flag with the specific guild_id. Cache results in-memory for ~60s (similar to `server_config` refresh pattern).
3. **Pass** `is_admin` and `requester_id` (the Discord user ID, or `None` for admins) through to `agent.chat()`.
4. **DMs from non-admin approved members:** `AdminChatCog` ignores them (returns early). `AdminCog` also skips them (Step 4 check). Net result: no response to non-admin DMs from approved members. This is acceptable for v1 — the bot could optionally reply "Try @mentioning me in a channel instead!" but that's a polish item.

## Step 6: Update the agent for role-aware behavior (`agent.py`)
**Scope:** Medium

1. **Modify** `AdminChatAgent.chat()` (`agent.py:111`) to accept `is_admin: bool` and `requester_id: Optional[int]`.
2. **Select tools:** Use `get_tools_for_role(is_admin)` when building the Claude API call (`agent.py:214`).
3. **Pass** `allowed_tools` and `requester_id` to `execute_tool()` calls (`agent.py:264`) for enforcement.
4. **Create** `MEMBER_SYSTEM_PROMPT` variant:
   - Remove all write-operation instructions (delete, send, share, upload sections)
   - Frame as a helpful community assistant: "You help community members explore and search the server"
   - List what they can do: search messages, look up members, browse channels, read summaries
   - Keep Discord formatting rules and error-handling guidance
5. **Select prompt:** Use `MEMBER_SYSTEM_PROMPT` for non-admin users, existing `SYSTEM_PROMPT` for admins.

## Step 7: Add rate limiting for non-admin users (`admin_chat_cog.py`)
**Scope:** Small

1. **Add** per-user rate limiting for non-admin users — max 10 messages per 5 minutes. Each message triggers a Claude API call, so this is cost protection.
2. **Implement** as an in-memory sliding window dict in `AdminChatCog` (same pattern as `_busy`).
3. **Reply** with a brief cooldown message when rate limit is hit.
4. **Lower** `MAX_CONVERSATION_LENGTH` for non-admins (e.g., 10 vs 20) to further bound costs.

## Step 8: Validate end-to-end
**Scope:** Medium

1. **Test admin flow:** Verify admin retains all 16 tools and full functionality via both DMs and @mentions — no regressions.
2. **Test DM conflict resolution:** Verify that:
   - Admin DMs → handled by `AdminChatCog` (no robot sounds)
   - Approved member DMs → robot sounds from `AdminCog` (v1: no DM support for non-admins)
   - Unapproved member DMs → robot sounds from `AdminCog`
3. **Test member @mention flow with flag on:** Verify:
   - Member can search messages, look up member info, browse channels
   - Member cannot invoke write tools — Claude doesn't offer them AND executor rejects if attempted
   - `query_table` is not available
   - Rate limiting works
4. **Test channel-visibility enforcement:**
   - Approved member in #general @mentions bot asking to search #admin-only → tool returns "You don't have access to that channel"
   - Approved member searches without specifying channel → results only include channels they can view
   - Admin searching same channels → unrestricted results
5. **Test prompt injection:** Member message attempts to instruct Claude to call `delete_message` or `send_message` — verify executor-level rejection.
6. **Test guild scoping:** `can_message_bot` flag on `guild_members` correctly gates per-guild.

## Execution Order
1. **Step 1** (migration) — no code dependencies, land first
2. **Step 2** (tool tiers + executor enforcement) — pure data/logic, no behavioral change yet
3. **Step 3** (channel-visibility enforcement) — builds on Step 2's `execute_tool` changes, no behavioral change yet since non-admins can't reach the tools
4. **Step 4** (DM handler conflict) — must land before Step 5 to avoid dual-response bug
5. **Step 5 + 6** together — cog and agent changes are tightly coupled
6. **Step 7** (rate limiting) — independent, can be added before or after
7. **Step 8** (validation) — after all changes are wired

## Validation Order
1. Run migration and verify `can_message_bot` column exists on `guild_members`
2. Unit-test `get_tools_for_role()` returns correct tool sets for admin vs member
3. Unit-test `execute_tool()` rejects admin-only tools when `allowed_tools` is the member set
4. Unit-test `_get_visible_channel_ids()` returns correct channels for a test member
5. Manual test: DM bot as approved member → robot sounds (v1: no DM support)
6. Manual test: @mention bot in guild as approved member → response with read-only tools
7. Manual test: approved member tries to search a channel they can't see → rejected
8. Manual test: verify admin flow is completely unaffected (DMs + @mentions, all tools)
9. Prompt injection test: approved member tries to trick Claude into calling restricted tools
