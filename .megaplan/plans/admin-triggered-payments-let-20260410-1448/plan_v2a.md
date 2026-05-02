# Implementation Plan: Admin-Triggered Payments

## Overview

Add an `initiate_payment` admin chat tool that creates a payment to a Banodoco user. The flow has two paths depending on whether the recipient already has a Solana wallet on file:

**Wallet on file →** Immediately: upsert wallet → test payment → auto-confirm test → (async callback) on test confirmed → create final payment → post in-channel confirmation button → recipient clicks → payout executes.

**No wallet on file →** Create a pending payment intent, ping the recipient in the originating channel asking them to reply with their Solana wallet address. A dedicated `on_message` interceptor in `AdminChatCog` catches the reply (bypassing `_can_user_message_bot`), validates the wallet, and starts the same test→final flow. The interceptor uses an LLM classification step to handle ambiguous replies (wallet vs. confirmation vs. nonsense) and fail-closes any suspicious signals by tagging the admin.

### Key constraints
- **Fail-closed everywhere**: missing wallet → abort, bad wallet → reject, test failed → abort, no confirmation → no payout, ambiguous reply → notify admin.
- **Revision lock**: Multiple recipients can have concurrent intents in the same channel. Intent matching is deterministic: `(guild_id, channel_id, recipient_user_id, status='active')` — identity-first, then LLM classification of the reply's semantics. Any ambiguity or suspicion tags the admin and halts.
- **Producer name `'admin_chat'`**: `_candidate_producer_cog_names('admin_chat')` produces `['AdminChatCog', ...]` — auto-discovered by `PaymentCog` with zero changes.
- **In-memory intent store**: Pending intents live on `AdminChatCog`. Lost on restart; admin re-initiates. No DB migration.
- **Guild-channel only**: Admin must invoke from a guild channel (not DM) so there's a channel to ping the recipient in and `guild_id` is available.

## Phase 1: Pending Payment Intents and Wallet Reply Handler

### Step 1: Add intent store and wallet-reply interceptor to `AdminChatCog` (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium

1. **Add imports** at top of file: `is_valid_solana_address` from `src.features.grants.solana_client`, `time`.

2. **Add to `__init__`** (~line 34):
   ```python
   # Pending admin payment intents: {(guild_id, channel_id, recipient_user_id): intent_dict}
   self._pending_payment_intents: dict[tuple[int, int, int], dict] = {}
   # Race-condition guard for intent processing
   self._processing_intents: set[tuple[int, int, int]] = set()
   ```

3. **Add `register_payment_intent` method** (public, called by the tool executor):
   ```python
   def register_payment_intent(self, guild_id, channel_id, recipient_user_id, amount_usd, reason, producer_ref):
       key = (int(guild_id), int(channel_id), int(recipient_user_id))
       self._pending_payment_intents[key] = {
           'guild_id': int(guild_id),
           'channel_id': int(channel_id),
           'recipient_user_id': int(recipient_user_id),
           'amount_usd': float(amount_usd),
           'reason': reason,
           'producer_ref': producer_ref,
           'created_at': time.time(),
           'status': 'awaiting_wallet',
       }
       return key
   ```

4. **Add `_check_pending_payment_reply` method** — intercepts messages from users who have an active intent. Insert call at the **very top** of `on_message` (before `_is_directed_at_bot()` at line 201):
   ```python
   async def on_message(self, message: discord.Message):
       # Intercept pending payment wallet/confirmation replies (bypasses bot-directed + permission gates)
       if await self._check_pending_payment_reply(message):
           return
       # ... existing code ...
   ```
   
   The `_check_pending_payment_reply` method:
   - Returns `False` immediately if `message.author.bot` or `message.guild is None` (fast path).
   - Builds the intent key `(message.guild.id, message.channel.id, message.author.id)`.
   - Looks up in `_pending_payment_intents`. If no match → return `False` (fast path, O(1) dict lookup).
   - If match found, checks `intent['status']`:
     - **`'awaiting_wallet'`**: Validate with `is_valid_solana_address(message.content.strip())`. If valid → proceed to start payment flow. If invalid → reply with validation error, return `True`.
     - **`'awaiting_confirmation'`**: Use a lightweight LLM call (same Anthropic client already available on `self.agent`) to classify the message as one of: `wallet_provided`, `positive_confirmation`, `negative/declined`, `ambiguous`, `suspicious`. Classification prompt is short and deterministic — it receives only the single message text plus the intent context (amount, reason). Based on result:
       - `positive_confirmation` → confirm the final payment via `PaymentService.confirm_payment`, return `True`.
       - `negative/declined` → cancel the payment, remove intent, notify channel, return `True`.
       - `ambiguous` / `suspicious` / anything else → **fail closed**: do NOT advance, tag admin with `<@{admin_user_id}>` in channel, return `True`.
   - Race-condition guard: skip if key is in `_processing_intents`, add before processing, remove in `finally`.
   - On any exception, send error message, tag admin, return `True`.

5. **Add `_start_admin_payment_flow` method** — shared by both immediate path (wallet on file) and deferred path (wallet from reply). Mirrors `grants_cog.py:550-609`:
   - `db_handler.upsert_wallet(guild_id, recipient_user_id, 'solana', wallet, metadata={'producer': 'admin_chat'})`. Fail if None.
   - Resolve destinations: `server_config.resolve_payment_destinations(guild_id, intent['channel_id'], 'admin_chat')` with fallback to `{'route_key': None, 'confirm_channel_id': intent['channel_id'], 'confirm_thread_id': None, 'notify_channel_id': intent['channel_id'], 'notify_thread_id': None}` (mirroring `grants_cog.py:619-625`).
   - `bot.payment_service.request_payment(producer='admin_chat', producer_ref=intent['producer_ref'], guild_id=guild_id, recipient_wallet=wallet, chain='solana', provider='solana', is_test=True, confirm_channel_id=..., notify_channel_id=..., recipient_discord_id=recipient_user_id, wallet_id=wallet_record['wallet_id'], metadata={'reason': intent['reason'], 'amount_usd': intent['amount_usd']})`. Fail if None.
   - `bot.payment_service.confirm_payment(test_payment['payment_id'], guild_id=guild_id, confirmed_by='auto', confirmed_by_user_id=recipient_user_id)`. Fail if None.
   - Update intent status to `'test_in_progress'` (or remove it — the terminal callback takes over from here).
   - Send channel message: "Wallet registered. I've sent a small test payment to verify it — once that lands, I'll send the full payment confirmation."
   - On any failure: send error to channel, tag admin, remove intent.

### Step 2: Add `handle_payment_result` to `AdminChatCog` (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium — mirrors `grants_cog.py:640-714`

1. **Add** `async def handle_payment_result(self, payment)`:
   - Guard: `if str(payment.get('producer') or '').strip().lower() != 'admin_chat': return`
   - Branch on `payment.get('is_test')`:
   
   **Test payment — confirmed:**
   - Read `amount_usd` from `payment.get('metadata', {}).get('amount_usd')`. If missing → log error, notify channel, abort.
   - Create final payment: `bot.payment_service.request_payment(producer='admin_chat', producer_ref=payment['producer_ref'], guild_id=payment['guild_id'], ..., is_test=False, amount_usd=amount_usd, ...)`. Reuse `confirm_channel_id`, `notify_channel_id`, `recipient_discord_id`, `wallet_id`, `route_key` from the test payment dict.
   - If creation fails → notify channel, tag admin.
   - If success and status == `'pending_confirmation'` → call `bot.get_cog('PaymentCog').send_confirmation_request(final_payment['payment_id'])` to post the in-channel confirmation button.
   - Update any remaining intent to `'awaiting_confirmation'` (keyed by `producer_ref` → look up in `_pending_payment_intents` by matching `producer_ref`).
   - Send channel message: "Test payment confirmed. Please confirm the full payout by clicking the button above."
   
   **Test payment — failed/other:**
   - Notify channel: "The wallet verification payment ended in `{status}`. <@{admin_id}> will review."
   - Remove intent. Do NOT create final payment (fail-closed).
   
   **Final payment — confirmed:**
   - Post to notify channel: "**Payment sent!** Amount: {amount_token:.4f} SOL · Wallet: `{redacted}` · [View on Explorer]({url})"
   - Remove intent.
   
   **Final payment — failed/other:**
   - Notify channel: "The payment ended in `{status}`. <@{admin_id}> will review."

2. **Resolve channel** from payment's `notify_channel_id` / `notify_thread_id` using `bot.get_channel()` or `bot.fetch_channel()`.

3. **Admin mention**: Reuse `self.admin_user_id` (already stored in `__init__`) → `f"<@{self.admin_user_id}>"`.

## Phase 2: Tool Definition and Dispatcher

### Step 3: Add `initiate_payment` tool schema (`src/features/admin_chat/tools.py:~577`)
**Scope:** Small

1. **Add** new tool dict to `TOOLS` list after `cancel_payment` (~line 577):
   ```python
   {
       "name": "initiate_payment",
       "description": "Initiate a Solana payment to a Discord user. If the user already has a wallet on file, starts the verification test payment immediately. If not, pings the recipient in this channel to collect their wallet address first. Must be called from a guild channel.",
       "input_schema": {
           "type": "object",
           "properties": {
               "recipient_user_id": {
                   "type": "string",
                   "description": "Discord user ID of the payment recipient."
               },
               "amount_usd": {
                   "type": "number",
                   "description": "Payment amount in USD."
               },
               "reason": {
                   "type": "string",
                   "description": "Optional memo/reason for the payment."
               }
           },
           "required": ["recipient_user_id", "amount_usd"]
       }
   }
   ```

2. **Add** `"initiate_payment"` to `ADMIN_ONLY_TOOLS` set (~line 886). This satisfies the assertion at line 916.

### Step 4: Implement `execute_initiate_payment` (`src/features/admin_chat/tools.py`)
**Scope:** Medium

1. **Implement** `async def execute_initiate_payment(bot, db_handler, tool_input)`:
   
   **Input validation (fail-closed):**
   - Parse `recipient_user_id` to int. Return error if invalid.
   - Validate `amount_usd > 0`. Return error if not.
   - Resolve `guild_id` from `tool_input.get('guild_id')`. Return error if missing.
   - Resolve `source_channel_id` from `tool_input.get('source_channel_id')`. Return error if missing: `"This tool must be called from a guild channel, not a DM."`
   - Check `getattr(bot, 'payment_service', None)`. Return error if None.
   
   **Generate producer_ref:** `f"{guild_id}_{recipient_user_id}_{int(time.time())}"` — unique per payment, satisfies the `(producer, producer_ref, is_test)` unique index.
   
   **Check existing wallet:** `db_handler.get_wallet(guild_id, recipient_user_id, 'solana')`
   
   **Path A — wallet on file:**
   - Get cog: `cog = bot.get_cog('AdminChatCog')`. Return error if None.
   - Build intent dict with all params.
   - Get channel: `channel = bot.get_channel(source_channel_id)`. Return error if None.
   - Call `cog._start_admin_payment_flow(channel, intent, wallet_record['wallet_address'])`.
   - Return `{"success": True, "status": "test_payment_queued", "message": "Wallet on file. Test payment queued — I'll send the full payment confirmation once the test lands."}`.
   
   **Path B — no wallet:**
   - Get cog: `cog = bot.get_cog('AdminChatCog')`. Return error if None.
   - Call `cog.register_payment_intent(guild_id, source_channel_id, recipient_user_id, amount_usd, reason, producer_ref)`.
   - Get channel and send ping: `"<@{recipient_user_id}> — a payment of ${amount_usd:.2f} has been initiated for you. Please reply in this channel with your **Solana wallet address** to proceed."`
   - Return `{"success": True, "status": "awaiting_wallet", "message": "No wallet on file. I've pinged the recipient in this channel to collect their wallet address."}`.
   
   **On any exception:** Return `{"success": False, "error": str(e)}`.

### Step 5: Inject `source_channel_id` into tool_input and wire dispatcher
**Scope:** Small

1. **In `agent.py:~409`** — add `source_channel_id` injection alongside the existing `guild_id` injection:
   ```python
   if channel_context and channel_context.get('channel_id') and 'source_channel_id' not in tool_input:
       if not isinstance(tool_input, dict):
           tool_input = dict(tool_input)
       try:
           tool_input['source_channel_id'] = int(channel_context['channel_id'])
       except (TypeError, ValueError):
           pass
   ```
   This uses the key `source_channel_id` which no existing tool uses, so it's safe for all tool calls.

2. **In `tools.py:~2986`** — add dispatcher case after `cancel_payment`:
   ```python
   elif tool_name == "initiate_payment":
       return await execute_initiate_payment(bot, db_handler, trusted_tool_input)
   ```

## Phase 3: System Prompt and Tests

### Step 6: Update system prompt (`src/features/admin_chat/agent.py:~61`)
**Scope:** Small

1. **Add** after the `cancel_payment` line (~line 61) in the SYSTEM_PROMPT "Doing things" section:
   ```
   - initiate_payment(recipient_user_id, amount_usd, reason?) — start a Solana payment to a user. If the user has a wallet on file, queues a test payment immediately. If not, pings them in this channel to collect their wallet first. After test confirms, posts a confirmation button for the recipient. Must be called from a guild channel.
   ```

### Step 7: Update existing test classification (`tests/test_social_route_tools.py:~264`)
**Scope:** Small

1. **Add** `"initiate_payment"` to the expected admin-only tools set in `test_route_tools_are_admin_only` (~line 264). The existing test uses `.issubset()`, so adding to the expected set is forward-compatible.

2. **Add** `"initiate_payment"` assertion to `test_agent_prompt_mentions_payment_tools` (~line 413):
   ```python
   assert "initiate_payment" in admin_agent.SYSTEM_PROMPT
   ```

### Step 8: Add new test file (`tests/test_admin_payments.py`)
**Scope:** Medium

Create a focused test file covering the critical paths. Mock `PaymentService`, `db_handler`, `bot`, and `AdminChatCog` as needed (follow patterns from `test_scheduler.py`).

1. **`test_initiate_payment_wallet_on_file`**: Mock `db_handler.get_wallet` returning a wallet. Assert `request_payment(is_test=True)` and `confirm_payment` are called. Assert return has `status='test_payment_queued'`.

2. **`test_initiate_payment_no_wallet`**: Mock `get_wallet` returning None. Assert `register_payment_intent` is called on the cog. Assert a ping message is sent to the channel. Assert return has `status='awaiting_wallet'`.

3. **`test_initiate_payment_validation_failures`**: Bad `recipient_user_id` (non-numeric), `amount_usd <= 0`, missing `guild_id`, missing `source_channel_id`. Each returns `success=False`.

4. **`test_wallet_reply_valid`**: Create a pending intent with `status='awaiting_wallet'`. Simulate a message from the recipient containing a valid Solana address. Assert `_start_admin_payment_flow` is called and intent is consumed.

5. **`test_wallet_reply_invalid`**: Simulate bad address. Assert error reply is sent and intent is NOT consumed.

6. **`test_wallet_reply_no_intent`**: Simulate message from user with no pending intent. Assert `_check_pending_payment_reply` returns `False` (fast path).

7. **`test_handle_payment_result_test_confirmed`**: Mock payment with `producer='admin_chat', is_test=True, status='confirmed'`. Assert `request_payment(is_test=False)` and `send_confirmation_request` are called.

8. **`test_handle_payment_result_test_failed`**: Mock payment with `is_test=True, status='failed'`. Assert `request_payment` is NOT called (fail-closed).

9. **`test_concurrent_intents_same_channel`**: Register two intents for different recipients in the same channel. Simulate wallet reply from one. Assert only the correct intent is matched and consumed.

### Step 9: Run full test suite
**Scope:** Small

1. `python -c "from src.features.admin_chat import tools"` — classification assertion.
2. `python -m pytest tests/test_admin_payments.py -x` — new tests.
3. `python -m pytest tests/test_social_route_tools.py -x` — existing classification.
4. `python -m pytest tests/test_scheduler.py -x` — scheduler unaffected.
5. `python -m pytest tests/ -x` — full suite green.

## Execution Order
1. Step 1 (intent store + interceptor) — foundation for everything else.
2. Step 2 (handle_payment_result) — callback for test→final escalation.
3. Steps 3-5 (tool schema, executor, dispatcher + context injection) — wires the feature end-to-end.
4. Step 6 (system prompt) — documents the new tool for Claude.
5. Steps 7-9 (tests) — proves everything works.

## Validation Order
1. Import check: `python -c "from src.features.admin_chat import tools"` (classification assertion).
2. New tests: `python -m pytest tests/test_admin_payments.py -x`.
3. Existing tests: `python -m pytest tests/test_social_route_tools.py tests/test_scheduler.py -x`.
4. Full suite: `python -m pytest tests/ -x`.
