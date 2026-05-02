# Implementation Plan: Admin-Triggered Payment Tool

## Overview
Add an `initiate_payment` admin chat tool that lets the admin pay any Banodoco user by mentioning the bot. The tool follows the same test→confirm→final pattern as grants: upsert wallet → send test payment (auto-confirmed) → on test success, create final payment as `pending_confirmation` → post Discord button for recipient to confirm → execute payout. All steps are fail-closed.

The grants flow in `grants_cog.py:550-750` is the exact blueprint. The key architectural choice is whether to require the admin to supply the wallet address directly (simplest, no async blocking) or to DM the user. The recommended approach: accept an optional `wallet_address` param — if provided, use it; if omitted, look up the user's existing wallet; if none exists, fail with an error telling the admin to collect it. A DM-based collection flow can be a follow-up.

For terminal payment handling, add `handle_payment_result` to `AdminChatCog` so PaymentCog's `_handoff_terminal_result` can dispatch `producer='admin'` results. This mirrors grants exactly and preserves the fail-closed guarantee (final payment only created after test succeeds).

## Phase 1: Tool Definition and Classification

### Step 1: Add `initiate_payment` tool schema (`src/features/admin_chat/tools.py:~577`)
**Scope:** Small
1. **Add** a new tool dict to the `TOOLS` list after `cancel_payment` (~line 577):
   ```python
   {
       "name": "initiate_payment",
       "description": "Initiate a Solana payment to a Discord user. Registers their wallet, sends a test payment, and on success creates the final payment with a confirmation button for the recipient.",
       "input_schema": {
           "type": "object",
           "properties": {
               "recipient_user_id": {"type": "string", "description": "Discord user ID of the recipient."},
               "amount_usd": {"type": "number", "description": "Payment amount in USD."},
               "wallet_address": {"type": "string", "description": "Recipient's Solana wallet address. If omitted, uses the wallet already on file."},
               "reason": {"type": "string", "description": "Optional memo/reason for the payment."}
           },
           "required": ["recipient_user_id", "amount_usd"]
       }
   }
   ```
2. **Add** `"initiate_payment"` to the `ADMIN_ONLY_TOOLS` set (~line 886). This satisfies the assertion at line 916 that `MEMBER_TOOLS | ADMIN_ONLY_TOOLS == ALL_TOOL_NAMES`.

### Step 2: Implement `execute_initiate_payment` function (`src/features/admin_chat/tools.py`)
**Scope:** Large — this is the core of the feature
1. **Implement** `async def execute_initiate_payment(bot, db_handler, tool_input)` with this flow:
   - **Validate inputs:** `recipient_user_id` must be a valid int, `amount_usd` must be > 0. Fail-closed on bad input.
   - **Resolve guild_id** from `tool_input['guild_id']` (injected by dispatcher).
   - **Wallet resolution:**
     - If `wallet_address` provided → `db_handler.upsert_wallet(guild_id, recipient_user_id, 'solana', wallet_address, metadata={'producer': 'admin'})`. Fail if upsert returns None.
     - If not provided → `db_handler.get_wallet(guild_id, recipient_user_id, 'solana')`. If no wallet on file, return error: `"No wallet on file for this user. Provide wallet_address or ask the user for their Solana address first."`
   - **Generate producer_ref:** Use `f"{guild_id}_{recipient_user_id}_{int(time.time())}"` — unique per active payment due to timestamp, satisfies the unique index on `(producer, producer_ref, is_test)`.
   - **Resolve payment destinations:** Use `server_config.resolve_payment_destinations(guild_id, source_channel_id, 'admin')` with fallback to source channel (matching grants pattern at `grants_cog.py:611-625`). The `source_channel_id` comes from `tool_input.get('channel_id')` or falls back to the guild's first text channel.
   - **Create test payment:** `bot.payment_service.request_payment(producer='admin', producer_ref=ref, guild_id=guild_id, recipient_wallet=wallet_address, chain='solana', provider='solana', is_test=True, confirm_channel_id=..., notify_channel_id=..., recipient_discord_id=recipient_user_id, wallet_id=wallet_record['wallet_id'], metadata={'reason': reason, 'amount_usd': amount_usd, 'admin_ref': ref})`. Fail if returns None.
   - **Auto-confirm test:** `bot.payment_service.confirm_payment(test_payment['payment_id'], guild_id=guild_id, confirmed_by='auto', confirmed_by_user_id=recipient_user_id)`. Fail if returns None.
   - **Return** success with test payment_id, status message, and the producer_ref for tracking.
2. **Store `amount_usd` in metadata** so the terminal callback can read it when creating the final payment (grants reads `total_cost_usd` from the grant row; we don't have a grant row, so metadata is the equivalent).

### Step 3: Wire dispatcher (`src/features/admin_chat/tools.py:~2986`)
**Scope:** Small
1. **Add** an `elif tool_name == "initiate_payment":` case in `execute_tool()` after the `cancel_payment` case (~line 2986):
   ```python
   elif tool_name == "initiate_payment":
       return await execute_initiate_payment(bot, db_handler, trusted_tool_input)
   ```

## Phase 2: Terminal Payment Callback

### Step 4: Add `handle_payment_result` to `AdminChatCog` (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium — mirrors `grants_cog.py:640-714`
1. **Add** `async def handle_payment_result(self, payment)` method to `AdminChatCog`:
   - Guard: `if payment.get('producer') != 'admin': return`
   - Branch on `payment.get('is_test')`:
     - **Test payment result:**
       - If `status != 'confirmed'` → notify admin via the notify channel that the test failed, abort.
       - If `status == 'confirmed'` → read `amount_usd` from `payment['metadata']`, create final payment via `bot.payment_service.request_payment(producer='admin', producer_ref=payment['producer_ref'], ..., is_test=False, amount_usd=amount_usd)`. If creation fails, notify admin channel. If success, call `payment_cog.send_confirmation_request(final_payment['payment_id'])` to post the Discord button for recipient confirmation.
     - **Final payment result:**
       - If `status == 'confirmed'` → post success message with amount, tx signature, explorer link to notify channel.
       - If `status != 'confirmed'` → post failure message to notify channel.
2. **PaymentCog discovery:** `_candidate_producer_cog_names('admin')` at `payment_cog.py:313-318` generates `['AdminCog', 'AdminCog', 'Admin']`. Since our cog is `AdminChatCog`, it won't match. Two options:
   - **Option A (recommended):** Override `_candidate_producer_cog_names` or add `'AdminChatCog'` to the candidate list. Actually, looking at the code, the simplest fix is to just make the producer name `'admin_chat'` instead of `'admin'`. Then `_candidate_producer_cog_names('admin_chat')` generates `['AdminChatCog', 'Admin_chatCog', 'AdminChat']` — the first one matches `AdminChatCog`.
   - **Option B:** Use producer name `'admin'` and register a lookup. 
   - **Decision:** Use producer `'admin_chat'` — it matches the cog name convention automatically with zero changes to PaymentCog. Update the producer string in Step 2 accordingly.

### Step 5: Update system prompt (`src/features/admin_chat/agent.py:~25`)
**Scope:** Small
1. **Add** documentation for the new tool in the `SYSTEM_PROMPT` under the "Doing things" section (~line 41):
   ```
   - initiate_payment(recipient_user_id, amount_usd, wallet_address?, reason?) — start a Solana payment to a user. If wallet_address is provided it's registered; otherwise uses the wallet on file (fails if none). Sends a test payment first, then on success posts a confirmation button for the recipient.
   ```

## Phase 3: Validation

### Step 6: Update existing tests (`tests/test_social_route_tools.py`)
**Scope:** Small
1. **Update** the admin tool classification test (~line 280) to include `"initiate_payment"` in the expected admin-only tools subset.
2. **Verify** the assertion at `tools.py:916` passes by running the import.

### Step 7: Run tests
**Scope:** Small
1. **Run** `python -m pytest tests/test_social_route_tools.py` — must pass with new tool classified.
2. **Run** `python -m pytest tests/test_scheduler.py` — must still pass (no changes to scheduler/payment service).
3. **Run** `python -m pytest tests/` — full suite green.

## Execution Order
1. Steps 1-3 (tool definition, implementation, dispatcher) — these are tightly coupled.
2. Step 4 (terminal callback) — depends on the producer name decided in Step 2.
3. Step 5 (system prompt) — independent, can go in any order.
4. Steps 6-7 (tests) — must come last.

## Validation Order
1. `python -c "from src.features.admin_chat import tools"` — verifies the tool classification assertion passes.
2. `python -m pytest tests/test_social_route_tools.py -x` — verifies classification tests.
3. `python -m pytest tests/ -x` — full suite.
