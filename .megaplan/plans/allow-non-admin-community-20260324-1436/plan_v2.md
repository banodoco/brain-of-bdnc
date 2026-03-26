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

## Step 3: Resolve the DM handler conflict (`admin_cog.py`, `admin_chat_cog.py`)
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
2. **Add** `_can_user_message_bot()` to `AdminCog` — a simple DB lookup on `guild_members.can_message_bot`. This can share the same caching approach as the AdminChatCog check (Step 4). Consider extracting to a shared helper in `src/common/` if duplication is unacceptable.
3. **Ordering guarantee:** Discord.py processes cog listeners in registration order. `AdminCog` is loaded before `AdminChatCog` (`main.py`), so the robot-sounds listener fires first. The fix in sub-step 1 ensures it yields to `AdminChatCog` for approved users.

## Step 4: Refactor the cog for member access (`admin_chat_cog.py`)
**Scope:** Medium

1. **Replace** the admin-only gate (`admin_chat_cog.py:105-107`) with a two-tier check:
   ```python
   is_admin = self._is_admin(message.author.id)
   if not is_admin:
       can_message = await self._can_user_message_bot(message.author.id, guild_id)
       if not can_message:
           return  # Silently ignore — robot sounds already handled by AdminCog
   ```
2. **Add** `_can_user_message_bot(user_id, guild_id)` method that queries `guild_members` for the `can_message_bot` flag. Cache results in-memory for ~60s (similar to `server_config` refresh pattern).
3. **For DMs** (no guild context): query across all guilds the user belongs to — if `can_message_bot=True` in any guild, allow. Use the first matching guild_id as context.
4. **Pass** `is_admin` through to `agent.chat()`.

## Step 5: Update the agent for role-aware behavior (`agent.py`)
**Scope:** Medium

1. **Modify** `AdminChatAgent.chat()` (`agent.py:111`) to accept `is_admin: bool`.
2. **Select tools:** Use `get_tools_for_role(is_admin)` when building the Claude API call (`agent.py:214`).
3. **Pass** `allowed_tools` set to `execute_tool()` calls (`agent.py:264`) for enforcement.
4. **Create** `MEMBER_SYSTEM_PROMPT` variant:
   - Remove all write-operation instructions (delete, send, share, upload sections)
   - Frame as a helpful community assistant: "You help community members explore and search the server"
   - List what they can do: search messages, look up members, browse channels, read summaries
   - Keep Discord formatting rules and error-handling guidance
5. **Select prompt:** Use `MEMBER_SYSTEM_PROMPT` for non-admin users, existing `SYSTEM_PROMPT` for admins.

## Step 6: Scope channel context for non-admin @mentions (`admin_chat_cog.py`)
**Scope:** Small

When a non-admin @mentions the bot in a public channel, the cog currently injects replied-to content and the last 10 channel messages into the prompt (`admin_chat_cog.py:136-169`). This is fine — the user can already see those messages in the channel they're posting from. The real concern is tool-based access to _other_ channels.

1. **For non-admin users:** The read-only tools (`find_messages`, `inspect_message`) already only return data from channels the bot has access to, which aligns with Discord's own visibility model. No additional restriction needed for v1 — the user could read these messages themselves via Discord.
2. **Channel context injection** (`admin_chat_cog.py:161-169`): Keep as-is for non-admins. The context comes from the channel they're messaging in — they can already see it.
3. **Document the policy:** Non-admin users can search/read any public channel's messages via tools (same data they could access via Discord UI). If private-channel restrictions are needed later, add a channel allowlist to the tool executor.

## Step 7: Add rate limiting for non-admin users (`admin_chat_cog.py`)
**Scope:** Small

1. **Add** per-user rate limiting for non-admin users — max 10 messages per 5 minutes. Each message triggers a Claude API call, so this is cost protection.
2. **Implement** as an in-memory sliding window dict in `AdminChatCog` (same pattern as `_busy`).
3. **Reply** with a brief cooldown message when rate limit is hit.
4. **Lower** `MAX_CONVERSATION_LENGTH` for non-admins (e.g., 10 vs 20) to further bound costs.

## Step 8: Validate end-to-end
**Scope:** Medium

1. **Test admin flow:** Verify admin retains all 16 tools and full functionality — no regressions.
2. **Test DM conflict resolution:** Verify that:
   - Admin DMs → handled by `AdminChatCog` (no robot sounds)
   - Approved member DMs → handled by `AdminChatCog` (no robot sounds)
   - Unapproved member DMs → robot sounds from `AdminCog` (no `AdminChatCog` response)
3. **Test member flow with flag on:** Verify:
   - Member can search messages, look up member info, browse channels
   - Member cannot invoke write tools — Claude doesn't offer them AND executor rejects if attempted
   - `query_table` is not available
   - Rate limiting works
4. **Test prompt injection:** Member message attempts to instruct Claude to call `delete_message` or `send_message` — verify executor-level rejection.
5. **Test guild scoping:** `can_message_bot` flag on `guild_members` correctly gates per-guild.

## Execution Order
1. **Step 1** (migration) — no code dependencies, land first
2. **Step 2** (tool tiers + executor enforcement) — pure data/logic, no behavioral change yet
3. **Step 3** (DM handler conflict) — must land before Step 4 to avoid dual-response bug
4. **Step 4 + 5 + 6** together — cog, agent, and context changes are tightly coupled
5. **Step 7** (rate limiting) — independent, can be added before or after
6. **Step 8** (validation) — after all changes are wired

## Validation Order
1. Run migration and verify `can_message_bot` column exists on `guild_members`
2. Unit-test `get_tools_for_role()` returns correct tool sets for admin vs member
3. Unit-test `execute_tool()` rejects admin-only tools when `allowed_tools` is the member set
4. Manual test: DM bot as unapproved user → get robot sounds (no `AdminChatCog` response)
5. Manual test: DM bot as approved member → get `AdminChatCog` response with read-only tools
6. Manual test: verify admin flow is completely unaffected
7. Prompt injection test: approved member tries to trick Claude into calling restricted tools
