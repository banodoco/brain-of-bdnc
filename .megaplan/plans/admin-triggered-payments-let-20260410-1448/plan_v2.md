# Implementation Plan: Admin-Triggered Payment Tool

## Overview
Add an `initiate_payment` admin chat tool that lets the admin pay any Banodoco user by mentioning the bot in a guild channel. The flow:

1. **Admin calls `initiate_payment(recipient_user_id, amount_usd, reason?)`** — no `wallet_address` param.
2. **If the recipient already has a Solana wallet on file** → proceed directly to test payment (upsert wallet → `request_payment(is_test=True)` → auto-confirm → queue).
3. **If no wallet on file** → store a pending payment intent in-memory, send a message in the originating channel pinging the recipient asking for their wallet address, and return a status message to the admin.
4. **Recipient replies in-channel with their wallet** → a dedicated handler at the top of `AdminChatCog.on_message` intercepts the reply (bypassing the `_can_user_message_bot` gate), validates the address, upserts the wallet, and starts the test payment flow.
5. **On test payment confirmed** → `AdminChatCog.handle_payment_result` creates the final payment as `pending_confirmation` and calls `PaymentCog.send_confirmation_request` to post a Discord confirmation button in the originating channel.
6. **Recipient clicks the in-channel confirm button** → `PaymentConfirmView` (existing) checks `recipient_discord_id` and queues the real payout.
7. **On final payment confirmed** → `handle_payment_result` posts a success notification.

All steps are fail-closed: missing wallet intent aborts, invalid wallet rejects, failed test payment aborts, missing confirmation blocks payout.

**Key architectural choices:**
- **Producer name `'admin_chat'`** — `PaymentCog._candidate_producer_cog_names('admin_chat')` generates `['AdminChatCog', ...]` which auto-discovers `AdminChatCog` with zero changes to PaymentCog.
- **In-memory pending intents** — keyed by `(guild_id, channel_id, recipient_user_id)`. Lost on restart, but admin can re-initiate. No DB migration needed.
- **Wallet-reply interception** — added at the top of `AdminChatCog.on_message`, before the `_is_directed_at_bot()` and `_can_user_message_bot()` gates, so ANY guild member can reply with their wallet when there's a pending intent for them.
- **`source_channel_id` injection** — `AdminChatAgent.chat()` injects the source channel ID into tool_input so the tool knows where to ping the recipient and where to route payment destinations.
- **Admin must call from a guild channel** — DM-initiated payments fail-closed (no channel to ping the recipient in, no guild_id for admin DMs).

## Phase 1: Context Injection and Pending Intent Infrastructure

### Step 1: Inject `source_channel_id` into tool_input (`src/features/admin_chat/agent.py:~407`)
**Scope:** Small
1. **Add** `source_channel_id` injection alongside the existing `guild_id` injection at `agent.py:407-409`:
   ```python
   if channel_context and channel_context.get('guild_id') and 'guild_id' not in tool_input:
       tool_input = dict(tool_input)
       tool_input['guild_id'] = int(channel_context['guild_id'])
   # NEW: inject source_channel_id for tools that need to know where the admin sent the message
   if channel_context and channel_context.get('channel_id') and 'source_channel_id' not in tool_input:
       if not isinstance(tool_input, dict):
           tool_input = dict(tool_input)
       try:
           tool_input['source_channel_id'] = int(channel_context['channel_id'])
       except (TypeError, ValueError):
           pass
   ```
   This is safe because no existing tool uses the key `source_channel_id`.

### Step 2: Add pending intent store and wallet-reply handler to `AdminChatCog` (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** to `__init__`:
   ```python
   # Pending admin payment intents: {(guild_id, channel_id, recipient_user_id): intent_dict}
   self._pending_payment_intents: dict[tuple[int, int, int], dict] = {}
   ```
2. **Add** a `register_payment_intent(self, guild_id, channel_id, recipient_user_id, amount_usd, reason, producer_ref)` method that stores the intent dict keyed by `(guild_id, channel_id, recipient_user_id)`.
3. **Add** a `_check_wallet_reply(self, message)` method at the top of `on_message` (before the `_is_directed_at_bot()` check at line 201). This method:
   - Returns `False` if `message.author.bot` or no `message.guild`.
   - Looks up `(message.guild.id, message.channel.id, message.author.id)` in `_pending_payment_intents`.
   - If no match, returns `False` (fast path — normal messages fall through).
   - If match found, validates wallet via `is_valid_solana_address(message.content.strip())` (imported from `src.features.grants.solana_client`).
   - If invalid, replies "That doesn't look like a valid Solana wallet address. Please reply with a valid base58-encoded address." and returns `True`.
   - If valid, pops the intent, calls `_start_admin_payment_flow(message.channel, intent, wallet)`, and returns `True`.
4. **Modify** `on_message` to call the check first:
   ```python
   async def on_message(self, message: discord.Message):
       # Check for pending payment wallet replies first (bypasses permission gates)
       if await self._check_wallet_reply(message):
           return
       # ... existing code from line 201 ...
   ```
   This bypasses `_is_directed_at_bot()` and `_can_user_message_bot()` — the recipient doesn't need to mention the bot or have `can_message_bot` permission, they just reply in the channel where they were pinged.
5. **Add** `_start_admin_payment_flow(self, channel, intent, wallet)` — shared helper used by both the immediate path (wallet on file) and the deferred path (wallet from reply). Logic:
   - `upsert_wallet(guild_id, recipient_user_id, 'solana', wallet, metadata={'producer': 'admin_chat'})`
   - Resolve payment destinations via `server_config.resolve_payment_destinations(guild_id, channel.id, 'admin_chat')` with fallback to source channel (mirroring `grants_cog.py:611-625`).
   - `bot.payment_service.request_payment(producer='admin_chat', producer_ref=intent['producer_ref'], ..., is_test=True, metadata={'reason': intent['reason'], 'amount_usd': intent['amount_usd']})`. Fail if None.
   - `bot.payment_service.confirm_payment(test_payment['payment_id'], confirmed_by='auto', confirmed_by_user_id=recipient_user_id)`. Fail if None.
   - Send confirmation message in channel: "Wallet registered. I've queued a small test payment to verify it — once it lands, I'll send the full payment confirmation prompt."
   - On any failure, send an error message in channel and abort.
6. **Add** race-condition guard (`_processing_intents: set[tuple]`) mirroring GrantsCog's `_processing_threads` pattern to prevent double-processing of wallet replies.

## Phase 2: Tool Definition and Dispatcher

### Step 3: Add `initiate_payment` tool schema (`src/features/admin_chat/tools.py:~577`)
**Scope:** Small
1. **Add** a new tool dict to the `TOOLS` list after `cancel_payment` (~line 577):
   ```python
   {
       "name": "initiate_payment",
       "description": "Initiate a Solana payment to a Discord user. If the user has a wallet on file, starts the test payment immediately. If not, pings them in this channel to collect their wallet address first.",
       "input_schema": {
           "type": "object",
           "properties": {
               "recipient_user_id": {"type": "string", "description": "Discord user ID of the recipient."},
               "amount_usd": {"type": "number", "description": "Payment amount in USD."},
               "reason": {"type": "string", "description": "Optional memo/reason for the payment."}
           },
           "required": ["recipient_user_id", "amount_usd"]
       }
   }
   ```
   No `wallet_address` parameter — wallet is either on file or collected in-channel.
2. **Add** `"initiate_payment"` to the `ADMIN_ONLY_TOOLS` set (~line 886).

### Step 4: Implement `execute_initiate_payment` function (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Implement** `async def execute_initiate_payment(bot, db_handler, tool_input)`:
   - **Validate inputs:** `recipient_user_id` must parse to a valid int. `amount_usd` must be > 0. Fail-closed on bad input.
   - **Resolve context:** `guild_id = tool_input.get('guild_id')`, `source_channel_id = tool_input.get('source_channel_id')`. If either is missing, return error: `"This tool must be called from a guild channel (not DM)."` This fail-closes the DM path.
   - **Check payment_service:** `payment_service = getattr(bot, 'payment_service', None)`. If None, return error.
   - **Generate producer_ref:** `f"{guild_id}_{recipient_user_id}_{int(time.time())}"` — unique per active payment.
   - **Check existing wallet:** `db_handler.get_wallet(guild_id, recipient_user_id, 'solana')`.
   - **Path A — wallet on file:**
     - Get `AdminChatCog` via `bot.get_cog('AdminChatCog')` and call `_start_admin_payment_flow(channel, intent_dict, wallet_address)`. 
     - Return success: `{"success": True, "status": "test_payment_queued", "payment_id": ..., "message": "Wallet on file. Test payment queued — once it confirms, the recipient will get a confirmation prompt for the full amount."}`.
   - **Path B — no wallet:**
     - Get `AdminChatCog` via `bot.get_cog('AdminChatCog')` and call `register_payment_intent(guild_id, source_channel_id, recipient_user_id, amount_usd, reason, producer_ref)`.
     - Send a message in the source channel: `"<@{recipient_user_id}> — a payment of ${amount_usd} has been initiated for you. Please reply in this channel with your Solana wallet address to proceed."`.
     - Return success: `{"success": True, "status": "awaiting_wallet", "message": "No wallet on file. I've pinged the recipient in this channel to collect their wallet address."}`.

### Step 5: Wire dispatcher (`src/features/admin_chat/tools.py:~2986`)
**Scope:** Small
1. **Add** `elif tool_name == "initiate_payment":` in `execute_tool()` after the `cancel_payment` case (~line 2986):
   ```python
   elif tool_name == "initiate_payment":
       return await execute_initiate_payment(bot, db_handler, trusted_tool_input)
   ```

## Phase 3: Terminal Payment Callback

### Step 6: Add `handle_payment_result` to `AdminChatCog` (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium — mirrors `grants_cog.py:640-714`
1. **Add** `async def handle_payment_result(self, payment)`:
   - Guard: `if str(payment.get('producer') or '').strip().lower() != 'admin_chat': return`
   - Branch on `payment.get('is_test')`:
     - **Test payment — confirmed:**
       - Read `amount_usd` from `payment['metadata']['amount_usd']`. If missing, log error and abort.
       - Create final payment: `bot.payment_service.request_payment(producer='admin_chat', producer_ref=payment['producer_ref'], guild_id=payment['guild_id'], recipient_wallet=payment['recipient_wallet'], chain='solana', provider='solana', is_test=False, amount_usd=amount_usd, confirm_channel_id=payment['confirm_channel_id'], confirm_thread_id=payment.get('confirm_thread_id'), notify_channel_id=payment['notify_channel_id'], notify_thread_id=payment.get('notify_thread_id'), recipient_discord_id=payment['recipient_discord_id'], wallet_id=payment.get('wallet_id'), route_key=payment.get('route_key'), metadata=payment.get('metadata'))`.
       - If creation fails, notify via `notify_channel_id`.
       - If success and status == `'pending_confirmation'`, call `bot.get_cog('PaymentCog').send_confirmation_request(final_payment['payment_id'])` to post the in-channel confirmation button.
       - Post message to notify channel: "Test payment confirmed. Please confirm the full payout using the button above."
     - **Test payment — failed/other:**
       - Post to notify channel: "The wallet verification payment ended in `{status}`. The admin will review before any payout."
       - Do NOT create a final payment (fail-closed).
     - **Final payment — confirmed:**
       - Post to notify channel: "**Payment sent!** Amount: {amount_token} SOL, Wallet: `{wallet}`, Transaction: [View on Explorer]({url})". This is a producer-specific notification with payment context; the generic PaymentCog notification is minimal.
     - **Final payment — failed/other:**
       - Post to notify channel: "The final payment ended in `{status}`. The admin will review."
2. **Resolve notification destination** using the `confirm_channel_id`/`notify_channel_id` stored on the payment row (set during `request_payment` in Step 2/4). Use `bot.get_channel()` to resolve.

## Phase 4: System Prompt and Tests

### Step 7: Update system prompt (`src/features/admin_chat/agent.py:~41`)
**Scope:** Small
1. **Add** under the "Doing things" section:
   ```
   - initiate_payment(recipient_user_id, amount_usd, reason?) — initiate a Solana payment to a user. If the user has a wallet on file, starts a test payment immediately. If not, pings the user in this channel to collect their wallet first. After the test payment confirms, the recipient gets a confirmation button for the full amount. Must be called from a guild channel, not DM.
   ```

### Step 8: Update existing tests (`tests/test_social_route_tools.py`)
**Scope:** Small
1. **Update** the admin tool classification test (~line 280) to include `"initiate_payment"` in the expected admin-only tools subset.
2. **Verify** the assertion at `tools.py:916` passes.

### Step 9: Add new test file (`tests/test_admin_payments.py`)
**Scope:** Medium
1. **Test `execute_initiate_payment` — wallet on file path:** Mock `db_handler.get_wallet` to return a wallet, mock `bot.payment_service.request_payment` and `confirm_payment`. Assert test payment is created with `producer='admin_chat'`, `is_test=True`, and auto-confirmed.
2. **Test `execute_initiate_payment` — no wallet path:** Mock `db_handler.get_wallet` to return None. Assert the tool registers a pending intent and returns `status='awaiting_wallet'`. Assert a ping message is sent to the source channel.
3. **Test `execute_initiate_payment` — validation failures:** Bad `recipient_user_id`, `amount_usd <= 0`, missing `guild_id`/`source_channel_id`. Assert fail-closed error responses.
4. **Test `_check_wallet_reply` — valid wallet:** Create a pending intent, simulate a message from the recipient with a valid Solana address. Assert wallet is upserted and test payment is queued.
5. **Test `_check_wallet_reply` — invalid wallet:** Simulate invalid address. Assert error reply and intent NOT consumed.
6. **Test `_check_wallet_reply` — no pending intent:** Simulate message from arbitrary user. Assert returns `False` (falls through).
7. **Test `handle_payment_result` — test confirmed:** Mock payment with `is_test=True, status='confirmed'`. Assert final payment created with `is_test=False` and `send_confirmation_request` called.
8. **Test `handle_payment_result` — test failed:** Mock payment with `is_test=True, status='failed'`. Assert NO final payment created.

### Step 10: Run tests
**Scope:** Small
1. **Run** `python -c "from src.features.admin_chat import tools"` — verifies classification assertion.
2. **Run** `python -m pytest tests/test_admin_payments.py -x` — new tests.
3. **Run** `python -m pytest tests/test_social_route_tools.py -x` — existing classification tests.
4. **Run** `python -m pytest tests/test_scheduler.py -x` — scheduler unaffected.
5. **Run** `python -m pytest tests/ -x` — full suite green.

## Execution Order
1. Step 1 (context injection) — enables Step 4.
2. Steps 2-5 (intent store, tool, dispatcher) — core feature, tightly coupled.
3. Step 6 (terminal callback) — depends on producer name from Step 4.
4. Step 7 (system prompt) — independent.
5. Steps 8-10 (tests) — last.

## Validation Order
1. `python -c "from src.features.admin_chat import tools"` — classification assertion.
2. `python -m pytest tests/test_admin_payments.py -x` — new feature tests.
3. `python -m pytest tests/test_social_route_tools.py tests/test_scheduler.py -x` — existing tests unbroken.
4. `python -m pytest tests/ -x` — full suite.
