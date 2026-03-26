# Implementation Plan: Open Bot Chat to Non-Admin Members with Access Controls

## Overview
The BNDC Discord bot currently has a single admin chat feature (`src/features/admin_chat/`) that only responds to one hardcoded `ADMIN_USER_ID`. The goal is to allow approved community members to DM or @mention the bot with access to a restricted (read-only) subset of tools, while admins retain full tool access.

**Current architecture:**
- `AdminChatCog` (`admin_chat_cog.py:99-107`) gates all access behind `_is_admin()`, rejecting everyone else with "I can only respond to admins for now!"
- `AdminChatAgent` (`agent.py`) passes all 16 `TOOLS` to Claude on every request
- `execute_tool()` (`tools.py:1388-1436`) dispatches without any permission check
- The `members` table has no access-control flag for bot interaction

**Key constraint:** The system prompt and tool list are injected per-Claude-call (`agent.py:201-214`), so we can vary them by user role at that point. Tool execution also has a single dispatch point (`tools.py:1388`) where we can enforce a blocklist.

## Step 1: Add `can_message_bot` column to `members` table
**Scope:** Small
1. **Create migration** `supabase/migrations/YYYYMMDD_add_can_message_bot.sql` that adds `can_message_bot BOOLEAN DEFAULT FALSE` to the `members` table.
2. **Add** the column to `QUERYABLE_TABLES` awareness — no code change needed since `query_table` already queries `members` dynamically.
3. **Validate** by running the migration locally against the Supabase instance.

## Step 2: Define tool permission tiers
**Scope:** Small
1. **Create** a constant in `tools.py` (near `QUERYABLE_TABLES` at line 20) that categorizes tools:
   ```python
   # Tools available to all approved users (read-only + response)
   MEMBER_TOOLS = {
       'reply', 'end_turn',
       'find_messages', 'inspect_message',
       'get_active_channels', 'get_daily_summaries',
       'get_member_info', 'get_bot_status',
       'resolve_user', 'query_table',
   }
   
   # Tools restricted to admins only
   ADMIN_ONLY_TOOLS = {
       'send_message', 'edit_message', 'delete_message',
       'upload_file', 'share_to_social', 'search_logs',
   }
   ```
2. **Add** a helper `get_tools_for_role(is_admin: bool) -> list` that filters the `TOOLS` list by the user's role, so only permitted tool schemas are sent to Claude.
3. **Add** an enforcement check in `execute_tool()` (`tools.py:1388`) that takes an `allowed_tools: set` parameter and rejects tool calls not in the set — defense in depth against prompt injection.

### Sensitive operations rationale
- `send_message`, `edit_message`, `delete_message`, `upload_file`: Write operations that can modify server state or impersonate the bot.
- `share_to_social`: Publishes content to external platforms (Twitter, Instagram, etc.).
- `search_logs`: Exposes internal system logs which may contain sensitive operational data.
- `query_table`: Kept as member-accessible but needs scoping (see Step 4).

## Step 3: Refactor the cog to support member access
**Scope:** Medium
1. **Rename** `AdminChatCog` to `ChatCog` (or keep internal name, just change behavior) — update `admin_chat_cog.py:99-107` to allow non-admin users who have `can_message_bot = True`.
2. **Replace** the single `_is_admin()` gate with a two-tier check:
   ```python
   # admin_chat_cog.py on_message handler
   is_admin = self._is_admin(message.author.id)
   if not is_admin:
       can_message = await self._can_user_message_bot(message.author.id)
       if not can_message:
           return  # Silently ignore (or brief rejection message)
   ```
3. **Add** `_can_user_message_bot()` method that queries `members` table for the `can_message_bot` flag. Cache results for ~60s to avoid DB round-trips on every message.
4. **Pass** `is_admin` through to the agent's `chat()` method so it can select the correct tool set and system prompt.

## Step 4: Update the agent for role-aware behavior
**Scope:** Medium
1. **Modify** `AdminChatAgent.chat()` (`agent.py:111`) to accept an `is_admin: bool` parameter.
2. **Select tools:** Use `get_tools_for_role(is_admin)` when building the Claude API call (`agent.py:214`), so non-admin users only see read-only tools.
3. **Adjust system prompt:** Create a `MEMBER_SYSTEM_PROMPT` variant that:
   - Removes instructions about write operations (delete, send, share, etc.)
   - Frames the bot as a helpful community assistant rather than admin tool
   - Adds guidance about what the user can ask (search messages, look up members, browse channels, etc.)
4. **Pass** `allowed_tools` to `execute_tool()` calls (`agent.py:264`) for enforcement.
5. **Scope `query_table` for non-admins:** Restrict which tables members can query (e.g., exclude `system_logs`, `timed_mutes`, `invite_codes`) and potentially limit which columns are visible on sensitive tables. Add a `MEMBER_QUERYABLE_TABLES` subset.

## Step 5: Add rate limiting for non-admin users
**Scope:** Small
1. **Add** per-user rate limiting in the cog for non-admin users — e.g., max 10 messages per 5 minutes. This is cheap insurance against abuse since each message triggers a Claude API call.
2. **Implement** as a simple in-memory sliding window in `AdminChatCog` (similar to existing `_busy` dict pattern).
3. **Reply** with a brief cooldown message when rate limit is hit.

## Step 6: Separate conversation namespaces
**Scope:** Small
1. **Ensure** `_conversations` dict (`agent.py:22`) already keys by `user_id` — it does, so admin and member conversations are naturally isolated.
2. **Consider** a lower `MAX_CONVERSATION_LENGTH` for non-admin users (e.g., 10 vs 20) to control costs.

## Step 7: Validate end-to-end
**Scope:** Medium
1. **Test admin flow:** Verify admin retains all tools and full functionality — no regressions.
2. **Test member flow with flag off:** Verify bot ignores/rejects users without `can_message_bot`.
3. **Test member flow with flag on:** Verify:
   - Member can search messages, look up member info, browse channels
   - Member cannot invoke write tools (Claude shouldn't offer them, and executor rejects if attempted)
   - Rate limiting works
4. **Test edge cases:**
   - Member @mentions bot in public channel vs DM
   - Member tries to trick bot into calling admin tools via prompt injection (defense-in-depth check at executor level)
   - Bot behavior when member and admin message simultaneously

## Execution Order
1. **Step 1** (migration) — no code dependencies, can land first
2. **Step 2** (tool tiers) — pure data, no behavioral change yet
3. **Step 3 + 4** together — the cog and agent changes are tightly coupled
4. **Step 5** (rate limiting) — independent, can be added before or after
5. **Step 6** (conversation tuning) — trivial, fold into Step 4
6. **Step 7** (validation) — after all changes are wired

## Validation Order
1. Run migration and verify `can_message_bot` column exists
2. Unit-test `get_tools_for_role()` returns correct tool sets
3. Test `execute_tool()` rejects admin-only tools when `allowed_tools` is restricted
4. Integration test: DM the bot as a non-admin with `can_message_bot=True` and verify read-only tools work
5. Integration test: verify admin tools are blocked both at Claude-prompt level (not offered) and executor level (rejected if called)
6. Verify admin flow is unaffected
