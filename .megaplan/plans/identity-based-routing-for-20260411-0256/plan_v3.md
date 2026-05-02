# Implementation Plan: Identity-Based Admin Chat Routing + Admin-DM Approval Gate (Revision 3)

## Overview
Refactor `admin_chat` from content-based classification to strict identity-based routing (admin → agent, active-intent recipient → deterministic state machine, else → fallthrough). Gate recipient `verified_at` on a text-ack of the on-chain test. Replace `PaymentConfirmView` with a DM-only `AdminApprovalView` for `producer='admin_chat'` NEW-flow real payments, authorized via the **already-existing** `PaymentActorKind.ADMIN_DM`. Add 5 new agent tools (`query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user`) plus `resolve_admin_intent` as the minimum operator surface. Auto-skip the test for verified wallets. Add a 24h timeout sweep. Grants + all hardening/cleanup/polish invariants untouched.

**This revision is a surgical touch-up of Revision 2**, applying six targeted fixes from iteration-2 gate feedback without destabilizing Revision 2's resolved flags:
1. **`manual_review` stays BLOCKING** — revert the Rev-2 unique-index exclusion and active-intent exclusion. The brief only asked for escalation + admin DM, not "non-blocking". Fail-closed is preserved; admin clears via `resolve_admin_intent(cancelled)`.
2. **`resolve_admin_intent` accepts `cancelled` only** — `completed` is reserved for `handle_payment_result` after on-chain confirm. Protects audit semantics.
3. **Verified-wallet fast path is crash-safe**: create the `payment_requests` row FIRST, then write the intent with both `status='awaiting_admin_approval'` AND `final_payment_id` in a single update. No intent is ever persisted in `awaiting_admin_approval` without a `final_payment_id`.
4. **Restart-recovery filter is state-aware**: exclude only admin_chat real payments whose intent is in `awaiting_admin_approval` (new flow). Legacy admin_chat rows still in `awaiting_confirmation`/`pending_confirmation` keep their `PaymentConfirmView` across restart.
5. **`upsert_wallet_for_user` executor explicitly DMs the admin** via `bot.fetch_user(ADMIN_USER_ID).send(...)` instead of relying on the agent reply (which may land in a public channel).
6. **`resolve_admin_intent` is added to `_ADMIN_IDENTITY_INJECTED_TOOLS`** in `agent.py`.

All twelve open flags close with these six fixes. Resolved flags from Rev-2 (producer_flows, restart filter existence, identity routing, wallet parser, queued-not-sent, receipt_prompt_message_id, verified-wallet-skips-awaiting_test, ADMIN_ONLY_TOOLS exhaustiveness, initiate_batch_payment / upsert_wallet_for_user caller injection) are preserved verbatim.

Evidence carried from Rev-2 (unchanged):
- `PaymentActorKind.ADMIN_DM` exists at `src/features/payments/payment_service.py:30`; `_authorize_actor` at `:539-546` enforces `actor_id == ADMIN_USER_ID` for it.
- `PRODUCER_FLOWS['admin_chat'].real_confirmed_by` at `src/features/payments/producer_flows.py:21-29` lacks `ADMIN_DM` — must add.
- `PaymentUICog._register_pending_confirmation_views` at `src/features/payments/payment_ui_cog.py:110-130` re-registers views for every `pending_confirmation` payment via `db_handler.get_pending_confirmation_payments` at `:2479-2503`.
- `admin_chat_cog.on_message` at `:790-817` currently guards the agent path with `_is_directed_at_bot` — must be dropped for `ADMIN_USER_ID`.
- `ADMIN_ONLY_TOOLS` at `src/features/admin_chat/tools.py:908-936` is asserted exhaustive at `:938-940`.
- `agent.py:431-441` injects `admin_user_id` for `initiate_payment`; must generalize.
- `confirm_payment` at `payment_service.py:550-581` only transitions `pending_confirmation → queued`; click handler must say "queued".
- `handle_payment_result` at `admin_chat_cog.py:416-440` is the sole authoritative source of the "Payment sent." channel post.

Scope boundaries (unchanged): do NOT touch `solana_client.py`/`solana_provider.py`, `grants_cog.py`, `PaymentService` rebroadcast/fee logic, hardening/cleanup/polish invariants. `MEMBER_SYSTEM_PROMPT`, `_can_user_message_bot`, `_is_directed_at_bot` stay defined as dead code. **`has_active_payment_or_intent` at `src/common/db_handler.py:1575-1601` is NOT modified** — because `manual_review` is kept in the active set (Rev-3 fix #1), the existing helper remains consistent with `get_active_intent_for_recipient` and no cross-caller drift exists.

## Phase 1: Schema + DB Helpers

### Step 1: Extend `admin_payment_intents` schema (`sql/admin_payment_intents.sql`)
**Scope:** Small
1. **Add** idempotent `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT ...` extending the status CHECK to include `awaiting_test_receipt_confirmation` (NEW), `awaiting_admin_approval` (NEW), and `manual_review` (NEW) alongside the existing statuses and `awaiting_confirmation` (kept for back-compat of in-flight legacy rows).
2. **Add** `ambiguous_reply_count integer not null default 0` via `add column if not exists`.
3. **Add** `receipt_prompt_message_id bigint` via `add column if not exists` — separate from `prompt_message_id` so safe-delete can clear both prompts.
4. **DO NOT modify the unique partial index.** `manual_review` stays in the blocking (non-excluded) set alongside `awaiting_wallet`, `awaiting_test`, etc. The existing exclusion list `('completed','failed','cancelled')` is correct. Resolves **FLAG-010 / scope-1**: `manual_review` is blocking so a negative-ack or 24h-timeout case cannot spawn a fresh payout intent until the admin explicitly clears it via `resolve_admin_intent(cancelled)`.

### Step 2: DB helpers (`src/common/db_handler.py`)
**Scope:** Medium
1. **Add** `create_admin_payment_intents_batch(records: List[Dict], guild_id: int) -> Optional[List[Dict]]` via `self.supabase.table('admin_payment_intents').insert([...]).execute()`. All-or-none; `None` on any exception.
2. **Add** `list_intents_by_status(guild_id: int, status: str) -> List[Dict]` for the persistent-view registration pass.
3. **Add** `list_stale_test_receipt_intents(cutoff_iso: str) -> List[Dict]` — cross-guild scan for the 24h sweep (`status='awaiting_test_receipt_confirmation' AND updated_at < cutoff`).
4. **Add** `increment_intent_ambiguous_reply_count(intent_id: str, guild_id: int) -> Optional[int]` — read-then-write, returns new value.
5. **Add** `get_pending_confirmation_admin_chat_intents_by_payment(payment_ids: List[str]) -> Dict[str, str]` — returns `{final_payment_id: intent_status}` for admin_chat intents referencing the given payment ids. Used by the restart-recovery filter (Step 4) to decide whether a `pending_confirmation` admin_chat payment is NEW-flow (intent status `awaiting_admin_approval`) or LEGACY-flow (intent status `awaiting_confirmation` / anything else).
6. **Do NOT** modify `get_active_intent_for_recipient`, `list_active_intents`, or `has_active_payment_or_intent`. `manual_review` stays in the active set for ALL three — keeps semantic consistency across initiation and wallet-update code paths. Resolves **scope-2 / all_locations-2 / callers-3 / FLAG-010**.

## Phase 2: Payment Policy + Restart Recovery Filter (Interlocked with Phase 4)

### Step 3: Authorize `ADMIN_DM` for admin_chat real payments (`src/features/payments/producer_flows.py`)
**Scope:** Small
1. **Add** `PaymentActorKind.ADMIN_DM` to `admin_chat.real_confirmed_by` at `src/features/payments/producer_flows.py:21-29`. Resulting set: `{RECIPIENT_CLICK, RECIPIENT_MESSAGE, ADMIN_DM}` — `RECIPIENT_CLICK`/`RECIPIENT_MESSAGE` stay as a safety-belt for legacy in-flight rows during deployment. `_authorize_actor` at `payment_service.py:539-546` already enforces `actor_id == ADMIN_USER_ID` for `ADMIN_DM`.
2. **Do NOT** add a new `PaymentActorKind` enum value; reuse existing `ADMIN_DM`.
3. **Do NOT** touch `grants.real_confirmed_by`.

### Step 4: State-aware restart-recovery filter for `PaymentConfirmView` (`src/features/payments/payment_ui_cog.py`)
**Scope:** Small
1. **Modify** `_register_pending_confirmation_views` at `src/features/payments/payment_ui_cog.py:113-130` to filter selectively — NOT a blanket producer filter. Pseudocode:
   ```python
   pending = self.payment_service.get_pending_confirmation_payments(guild_ids=...)
   if not pending:
       return
   admin_chat_real_ids = [p['payment_id'] for p in pending
                          if p.get('producer') == 'admin_chat' and not p.get('is_test')]
   intent_status_by_payment = {}
   if admin_chat_real_ids:
       intent_status_by_payment = self.db_handler.get_pending_confirmation_admin_chat_intents_by_payment(
           admin_chat_real_ids,
       )
   for payment in pending:
       payment_id = payment.get('payment_id')
       if not payment_id:
           continue
       # Skip ONLY new-flow admin_chat real payments whose intent is awaiting_admin_approval —
       # those are re-registered by _register_pending_admin_approval_views (Step 10.3).
       # Legacy admin_chat real payments in old awaiting_confirmation flow STAY here so
       # their PaymentConfirmView survives restart.
       if payment.get('producer') == 'admin_chat' and not payment.get('is_test'):
           intent_status = intent_status_by_payment.get(payment_id)
           if intent_status == 'awaiting_admin_approval':
               continue
       self.bot.add_view(PaymentConfirmView(self, payment_id))
   ```
2. **Test coverage:** two cases — (a) admin_chat real payment whose intent is `awaiting_admin_approval` → `PaymentConfirmView` NOT re-registered; (b) admin_chat real payment whose intent is `awaiting_confirmation` (legacy) → `PaymentConfirmView` IS re-registered. Resolves **FLAG-001, FLAG-007, issue_hints-2, all_locations-1**.
3. **Grants:** unaffected — the new filter only applies when `producer == 'admin_chat'`.

## Phase 3: Parsers + Identity Router

### Step 5: Deterministic parsers (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `_parse_wallet_from_text(content: str) -> Optional[str]` with tolerant token extraction:
   ```python
   _WALLET_DELIMITERS = re.compile(r"[\s`<>\"'(),\[\]{}*_~|]+")
   def _parse_wallet_from_text(content: str) -> Optional[str]:
       if not content:
           return None
       for token in _WALLET_DELIMITERS.split(content.strip()):
           token = token.strip('.,;:!?')
           if token and is_valid_solana_address(token):
               return token
       return None
   ```
2. **Add** `_classify_confirmation(content: str) -> Literal['positive','negative','ambiguous']`:
   - Positive keywords: `{'confirmed','received','got it','yes','yep','confirm','👍'}`
   - Negative keywords: `{'no','didnt',"didn't",'not received','missing','nothing'}`
   - Case-insensitive whole-word matching (`\b{kw}\b`); multi-word phrases via substring with word-boundary anchors; emoji `👍` via substring. Ambiguous = neither matched OR both matched.
3. **Unit tests** for both parsers.

### Step 6: Identity router + drop content gating for admin (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Rewrite** `on_message` at `:790-` as a strict three-way switch on `message.author.id`:
   ```python
   if message.author.bot:
       return
   author_id = message.author.id
   if self._is_admin(author_id):
       await self._handle_admin_message(message)
       return
   if message.guild is not None:
       intent = self.db_handler.get_active_intent_for_recipient(
           message.guild.id, message.channel.id, author_id,
       )
       if intent:
           await self._handle_pending_recipient_message(message, intent)
       return
   ```
2. **Extract** `_handle_admin_message(message)` from `:797-end` with `_is_directed_at_bot` gate and `_can_user_message_bot` branch REMOVED. Admin messages reach the agent unconditionally. Keep: abort handling, busy/pending queue, admin rate-limit exemption, channel context.
3. **Delete** the `_check_pending_payment_reply` call from `on_message`. Leave the method defined until Step 18 deletes it.
4. **Document:** `_can_user_message_bot` and the approved-member flow are dead under identity routing.

### Step 7: State-machine dispatch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_handle_pending_recipient_message(message, intent)` — dispatch on `intent['status']`:
   - `awaiting_wallet` → `_parse_wallet_from_text(...)`. If found → `_handle_wallet_received(message, intent, wallet)`. If not → silent return.
   - `awaiting_test_receipt_confirmation` → `_classify_confirmation(...)` → dispatch to the three `_handle_test_receipt_*` helpers (Step 9).
   - Other states (`awaiting_test`, `awaiting_admin_approval`, `manual_review`, `confirmed`) → silent return. Note: `manual_review` IS in the active set again (Rev-3 revert), so a message from a recipient whose intent is in `manual_review` will land here and be silently ignored — which is the correct fail-closed behavior. The admin has been tagged and must resolve via `resolve_admin_intent(cancelled)` before a new intent can be created.
2. **Reuse** the `_processing_intents` dedupe set.
3. **Unit-test invariant:** patch `self._classifier_client = Mock(side_effect=AssertionError)` — no state-machine path may call Anthropic.

## Phase 4: Flow Rewiring + Admin Approval Gate

### Step 8: Rewire `handle_payment_result` test-success branch (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Replace** the test-success branch at `:332-414`. On `is_test=True + status=='confirmed'`:
   - Transition intent to `awaiting_test_receipt_confirmation`.
   - Post the receipt prompt in the source channel: `"<@{recipient}> test payment confirmed on-chain. Please check your wallet and reply 'confirmed' once you see {amount} SOL."`
   - Store returned message id in `receipt_prompt_message_id` (new column). Do NOT overwrite `prompt_message_id`.
   - DO NOT create the real payment here.
   - DO NOT call `send_confirmation_request`. Delete the call at `:393`.
2. **Non-confirmed test status:** unchanged.

### Step 9: Test-receipt handlers + crash-safe gate helper (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Medium
1. **Add** `_create_real_payment_and_gate(channel, intent, wallet_record, amount_sol) -> Optional[Dict]` — the single shared helper used by both the test-receipt positive handler (Step 9.2) AND the auto-skip verified-wallet paths (Steps 11 + 13). **Crash-safe ordering** (resolves **FLAG-008, correctness-1, callers-2**):
   ```
   Step A: Call payment_service.request_payment(is_test=False, producer='admin_chat',
           recipient_wallet=wallet_record['wallet_address'],
           metadata={'intent_id': intent['intent_id']},
           amount_token=amount_sol, ...) → returns row with status='pending_confirmation'
           and a populated payment_id.
   Step B: In ONE update call, set both intent.final_payment_id = new_payment['payment_id']
           AND intent.status = 'awaiting_admin_approval'.
   Step C: Invoke bot.payment_ui_cog._send_admin_approval_dm(new_payment) — registers the
           persistent view and DMs the admin.
   ```
   **Crash-safety argument:** if the process dies between A and B, the `payment_requests` row exists in `pending_confirmation` but no intent points at it. On restart, `_register_pending_confirmation_views` (Step 4) sees `producer='admin_chat'`, `is_test=False`, and `get_pending_confirmation_admin_chat_intents_by_payment` returns nothing for this payment id — the filter does NOT skip, so `PaymentConfirmView` gets re-registered as a safety net. The admin sees the row via `/payment-resolve` or the existing recipient button and can resolve it. No intent is ever persisted with `status='awaiting_admin_approval'` + `final_payment_id=NULL`. If the process dies between B and C, the intent and payment are both consistent; `_register_pending_admin_approval_views` on restart re-registers the `AdminApprovalView` for the payment id (Step 10.3), and the admin sees the row the next time they check their DMs.
   - On any failure at Step A or B, transition intent to `failed` and `_notify_admin_review`. Return `None`.
2. **Add** `_handle_test_receipt_positive(message, intent)`:
   - `wallet_record = db_handler.get_wallet_by_id(intent['wallet_id'], guild_id=intent['guild_id'])`.
   - `db_handler.mark_wallet_verified(intent['wallet_id'], guild_id=intent['guild_id'])`.
   - Call `_create_real_payment_and_gate(...)`.
   - Post thread ack: `"<@{recipient}> thanks — admin approval requested."`
3. **Add** `_handle_test_receipt_negative(message, intent)`:
   - Fetch test payment for `tx_signature` + wallet.
   - Post admin-tagged message in the intent channel with full context.
   - Transition intent to `status='manual_review'`.
   - DM admin via `_notify_intent_admin`.
4. **Add** `_handle_test_receipt_ambiguous(message, intent)`:
   - `count = db_handler.increment_intent_ambiguous_reply_count(...)`.
   - `count == 1` → silent return.
   - `count >= 2` → negative-handler escalation.

### Step 10: `AdminApprovalView` + admin DM helper (`src/features/payments/payment_ui_cog.py`)
**Scope:** Large
1. **Add** `AdminApprovalView(discord.ui.View)` next to `PaymentConfirmView` at `:19`:
   - `timeout=None`, single `Approve Payment` button with `custom_id=f"payment_admin_approve:{payment_id}"` (grep-verified: no collision with `payment_confirm:`).
   - Callback:
     - `await interaction.response.defer(ephemeral=True)`.
     - `int(interaction.user.id) != int(os.getenv('ADMIN_USER_ID'))` → ephemeral denial, return.
     - Fetch payment via `db_handler.get_payment_request(payment_id)`.
     - `payment_service.confirm_payment(payment_id, guild_id=payment['guild_id'], actor=PaymentActor(PaymentActorKind.ADMIN_DM, interaction.user.id))`.
     - On success (status `queued`): edit the DM to `"✅ approved — queued for sending"`, disable the button; send follow-up DM `"Payment `{payment_id}` queued. You'll see the 'sent' confirmation in the source channel once the worker processes it."`; post in source channel `"<@{recipient}> your payment has been approved by the admin and queued for sending."`; `safe_delete_messages(channel, [intent['prompt_message_id'], intent['receipt_prompt_message_id']], logger=logger)`.
     - **Do NOT** post "Payment sent to @user" here — that fires from existing `handle_payment_result` at `admin_chat_cog.py:416-440`.
2. **Add** `PaymentUICog._send_admin_approval_dm(payment) -> Optional[discord.Message]`:
   - Build content: amount, recipient mention, redacted wallet, intent_id, jump link.
   - `admin = await self.bot.fetch_user(int(os.getenv('ADMIN_USER_ID')))`.
   - `view = AdminApprovalView(self, payment['payment_id']); self.bot.add_view(view); await admin.send(content, view=view)`.
   - `discord.Forbidden` / rate limit → ERROR log with `payment_id` + `intent_id`, intent stays in `awaiting_admin_approval`, return `None`.
3. **Add** `PaymentUICog._register_pending_admin_approval_views()` called from `cog_load`:
   - For each writable guild, `db_handler.list_intents_by_status(guild_id, 'awaiting_admin_approval')`.
   - For each intent with `final_payment_id`, `self.bot.add_view(AdminApprovalView(self, intent['final_payment_id']))`.
   - **Note:** the crash-safety ordering in Step 9.1 guarantees that any intent in `awaiting_admin_approval` already has `final_payment_id` set. The belt-and-braces check here is just defensive programming.
4. **Grants:** Step 4 filter is state-aware and narrow — grants stays intact.

## Phase 5: New Agent Tools + Tool Infrastructure

### Step 11: Auto-skip verified-wallet path in `execute_initiate_payment` (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Modify** `execute_initiate_payment` at `:2543-2595`:
   - Verified wallet (`wallet_record.verified_at is not None`): create the intent with `status='awaiting_wallet'` as a transient placeholder AND immediately call `cog._create_real_payment_and_gate(channel, intent, wallet_record, amount_sol)`. That helper (Step 9.1) creates the `payment_requests` row first, then updates the intent to `awaiting_admin_approval` + `final_payment_id` in one write. No intent is ever in `awaiting_admin_approval` without a `final_payment_id`.
   - **Alternative simpler wording:** pre-create the intent row at `status='awaiting_wallet'`, then let Step 9.1 run. At no point does the intent transition to `awaiting_admin_approval` without a concurrent `final_payment_id` write. The final update is a single `db_handler.update_admin_payment_intent(intent_id, {'status': 'awaiting_admin_approval', 'final_payment_id': payment_id}, guild_id)` call.
   - Unverified wallet path: unchanged — intent starts in `awaiting_wallet`, wallet prompt posted.
2. **Do not** modify `_start_admin_payment_flow` — still the test-only entry point for unverified paths.

### Step 12: Read-only query tools (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** schemas near `list_payments`/`get_payment_status` at `:455-559`:
   - `query_payment_state(payment_id?: str, user_id?: str)` — exactly one required.
   - `query_wallet_state(user_id: str)` — required.
   - `list_recent_payments(producer?: str, user_id?: str, limit?: int=20)`.
2. **Add** executors — thin wrappers over `db_handler.get_payment_request`/`list_payment_requests`/`list_wallets` with existing `_redact_payment_row`/`_redact_wallet_row`.
3. **Wire** dispatch in the `execute_tool` switch near `:3184-3199`.
4. **Pop-and-warn guard** at the top of each executor.

### Step 13: `initiate_batch_payment` tool (`src/features/admin_chat/tools.py`)
**Scope:** Large
1. **Add** schema: `initiate_batch_payment(payments: List[{recipient_user_id: str, amount_sol: number, reason?: string}])` — 1..20 entries.
2. **Add** `execute_initiate_batch_payment(bot, db_handler, params)`:
   - Per-element pop-and-warn for `wallet_address`/`recipient_wallet`.
   - Validate every element (all-or-none) — `recipient_user_id > 0`, `amount_sol > 0`, no active duplicate.
   - Look up each recipient's `wallet_record` BEFORE insert and mark as verified/unverified.
   - Build records: **unverified** → `status='awaiting_wallet'`. **Verified** → `status='awaiting_wallet'` as a transient placeholder (NOT `awaiting_admin_approval` directly — the real transition to `awaiting_admin_approval` happens inside `_create_real_payment_and_gate` ONLY after the `payment_requests` row exists, preserving crash-safety). No element is ever written to `awaiting_test` at creation time (the brief's "don't create a fake test state first" requirement is honored by skipping the test payment entirely; the intent uses `awaiting_wallet` only as a pre-insert placeholder and transitions directly to `awaiting_admin_approval` within the same tool invocation).
   - `db_handler.create_admin_payment_intents_batch(records, guild_id)`. `None` → failure.
   - Iterate returned intents:
     - Verified → call `cog._create_real_payment_and_gate(channel, intent, wallet_record, amount_sol)` — one admin DM per intent.
     - Unverified → post the wallet prompt message.
   - 16-payment batch → 16 admin DMs OR 16 wallet prompts OR a mix.
3. **Wire** dispatch.

### Step 14: `upsert_wallet_for_user` tool with explicit admin DM (`src/features/admin_chat/tools.py`)
**Scope:** Medium
1. **Add** schema: `upsert_wallet_for_user(user_id: str, wallet_address: str, chain?: string='solana', reason?: string)`.
2. **Add** executor:
   - Require trusted `admin_user_id` injected by the agent (Step 16); verify `int(admin_user_id) == int(os.getenv('ADMIN_USER_ID'))`.
   - Validate `wallet_address` via `is_valid_solana_address`.
   - `db_handler.upsert_wallet(guild_id, discord_user_id=user_id, chain='solana', address=wallet_address, metadata={...})`.
   - On `WalletUpdateBlockedError` → return `{success: False, error: "Active payment intent in flight — wallet change blocked"}`.
   - **On success → explicitly DM the admin** (resolves **FLAG-009, issue_hints-1**):
     ```python
     try:
         admin_user = await bot.fetch_user(int(os.getenv('ADMIN_USER_ID')))
         await admin_user.send(
             f"Wallet for <@{user_id}> set to `{_redact_wallet_address(wallet_address)}`. "
             f"Will be verified on next payment."
         )
     except (discord.Forbidden, discord.HTTPException) as e:
         logger.error("[AdminChat] upsert_wallet_for_user admin DM failed: %s", e, exc_info=True)
         # Do not fail the tool call — the DB row is already written; admin DM is confirmation only.
     ```
     The executor guarantees the admin receives a DM regardless of which channel the tool was invoked from.
   - Return a redacted result for the agent's reply: `{success: True, wallet: redacted, verified_at: None, note: "will be verified on next payment"}`.
3. **Wire** dispatch.

### Step 15: Register new tools in `ADMIN_ONLY_TOOLS` (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** `query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user`, AND `resolve_admin_intent` (from Step 20) to `ADMIN_ONLY_TOOLS` at `:908-936`.
2. **Verify** the module-level exhaustiveness + disjoint asserts at `:938-940` still hold.

### Step 16: Extend caller-side admin identity injection (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Modify** `agent.py:438-441`. Generalize to a frozenset including the new tool `resolve_admin_intent` (resolves **FLAG-004, callers-1**):
   ```python
   _ADMIN_IDENTITY_INJECTED_TOOLS = frozenset({
       "initiate_payment",
       "initiate_batch_payment",
       "upsert_wallet_for_user",
       "resolve_admin_intent",
   })
   if is_admin and tool_name in _ADMIN_IDENTITY_INJECTED_TOOLS and 'admin_user_id' not in tool_input:
       if tool_input is tool_use.input:
           tool_input = dict(tool_input)
       tool_input['admin_user_id'] = user_id
   ```
2. **Add** `initiate_batch_payment` to `_CHANNEL_POSTING_TOOLS` at `agent.py:21-26`.

### Step 17: System prompt updates (`src/features/admin_chat/agent.py`)
**Scope:** Small
1. **Add** six new tool bullets to `**Doing things:**` at `:77`:
   - `query_payment_state`, `query_wallet_state`, `list_recent_payments`, `initiate_batch_payment`, `upsert_wallet_for_user`, `resolve_admin_intent(intent_id, resolution='cancelled', note?)` — cancel a stuck `manual_review` admin payment intent. Only `cancelled` is allowed; do NOT attempt to mark anything `completed` — that status is reserved for payments confirmed on-chain.
2. **Add** a new "State questions → query first" subsection after `**Use the right route tools.**`.
3. **Do not** touch `MEMBER_SYSTEM_PROMPT`.

## Phase 6: Reconciliation, Timeout Sweep, Intent Resolution

### Step 18: Startup reconciliation (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Extend** `_reconcile_active_intents` at `:546-561`:
   - `awaiting_test_receipt_confirmation` → `_reconcile_intent_history` + replay via `_handle_pending_recipient_message`.
   - `awaiting_admin_approval` → log count; persistence via Step 10.3.
   - `manual_review` → log count only; no replay (admin must resolve).
2. **Modify** `_reconcile_intent_history` at `:498-544` to call `_handle_pending_recipient_message(message, refreshed_intent)` per recipient message.
3. **Delete** `_classify_payment_reply` and `_check_pending_payment_reply` once grep confirms no remaining callers.

### Step 19: 24h timeout sweep (`src/features/admin_chat/admin_chat_cog.py`)
**Scope:** Small
1. **Add** `@tasks.loop(minutes=15) _sweep_stale_test_receipts` on `AdminChatCog`.
2. **Logic:** `cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()`; `db_handler.list_stale_test_receipt_intents(cutoff)`; for each → transition to `manual_review`, post admin tag in source channel with full context (intent_id, recipient, test_payment_id, tx signature), DM admin. Per-intent try/except.
3. **Start** in `cog_load`; stop in `cog_unload`.

### Step 20: `resolve_admin_intent` operator tool (`src/features/admin_chat/tools.py`)
**Scope:** Small
1. **Add** schema: `resolve_admin_intent(intent_id: str, note?: str)`. **Only one resolution: the intent is marked `cancelled`.** No `resolution` parameter — the tool's sole purpose is to let the admin clear a stuck `manual_review` (or other blocked) intent. Removes **FLAG-011, correctness-2**: `completed` is never writeable by this tool, preserving the "`completed` means on-chain confirmed" audit invariant.
2. **Add** executor:
   - Require trusted `admin_user_id` injection (Step 16 frozenset).
   - Verify `int(admin_user_id) == int(os.getenv('ADMIN_USER_ID'))`.
   - Fetch intent: must exist and status must be in `{'manual_review', 'awaiting_wallet', 'awaiting_test', 'awaiting_test_receipt_confirmation', 'awaiting_admin_approval', 'awaiting_confirmation'}` — refuse to touch `completed`/`failed`/`cancelled` (already terminal) so the tool is idempotent-safe.
   - `db_handler.update_admin_payment_intent(intent_id, {'status': 'cancelled', 'resolved_by_message_id': None}, guild_id)` with an audit log entry carrying the `note`.
   - DM admin: `"Intent {intent_id} resolved → cancelled. Note: {note}"` via the same explicit-DM pattern as Step 14.
   - Return a redacted result.
3. **Wire** dispatch. Already in `ADMIN_ONLY_TOOLS` via Step 15. Already in `_ADMIN_IDENTITY_INJECTED_TOOLS` via Step 16.
4. **Note:** to `complete` an intent honestly (e.g., paid out-of-band), the admin must use existing tooling that writes a real `payment_requests` row and lets `handle_payment_result` flow. That path is out of scope for this plan.

## Phase 7: Tests + Verification

### Step 21: Rewrite `tests/test_admin_payments.py`
**Scope:** Large
1. **Remove** tests that mock `_classify_payment_reply`.
2. **Parser unit tests** — punctuation-wrapped wallet, non-address rejection, parametrized confirmation keywords (positive/negative/ambiguous).
3. **Identity router tests (fail_to_pass)** — admin plain-channel message reaches agent; pending-recipient path with Anthropic mock raising on call; fallthrough; approved-member mention NOT routed.
4. **State-machine tests** — awaiting_wallet valid/ignore; test-receipt positive (`mark_wallet_verified` + real payment via crash-safe helper + admin DM); negative (→ manual_review + tag); ambiguous (first silent, second escalates).
5. **Payment-gate tests:**
   - `test_producer_flows_admin_chat_real_authorizes_admin_dm`.
   - `test_admin_approval_view_only_admin_can_click`.
   - `test_admin_approval_view_persists_across_restart`.
   - `test_admin_approval_click_queues_payment_and_posts_queued_message` — DM says "queued for sending" (NOT "sent"); `safe_delete_messages` called with BOTH prompt ids; `confirm_payment` called with `ADMIN_DM` actor.
   - `test_admin_dm_fail_closed_on_forbidden`.
   - `test_payment_confirm_view_not_sent_for_admin_chat_real_payments`.
   - `test_restart_filter_state_aware` — (a) new-flow admin_chat real payment + intent `awaiting_admin_approval` → `PaymentConfirmView` NOT re-registered; (b) legacy admin_chat real payment + intent `awaiting_confirmation` → `PaymentConfirmView` IS re-registered; (c) grants pending_confirmation → re-registered. Resolves **FLAG-007, issue_hints-2, all_locations-1**.
6. **Auto-skip + batch tests:**
   - `test_auto_skip_verified_wallet_creates_real_payment_directly` — no `is_test=True` row, intent ends in `awaiting_admin_approval` with `final_payment_id` set.
   - `test_verified_wallet_crash_safety_payment_row_before_intent_status` — mock `update_admin_payment_intent` to fail after `request_payment` has succeeded; assert the `payment_requests` row exists, the intent is NOT in `awaiting_admin_approval`, and no intent exists with `final_payment_id=NULL + status=awaiting_admin_approval`. Resolves **FLAG-008, correctness-1, callers-2**.
   - `test_initiate_batch_payment_atomic_all_or_none` — invalid element → zero writes; valid → all N intents, N admin DMs, verified entries skip `awaiting_test` entirely.
7. **New-tool tests:**
   - `test_upsert_wallet_for_user_admin_only`.
   - `test_upsert_wallet_for_user_dms_admin_explicitly` — mock `bot.fetch_user(ADMIN_USER_ID).send`, assert it is called with a redacted-wallet confirmation message regardless of invoking channel. Resolves **FLAG-009, issue_hints-1**.
   - `test_upsert_wallet_for_user_respects_wallet_update_blocked`.
   - `test_query_payment_state_read_only`, `test_query_wallet_state_read_only`, `test_query_tools_reject_llm_injected_wallet`.
8. **Lifecycle tests:**
   - `test_manual_review_blocks_new_intents` — populate a `manual_review` intent for a recipient; `get_active_intent_for_recipient` returns the row; a fresh `initiate_payment` for the same recipient returns the duplicate path. Confirms Rev-3 revert.
   - `test_resolve_admin_intent_cancelled_only` — admin tool marks a `manual_review` intent `cancelled`; subsequent `initiate_payment` for the same recipient succeeds. Resolves **FLAG-010, scope-1**.
   - `test_resolve_admin_intent_rejects_completed` — attempting to pass `resolution='completed'` (or any extra field) is rejected (schema has no such parameter). Resolves **FLAG-011, correctness-2**.
   - `test_resolve_admin_intent_admin_injection` — `resolve_admin_intent` is in `_ADMIN_IDENTITY_INJECTED_TOOLS`; non-admin caller rejected at executor. Resolves **FLAG-004, callers-1**.
   - `test_24h_timeout_sweep_escalates_stuck_test_receipt`.
9. **Infrastructure tests:**
   - `test_admin_only_tools_includes_all_new_tools` — query_*, initiate_batch_payment, upsert_wallet_for_user, resolve_admin_intent all in `ADMIN_ONLY_TOOLS`; module assert holds.
   - `test_agent_injects_admin_user_id_for_all_admin_tools` — parametrized over the frozenset.

### Step 22: Validation runs
**Scope:** Small
1. Parser unit tests first.
2. `pytest tests/test_admin_payments.py -x`.
3. Invariant suites: `test_payment_state_machine`, `test_payment_cog_split`, `test_solana_client`, `test_payment_race`, `test_payment_reconcile`, `test_payment_authorization`, `test_safe_delete_messages`, `test_check_payment_invariants`, `test_tx_signature_history`.
4. `pytest` full suite.
5. Manual smoke (info-only): dev-guild end-to-end — first-time flow, auto-skip flow, verified crash-window scenario (kill bot between `request_payment` success and intent update), batch mixed, negative ack, ambiguous 2x, admin DM click, restart mid `awaiting_admin_approval`, restart with legacy `awaiting_confirmation` row, 24h sweep, upsert success + admin DM (both from DM and from public channel), upsert during active intent, grants unchanged, query tools, approved-member no-route, `resolve_admin_intent` clears `manual_review`.

## Execution Order
1. **Phase 1** (schema + DB helpers incl. the new `get_pending_confirmation_admin_chat_intents_by_payment` helper) — self-contained; migration runs first.
2. **Phase 2** (producer_flows + state-aware restart filter) — CRITICAL interlocked pair with Phase 4. Step 3 authorizes `ADMIN_DM`; Step 4 prevents `PaymentConfirmView` from stealing new-flow admin_chat real payments on restart while preserving legacy rows.
3. **Phase 3** (parsers + identity router) — unit-testable.
4. **Phase 4** (flow rewiring + crash-safe `AdminApprovalView`) — relies on Phases 2 and 3. The crash-safe ordering in Step 9.1 is the keystone of `awaiting_admin_approval` state correctness.
5. **Phase 5** (agent tools + infrastructure) — Steps 15 and 16 land with Steps 12–14 and 20; all six admin tools register together.
6. **Phase 6** (reconciliation + 24h sweep + `resolve_admin_intent`).
7. **Phase 7** (tests + verification).

## Validation Order
1. Parser unit tests (pure functions).
2. Identity router + state machine tests with Anthropic client mocked to raise.
3. `producer_flows` authorization tests (`ADMIN_DM` allowed for admin_chat real; still rejected for grants).
4. State-aware restart-recovery filter tests — three scenarios.
5. Crash-safety test — verified-wallet path with an injected failure between `request_payment` and intent update.
6. `AdminApprovalView` click handler — "queued" messaging, both prompt ids deleted, `ADMIN_DM` actor.
7. Batch atomicity + verified-skip path.
8. `manual_review` blocking tests + `resolve_admin_intent` tests (cancel-only, admin injection, non-admin rejection).
9. `upsert_wallet_for_user` explicit-DM test.
10. Infrastructure tests (`ADMIN_ONLY_TOOLS` + caller injection frozenset exhaustiveness).
11. Full `test_admin_payments.py`.
12. Invariant suites (pass_to_pass).
13. Full `pytest`.
14. Manual smoke in dev guild.
