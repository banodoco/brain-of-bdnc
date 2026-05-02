# Implementation Plan: Admin-Triggered Payments

## Overview

Add a persisted, restart-safe admin-triggered payment flow. The admin mentions the bot in a guild channel to initiate a SOL payment to a Banodoco user. The system stores the intent in a new `admin_payment_intents` DB table, pings the recipient in-channel to collect their wallet (if not on file), runs the test→final payment flow through the existing PaymentService, and confirms via either the recipient's free-text reply or the existing Discord button. All state transitions are fail-closed; any ambiguity or suspicious behavior tags the admin.

### Key architectural decisions

- **`admin_payment_intents` table** — persisted in Supabase, survives restarts. Status state machine: `awaiting_wallet → awaiting_test → awaiting_confirmation → confirmed → completed | failed | cancelled`. Includes message cursor fields (`prompt_message_id`, `last_scanned_message_id`, `resolved_by_message_id`) for recovery.
- **`requested_amount_sol`** — canonical admin-entered amount. PaymentService.request_payment gets a new optional `amount_token` parameter that, when provided, bypasses USD conversion. `amount_usd` and `token_price_usd` stay null for direct-SOL payments.
- **Producer `'admin_chat'`** — `_candidate_producer_cog_names('admin_chat')` → `['AdminChatCog', ...]` auto-matches the cog.
- **Deterministic identity gating** — reply interceptor at top of `AdminChatCog.on_message` (before `_is_directed_at_bot` / `_can_user_message_bot`). Lookup key: `(guild_id, channel_id, author_id)` against active intents. Only after a single intent is identified does the agent classify the message semantics.
- **Agent classification** — lightweight LLM call classifies recipient replies as: `wallet_provided` (with extracted address), `positive_confirmation`, `declined`, `ambiguous`, `suspicious`. Only `wallet_provided` and `positive_confirmation` advance the flow; everything else fails closed and tags admin.
- **Dual confirmation path** — PaymentConfirmView button (existing, secondary) + free-text confirmation (primary). Both converge at PaymentService.confirm_payment.
- **Startup recovery** — `AdminChatCog.cog_load` queries active intents, re-scans channels for missed replies using `last_scanned_message_id` cursor.

## Phase 1: Foundation — DB Schema, DB Methods, PaymentService Extension

### Step 1: Create `admin_payment_intents` table (`sql/admin_payment_intents.sql`)
**Scope:** Medium

1. **Create** new file `sql/admin_payment_intents.sql` following the idempotent pattern from `sql/payments.sql`:

   ```sql
   -- Trigger function
   create or replace function set_admin_payment_intent_updated_at() ...

   create table if not exists admin_payment_intents (
       intent_id uuid primary key default gen_random_uuid(),
       guild_id bigint not null,
       channel_id bigint not null,
       recipient_user_id bigint not null,
       initiated_by_user_id bigint not null,
       requested_amount_sol numeric(38, 18) not null check (requested_amount_sol > 0),
       reason text,
       producer_ref text not null,
       
       status text not null default 'awaiting_wallet'
           check (status in (
               'awaiting_wallet', 'awaiting_test', 'awaiting_confirmation',
               'confirmed', 'completed', 'failed', 'cancelled'
           )),
       
       wallet_address text,
       wallet_id uuid,
       test_payment_id uuid,
       final_payment_id uuid,
       
       prompt_message_id bigint,
       last_scanned_message_id bigint,
       resolved_by_message_id bigint,
       
       last_error text,
       created_at timestamptz not null default timezone('utc', now()),
       updated_at timestamptz not null default timezone('utc', now()),
       
       check (char_length(btrim(producer_ref)) > 0)
   );
   ```

2. **Add indexes**: unique on `(guild_id, channel_id, recipient_user_id)` WHERE status NOT IN terminal states (prevents duplicate active intents for same recipient in same channel); index on `(guild_id, status)` for recovery queries; index on `(guild_id, channel_id, status)` for interceptor lookups.

3. **Add trigger** for `updated_at`, enable RLS, revoke from anon/authenticated — same pattern as `payments.sql`.

### Step 2: Add intent CRUD methods to `db_handler.py` (`src/common/db_handler.py`)
**Scope:** Medium

1. **Add methods** following the existing payment CRUD pattern (`db_handler.py:1703+`):
   - `create_admin_payment_intent(record, guild_id)` → insert, return row or None.
   - `get_admin_payment_intent(intent_id, guild_id)` → select by ID.
   - `get_active_intent_for_recipient(guild_id, channel_id, recipient_user_id)` → select where `status NOT IN ('completed', 'failed', 'cancelled')`, limit 1. This is the deterministic identity-gated lookup for the interceptor.
   - `list_active_intents(guild_id)` → select where `status NOT IN ('completed', 'failed', 'cancelled')`, ordered by `created_at`. Used for startup recovery.
   - `update_admin_payment_intent(intent_id, payload, guild_id)` → general update with `_serialize_supabase_value`. Used for status transitions and cursor updates.

2. **All methods** use `_gate_check(guild_id)`, check `self.supabase`, and return None/empty on failure (fail-closed pattern).

### Step 3: Extend `PaymentService.request_payment` to accept direct SOL amounts (`src/features/payments/payment_service.py:32-51`)
**Scope:** Small

1. **Add parameter** `amount_token: Optional[float] = None` to the `request_payment` signature (after `amount_usd` at line 44).

2. **Modify** the amount resolution block (lines 91-110):
   ```python
   if is_test:
       # unchanged — uses self.test_payment_amount
   elif amount_token is not None:
       # NEW: Direct SOL amount, bypass USD conversion
       if amount_token <= 0:
           self.logger.error("[PaymentService] request_payment amount_token must be > 0")
           return None
       normalized_amount_usd = None
       token_price_usd = None
   elif amount_usd is not None:
       # unchanged — existing USD conversion path
   else:
       self.logger.error("[PaymentService] request_payment requires amount_usd or amount_token for non-test payments")
       return None
   ```

3. **The DB schema already supports this** — `amount_usd` and `token_price_usd` are nullable columns on `payment_requests`, and the CHECK constraint `(not is_test or (amount_usd is null and token_price_usd is null))` only restricts test payments.

4. **No changes** to `confirm_payment`, `execute_payment`, or any downstream code — they only use `amount_token` from the payment record.

## Phase 2: Tool Definition, Executor, and Dispatcher

### Step 4: Add `initiate_payment` tool schema (`src/features/admin_chat/tools.py:~577`)
**Scope:** Small

1. **Add** tool dict to `TOOLS` list after `cancel_payment`:
   ```python
   {
       "name": "initiate_payment",
       "description": "Initiate a Solana payment to a Discord user. If the user has a wallet on file, starts the test payment immediately. If not, pings them in this channel to collect their wallet address. Must be called from a guild channel.",
       "input_schema": {
           "type": "object",
           "properties": {
               "recipient_user_id": {"type": "string", "description": "Discord user ID of the payment recipient."},
               "amount_sol": {"type": "number", "description": "Payment amount in SOL."},
               "reason": {"type": "string", "description": "Optional memo/reason for the payment."}
           },
           "required": ["recipient_user_id", "amount_sol"]
       }
   }
   ```

2. **Add** `"initiate_payment"` to `ADMIN_ONLY_TOOLS` (~line 886).

### Step 5: Inject `source_channel_id` into tool_input (`src/features/admin_chat/agent.py:~409`)
**Scope:** Small

1. **Add** alongside existing `guild_id` injection (after line 409):
   ```python
   if channel_context and channel_context.get('channel_id') and 'source_channel_id' not in tool_input:
       if not isinstance(tool_input, dict):
           tool_input = dict(tool_input)
       try:
           tool_input['source_channel_id'] = int(channel_context['channel_id'])
       except (TypeError, ValueError):
           pass
   ```

### Step 6: Implement `execute_initiate_payment` and wire dispatcher (`src/features/admin_chat/tools.py`)
**Scope:** Medium

1. **Implement** `async def execute_initiate_payment(bot, db_handler, tool_input)`:

   **Validation (fail-closed):**
   - Parse `recipient_user_id` to int; error if invalid.
   - Parse `amount_sol` to float; error if `<= 0`.
   - Require `guild_id` and `source_channel_id` from tool_input; error if missing: `"Must be called from a guild channel, not a DM."`
   - Require `bot.payment_service`; error if None.

   **Check for existing active intent:**
   - `db_handler.get_active_intent_for_recipient(guild_id, source_channel_id, recipient_user_id)`. If exists, return info about existing intent (idempotent — don't create duplicates).

   **Generate producer_ref:** `f"{guild_id}_{recipient_user_id}_{int(time.time())}"`.

   **Check existing wallet:** `db_handler.get_wallet(guild_id, recipient_user_id, 'solana')`.

   **Path A — wallet on file:**
   - Create intent in DB with `status='awaiting_test'`, storing `wallet_address` and `wallet_id`.
   - Get cog via `bot.get_cog('AdminChatCog')`.
   - Call `cog._start_admin_payment_flow(channel, intent)` — shared helper.
   - Return `{"success": True, "status": "test_payment_queued", ...}`.

   **Path B — no wallet:**
   - Create intent in DB with `status='awaiting_wallet'`.
   - Send channel ping: `"<@{recipient_user_id}> — a payment of {amount_sol} SOL has been initiated for you. Please reply in this channel with your **Solana wallet address** to proceed."`
   - Update intent with `prompt_message_id` from the sent message.
   - Return `{"success": True, "status": "awaiting_wallet", ...}`.

2. **Add** dispatcher case in `execute_tool` after `cancel_payment` (~line 2986):
   ```python
   elif tool_name == "initiate_payment":
       return await execute_initiate_payment(bot, db_handler, trusted_tool_input)
   ```

## Phase 3: Reply Interceptor, Agent Classification, and Payment Flow

### Step 7: Add reply interceptor to `AdminChatCog.on_message` (`src/features/admin_chat/admin_chat_cog.py:~197`)
**Scope:** Medium

1. **Add** `_check_pending_payment_reply(self, message)` called at the **very top** of `on_message`, before `_is_directed_at_bot()` (line 201):
   ```python
   async def on_message(self, message):
       if await self._check_pending_payment_reply(message):
           return
       if not self._is_directed_at_bot(message):
           return
       # ... existing code ...
   ```

2. **`_check_pending_payment_reply` logic:**
   - **Fast exit**: return `False` if `message.author.bot` or `message.guild is None`.
   - **Deterministic identity gate**: `intent = self.db_handler.get_active_intent_for_recipient(message.guild.id, message.channel.id, message.author.id)`. If None → return `False` (O(1) at Python level, single indexed DB query).
   - **Race guard**: if `intent['intent_id']` in `self._processing_intents` → return `False`. Add to set, remove in `finally`.
   - **Branch on `intent['status']`:**

     **`'awaiting_wallet'`:**
     - First try deterministic validation: `is_valid_solana_address(message.content.strip())`.
     - If valid → call `_handle_wallet_received(message, intent, wallet_address)`, return `True`.
     - If invalid → call lightweight agent classification on `message.content` with context: "Recipient was asked for their Solana wallet address. Classify this reply." Categories: `wallet_provided` (with extracted address if embedded in surrounding text), `declined`, `ambiguous`, `suspicious`.
     - On `wallet_provided` with valid extracted address → `_handle_wallet_received(...)`, return `True`.
     - On `declined` → update intent to `'cancelled'`, notify channel, return `True`.
     - On `ambiguous` / `suspicious` / anything else → **fail closed**, tag admin `<@{admin_id}>` in channel, return `True`.

     **`'awaiting_confirmation'`:**
     - Call agent classification: "Recipient was asked to confirm a payment of {amount_sol} SOL. Classify this reply." Categories: `positive_confirmation`, `declined`, `ambiguous`, `suspicious`.
     - On `positive_confirmation` → call `_handle_confirmation_received(message, intent)`, return `True`.
     - On `declined` → cancel intent and final payment, notify channel, return `True`.
     - On `ambiguous` / `suspicious` → fail closed, tag admin, return `True`.

     **Any other status** → return `False` (intent is not awaiting user input).

3. **Add** `_processing_intents: set[str] = set()` to `__init__` for race-condition guard.

### Step 8: Implement shared payment flow helpers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium

1. **`_handle_wallet_received(self, message, intent, wallet_address)`:**
   - `upsert_wallet(guild_id, recipient_user_id, 'solana', wallet_address, metadata={'producer': 'admin_chat'})`. Fail if None.
   - Update intent: `status='awaiting_test'`, `wallet_address`, `wallet_id`, `resolved_by_message_id=message.id`.
   - Call `_start_admin_payment_flow(message.channel, intent)`.

2. **`_start_admin_payment_flow(self, channel, intent)`:**
   - Resolve destinations via `server_config.resolve_payment_destinations(guild_id, channel.id, 'admin_chat')` with fallback to source channel (pattern from `grants_cog.py:611-625`).
   - `bot.payment_service.request_payment(producer='admin_chat', producer_ref=intent['producer_ref'], ..., is_test=True, ..., metadata={'reason': intent['reason'], 'requested_amount_sol': intent['requested_amount_sol'], 'intent_id': intent['intent_id']})`. Fail if None.
   - `bot.payment_service.confirm_payment(test_payment['payment_id'], confirmed_by='auto', confirmed_by_user_id=intent['recipient_user_id'])`. Fail if None.
   - Update intent: `test_payment_id=test_payment['payment_id']`.
   - Send channel message: "Wallet registered. I've sent a small test payment to verify it — once that lands, I'll send the full payment confirmation."
   - On failure: update intent `status='failed'`, `last_error=str(e)`, tag admin in channel.

3. **`_handle_confirmation_received(self, message, intent)`:**
   - Look up the final payment via `intent['final_payment_id']`.
   - Call `bot.payment_service.confirm_payment(final_payment_id, confirmed_by='free_text', confirmed_by_user_id=message.author.id)`. This is identity-gated by PaymentService's existing `recipient_discord_id` check.
   - Update intent: `status='confirmed'`, `resolved_by_message_id=message.id`.
   - On failure: tag admin, do NOT advance.

### Step 9: Add `handle_payment_result` to `AdminChatCog` (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium — mirrors `grants_cog.py:640-714`

1. **`async def handle_payment_result(self, payment)`:**
   - Guard: `if str(payment.get('producer') or '').strip().lower() != 'admin_chat': return`
   - Extract `intent_id` from `payment.get('metadata', {}).get('intent_id')`. If missing, log warning and return.
   - Fetch intent: `db_handler.get_admin_payment_intent(intent_id, guild_id=payment['guild_id'])`.

   **Test confirmed:**
   - Read `requested_amount_sol` from intent (or payment metadata).
   - Create final payment: `bot.payment_service.request_payment(..., is_test=False, amount_token=requested_amount_sol, ...)`. Reuse destinations from test payment.
   - If creation fails → update intent `status='failed'`, tag admin.
   - If success → update intent `status='awaiting_confirmation'`, `final_payment_id=final_payment['payment_id']`.
   - Post PaymentConfirmView button via `bot.get_cog('PaymentCog').send_confirmation_request(final_payment['payment_id'])` (secondary confirmation path).
   - Post channel message: "Test payment confirmed. <@{recipient_user_id}> — please confirm you'd like to receive the full payment of {amount_sol} SOL. You can click the button above or reply here to confirm."
   - Update intent `prompt_message_id` with the confirmation prompt message ID, reset `last_scanned_message_id`.

   **Test failed:**
   - Update intent `status='failed'`, `last_error=f"Test payment {status}"`.
   - Notify channel, tag admin. Do NOT create final payment (fail-closed).

   **Final confirmed:**
   - Update intent `status='completed'`.
   - Post success: "**Payment sent!** {amount_token:.4f} SOL · `{redacted_wallet}` · [Explorer]({url})"

   **Final failed:**
   - Update intent `status='failed'`.
   - Notify channel, tag admin.

2. **Resolve channel** from payment's `notify_channel_id`/`notify_thread_id`.

## Phase 4: Startup Recovery

### Step 10: Add startup reconciliation (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium

1. **Add** `async def cog_load(self)` to `AdminChatCog`:
   ```python
   async def cog_load(self):
       await self.bot.wait_until_ready()
       await self._reconcile_active_intents()
   ```

2. **`_reconcile_active_intents(self)`:**
   - `intents = self.db_handler.list_active_intents(guild_id)` — query all active intents across guilds (or per enabled guild via server_config).
   - For each intent:
     - If `status == 'awaiting_wallet'` or `status == 'awaiting_confirmation'`:
       - Resolve `channel = bot.get_channel(intent['channel_id'])`. Skip if channel gone.
       - Scan channel history from `last_scanned_message_id` forward (or from `prompt_message_id` if `last_scanned_message_id` is None).
       - For each message from the recipient (`message.author.id == intent['recipient_user_id']`):
         - Run through the same classification logic as the live interceptor.
         - On match → process and advance intent.
         - On ambiguity → tag admin, leave intent in current state.
       - Update `last_scanned_message_id` to the latest scanned message ID.
     - If `status == 'awaiting_test'`:
       - Check test payment status via `db_handler.get_payment_request(intent['test_payment_id'])`.
       - If terminal and not yet handled → call `handle_payment_result` manually (PaymentCog's handoff may have been lost during restart).
     - If `status == 'confirmed'`:
       - Check final payment status. If terminal → advance intent to completed/failed.

3. **Rate-limit scanning**: Process at most N messages per intent per reconciliation pass. Log and skip if channel history is too large.

## Phase 5: System Prompt and Tests

### Step 11: Update system prompt (`src/features/admin_chat/agent.py:~61`)
**Scope:** Small

1. **Add** to "Doing things" section after cancel_payment:
   ```
   - initiate_payment(recipient_user_id, amount_sol, reason?) — start a Solana payment to a user. Amount is in SOL. If the user has a wallet on file, queues a test payment immediately. If not, pings them in this channel to collect their wallet first. After test confirms, the recipient gets a confirmation prompt. Must be called from a guild channel.
   ```

### Step 12: Update existing tests (`tests/test_social_route_tools.py`)
**Scope:** Small

1. **Add** `"initiate_payment"` to the expected admin-only tools subset in `test_route_tools_are_admin_only` (~line 264). Uses `.issubset()` so adding to expected set is forward-compatible.

2. **Add** assertion in `test_agent_prompt_mentions_payment_tools` (~line 413):
   ```python
   assert "initiate_payment" in admin_agent.SYSTEM_PROMPT
   ```

### Step 13: Add new test file (`tests/test_admin_payments.py`)
**Scope:** Large

1. **`test_initiate_payment_wallet_on_file`**: Mock `get_wallet` returning wallet, mock `create_admin_payment_intent`, mock `request_payment` + `confirm_payment`. Assert test payment created with `producer='admin_chat'`, `is_test=True`, auto-confirmed. Assert intent created with `status='awaiting_test'`.

2. **`test_initiate_payment_no_wallet`**: Mock `get_wallet` returning None. Assert intent created with `status='awaiting_wallet'`. Assert ping message sent to channel.

3. **`test_initiate_payment_validation`**: Bad `recipient_user_id`, `amount_sol <= 0`, missing `guild_id`/`source_channel_id`. Each returns `success=False`.

4. **`test_initiate_payment_duplicate_intent`**: Mock `get_active_intent_for_recipient` returning existing. Assert no duplicate created.

5. **`test_wallet_reply_valid`**: Mock active intent `status='awaiting_wallet'`. Simulate message with valid Solana address. Assert `upsert_wallet` called, intent updated to `awaiting_test`, test payment started.

6. **`test_wallet_reply_invalid_then_classified`**: Simulate non-address message. Mock agent classification returning `ambiguous`. Assert admin tagged, intent not advanced.

7. **`test_wallet_reply_no_intent`**: Simulate message from user with no active intent. Assert `_check_pending_payment_reply` returns `False`.

8. **`test_handle_payment_result_test_confirmed`**: Mock payment `producer='admin_chat', is_test=True, status='confirmed'`. Assert `request_payment(is_test=False, amount_token=...)` called and `send_confirmation_request` called. Assert intent updated to `awaiting_confirmation`.

9. **`test_handle_payment_result_test_failed`**: Mock `is_test=True, status='failed'`. Assert no final payment created. Assert intent updated to `failed`.

10. **`test_confirmation_reply`**: Mock active intent `status='awaiting_confirmation'`. Mock classification as `positive_confirmation`. Assert `confirm_payment` called on final payment. Assert intent updated to `confirmed`.

11. **`test_concurrent_intents_same_channel`**: Two intents for different recipients in same channel. Simulate reply from one. Assert only correct intent matched.

12. **`test_request_payment_amount_token`**: Test new `amount_token` parameter in PaymentService. Assert payment created with direct SOL amount, `amount_usd=None`, `token_price_usd=None`.

13. **`test_startup_reconciliation`**: Create active intent in DB. Mock channel history with a matching wallet reply. Call `_reconcile_active_intents`. Assert intent advanced.

### Step 14: Run full test suite
**Scope:** Small

1. `python -c "from src.features.admin_chat import tools"` — classification assertion.
2. `python -m pytest tests/test_admin_payments.py -x` — new tests.
3. `python -m pytest tests/test_social_route_tools.py tests/test_scheduler.py -x` — existing tests.
4. `python -m pytest tests/ -x` — full suite.

## Execution Order
1. Steps 1-3 (DB schema, CRUD methods, PaymentService extension) — foundation, no behavioral changes.
2. Steps 4-6 (tool schema, context injection, executor + dispatcher) — creates intents.
3. Steps 7-9 (interceptor, flow helpers, terminal callback) — processes intents.
4. Step 10 (recovery) — restart safety.
5. Steps 11-14 (prompt, tests, validation) — proves it all works.

## Validation Order
1. `python -c "from src.features.admin_chat import tools"` — classification assertion.
2. `python -m pytest tests/test_admin_payments.py -x` — new feature tests.
3. `python -m pytest tests/test_social_route_tools.py tests/test_scheduler.py -x` — existing tests unbroken.
4. `python -m pytest tests/ -x` — full suite.
